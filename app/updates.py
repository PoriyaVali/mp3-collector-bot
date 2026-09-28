"""The bot's side of automatic updates.

A container cannot replace itself, so the updating is done on the host by
`mp3bot.sh auto-update` (run by a systemd timer or cron). The two sides talk
through files in the shared data folder:

  update-status      written by the host: last check and last update event (key=value lines)
  auto-update-off    exists = only check and tell the admin; do not install
  update-now         exists = the admin asked for an update; the host picks it up within minutes
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from . import REPO_URL, __version__
from .db import Database, now

log = logging.getLogger(__name__)

POLL_SECONDS = 60
REQUEST_TIMEOUT = 20 * 60  # the host timer runs every 5 minutes; give it plenty of slack


def read_status(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    status = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            status[key.strip()] = value.strip()
    return status


def version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def release_link(version: str) -> str:
    return f"{REPO_URL}/releases/tag/v{version}"


def ago(timestamp: int) -> str:
    seconds = max(0, now() - timestamp)
    if seconds < 3600:
        return f"{max(1, seconds // 60)} دقیقه پیش"
    if seconds < 86400:
        return f"{seconds // 3600} ساعت پیش"
    return f"{seconds // 86400} روز پیش"


class UpdateWatcher:
    def __init__(self, db: Database, data_dir: Path,
                 notify: Callable[[str], Awaitable[None]],
                 send: Callable[[int, str], Awaitable[object]]):
        self.db = db
        self.status_path = data_dir / "update-status"
        self.off_flag = data_dir / "auto-update-off"
        self.now_flag = data_dir / "update-now"
        self.notify = notify
        self.send = send
        self._request: tuple[int, int] | None = None  # (chat id, asked at)

    # -- state the UI shows and changes -------------------------------------
    @property
    def status(self) -> dict[str, str]:
        return read_status(self.status_path)

    @property
    def auto_enabled(self) -> bool:
        return not self.off_flag.exists()

    def set_auto(self, enabled: bool) -> None:
        if enabled:
            self.off_flag.unlink(missing_ok=True)
        else:
            self.off_flag.touch()

    def request_update(self, chat_id: int) -> None:
        self.now_flag.touch()
        self._request = (chat_id, now())

    def describe(self) -> str:
        status = self.status
        mode = "روشن" if self.auto_enabled else "خاموش (فقط خبر می‌دهد)"
        checked = status.get("last_check_at", "")
        when = ago(int(checked)) if checked.isdigit() and checked != "0" else "هنوز نه"
        if not status:
            when = "به‌روزرسان روی سرور نصب نیست"
        return f"🔄 نسخه <b>{__version__}</b> · به‌روزرسانی خودکار: {mode} · آخرین بررسی: {when}"

    # -- background ------------------------------------------------------------
    async def start(self) -> None:
        await self.announce_new_version()
        asyncio.create_task(self._loop(), name="update-watcher")

    async def announce_new_version(self) -> None:
        last = await self.db.get_str("last_version")
        if last == __version__:
            return
        if last:
            direction = "به‌روز شد" if version_key(__version__) > version_key(last) else "برگشت"
            await self.notify(
                f"✅ ربات به نسخه <b>{__version__}</b> {direction} (نسخه قبلی: {last}).\n"
                f"تغییرات: {release_link(__version__)}"
            )
        await self.db.set_setting("last_version", __version__)

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception:  # noqa: BLE001 - never let this kill the bot
                log.exception("update watcher failed")
            await asyncio.sleep(POLL_SECONDS)

    async def poll(self) -> None:
        status = self.status
        await self._report_failed_update(status)
        await self._report_available(status)
        await self._answer_request(status)

    async def _report_failed_update(self, status: dict[str, str]) -> None:
        event, event_at = status.get("event"), status.get("event_at", "")
        if event not in ("failed", "rolled_back") or not event_at:
            return
        if event_at == await self.db.get_str("update_event_seen"):
            return
        await self.db.set_setting("update_event_seen", event_at)
        target = status.get("event_to", "?")
        if event == "rolled_back":
            text = (
                f"⚠️ نسخه {target} نصب شد ولی درست بالا نیامد، پس نسخه قبلی ({status.get('event_from', '?')}) "
                "خودکار برگردانده شد و ربات کار می‌کند.\nتا یک روز دوباره سراغ این نسخه نمی‌روم."
            )
        else:
            text = (
                f"⚠️ به‌روزرسانی به نسخه {target} انجام نشد؛ ربات با همان نسخه قبلی کار می‌کند.\n"
                f"علت: {status.get('event_error', '')}"
            )
        await self.notify(text)

    async def _report_available(self, status: dict[str, str]) -> None:
        latest = status.get("latest", "")
        if status.get("last_check_result") != "available" or version_key(latest) <= version_key(__version__):
            return
        if latest == await self.db.get_str("update_available_seen"):
            return
        await self.db.set_setting("update_available_seen", latest)
        await self.notify(
            f"🆕 نسخه {latest} آمده: {release_link(latest)}\n"
            "به‌روزرسانی خودکار خاموش است؛ برای نصب: ⚙️ تنظیمات ← «به‌روزرسانی الان»"
        )

    async def _answer_request(self, status: dict[str, str]) -> None:
        if not self._request:
            return
        chat_id, asked = self._request
        checked = status.get("last_check_at", "")
        result = status.get("last_check_result")
        if checked.isdigit() and int(checked) >= asked and result != "updating":
            self._request = None
            if result == "up_to_date":
                await self.send(chat_id, f"✅ آخرین نسخه ({__version__}) نصب است.")
            elif result == "error":
                await self.send(chat_id, f"⚠️ بررسی نسخه جدید نشد: {status.get('last_check_error', '')}")
            # failed / rolled back are reported by _report_failed_update; a successful
            # update restarts the bot, which then announces the new version itself.
        elif now() - asked > REQUEST_TIMEOUT:
            self._request = None
            await self.send(
                chat_id,
                "⚠️ به‌روزرسان روی سرور جواب نداد. روی سرور این را بزنید: <code>mp3bot update</code>",
            )
