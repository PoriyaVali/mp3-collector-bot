"""Turns downloaded MP3s into numbered ZIP batches, per channel."""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections import defaultdict
from pathlib import Path
from typing import Awaitable, Callable

from .db import Database, now
from .files import build_zip, safe_name

log = logging.getLogger(__name__)


def plan_batch(rows: list[dict], batch_size: int, max_bytes: int, force: bool) -> list[dict]:
    """Pick the tracks for the next ZIP, or [] if it is not time yet.

    A batch closes when it holds `batch_size` tracks, or earlier when the next
    track would push it past `max_bytes` (Telegram's upload limit). With
    `force`, whatever is left goes out as a smaller final batch.
    """
    picked: list[dict] = []
    total = 0
    for row in rows:
        if picked and total + row["size"] > max_bytes:
            return picked
        picked.append(row)
        total += row["size"]
        if len(picked) >= batch_size:
            return picked
    return picked if force else []


class Batcher:
    def __init__(self, db: Database, zip_dir: Path, max_bytes: int, keep_mp3: bool,
                 on_batch: Callable[[int], Awaitable[None]]):
        self.db = db
        self.zip_dir = zip_dir
        self.max_bytes = max_bytes
        self.keep_mp3 = keep_mp3
        self.on_batch = on_batch
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check(self, channel_id: int, force: bool = False) -> list[int]:
        """Create every batch that is due for this channel; returns the new batch ids."""
        created: list[int] = []
        async with self._locks[channel_id]:
            while True:
                batch_size = await self.db.get_int("batch_size", 30)
                rows = await self.db.unbatched(channel_id, in_order=not force)
                picked = plan_batch(rows, batch_size, self.max_bytes, force)
                if not picked:
                    break
                created.append(await self._make(channel_id, picked))
        for batch_id in created:
            await self.on_batch(batch_id)
        return created

    async def check_all(self, force: bool = False) -> list[int]:
        created: list[int] = []
        for channel_id in await self.db.channels_with_unbatched():
            created += await self.check(channel_id, force=force)
        return created

    async def _make(self, channel_id: int, tracks: list[dict]) -> int:
        channel = await self.db.get_channel(channel_id)
        title = channel["title"] if channel else f"channel_{channel_id}"
        number = await self.db.next_batch_number(channel_id)
        file_name = f"{safe_name(title, max_len=60, fallback='music')}_{number:03d}.zip"
        token = secrets.token_urlsafe(16)
        path = self.zip_dir / f"{channel_id}_{number:03d}_{token[:8]}.zip"
        entries = [(t["path"], t["file_name"]) for t in tracks]
        size = await asyncio.to_thread(build_zip, entries, path)
        days = await self.db.get_int("link_expire_days", 7)
        expires_at = now() + days * 86400 if days > 0 else None
        batch_id = await self.db.create_batch(
            channel_id, number, file_name, str(path), size, [t["id"] for t in tracks], token, expires_at
        )
        log.info("batch %s: %s (%d tracks, %d bytes)", batch_id, file_name, len(tracks), size)
        if not self.keep_mp3:
            for t in tracks:
                try:
                    Path(t["path"]).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not delete %s: %s", t["path"], exc)
        return batch_id
