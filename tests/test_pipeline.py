"""Collector + Batcher + Deliverer against a fake Telegram: find -> download -> zip -> ship."""
import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace

from telethon.tl import types

from app.batcher import Batcher
from app.bot import Deliverer
from app.collector import Collector
from app.db import Database


def doc(doc_id, mime, size, file_name=None, voice=False, performer=None, title=None):
    attrs = []
    if file_name:
        attrs.append(types.DocumentAttributeFilename(file_name=file_name))
    if mime.startswith("audio/"):
        attrs.append(types.DocumentAttributeAudio(duration=180, voice=voice, performer=performer, title=title))
    return types.Document(id=doc_id, access_hash=0, file_reference=b"", date=None, mime_type=mime,
                          size=size, dc_id=4, attributes=attrs)


class FakeUserClient:
    def __init__(self, posts):
        self.posts = {m.id: m for m in posts}
        self.downloads = 0

    def add_event_handler(self, *_):
        pass

    async def get_input_entity(self, peer):
        return peer

    async def get_messages(self, entity, limit=None, ids=None):
        if ids is not None:
            return self.posts.get(ids)
        return [self.posts[max(self.posts)]] if self.posts else []

    async def iter_messages(self, entity, min_id=0, max_id=0, filter=None, reverse=False):
        # Both search filters return every document here, so each MP3 is seen twice: must be counted once.
        for msg_id in sorted(self.posts, reverse=not reverse):
            if min_id < msg_id < max_id and getattr(self.posts[msg_id], "document", None):
                yield self.posts[msg_id]

    async def download_media(self, msg, file):
        self.downloads += 1
        if msg.id == 1:
            await asyncio.sleep(0.2)   # the oldest song finishes last; its ZIP must still come first
        Path(file).write_bytes(b"ID3" + bytes([msg.id % 256]) * (msg.document.size - 3))
        return file


class FakeBot:
    def __init__(self):
        self.files, self.messages = [], []

    async def upload_file(self, path, file_name=None, part_size_kb=None):
        return SimpleNamespace(path=path, name=file_name)

    async def send_file(self, chat, uploaded, caption=None, force_document=False):
        self.files.append((chat, uploaded.name, caption))

    async def send_message(self, chat, text, link_preview=True):
        self.messages.append((chat, text))


def test_full_pipeline(tmp_path):
    posts = [
        SimpleNamespace(id=1, document=doc(11, "audio/mpeg", 400, "first.mp3")),
        SimpleNamespace(id=2, document=None),                                            # text post
        SimpleNamespace(id=3, document=doc(13, "audio/ogg", 300, voice=True)),           # voice note
        SimpleNamespace(id=4, document=doc(14, "audio/mpeg", 500, performer="Artist", title="Song")),
        SimpleNamespace(id=5, document=doc(15, "audio/x-m4a", 500, "other.m4a")),        # not mp3
        SimpleNamespace(id=6, document=doc(16, "application/octet-stream", 600, "sent as file.mp3")),
        SimpleNamespace(id=7, document=doc(17, "audio/mpeg", 700, "fourth.mp3")),
        SimpleNamespace(id=8, document=doc(18, "audio/mpeg", 400, "first.mp3")),         # re-upload of #1
    ]

    async def go():
        db = await Database.open(str(tmp_path / "t.db"))
        try:
            await check(db)
        finally:
            await db.close()   # an unclosed database thread would keep the test process alive

    async def check(db):
        await db.seed_settings({"batch_size": 2, "link_expire_days": 7, "auto_flush_hours": 0, "allow_users": True})
        bot = FakeBot()
        deliverer = Deliverer(db, bot, frozenset({1}), "http://example.test:8080")
        batcher = Batcher(db, tmp_path / "zips", 10**9, False, deliverer.enqueue)
        user = FakeUserClient(posts)
        collector = Collector(db, user, batcher, tmp_path / "music", max_track_bytes=10**9, workers=2, scan_interval=3600)
        (tmp_path / "music").mkdir()
        ch, _ = await db.add_channel(-100123, "Test Channel", "test", "https://t.me/test", 42)
        await db.subscribe(ch["id"], 42)   # the user who sent the link
        await db.subscribe(ch["id"], 1)    # an admin who also subscribed: gets the file, not a second message
        deliverer.start()
        await collector.start()            # the scan loop scans once right away

        for _ in range(250):
            if (await db.status_counts())["zipped"] == 4 and not await db.undelivered_batches():
                break
            await asyncio.sleep(0.02)
        for task in collector._tasks:
            task.cancel()

        counts = await db.status_counts()
        assert counts["zipped"] == 4 and counts["duplicate"] == 1 and counts["pending"] == 0
        assert user.downloads == 4                                   # nothing downloaded twice
        assert (await db.get_channel(ch["id"]))["last_msg_id"] == 8

        # admin got two ZIP files, the subscriber got two links
        assert [f[0] for f in bot.files] == [1, 1]
        assert [f[1] for f in bot.files] == ["Test Channel_001.zip", "Test Channel_002.zip"]
        assert all(chat == 42 for chat, _ in bot.messages)
        assert "http://example.test:8080/dl/" in bot.messages[0][1]

        first = await db.get_batch(1)
        with zipfile.ZipFile(first["path"]) as zf:
            assert zf.namelist() == ["first.mp3", "Artist - Song.mp3"]
        second = await db.get_batch(2)
        with zipfile.ZipFile(second["path"]) as zf:
            assert zf.namelist() == ["sent as file.mp3", "fourth.mp3"]
        assert not list((tmp_path / "music").rglob("*.mp3"))      # MP3s removed after zipping
        assert await db.undelivered_batches() == []

        # a rescan finds nothing new, so nothing is downloaded again
        assert await collector.scan_channel(ch["id"]) == 0

    asyncio.run(go())
