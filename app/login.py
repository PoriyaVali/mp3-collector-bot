"""Log the Telegram user account in (interactive). `--check` only reports whether it is logged in."""
from __future__ import annotations

import asyncio
import getpass
import sys

from telethon import TelegramClient

from .config import ConfigError, load_config
from .main import client_kwargs


async def run(check_only: bool) -> int:
    try:
        cfg = load_config(require_bot=False)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    cfg.ensure_dirs()
    client = TelegramClient(str(cfg.user_session), cfg.api_id, cfg.api_hash, **client_kwargs(cfg))
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Logged in as {me.first_name} (@{me.username or '-'}, id {me.id}).")
            return 0
        if check_only:
            print("Not logged in.")
            return 1
        print("Log in with the Telegram account that will JOIN the channels and download the music.")
        print("Tip: use a spare account, not your personal one.\n")
        await client.start(
            phone=lambda: cfg.phone or input("Phone number with country code (e.g. +44...): ").strip(),
            code_callback=lambda: input("Login code Telegram just sent you: ").strip(),
            password=lambda: getpass.getpass("Two-step verification password: "),
        )
        me = await client.get_me()
        print(f"\nDone: logged in as {me.first_name} (@{me.username or '-'}, id {me.id}).")
        return 0
    finally:
        await client.disconnect()


def main() -> None:
    sys.exit(asyncio.run(run("--check" in sys.argv)))


if __name__ == "__main__":
    main()
