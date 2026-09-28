"""Settings read from the environment (.env in Docker)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


class ConfigError(RuntimeError):
    pass


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def parse_proxy(url: str) -> dict | None:
    """socks5://user:pass@host:port or http://host:port -> Telethon proxy dict."""
    if not url:
        return None
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("socks5", "socks4", "http"):
        raise ConfigError(f"TG_PROXY: unsupported scheme {scheme!r} (use socks5:// or http://)")
    if not parts.hostname or not parts.port:
        raise ConfigError("TG_PROXY must look like socks5://host:port")
    proxy = {
        "proxy_type": scheme,
        "addr": parts.hostname,
        "port": parts.port,
        "rdns": True,
    }
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy


def parse_admin_ids(raw: str) -> frozenset[int]:
    ids = set()
    for chunk in raw.replace(" ", ",").split(","):
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_IDS: {chunk!r} is not a numeric Telegram id") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: frozenset[int]
    base_url: str
    data_dir: Path
    listen_port: int
    batch_size: int
    max_zip_mb: int
    link_expire_days: int
    auto_flush_hours: int
    scan_interval_min: int
    download_workers: int
    allow_users: bool
    keep_mp3: bool
    proxy: dict | None
    phone: str

    @property
    def user_session(self) -> Path:
        return self.data_dir / "user"

    @property
    def bot_session(self) -> Path:
        return self.data_dir / "bot"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def music_dir(self) -> Path:
        return self.data_dir / "music"

    @property
    def zip_dir(self) -> Path:
        return self.data_dir / "zips"

    @property
    def max_zip_bytes(self) -> int:
        return self.max_zip_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.music_dir, self.zip_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_config(require_bot: bool = True) -> Config:
    api_id = _int("API_ID", 0)
    api_hash = _str("API_HASH")
    if not api_id or not api_hash:
        raise ConfigError("API_ID and API_HASH are required (get them from https://my.telegram.org)")
    bot_token = _str("BOT_TOKEN")
    admin_ids = parse_admin_ids(_str("ADMIN_IDS"))
    if require_bot:
        if not bot_token:
            raise ConfigError("BOT_TOKEN is required (create a bot with @BotFather)")
        if not admin_ids:
            raise ConfigError("ADMIN_IDS is required (your numeric Telegram id, e.g. from @userinfobot)")
    web_port = _int("WEB_PORT", 8080)
    base_url = _str("BASE_URL") or f"http://127.0.0.1:{web_port}"
    return Config(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        admin_ids=admin_ids,
        base_url=base_url.rstrip("/"),
        data_dir=Path(_str("DATA_DIR", "/data")),
        # Inside the container the web server always listens on 8080;
        # WEB_PORT is the host port docker-compose publishes it on.
        listen_port=_int("LISTEN_PORT", 8080),
        batch_size=max(1, _int("BATCH_SIZE", 30)),
        max_zip_mb=max(10, min(_int("MAX_ZIP_MB", 1900), 1990)),
        link_expire_days=max(0, _int("LINK_EXPIRE_DAYS", 7)),
        auto_flush_hours=max(0, _int("AUTO_FLUSH_HOURS", 12)),
        scan_interval_min=max(1, _int("SCAN_INTERVAL_MIN", 5)),
        download_workers=max(1, min(_int("DOWNLOAD_WORKERS", 2), 8)),
        allow_users=_bool("ALLOW_USERS", True),
        keep_mp3=_bool("KEEP_MP3", False),
        proxy=parse_proxy(_str("TG_PROXY")),
        phone=_str("PHONE"),
    )
