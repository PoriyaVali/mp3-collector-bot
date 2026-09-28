"""The bot's side of automatic updates: reading the host's status file and telling the admin."""
import asyncio

import app.updates as updates
from app.db import Database, now
from app.updates import UpdateWatcher, read_status, version_key


def run(coro):
    return asyncio.run(coro)


class Outbox:
    def __init__(self):
        self.admin, self.direct = [], []

    async def notify(self, text):
        self.admin.append(text)

    async def send(self, chat_id, text):
        self.direct.append((chat_id, text))


def write_status(path, **values):
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


async def watcher_for(tmp_path):
    db = await Database.open(str(tmp_path / "t.db"))
    box = Outbox()
    return db, box, UpdateWatcher(db, tmp_path, box.notify, box.send)


def test_read_status_and_versions(tmp_path):
    assert read_status(tmp_path / "missing") == {}
    (tmp_path / "s").write_text("a=1\nerror=x = y\nnot a pair\n", encoding="utf-8")
    assert read_status(tmp_path / "s") == {"a": "1", "error": "x = y"}
    assert version_key("1.10.0") > version_key("1.9.3")
    assert version_key("junk") == ()


def test_new_version_is_announced_once(tmp_path, monkeypatch):
    async def go():
        db, box, w = await watcher_for(tmp_path)
        try:
            monkeypatch.setattr(updates, "__version__", "1.0.0")
            await w.announce_new_version()                 # first start: nothing to announce
            assert box.admin == []
            monkeypatch.setattr(updates, "__version__", "1.1.0")
            await w.announce_new_version()
            assert len(box.admin) == 1 and "1.1.0" in box.admin[0] and "1.0.0" in box.admin[0]
            assert "releases/tag/v1.1.0" in box.admin[0]
            await w.announce_new_version()                 # a plain restart says nothing
            assert len(box.admin) == 1
        finally:
            await db.close()
    run(go())


def test_failed_update_reported_once(tmp_path):
    async def go():
        db, box, w = await watcher_for(tmp_path)
        try:
            write_status(w.status_path, event="rolled_back", event_from="1.1.0", event_to="1.2.0",
                         event_at="100", event_error="v1.2.0 did not come online")
            await w.poll()
            await w.poll()
            assert len(box.admin) == 1 and "1.2.0" in box.admin[0] and "1.1.0" in box.admin[0]
            write_status(w.status_path, event="failed", event_to="1.3.0", event_at="200",
                         event_error="building v1.3.0 failed")
            await w.poll()
            assert len(box.admin) == 2 and "building v1.3.0 failed" in box.admin[1]
            write_status(w.status_path, event="updated", event_to="1.3.1", event_at="300")
            await w.poll()                                  # success is announced by the new version itself
            assert len(box.admin) == 2
        finally:
            await db.close()
    run(go())


def test_available_version_reported_once_when_auto_is_off(tmp_path, monkeypatch):
    async def go():
        db, box, w = await watcher_for(tmp_path)
        try:
            monkeypatch.setattr(updates, "__version__", "1.1.0")
            write_status(w.status_path, last_check_result="available", latest="1.4.0")
            await w.poll()
            await w.poll()
            assert len(box.admin) == 1 and "1.4.0" in box.admin[0]
            write_status(w.status_path, last_check_result="available", latest="1.1.0")
            await w.poll()                                  # not newer than what runs: silent
            assert len(box.admin) == 1
        finally:
            await db.close()
    run(go())


def test_switch_and_update_now(tmp_path, monkeypatch):
    async def go():
        db, box, w = await watcher_for(tmp_path)
        try:
            monkeypatch.setattr(updates, "__version__", "1.4.0")
            assert w.auto_enabled
            w.set_auto(False)
            assert (tmp_path / "auto-update-off").exists() and not w.auto_enabled
            w.set_auto(True)
            assert w.auto_enabled
            assert "نصب نیست" in w.describe()               # no status file: the host part is missing

            w.request_update(77)
            assert (tmp_path / "update-now").exists()
            asked = now()
            write_status(w.status_path, last_check_at=str(asked - 100), last_check_result="up_to_date")
            await w.poll()                                  # an older check does not answer the request
            assert box.direct == []
            write_status(w.status_path, last_check_at=str(asked), last_check_result="updating")
            await w.poll()                                  # still working on it
            assert box.direct == []
            write_status(w.status_path, last_check_at=str(asked + 5), last_check_result="up_to_date")
            await w.poll()
            assert box.direct == [(77, "✅ آخرین نسخه (1.4.0) نصب است.")]
            assert "1.4.0" in w.describe() and "روشن" in w.describe()

            w.request_update(77)                            # the host never answers
            w._request = (77, now() - updates.REQUEST_TIMEOUT - 1)
            write_status(w.status_path, last_check_at="1", last_check_result="up_to_date")
            await w.poll()
            assert "mp3bot update" in box.direct[-1][1]
        finally:
            await db.close()
    run(go())
