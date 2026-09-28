"""Understands the ways people paste a channel: @name, t.me/name, t.me/+invite, t.me/joinchat/..., post links."""
from __future__ import annotations

import re
from dataclasses import dataclass

# t.me paths that are not channel usernames
_RESERVED = {
    "joinchat", "addstickers", "addemoji", "addtheme", "addlist", "share", "proxy", "socks",
    "c", "s", "iv", "login", "setlanguage", "confirmphone", "bg", "invoice", "boost", "m", "contact",
}
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_HOST = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(.+)$", re.I)
_RESOLVE = re.compile(r"^tg://resolve\?(?:.*&)?domain=([A-Za-z0-9_]+)", re.I)
_TG_JOIN = re.compile(r"^tg://join\?invite=([A-Za-z0-9_-]+)", re.I)


@dataclass(frozen=True)
class ChannelRef:
    username: str | None = None
    invite_hash: str | None = None

    @property
    def link(self) -> str:
        if self.username:
            return f"https://t.me/{self.username}"
        return f"https://t.me/+{self.invite_hash}"


def parse_ref(token: str) -> ChannelRef | None:
    token = token.strip().strip("<>()[]\"'،,")
    if not token:
        return None
    if token.startswith("@"):
        name = token[1:]
        return ChannelRef(username=name) if _USERNAME.match(name) else None
    m = _TG_JOIN.match(token)
    if m:
        return ChannelRef(invite_hash=m.group(1))
    m = _RESOLVE.match(token)
    if m:
        return ChannelRef(username=m.group(1)) if _USERNAME.match(m.group(1)) else None
    m = _HOST.match(token)
    if not m:
        return None
    path = m.group(1).split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    first = parts[0]
    if first.startswith("+"):
        invite = first[1:]
        return ChannelRef(invite_hash=invite) if re.fullmatch(r"[A-Za-z0-9_-]{5,}", invite) else None
    if first.lower() == "joinchat" and len(parts) > 1:
        return ChannelRef(invite_hash=parts[1])
    if first.lower() == "s" and len(parts) > 1:  # t.me/s/name = web preview of a public channel
        first = parts[1]
    elif first.lower() in _RESERVED:
        return None
    return ChannelRef(username=first) if _USERNAME.match(first) else None


def parse_refs(text: str) -> list[ChannelRef]:
    """Every channel reference in a message, in order, without repeats."""
    seen: list[ChannelRef] = []
    for token in text.split():
        ref = parse_ref(token)
        if ref and ref not in seen:
            seen.append(ref)
    return seen
