"""SQLite storage: channels, tracks (what was / was not downloaded), ZIP batches, users, settings."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id      INTEGER NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    username     TEXT,
    link         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',   -- active | paused | removed
    last_msg_id  INTEGER NOT NULL DEFAULT 0,       -- history scanned up to here
    last_error   TEXT,
    added_by     INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    last_scan_at INTEGER
);
CREATE TABLE IF NOT EXISTS subscriptions (
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (channel_id, user_id)
);
-- One row per MP3 ever seen. status:
--   pending -> downloading -> downloaded -> zipped
--   failed (gave up after retries), skipped (too big / channel removed),
--   duplicate (same file name + size already known from another post)
CREATE TABLE IF NOT EXISTS tracks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER NOT NULL,
    msg_id        INTEGER NOT NULL,
    doc_id        INTEGER NOT NULL UNIQUE,
    file_name     TEXT NOT NULL,
    size          INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    path          TEXT,
    batch_id      INTEGER,
    created_at    INTEGER NOT NULL,
    downloaded_at INTEGER,
    UNIQUE (channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS tracks_status ON tracks (status, channel_id);
CREATE INDEX IF NOT EXISTS tracks_name_size ON tracks (size, file_name);
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL,
    number      INTEGER NOT NULL,
    file_name   TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    track_count INTEGER NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER,                 -- NULL = link never expires
    admin_sent  INTEGER NOT NULL DEFAULT 0,
    users_sent  INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0
);
"""

TRACK_STATUSES = ("pending", "downloading", "downloaded", "zipped", "failed", "skipped", "duplicate")


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self._claim_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # -- helpers -----------------------------------------------------------
    async def _one(self, sql: str, args: tuple = ()) -> dict | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _exec(self, sql: str, args: tuple = ()) -> int:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.rowcount

    # -- settings ----------------------------------------------------------
    async def seed_settings(self, defaults: dict[str, Any]) -> None:
        """Store env defaults once; later changes made from the bot win."""
        for key, value in defaults.items():
            await self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, str(int(value) if isinstance(value, bool) else value))
            )
        await self.conn.commit()

    async def get_int(self, key: str, default: int = 0) -> int:
        row = await self._one("SELECT value FROM settings WHERE key = ?", (key,))
        try:
            return int(row["value"]) if row else default
        except ValueError:
            return default

    async def get_str(self, key: str, default: str = "") -> str:
        row = await self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def get_bool(self, key: str, default: bool = False) -> bool:
        return bool(await self.get_int(key, int(default)))

    async def set_setting(self, key: str, value: Any) -> None:
        if isinstance(value, bool):
            value = int(value)
        await self._exec(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # -- users -------------------------------------------------------------
    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        await self._exec(
            "INSERT INTO users (id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
            (user_id, username, first_name, now()),
        )

    async def count_users(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM users")
        return row["n"]

    # -- channels ----------------------------------------------------------
    async def add_channel(self, peer_id: int, title: str, username: str | None, link: str, added_by: int) -> tuple[dict, bool]:
        """Returns (channel, created). A removed channel that is added again is re-activated."""
        existing = await self.get_channel_by_peer(peer_id)
        if existing:
            if existing["status"] == "removed":
                await self._exec(
                    "UPDATE channels SET status = 'active', title = ?, username = ?, link = ?, last_error = NULL WHERE id = ?",
                    (title, username, link, existing["id"]),
                )
                return await self.get_channel(existing["id"]), True
            await self._exec("UPDATE channels SET title = ?, username = ? WHERE id = ?", (title, username, existing["id"]))
            return await self.get_channel(existing["id"]), False
        await self._exec(
            "INSERT INTO channels (peer_id, title, username, link, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (peer_id, title, username, link, added_by, now()),
        )
        return await self.get_channel_by_peer(peer_id), True

    async def get_channel(self, channel_id: int) -> dict | None:
        return await self._one("SELECT * FROM channels WHERE id = ?", (channel_id,))

    async def get_channel_by_peer(self, peer_id: int) -> dict | None:
        return await self._one("SELECT * FROM channels WHERE peer_id = ?", (peer_id,))

    async def list_channels(self, statuses: tuple[str, ...] = ("active", "paused")) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return await self._all(f"SELECT * FROM channels WHERE status IN ({marks}) ORDER BY id", statuses)

    async def set_channel_status(self, channel_id: int, status: str) -> None:
        await self._exec("UPDATE channels SET status = ? WHERE id = ?", (status, channel_id))

    async def set_channel_scanned(self, channel_id: int, last_msg_id: int) -> None:
        await self._exec(
            "UPDATE channels SET last_msg_id = MAX(last_msg_id, ?), last_scan_at = ?, last_error = NULL WHERE id = ?",
            (last_msg_id, now(), channel_id),
        )

    async def set_channel_error(self, channel_id: int, error: str) -> None:
        await self._exec("UPDATE channels SET last_error = ?, last_scan_at = ? WHERE id = ?", (error[:300], now(), channel_id))

    async def channel_counts(self, channel_id: int) -> dict[str, int]:
        rows = await self._all("SELECT status, COUNT(*) AS n FROM tracks WHERE channel_id = ? GROUP BY status", (channel_id,))
        counts = {s: 0 for s in TRACK_STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts

    # -- subscriptions -----------------------------------------------------
    async def subscribe(self, channel_id: int, user_id: int) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO subscriptions (channel_id, user_id, created_at) VALUES (?, ?, ?)",
            (channel_id, user_id, now()),
        )

    async def subscribers(self, channel_id: int) -> list[int]:
        rows = await self._all("SELECT user_id FROM subscriptions WHERE channel_id = ?", (channel_id,))
        return [r["user_id"] for r in rows]

    async def user_channels(self, user_id: int) -> list[dict]:
        return await self._all(
            "SELECT c.* FROM channels c JOIN subscriptions s ON s.channel_id = c.id "
            "WHERE s.user_id = ? AND c.status != 'removed' ORDER BY c.id",
            (user_id,),
        )

    # -- tracks ------------------------------------------------------------
    async def add_track(self, channel_id: int, msg_id: int, doc_id: int, file_name: str, size: int, too_big: bool = False) -> str:
        """Record an MP3 post. Returns 'new', 'known' (already recorded) or 'duplicate'/'skipped'."""
        known = await self._one("SELECT 1 FROM tracks WHERE doc_id = ? OR (channel_id = ? AND msg_id = ?)", (doc_id, channel_id, msg_id))
        if known:
            return "known"
        if too_big:
            status = "skipped"
        else:
            same = await self._one("SELECT 1 FROM tracks WHERE size = ? AND file_name = ? AND status NOT IN ('duplicate', 'failed', 'skipped')", (size, file_name))
            status = "duplicate" if same else "pending"
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO tracks (channel_id, msg_id, doc_id, file_name, size, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel_id, msg_id, doc_id, file_name, size, status, now()),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            return "known"
        return "new" if status == "pending" else status

    async def claim_pending(self) -> dict | None:
        """Take the oldest pending track of an active channel and mark it downloading."""
        async with self._claim_lock:
            row = await self._one(
                "SELECT t.* FROM tracks t JOIN channels c ON c.id = t.channel_id "
                "WHERE t.status = 'pending' AND c.status = 'active' ORDER BY t.channel_id, t.msg_id LIMIT 1"
            )
            if row:
                await self._exec("UPDATE tracks SET status = 'downloading' WHERE id = ?", (row["id"],))
                row["status"] = "downloading"
            return row

    async def reset_downloading(self) -> int:
        """After a crash/restart nothing is really downloading any more."""
        return await self._exec("UPDATE tracks SET status = 'pending' WHERE status = 'downloading'")

    async def release_track(self, track_id: int) -> None:
        """Put a track back in the queue without counting it as a failure (e.g. flood wait)."""
        await self._exec("UPDATE tracks SET status = 'pending' WHERE id = ?", (track_id,))

    async def mark_downloaded(self, track_id: int, path: str, size: int) -> None:
        await self._exec(
            "UPDATE tracks SET status = 'downloaded', path = ?, size = ?, error = NULL, downloaded_at = ? WHERE id = ?",
            (path, size, now(), track_id),
        )

    async def mark_failed(self, track_id: int, error: str, max_attempts: int, permanent: bool = False) -> str:
        row = await self._one("SELECT attempts FROM tracks WHERE id = ?", (track_id,))
        attempts = (row["attempts"] if row else 0) + 1
        status = "failed" if permanent or attempts >= max_attempts else "pending"
        await self._exec(
            "UPDATE tracks SET status = ?, attempts = ?, error = ? WHERE id = ?",
            (status, attempts, error[:300], track_id),
        )
        return status

    async def retry_failed(self) -> int:
        return await self._exec("UPDATE tracks SET status = 'pending', attempts = 0, error = NULL WHERE status = 'failed'")

    async def skip_pending(self, channel_id: int) -> int:
        return await self._exec(
            "UPDATE tracks SET status = 'skipped', error = 'channel removed' WHERE channel_id = ? AND status IN ('pending', 'failed')",
            (channel_id,),
        )

    async def unbatched(self, channel_id: int, in_order: bool = True) -> list[dict]:
        """Downloaded tracks not yet in a ZIP, oldest post first.

        With `in_order`, only those older than every track still waiting to download:
        parallel downloads finish out of order, and a ZIP must not skip ahead of them.
        """
        gate = (
            " AND msg_id < COALESCE((SELECT MIN(msg_id) FROM tracks WHERE channel_id = ? "
            "AND status IN ('pending', 'downloading')), 9223372036854775807)"
            if in_order else ""
        )
        args = (channel_id, channel_id) if in_order else (channel_id,)
        return await self._all(
            "SELECT * FROM tracks WHERE channel_id = ? AND status = 'downloaded' AND batch_id IS NULL"
            + gate + " ORDER BY msg_id",
            args,
        )

    async def channels_with_unbatched(self) -> list[int]:
        rows = await self._all("SELECT DISTINCT channel_id FROM tracks WHERE status = 'downloaded' AND batch_id IS NULL")
        return [r["channel_id"] for r in rows]

    async def channels_idle_since(self, before: int) -> list[int]:
        """Channels with a partial batch, nothing left to download, and no new file since `before`."""
        rows = await self._all(
            "SELECT channel_id FROM tracks WHERE status = 'downloaded' AND batch_id IS NULL "
            "GROUP BY channel_id HAVING MAX(downloaded_at) < ? "
            "AND channel_id NOT IN (SELECT channel_id FROM tracks WHERE status IN ('pending', 'downloading'))",
            (before,),
        )
        return [r["channel_id"] for r in rows]

    async def status_counts(self) -> dict[str, int]:
        rows = await self._all("SELECT status, COUNT(*) AS n FROM tracks GROUP BY status")
        counts = {s: 0 for s in TRACK_STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts

    async def recent_failures(self, limit: int = 5) -> list[dict]:
        return await self._all("SELECT * FROM tracks WHERE status = 'failed' ORDER BY id DESC LIMIT ?", (limit,))

    # -- batches -----------------------------------------------------------
    async def next_batch_number(self, channel_id: int) -> int:
        row = await self._one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM batches WHERE channel_id = ?", (channel_id,))
        return row["n"]

    async def create_batch(
        self,
        channel_id: int,
        number: int,
        file_name: str,
        path: str,
        size: int,
        track_ids: list[int],
        token: str,
        expires_at: int | None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO batches (channel_id, number, file_name, path, size, track_count, token, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, number, file_name, path, size, len(track_ids), token, now(), expires_at),
        )
        batch_id = cur.lastrowid
        await self.conn.executemany(
            "UPDATE tracks SET status = 'zipped', batch_id = ? WHERE id = ?", [(batch_id, t) for t in track_ids]
        )
        await self.conn.commit()
        return batch_id

    async def get_batch(self, batch_id: int) -> dict | None:
        return await self._one("SELECT * FROM batches WHERE id = ?", (batch_id,))

    async def get_batch_by_token(self, token: str) -> dict | None:
        return await self._one("SELECT * FROM batches WHERE token = ?", (token,))

    async def batch_track_paths(self, batch_id: int) -> list[str]:
        rows = await self._all("SELECT path FROM tracks WHERE batch_id = ? AND path IS NOT NULL", (batch_id,))
        return [r["path"] for r in rows]

    async def undelivered_batches(self) -> list[int]:
        rows = await self._all("SELECT id FROM batches WHERE deleted = 0 AND (admin_sent = 0 OR users_sent = 0) ORDER BY id")
        return [r["id"] for r in rows]

    async def mark_batch(self, batch_id: int, *, admin_sent: bool | None = None, users_sent: bool | None = None) -> None:
        if admin_sent is not None:
            await self._exec("UPDATE batches SET admin_sent = ? WHERE id = ?", (int(admin_sent), batch_id))
        if users_sent is not None:
            await self._exec("UPDATE batches SET users_sent = ? WHERE id = ?", (int(users_sent), batch_id))

    async def expired_batches(self, at: int) -> list[dict]:
        return await self._all("SELECT * FROM batches WHERE deleted = 0 AND expires_at IS NOT NULL AND expires_at < ?", (at,))

    async def mark_batch_deleted(self, batch_id: int) -> None:
        await self._exec("UPDATE batches SET deleted = 1 WHERE id = ?", (batch_id,))

    async def batch_totals(self) -> dict[str, int]:
        return await self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes, COALESCE(SUM(deleted = 0), 0) AS live FROM batches"
        )

    async def channel_batch_count(self, channel_id: int) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM batches WHERE channel_id = ?", (channel_id,))
        return row["n"]

    async def user_batches(self, user_id: int, at: int, limit: int = 10) -> list[dict]:
        return await self._all(
            "SELECT b.*, c.title FROM batches b JOIN subscriptions s ON s.channel_id = b.channel_id "
            "JOIN channels c ON c.id = b.channel_id "
            "WHERE s.user_id = ? AND b.deleted = 0 AND (b.expires_at IS NULL OR b.expires_at > ?) "
            "ORDER BY b.id DESC LIMIT ?",
            (user_id, at, limit),
        )

    async def recent_batches(self, at: int, limit: int = 10) -> list[dict]:
        return await self._all(
            "SELECT b.*, c.title FROM batches b JOIN channels c ON c.id = b.channel_id "
            "WHERE b.deleted = 0 AND (b.expires_at IS NULL OR b.expires_at > ?) ORDER BY b.id DESC LIMIT ?",
            (at, limit),
        )
