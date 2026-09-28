"""The bot side: admin panel, users sending channel links, delivering ZIP batches."""
from __future__ import annotations

import asyncio
import logging
import shutil
from html import escape
from pathlib import Path
from urllib.parse import quote

from telethon import Button, TelegramClient, errors, events

from .batcher import Batcher
from .collector import Collector, JoinError
from .db import Database, now
from .files import human_size
from .links import ChannelRef, parse_refs

log = logging.getLogger(__name__)

B_ADD = "➕ افزودن کانال"
B_CHANNELS = "📋 کانال‌ها"
B_STATUS = "📊 وضعیت"
B_SETTINGS = "⚙️ تنظیمات"
B_FLUSH = "📦 ارسال بسته‌های ناقص"
B_RETRY = "🔁 تلاش دوباره خطاها"
B_MINE = "🗂 بسته‌ها و لینک‌ها"
B_HELP = "❓ راهنما"

ADMIN_KEYBOARD = [
    [Button.text(B_ADD, resize=True), Button.text(B_CHANNELS)],
    [Button.text(B_STATUS), Button.text(B_SETTINGS)],
    [Button.text(B_FLUSH), Button.text(B_RETRY)],
    [Button.text(B_MINE), Button.text(B_HELP)],
]
USER_KEYBOARD = [
    [Button.text(B_ADD, resize=True), Button.text(B_MINE)],
    [Button.text(B_HELP)],
]

ADD_HELP = (
    "لینک کانال را بفرستید؛ یکی یا چند تا با هم:\n"
    "• <code>@channel</code>\n"
    "• <code>https://t.me/channel</code>\n"
    "• لینک خصوصی: <code>https://t.me/+AbCdEf...</code>\n"
    "یا یک پست از کانال عمومی را برای ربات فوروارد کنید."
)

STATUS_ICON = {"active": "🟢", "paused": "⏸", "removed": "🗑"}


def remaining(expires_at: int | None) -> str:
    if not expires_at:
        return "بدون انقضا"
    left = expires_at - now()
    if left <= 0:
        return "منقضی شده"
    if left >= 86400:
        return f"{left // 86400} روز دیگر"
    return f"{max(1, left // 3600)} ساعت دیگر"


class Deliverer:
    """Sends each new ZIP to the admins as a file and to subscribers as a direct link."""

    def __init__(self, db: Database, bot: TelegramClient, admin_ids: frozenset[int], base_url: str):
        self.db = db
        self.bot = bot
        self.admin_ids = admin_ids
        self.base_url = base_url
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued: set[int] = set()

    def link(self, batch: dict) -> str:
        return f"{self.base_url}/dl/{batch['token']}/{quote(batch['file_name'])}"

    async def enqueue(self, batch_id: int) -> None:
        if batch_id not in self._queued:
            self._queued.add(batch_id)
            await self._queue.put(batch_id)

    def start(self) -> None:
        asyncio.create_task(self._run(), name="deliverer")
        asyncio.create_task(self._maintenance(), name="maintenance")

    async def _run(self) -> None:
        while True:
            batch_id = await self._queue.get()
            try:
                await self.deliver(batch_id)
            except Exception:  # noqa: BLE001 - retried by the maintenance loop
                log.exception("delivery of batch %s failed", batch_id)
            finally:
                self._queued.discard(batch_id)

    async def _maintenance(self) -> None:
        while True:
            try:
                for batch_id in await self.db.undelivered_batches():
                    await self.enqueue(batch_id)
                for batch in await self.db.expired_batches(now()):
                    Path(batch["path"]).unlink(missing_ok=True)
                    await self.db.mark_batch_deleted(batch["id"])
                    log.info("expired and deleted %s", batch["file_name"])
            except Exception:  # noqa: BLE001
                log.exception("maintenance failed")
            await asyncio.sleep(600)

    async def deliver(self, batch_id: int) -> None:
        batch = await self.db.get_batch(batch_id)
        if not batch or batch["deleted"]:
            return
        if not Path(batch["path"]).is_file():
            log.warning("zip of batch %s is gone from disk; giving up on it", batch_id)
            await self.db.mark_batch_deleted(batch_id)
            return
        channel = await self.db.get_channel(batch["channel_id"])
        title = channel["title"] if channel else "?"
        link = self.link(batch)
        info = f"🎵 {batch['track_count']} آهنگ | 💾 {human_size(batch['size'])}"

        if not batch["admin_sent"]:
            caption = (
                f"📦 <b>{escape(title)}</b> — بسته {batch['number']}\n{info}\n\n"
                f"🔗 لینک مستقیم ({remaining(batch['expires_at'])}):\n{escape(link)}"
            )
            uploaded = await self.bot.upload_file(batch["path"], file_name=batch["file_name"], part_size_kb=512)
            retry = False
            for admin in self.admin_ids:
                try:
                    await self.bot.send_file(admin, uploaded, caption=caption, force_document=True)
                except errors.FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds)
                    retry = True
                    break
                except (errors.UserIsBlockedError, errors.InputUserDeactivatedError, errors.PeerIdInvalidError, ValueError):
                    log.warning("admin %s cannot be reached; they must press /start in the bot", admin)
            if retry:
                return
            await self.db.mark_batch(batch_id, admin_sent=True)

        if not batch["users_sent"]:
            text = (
                f"📦 بسته جدید از <b>{escape(title)}</b>\n"
                f"شماره {batch['number']} — {info}\n\n"
                f"🔗 لینک دانلود مستقیم:\n{escape(link)}\n\n"
                f"⏳ اعتبار لینک: {remaining(batch['expires_at'])}"
            )
            for user_id in await self.db.subscribers(batch["channel_id"]):
                if user_id in self.admin_ids:
                    continue  # admins already got the file with the link in its caption
                for _ in range(2):
                    try:
                        await self.bot.send_message(user_id, text, link_preview=False)
                        break
                    except errors.FloodWaitError as exc:
                        await asyncio.sleep(exc.seconds)
                    except (errors.RPCError, ValueError) as exc:
                        log.info("user %s not reachable: %s", user_id, exc)
                        break
                await asyncio.sleep(0.05)
            await self.db.mark_batch(batch_id, users_sent=True)


class BotUI:
    def __init__(self, db: Database, bot: TelegramClient, collector: Collector, batcher: Batcher,
                 deliverer: Deliverer, admin_ids: frozenset[int], data_dir: Path):
        self.db = db
        self.bot = bot
        self.collector = collector
        self.batcher = batcher
        self.deliverer = deliverer
        self.admin_ids = admin_ids
        self.data_dir = data_dir

    def register(self) -> None:
        self.bot.parse_mode = "html"
        self.bot.add_event_handler(self.on_message, events.NewMessage(incoming=True, func=lambda e: e.is_private))
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery())

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    async def notify_admins(self, text: str) -> None:
        for admin in self.admin_ids:
            try:
                await self.bot.send_message(admin, text, link_preview=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not notify admin %s: %s", admin, exc)

    # -- messages ----------------------------------------------------------
    async def on_message(self, event) -> None:
        uid = event.sender_id
        sender = await event.get_sender()
        await self.db.upsert_user(uid, getattr(sender, "username", None), getattr(sender, "first_name", None))
        admin = self.is_admin(uid)
        text = (event.raw_text or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        arg = arg.strip()

        if cmd == "/start":
            return await self.welcome(event, admin)
        if text == B_HELP or cmd == "/help":
            return await self.welcome(event, admin)
        if text == B_ADD or cmd == "/add" and not arg:
            return await event.reply(ADD_HELP)
        if text == B_MINE or cmd == "/links":
            return await self.show_links(event, admin)

        if admin:
            if text == B_CHANNELS or cmd == "/channels":
                return await self.show_channels(event)
            if text == B_STATUS or cmd == "/status":
                return await event.reply(await self.status_text())
            if text == B_SETTINGS or cmd == "/settings":
                body, buttons = await self.settings_view()
                return await event.reply(body, buttons=buttons)
            if text == B_FLUSH or cmd == "/flush":
                return await self.flush_all(event)
            if text == B_RETRY or cmd == "/retry":
                n = await self.db.retry_failed()
                self.collector.wake()
                return await event.reply(f"🔁 {n} آهنگ خطادار دوباره در صف دانلود قرار گرفت.")
            if cmd in ("/batch", "/expire", "/autoflush"):
                return await self.set_number(event, cmd, arg)
            if cmd.startswith("/ch_") and cmd[4:].isdigit():
                body, buttons = await self.channel_view(int(cmd[4:]))
                return await event.reply(body, buttons=buttons)

        refs = parse_refs(text)
        forwarded = self._forwarded_ref(event)
        if forwarded and forwarded not in refs:
            refs.append(forwarded)
        if refs:
            return await self.add_channels(event, refs, admin)
        await event.reply("متوجه نشدم. 🙂\n\n" + ADD_HELP)

    @staticmethod
    def _forwarded_ref(event) -> ChannelRef | None:
        fwd = event.message.forward
        chat = getattr(fwd, "chat", None) if fwd else None
        username = getattr(chat, "username", None)
        return ChannelRef(username=username) if username else None

    async def welcome(self, event, admin: bool) -> None:
        batch_size = await self.db.get_int("batch_size", 30)
        if admin:
            body = (
                "👋 سلام ادمین!\n\n"
                "لینک هر کانال را بفرستید تا حساب ربات عضو شود و فایل‌های mp3 آن را دانلود کند. "
                f"هر <b>{batch_size}</b> آهنگ داخل یک فایل zip برای شما فرستاده می‌شود و "
                "کاربری که کانال را معرفی کرده لینک دانلود مستقیم می‌گیرد.\n\n"
                "دستورها:\n"
                "/status وضعیت · /channels کانال‌ها · /settings تنظیمات\n"
                "/batch 30 تعداد آهنگ هر zip · /expire 7 اعتبار لینک (روز)\n"
                "/autoflush 12 ارسال بسته ناقص بعد از چند ساعت بی‌فعالیتی\n"
                "/flush ارسال فوری بسته‌های ناقص · /retry تلاش دوباره خطاها"
            )
            if not self.collector.ready:
                body += "\n\n⚠️ حساب کاربری ربات هنوز وارد نشده؛ روی سرور بزنید: <code>mp3bot login</code>"
            return await event.reply(body, buttons=ADMIN_KEYBOARD)
        body = (
            "👋 سلام!\n\n"
            "لینک یک کانال موزیک را بفرستید. ربات عضو کانال می‌شود، آهنگ‌های mp3 آن را جمع می‌کند و "
            f"هر {batch_size} آهنگ را در یک فایل zip می‌گذارد و <b>لینک دانلود مستقیم</b> آن را برای شما می‌فرستد.\n\n"
            + ADD_HELP
        )
        await event.reply(body, buttons=USER_KEYBOARD)

    async def add_channels(self, event, refs: list[ChannelRef], admin: bool) -> None:
        if not self.collector.ready:
            return await event.reply("⚠️ ربات هنوز آماده نیست (حساب کاربری وارد نشده). بعداً امتحان کنید.")
        if not admin and not await self.db.get_bool("allow_users", True):
            return await event.reply("⛔ فعلاً فقط ادمین می‌تواند کانال اضافه کند.")
        progress = await event.reply("⏳ در حال عضویت در کانال...")
        lines = []
        for ref in refs[:10]:
            try:
                channel, created = await self.collector.add_channel(ref, event.sender_id)
            except JoinError as exc:
                lines.append(f"❌ {escape(ref.link)}\n{exc}")
                continue
            title = escape(channel["title"])
            if created:
                lines.append(f"✅ عضو <b>{title}</b> شدم؛ دارم آهنگ‌هایش را پیدا می‌کنم...")
                asyncio.create_task(self._first_scan(channel, event.sender_id))
                if not admin:
                    await self.notify_admins(f"➕ کاربر <code>{event.sender_id}</code> کانال <b>{title}</b> را اضافه کرد. /ch_{channel['id']}")
            else:
                note = " (فعلاً متوقف است)" if channel["status"] == "paused" else ""
                lines.append(
                    f"ℹ️ <b>{title}</b> از قبل در لیست است{note}؛ "
                    f"لینک بسته‌های بعدی برای شما هم فرستاده می‌شود. بسته‌های قبلی: «{B_MINE}»"
                )
        if len(refs) > 10:
            lines.append("⚠️ هر بار حداکثر ۱۰ لینک؛ بقیه را جدا بفرستید.")
        await progress.edit("\n\n".join(lines), link_preview=False)

    async def _first_scan(self, channel: dict, user_id: int) -> None:
        title = escape(channel["title"])
        try:
            found = await self.collector.scan_channel(channel["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("first scan of %s failed: %s", channel["title"], exc)
            await self.db.set_channel_error(channel["id"], str(exc))
            text = f"⚠️ خواندن پست‌های <b>{title}</b> خطا داد؛ دوباره امتحان می‌کنم.\n<code>{escape(str(exc))[:200]}</code>"
        else:
            batch_size = await self.db.get_int("batch_size", 30)
            if found:
                text = (
                    f"🔎 در <b>{title}</b> تعداد <b>{found}</b> آهنگ mp3 تازه پیدا شد و دانلود شروع شد.\n"
                    f"هر {batch_size} آهنگ یک فایل zip می‌شود و لینکش برایتان می‌آید."
                )
            else:
                text = f"🔎 در <b>{title}</b> آهنگ mp3 تازه‌ای پیدا نشد؛ پست‌های جدید را زیر نظر دارم."
        try:
            await self.bot.send_message(user_id, text)
        except Exception as exc:  # noqa: BLE001
            log.info("could not report scan to %s: %s", user_id, exc)

    async def show_links(self, event, admin: bool) -> None:
        rows = (await self.db.recent_batches(now(), 15)) if admin else (await self.db.user_batches(event.sender_id, now(), 15))
        if not rows:
            if admin:
                return await event.reply("هنوز هیچ فایل zipی ساخته نشده.")
            channels = await self.db.user_channels(event.sender_id)
            if not channels:
                return await event.reply("هنوز کانالی معرفی نکرده‌اید.\n\n" + ADD_HELP)
            return await event.reply("هنوز فایل zipی برای کانال‌های شما آماده نشده؛ به محض آماده شدن، لینکش را می‌فرستم.")
        parts = ["🗂 <b>آخرین فایل‌ها</b>\n"]
        for b in rows:
            parts.append(
                f"📦 <b>{escape(b['title'])}</b> — بسته {b['number']} ({b['track_count']} آهنگ، {human_size(b['size'])})\n"
                f"{escape(self.deliverer.link(b))}\n⏳ {remaining(b['expires_at'])}"
            )
        await event.reply("\n\n".join(parts), link_preview=False)

    # -- admin views -------------------------------------------------------
    async def status_text(self) -> str:
        counts = await self.db.status_counts()
        channels = await self.db.list_channels(("active", "paused"))
        active = sum(1 for c in channels if c["status"] == "active")
        totals = await self.db.batch_totals()
        disk = shutil.disk_usage(self.data_dir)
        known = sum(counts.values())
        lines = [
            "📊 <b>وضعیت</b>\n",
            f"کانال‌ها: {active} فعال، {len(channels) - active} متوقف",
            f"کاربران: {await self.db.count_users()}",
            f"حساب کاربری ربات: {'✅ متصل' if self.collector.ready else '⚠️ وارد نشده'}\n",
            f"🎵 آهنگ‌های شناسایی‌شده: <b>{known}</b>",
            f"⏳ در صف دانلود: {counts['pending'] + counts['downloading']}",
            f"✅ دانلود شده، منتظر تکمیل بسته: {counts['downloaded']}",
            f"📦 داخل فایل zip: {counts['zipped']}",
            f"❌ خطا: {counts['failed']}",
            f"♻️ تکراری: {counts['duplicate']} · رد شده: {counts['skipped']}\n",
            f"🗜 فایل‌های zip: {totals['n']} ({human_size(totals['bytes'])})، روی سرور: {totals['live']}",
            f"💽 فضای آزاد دیسک: {human_size(disk.free)} از {human_size(disk.total)}",
        ]
        failures = await self.db.recent_failures(3)
        if failures:
            lines.append("\nآخرین خطاها:")
            lines += [f"• {escape(f['file_name'])}: <code>{escape(f['error'] or '')[:120]}</code>" for f in failures]
        return "\n".join(lines)

    async def show_channels(self, event) -> None:
        channels = await self.db.list_channels(("active", "paused"))
        if not channels:
            return await event.reply("هنوز کانالی اضافه نشده.\n\n" + ADD_HELP)
        lines = [f"📋 <b>کانال‌ها ({len(channels)})</b>\n"]
        for c in channels:
            counts = await self.db.channel_counts(c["id"])
            waiting = counts["pending"] + counts["downloading"]
            warn = " ⚠️" if c["last_error"] else ""
            lines.append(
                f"{STATUS_ICON.get(c['status'], '')} <b>{escape(c['title'])}</b>{warn}\n"
                f"   ✅ {counts['downloaded'] + counts['zipped']} · ⏳ {waiting} · ❌ {counts['failed']}   /ch_{c['id']}"
            )
        # Telegram caps a message at 4096 characters.
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 3800:
                await event.reply(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await event.reply(chunk)

    async def channel_view(self, channel_id: int) -> tuple[str, list | None]:
        c = await self.db.get_channel(channel_id)
        if not c or c["status"] == "removed":
            return "کانال پیدا نشد.", None
        counts = await self.db.channel_counts(channel_id)
        subs = await self.db.subscribers(channel_id)
        batches = await self.db.channel_batch_count(channel_id)
        scanned = f"{(now() - c['last_scan_at']) // 60} دقیقه پیش" if c["last_scan_at"] else "هنوز نه"
        body = [
            f"{STATUS_ICON.get(c['status'], '')} <b>{escape(c['title'])}</b>",
            f"{escape(c['link'])}\n",
            f"⏳ در صف: {counts['pending'] + counts['downloading']}",
            f"✅ دانلود شده، منتظر بسته: {counts['downloaded']}",
            f"📦 داخل zip: {counts['zipped']} ({batches} فایل)",
            f"❌ خطا: {counts['failed']} · ♻️ تکراری: {counts['duplicate']} · رد شده: {counts['skipped']}",
            f"👥 دریافت‌کننده لینک: {len(subs)}",
            f"🔎 آخرین بررسی: {scanned}",
        ]
        if c["last_error"]:
            body.append(f"\n⚠️ آخرین خطا: <code>{escape(c['last_error'][:200])}</code>")
        paused = c["status"] == "paused"
        buttons = [
            [
                Button.inline("▶️ ادامه" if paused else "⏸ توقف", f"cp:{channel_id}".encode()),
                Button.inline("🔎 بررسی الان", f"cs:{channel_id}".encode()),
            ],
            [
                Button.inline("📦 ارسال بسته ناقص", f"cf:{channel_id}".encode()),
                Button.inline("🗑 حذف و خروج", f"cr:{channel_id}".encode()),
            ],
        ]
        return "\n".join(body), buttons

    async def settings_view(self) -> tuple[str, list]:
        batch_size = await self.db.get_int("batch_size", 30)
        expire = await self.db.get_int("link_expire_days", 7)
        flush = await self.db.get_int("auto_flush_hours", 12)
        allow = await self.db.get_bool("allow_users", True)
        body = (
            "⚙️ <b>تنظیمات</b>\n\n"
            f"🎵 تعداد آهنگ در هر فایل zip: <b>{batch_size}</b>\n"
            f"⏳ اعتبار لینک دانلود: <b>{f'{expire} روز' if expire else 'بدون انقضا'}</b>\n"
            f"📦 ارسال خودکار بسته ناقص: <b>{f'{flush} ساعت بعد از آخرین آهنگ' if flush else 'خاموش'}</b>\n"
            f"👥 معرفی کانال توسط کاربران: <b>{'آزاد' if allow else 'فقط ادمین'}</b>\n\n"
            "عدد دلخواه: <code>/batch 50</code>  <code>/expire 3</code>  <code>/autoflush 24</code>\n"
            "(۰ برای اعتبار لینک یعنی بدون انقضا، برای ارسال خودکار یعنی خاموش)"
        )
        buttons = [
            [Button.inline(label, f"bs:{step}".encode()) for label, step in
             (("−10", -10), ("−5", -5), ("−1", -1), ("+1", 1), ("+5", 5), ("+10", 10))],
            [Button.inline("⏳ −1 روز", b"ex:-1"), Button.inline("⏳ +1 روز", b"ex:1")],
            [Button.inline("📦 −6 ساعت", b"af:-6"), Button.inline("📦 +6 ساعت", b"af:6")],
            [Button.inline(f"👥 کاربران: {'آزاد ✅' if allow else 'بسته ⛔'}", b"us:t")],
        ]
        return body, buttons

    async def set_number(self, event, cmd: str, arg: str) -> None:
        if not arg.isdigit():
            return await event.reply(f"یک عدد بنویسید، مثلاً: <code>{cmd} 30</code>")
        value = int(arg)
        if cmd == "/batch":
            if not 1 <= value <= 1000:
                return await event.reply("تعداد باید بین ۱ تا ۱۰۰۰ باشد.")
            await self.db.set_setting("batch_size", value)
            asyncio.create_task(self._recheck_batches())
            return await event.reply(f"✅ از این به بعد هر <b>{value}</b> آهنگ یک فایل zip می‌شود.")
        if cmd == "/expire":
            await self.db.set_setting("link_expire_days", min(value, 3650))
            return await event.reply(
                f"✅ اعتبار لینک‌های جدید: <b>{f'{value} روز' if value else 'بدون انقضا'}</b> (لینک‌های قبلی تغییری نمی‌کنند)"
            )
        await self.db.set_setting("auto_flush_hours", min(value, 24 * 365))
        await event.reply(f"✅ ارسال خودکار بسته ناقص: <b>{f'{value} ساعت' if value else 'خاموش'}</b>")

    async def _recheck_batches(self) -> None:
        try:
            await self.batcher.check_all()
        except Exception:  # noqa: BLE001
            log.exception("re-check after batch size change failed")

    async def flush_all(self, event) -> None:
        progress = await event.reply("⏳ در حال ساخت فایل‌های zip از آهنگ‌های باقی‌مانده...")
        created = await self.batcher.check_all(force=True)
        await progress.edit(f"✅ {len(created)} فایل zip ساخته و در صف ارسال گذاشته شد." if created else "آهنگ باقی‌مانده‌ای برای بسته‌بندی نیست.")

    # -- buttons -----------------------------------------------------------
    async def on_callback(self, event) -> None:
        if not self.is_admin(event.sender_id):
            return await event.answer("فقط ادمین", alert=True)
        kind, _, value = event.data.decode().partition(":")
        try:
            if kind in ("bs", "ex", "af", "us"):
                await self._settings_button(kind, value)
                body, buttons = await self.settings_view()
                await event.edit(body, buttons=buttons)
                return await event.answer("✅")
            channel_id = int(value)
            if kind == "cp":
                channel = await self.db.get_channel(channel_id)
                await self.collector.set_paused(channel_id, channel["status"] == "active")
                await event.answer("✅")
            elif kind == "cs":
                await event.answer("در حال بررسی...")
                found = await self.collector.scan_channel(channel_id)
                await event.respond(f"🔎 {found} آهنگ تازه پیدا شد.")
            elif kind == "cf":
                created = await self.batcher.check(channel_id, force=True)
                await event.answer(f"{len(created)} فایل zip ساخته شد" if created else "آهنگ باقی‌مانده‌ای نیست", alert=True)
            elif kind == "cr":
                return await event.edit(
                    "🗑 این کانال حذف شود؟ ربات از آن خارج می‌شود و آهنگ‌های دانلود‌نشده رها می‌شوند. "
                    "آهنگ‌های دانلودشده در یک فایل zip آخر فرستاده می‌شوند.",
                    buttons=[[Button.inline("بله، حذف کن", f"cR:{channel_id}".encode()),
                              Button.inline("نه", f"cv:{channel_id}".encode())]],
                )
            elif kind == "cR":
                await self.collector.remove_channel(channel_id)
                return await event.edit("🗑 کانال حذف شد.")
            body, buttons = await self.channel_view(channel_id)
            await event.edit(body, buttons=buttons)
        except errors.MessageNotModifiedError:
            await event.answer()
        except Exception as exc:  # noqa: BLE001
            log.exception("callback %s failed", event.data)
            await event.answer(f"خطا: {exc}"[:190], alert=True)

    async def _settings_button(self, kind: str, value: str) -> None:
        if kind == "us":
            await self.db.set_setting("allow_users", not await self.db.get_bool("allow_users", True))
            return
        key, low, high = {
            "bs": ("batch_size", 1, 1000),
            "ex": ("link_expire_days", 0, 3650),
            "af": ("auto_flush_hours", 0, 24 * 365),
        }[kind]
        current = await self.db.get_int(key)
        await self.db.set_setting(key, max(low, min(high, current + int(value))))
        if kind == "bs":
            asyncio.create_task(self._recheck_batches())
