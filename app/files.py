"""File-name hygiene, MP3 detection and ZIP building (pure functions, no Telegram)."""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path
from urllib.parse import quote

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SPACES = re.compile(r"\s+")

MP3_MIME = {"audio/mpeg", "audio/mp3", "audio/mpeg3", "audio/x-mpeg-3", "audio/x-mp3", "audio/mpg"}


def safe_name(name: str, max_len: int = 120, fallback: str = "file") -> str:
    """A name that is valid on Windows, Linux and inside a ZIP."""
    name = _BAD_CHARS.sub("_", name or "")
    name = _SPACES.sub(" ", name).strip(" ._")
    if not name:
        return fallback
    if len(name) > max_len:
        stem, ext = os.path.splitext(name)
        if len(ext) > 10:
            stem, ext = name, ""
        name = stem[: max_len - len(ext)].rstrip(" ._") + ext
    return name


def is_mp3(mime_type: str | None, file_name: str | None) -> bool:
    if file_name and file_name.lower().endswith(".mp3"):
        return True
    return (mime_type or "").lower() in MP3_MIME


def track_file_name(file_name: str | None, performer: str | None, title: str | None, msg_id: int) -> str:
    """Name used inside the ZIP: the uploader's file name, else 'Performer - Title', else the post id."""
    if file_name:
        name = file_name
    elif performer and title:
        name = f"{performer} - {title}"
    elif title:
        name = title
    else:
        name = f"track_{msg_id}"
    name = safe_name(name, fallback=f"track_{msg_id}")
    if not name.lower().endswith(".mp3"):
        name += ".mp3"
    return name


def unique_arcnames(names: list[str]) -> list[str]:
    """'a.mp3', 'a.mp3' -> 'a.mp3', 'a (2).mp3' (case-insensitive, like Windows)."""
    used: set[str] = set()
    out = []
    for name in names:
        stem, ext = os.path.splitext(name)
        candidate, n = name, 1
        while candidate.lower() in used:
            n += 1
            candidate = f"{stem} ({n}){ext}"
        used.add(candidate.lower())
        out.append(candidate)
    return out


def build_zip(entries: list[tuple[str, str]], dest: Path) -> int:
    """Write (source path, name in zip) pairs to `dest` atomically; returns the ZIP size.

    MP3 is already compressed, so files are stored as-is: same size, far less CPU.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    arcnames = unique_arcnames([arc for _, arc in entries])
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for (src, _), arc in zip(entries, arcnames):
                zf.write(src, arcname=arc)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return dest.stat().st_size


def content_disposition(file_name: str) -> str:
    """Attachment header that keeps non-ASCII (e.g. Persian) names intact."""
    ascii_name = file_name.encode("ascii", "ignore").decode() or "download.zip"
    ascii_name = ascii_name.replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name)}"


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
