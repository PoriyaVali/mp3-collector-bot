"""The user account side: joins channels, finds MP3 posts, downloads them."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient, errors, events, utils
from telethon.tl import functions, types

from .batcher import Batcher
from .db import Database, now
from .files import is_mp3, track_file_name
from .links import ChannelRef

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MIN_FREE_BYTES = 300 * 1024 * 1024

# Telegram error code -> message shown to the person who sent the link
JOIN_ERRORS = {
    "USERNAME_NOT_OCCUPIED": "چنین کانالی وجود ندارد.",
    "USERNAME_INVALID": "آیدی کانال نامعتبر است.",
    "INVITE_HASH_EXPIRED": "لینک دعوت منقضی شده است.",
    "INVITE_HASH_INVALID": "لینک دعوت نامعتبر است.",
    "INVITE_HASH_EMPTY": "لینک دعوت نامعتبر است.",
    "CHANNELS_TOO_MUCH": "حساب ربات به سقف تعداد کانال‌ها رسیده؛ چند کانال را حذف کنید.",
    "CHANNEL_PRIVATE": "کانال خصوصی است یا حساب ربات از آن اخراج شده.",
    "CHANNEL_INVALID": "کانال پیدا نشد.",
    "INVITE_REQUEST_SENT": "درخواست عضویت فرستاده شد؛ بعد از تأیید مدیر کانال، دوباره لینک را بفرستید.",
    "USERS_TOO_MUCH": "کانال پر است و عضو جدید نمی‌پذیرد.",
}


class JoinError(Exception):
    pass


class Collector:
    def __init__(self, db: Database, client: TelegramClient, batcher: Batcher, music_dir: Path,
                 max_track_bytes: int, workers: int, scan_interval: int):
        self.db = db
        self.client = client
        self.batcher = batcher
        self.music_dir = music_dir
        self.max_track_bytes = max_track_bytes
        self.workers = workers
        self.scan_interval = scan_interval
        self.ready = False
        self.alert: Callable[[str], Awaitable[None]] | None = None
        self._active: dict[int, dict] = {}
        self._wake = asyncio.Event()
        self._scan_locks: dict[int, asyncio.Lock] = {}
        self._alerted: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self.db.reset_downloading()
        await self.refresh_active()
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self.ready = True
        for n in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker(n), name=f"download-{n}"))
        self._tasks.append(asyncio.create_task(self._scan_loop(), name="scanner"))

    async def refresh_active(self) -> None:
        self._active = {c["peer_id"]: c for c in await self.db.list_channels(("active",))}

    def wake(self) -> None:
        self._wake.set()

    async def _alert_once(self, key: str, text: str, every: int = 3600) -> None:
        last = self._alerted.get(key, 0)
        if time.monotonic() - last < every or not self.alert:
            return
        self._alerted[key] = time.monotonic()
        try:
            await self.alert(text)
        except Exception:  # noqa: BLE001 - an alert must never break the worker
            log.exception("alert failed")

    # -- joining -----------------------------------------------------------
    async def _join(self, ref: ChannelRef):
        if ref.username:
            entity = await self.client.get_entity(ref.username)
            if isinstance(entity, types.User):
                raise JoinError("این آیدی مال یک کاربر یا ربات است، نه کانال.")
            if isinstance(entity, types.Channel) and entity.left:
                await self.client(functions.channels.JoinChannelRequest(entity))
            return entity
        invite = await self.client(functions.messages.CheckChatInviteRequest(ref.invite_hash))
        if isinstance(invite, types.ChatInviteAlready):
            return invite.chat
        try:
            updates = await self.client(functions.messages.ImportChatInviteRequest(ref.invite_hash))
            return updates.chats[0]
        except errors.RPCError as exc:
            if exc.message != "USER_ALREADY_PARTICIPANT":
                raise
            invite = await self.client(functions.messages.CheckChatInviteRequest(ref.invite_hash))
            return invite.chat

    async def add_channel(self, ref: ChannelRef, user_id: int) -> tuple[dict, bool]:
        """Join (if needed) and start tracking. Raises JoinError with a user-facing message."""
        try:
            entity = await self._join(ref)
        except JoinError:
            raise
        except errors.FloodWaitError as exc:
            raise JoinError(f"تلگرام محدودیت زمانی گذاشته؛ {exc.seconds // 60 + 1} دقیقه دیگر دوباره امتحان کنید.") from exc
        except errors.RPCError as exc:
            raise JoinError(JOIN_ERRORS.get(exc.message, f"خطای تلگرام: {exc.message}")) from exc
        except ValueError as exc:  # get_entity: "No user has ... as username"
            raise JoinError("چنین کانالی پیدا نشد.") from exc
        if not isinstance(entity, (types.Channel, types.Chat)):
            raise JoinError("این لینک مال کانال یا گروه نیست.")
        channel, created = await self.db.add_channel(
            peer_id=utils.get_peer_id(entity),
            title=entity.title or "channel",
            username=getattr(entity, "username", None),
            link=ref.link,
            added_by=user_id,
        )
        await self.db.subscribe(channel["id"], user_id)
        await self.refresh_active()
        return channel, created

    async def remove_channel(self, channel_id: int) -> None:
        channel = await self.db.get_channel(channel_id)
        if not channel:
            return
        await self.db.set_channel_status(channel_id, "removed")
        await self.db.skip_pending(channel_id)
        await self.refresh_active()
        await self.batcher.check(channel_id, force=True)  # ship what is already downloaded
        try:
            entity = await self._input_entity(channel["peer_id"])
            await self.client.delete_dialog(entity)
        except Exception as exc:  # noqa: BLE001 - leaving is best effort
            log.warning("could not leave %s: %s", channel["title"], exc)

    async def set_paused(self, channel_id: int, paused: bool) -> None:
        await self.db.set_channel_status(channel_id, "paused" if paused else "active")
        await self.refresh_active()
        if not paused:
            self.wake()

    async def _input_entity(self, peer_id: int):
        try:
            return await self.client.get_input_entity(peer_id)
        except ValueError:
            # Access hash not cached (e.g. the session was recreated): reload the dialog list once.
            await self.client.get_dialogs()
            return await self.client.get_input_entity(peer_id)

    # -- finding MP3 posts -------------------------------------------------
    @staticmethod
    def _mp3_info(msg) -> dict | None:
        """(msg_id, doc_id, file name, size) of an MP3 post, or None for anything else."""
        doc = getattr(msg, "document", None)
        if doc is None:
            return None
        file_name = performer = title = None
        for attr in doc.attributes:
            if isinstance(attr, types.DocumentAttributeFilename):
                file_name = attr.file_name
            elif isinstance(attr, types.DocumentAttributeAudio):
                if attr.voice:
                    return None
                performer, title = attr.performer, attr.title
        if not is_mp3(doc.mime_type, file_name):
            return None
        return {
            "msg_id": msg.id,
            "doc_id": doc.id,
            "file_name": track_file_name(file_name, performer, title, msg.id),
            "size": doc.size,
        }

    async def _store(self, channel: dict, info: dict) -> str:
        result = await self.db.add_track(
            channel_id=channel["id"], **info, too_big=info["size"] > self.max_track_bytes
        )
        if result == "new":
            self.wake()
        return result

    async def scan_channel(self, channel_id: int) -> int:
        """Look for MP3 posts newer than the last scan. Returns how many new tracks were queued."""
        lock = self._scan_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            channel = await self.db.get_channel(channel_id)
            if not channel or channel["status"] != "active":
                return 0
            entity = await self._input_entity(channel["peer_id"])
            latest = await self.client.get_messages(entity, limit=1)
            top = latest[0].id if latest else 0
            if top <= channel["last_msg_id"]:
                await self.db.set_channel_scanned(channel_id, top)
                return 0
            # Music = files Telegram tagged as audio; Document catches MP3s sent "as file".
            # Everything is collected first and queued oldest-first, so downloads, ZIPs and
            # the choice of which copy of a re-uploaded song counts all follow posting order.
            found: dict[int, dict] = {}
            for flt in (types.InputMessagesFilterMusic(), types.InputMessagesFilterDocument()):
                async for msg in self.client.iter_messages(
                    entity, min_id=channel["last_msg_id"], max_id=top + 1, filter=flt, reverse=True
                ):
                    info = self._mp3_info(msg)
                    if info:
                        found[info["msg_id"]] = info
            new = 0
            for msg_id in sorted(found):
                if await self._store(channel, found[msg_id]) == "new":
                    new += 1
            await self.db.set_channel_scanned(channel_id, top)
            if new:
                log.info("%s: %d new tracks", channel["title"], new)
            return new

    async def _on_new_message(self, event) -> None:
        channel = self._active.get(event.chat_id)
        info = self._mp3_info(event.message) if channel else None
        if info:
            await self._store(channel, info)

    async def _scan_loop(self) -> None:
        while True:
            for channel in await self.db.list_channels(("active",)):
                try:
                    await self.scan_channel(channel["id"])
                except errors.FloodWaitError as exc:
                    log.warning("flood wait %ss while scanning", exc.seconds)
                    await asyncio.sleep(exc.seconds)
                except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the rest
                    log.warning("scan of %s failed: %s", channel["title"], exc)
                    await self.db.set_channel_error(channel["id"], str(exc))
            await self._auto_flush()
            await asyncio.sleep(self.scan_interval)

    async def _auto_flush(self) -> None:
        hours = await self.db.get_int("auto_flush_hours", 0)
        if hours <= 0:
            return
        for channel_id in await self.db.channels_idle_since(now() - hours * 3600):
            try:
                await self.batcher.check(channel_id, force=True)
            except Exception:  # noqa: BLE001
                log.exception("auto flush of channel %s failed", channel_id)

    # -- downloading -------------------------------------------------------
    async def _worker(self, n: int) -> None:
        while True:
            track = await self.db.claim_pending()
            if not track:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._download(track)
            except Exception:  # noqa: BLE001 - keep the worker alive whatever happens
                log.exception("worker %d: unexpected error on track %s", n, track["id"])
                await self.db.mark_failed(track["id"], "unexpected error", MAX_ATTEMPTS)

    async def _download(self, track: dict) -> None:
        free = shutil.disk_usage(self.music_dir).free
        if free < track["size"] + MIN_FREE_BYTES:
            await self.db.release_track(track["id"])
            await self._alert_once("disk", "⚠️ فضای دیسک سرور تمام شده؛ دانلود متوقف است تا فضا آزاد شود.")
            await asyncio.sleep(300)
            return
        channel = await self.db.get_channel(track["channel_id"])
        try:
            entity = await self._input_entity(channel["peer_id"])
            msg = await self.client.get_messages(entity, ids=track["msg_id"])
            if msg is None or getattr(msg, "document", None) is None:
                await self.db.mark_failed(track["id"], "post was deleted", MAX_ATTEMPTS, permanent=True)
                return
            folder = self.music_dir / str(channel["id"])
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"{track['id']}.mp3"
            tmp = folder / f"{track['id']}.mp3.part"
            await self.client.download_media(msg, file=str(tmp))
            tmp.replace(dest)
        except errors.FloodWaitError as exc:
            await self.db.release_track(track["id"])
            log.warning("flood wait %ss while downloading", exc.seconds)
            await asyncio.sleep(exc.seconds)
            return
        except (ConnectionError, asyncio.TimeoutError) as exc:
            await self.db.release_track(track["id"])
            log.warning("network error, retrying later: %s", exc)
            await asyncio.sleep(15)
            return
        except Exception as exc:  # noqa: BLE001
            status = await self.db.mark_failed(track["id"], f"{type(exc).__name__}: {exc}", MAX_ATTEMPTS)
            log.warning("download of track %s failed (%s): %s", track["id"], status, exc)
            return
        await self.db.mark_downloaded(track["id"], str(dest), dest.stat().st_size)
        try:
            await self.batcher.check(channel["id"])
        except Exception as exc:  # noqa: BLE001 - the track stays 'downloaded'; the next check retries
            log.exception("building a batch for channel %s failed", channel["id"])
            await self._alert_once("zip", f"⚠️ ساخت فایل zip خطا داد: {exc}")
