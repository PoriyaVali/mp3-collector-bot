"""Database, batching and the download server, end to end without Telegram."""
import asyncio
import zipfile
from pathlib import Path

import aiohttp

from app.batcher import Batcher, plan_batch
from app.db import Database, now
from app.web import start_web


def run(coro):
    return asyncio.run(coro)


async def make_db(tmp_path: Path, batch_size: int = 3) -> Database:
    db = await Database.open(str(tmp_path / "t.db"))
    await db.seed_settings({"batch_size": batch_size, "link_expire_days": 7, "auto_flush_hours": 12, "allow_users": True})
    return db


async def add_downloaded(db: Database, channel_id: int, music: Path, n: int, start: int = 1, size: int = 100) -> None:
    for i in range(start, start + n):
        assert await db.add_track(channel_id, i, 1000 + i, f"song{i}.mp3", size) == "new"
        track = await db.claim_pending()
        path = music / f"{track['id']}.mp3"
        path.write_bytes(b"x" * size)
        await db.mark_downloaded(track["id"], str(path), size)


def test_plan_batch():
    rows = [{"size": 10}] * 7
    assert len(plan_batch(rows, 3, 1000, False)) == 3
    assert plan_batch(rows[:2], 3, 1000, False) == []
    assert len(plan_batch(rows[:2], 3, 1000, True)) == 2
    assert len(plan_batch(rows, 30, 25, False)) == 2      # size limit closes the batch early
    assert plan_batch([], 3, 1000, True) == []


def test_track_dedup_and_statuses(tmp_path):
    async def go():
        db = await make_db(tmp_path)
        ch, created = await db.add_channel(-1001, "Chan", "chan", "https://t.me/chan", 7)
        assert created
        assert await db.add_track(ch["id"], 1, 555, "a.mp3", 100) == "new"
        assert await db.add_track(ch["id"], 1, 555, "a.mp3", 100) == "known"      # same post again
        assert await db.add_track(ch["id"], 2, 555, "a.mp3", 100) == "known"      # same file forwarded
        assert await db.add_track(ch["id"], 3, 556, "a.mp3", 100) == "duplicate"  # re-upload, same name+size
        assert await db.add_track(ch["id"], 4, 557, "big.mp3", 10**10, too_big=True) == "skipped"
        counts = await db.status_counts()
        assert (counts["pending"], counts["duplicate"], counts["skipped"]) == (1, 1, 1)

        # failures are retried, then given up on
        t = await db.claim_pending()
        assert await db.mark_failed(t["id"], "boom", max_attempts=2) == "pending"
        t = await db.claim_pending()
        assert await db.mark_failed(t["id"], "boom", max_attempts=2) == "failed"
        assert await db.claim_pending() is None
        assert await db.retry_failed() == 1
        # paused channels are not downloaded
        await db.set_channel_status(ch["id"], "paused")
        assert await db.claim_pending() is None
        await db.set_channel_status(ch["id"], "active")
        assert (await db.claim_pending())["id"] == t["id"]
        # a crash while downloading puts the track back in the queue
        assert await db.reset_downloading() == 1
        await db.close()
    run(go())


def test_re_adding_a_removed_channel_reactivates_it(tmp_path):
    async def go():
        db = await make_db(tmp_path)
        ch, _ = await db.add_channel(-1001, "Chan", "chan", "https://t.me/chan", 7)
        again, created = await db.add_channel(-1001, "Chan 2", "chan", "https://t.me/chan", 8)
        assert not created and again["id"] == ch["id"] and again["title"] == "Chan 2"
        await db.set_channel_status(ch["id"], "removed")
        again, created = await db.add_channel(-1001, "Chan", "chan", "https://t.me/chan", 8)
        assert created and again["status"] == "active"
        await db.close()
    run(go())


def test_batches_zip_every_n_and_ship(tmp_path):
    async def go():
        db = await make_db(tmp_path, batch_size=3)
        music = tmp_path / "music"
        music.mkdir()
        shipped = []

        async def on_batch(batch_id):
            shipped.append(batch_id)

        batcher = Batcher(db, tmp_path / "zips", max_bytes=10**9, keep_mp3=False, on_batch=on_batch)
        ch, _ = await db.add_channel(-1001, "My/Chan", "chan", "https://t.me/chan", 7)
        await db.subscribe(ch["id"], 42)

        await add_downloaded(db, ch["id"], music, 7)
        created = await batcher.check(ch["id"])
        assert len(created) == 2 and shipped == created          # 7 songs -> two ZIPs of 3, one left
        assert len(await db.unbatched(ch["id"])) == 1

        b1 = await db.get_batch(created[0])
        assert b1["file_name"] == "My_Chan_001.zip" and b1["track_count"] == 3
        assert b1["expires_at"] > now() + 6 * 86400
        with zipfile.ZipFile(b1["path"]) as zf:
            assert zf.namelist() == ["song1.mp3", "song2.mp3", "song3.mp3"]
        assert list(music.iterdir()) == [music / "7.mp3"]       # zipped MP3s are deleted

        # nothing new -> nothing happens; flush ships the leftover
        assert await batcher.check(ch["id"]) == []
        flushed = await batcher.check(ch["id"], force=True)
        assert len(flushed) == 1 and (await db.get_batch(flushed[0]))["number"] == 3

        # admin lowers the batch size: waiting songs are re-grouped at once
        await add_downloaded(db, ch["id"], music, 2, start=100)
        await db.set_setting("batch_size", 2)
        assert len(await batcher.check_all()) == 1

        assert await db.undelivered_batches() == created + flushed + [created[-1] + 2]
        assert [b["number"] for b in await db.user_batches(42, now())] == [4, 3, 2, 1]
        assert await db.user_batches(99, now()) == []
        await db.close()
    run(go())


def test_idle_channel_is_flushed(tmp_path):
    async def go():
        db = await make_db(tmp_path, batch_size=30)
        music = tmp_path / "music"
        music.mkdir()
        ch, _ = await db.add_channel(-1001, "Chan", None, "https://t.me/+x", 7)
        await add_downloaded(db, ch["id"], music, 2)
        assert await db.channels_idle_since(now() - 3600) == []       # last song is fresh
        assert await db.channels_idle_since(now() + 10) == [ch["id"]]
        await db.add_track(ch["id"], 50, 9999, "late.mp3", 10)          # still something to download
        assert await db.channels_idle_since(now() + 10) == []
        await db.close()
    run(go())


def test_download_server(tmp_path):
    async def go():
        db = await make_db(tmp_path, batch_size=2)
        music = tmp_path / "music"
        music.mkdir()
        batcher = Batcher(db, tmp_path / "zips", 10**9, False, on_batch=lambda _: asyncio.sleep(0))
        ch, _ = await db.add_channel(-1001, "موزیک", None, "https://t.me/+x", 7)
        await add_downloaded(db, ch["id"], music, 2, size=5000)
        [batch_id] = await batcher.check(ch["id"])
        batch = await db.get_batch(batch_id)
        data = Path(batch["path"]).read_bytes()

        runner = await start_web(db, 0)
        port = runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{base}/dl/{batch['token']}/{batch['file_name']}") as r:
                    assert r.status == 200
                    assert await r.read() == data
                    assert "filename*=UTF-8''" in r.headers["Content-Disposition"]
                async with http.get(f"{base}/dl/{batch['token']}/x", headers={"Range": "bytes=10-19"}) as r:
                    assert r.status == 206 and await r.read() == data[10:20]   # resumable
                async with http.get(f"{base}/dl/wrong-token/x") as r:
                    assert r.status == 404
                await db.conn.execute("UPDATE batches SET expires_at = ? WHERE id = ?", (now() - 1, batch_id))
                await db.conn.commit()
                async with http.get(f"{base}/dl/{batch['token']}/x") as r:
                    assert r.status == 404                                     # expired
                async with http.get(f"{base}/health") as r:
                    assert await r.text() == "ok"
        finally:
            await runner.cleanup()
            await db.close()
    run(go())


def test_everything_imports():
    import app.bot  # noqa: F401
    import app.collector  # noqa: F401
    import app.login  # noqa: F401
    import app.main  # noqa: F401
