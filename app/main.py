"""Entry point: wires the user account, the bot, the downloader and the web server together."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from telethon import TelegramClient, errors

from . import __version__
from .batcher import Batcher
from .bot import BotUI, Deliverer
from .collector import Collector
from .config import ConfigError, load_config
from .db import Database
from .updates import UpdateWatcher
from .web import start_web

log = logging.getLogger("mp3bot")


def client_kwargs(cfg) -> dict:
    return {
        "proxy": cfg.proxy,
        "flood_sleep_threshold": 120,
        "device_model": "MP3 Collector",
        "system_version": "Linux",
        "app_version": __version__,
    }


async def run() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    cfg.ensure_dirs()
    db = await Database.open(str(cfg.db_path))
    bot = TelegramClient(str(cfg.bot_session), cfg.api_id, cfg.api_hash, **client_kwargs(cfg))
    user = TelegramClient(str(cfg.user_session), cfg.api_id, cfg.api_hash, **client_kwargs(cfg))
    runner = None
    # Everything below must end in the cleanup: the database keeps a non-daemon
    # thread, so a crash that skipped db.close() would leave a hung process that
    # Docker never restarts.
    try:
        runner = await start_web(db, cfg.listen_port)
        log.info("download server listening on :%s (public: %s)", cfg.listen_port, cfg.base_url)
        return await serve(cfg, db, bot, user)
    except errors.AccessTokenInvalidError:
        log.error("configuration error: BOT_TOKEN is wrong; get it again from @BotFather and run `mp3bot config`")
        return 1
    except errors.ApiIdInvalidError:
        log.error("configuration error: API_ID/API_HASH are wrong; copy them again from my.telegram.org and run `mp3bot config`")
        return 1
    except Exception:  # noqa: BLE001 - log it, exit non-zero, let Docker restart us
        log.exception("fatal error")
        return 1
    finally:
        for client in (bot, user):
            if client.is_connected():
                await client.disconnect()
        if runner:
            await runner.cleanup()
        await db.close()


async def serve(cfg, db: Database, bot: TelegramClient, user: TelegramClient) -> int:
    await db.seed_settings({
        "batch_size": cfg.batch_size,
        "link_expire_days": cfg.link_expire_days,
        "auto_flush_hours": cfg.auto_flush_hours,
        "allow_users": cfg.allow_users,
    })
    await bot.start(bot_token=cfg.bot_token)
    me = await bot.get_me()
    log.info("bot @%s is online", me.username)

    deliverer = Deliverer(db, bot, cfg.admin_ids, cfg.base_url)
    batcher = Batcher(db, cfg.zip_dir, cfg.max_zip_bytes, cfg.keep_mp3, deliverer.enqueue)
    collector = Collector(
        db, user, batcher, cfg.music_dir,
        max_track_bytes=cfg.max_zip_bytes,
        workers=cfg.download_workers,
        scan_interval=cfg.scan_interval_min * 60,
    )
    ui = BotUI(db, bot, collector, batcher, deliverer, cfg.admin_ids, cfg.data_dir)
    updates = UpdateWatcher(db, cfg.data_dir, ui.notify_admins, bot.send_message)
    ui.updates = updates
    ui.register()
    collector.alert = ui.notify_admins
    deliverer.start()
    await updates.start()

    await user.connect()
    if await user.is_user_authorized():
        account = await user.get_me()
        log.info("user account %s (%s) is online", account.first_name, account.id)
        await collector.start()
    else:
        log.error("the Telegram user account is not logged in: run `mp3bot login` on the server")
        await ui.notify_admins(
            "⚠️ حساب کاربری تلگرام (که عضو کانال‌ها می‌شود) هنوز وارد نشده.\n"
            "روی سرور این دستور را بزنید: <code>mp3bot login</code>"
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    waiters = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(bot.disconnected)]
    if user.is_connected():
        waiters.append(asyncio.ensure_future(user.disconnected))
    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    if stop.is_set():
        log.info("stopping")
        return 0
    log.error("lost the connection to Telegram; exiting so Docker restarts the bot")
    return 1


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
