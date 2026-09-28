import zipfile

import pytest

from app.files import (build_zip, content_disposition, human_size, is_mp3, safe_name,
                       track_file_name, unique_arcnames)
from app.links import ChannelRef, parse_ref, parse_refs


@pytest.mark.parametrize("text, expected", [
    ("@music_channel", ChannelRef(username="music_channel")),
    ("https://t.me/music_channel", ChannelRef(username="music_channel")),
    ("t.me/music_channel/1234", ChannelRef(username="music_channel")),
    ("https://t.me/s/music_channel", ChannelRef(username="music_channel")),
    ("https://telegram.me/music_channel?start=x", ChannelRef(username="music_channel")),
    ("https://t.me/+AbCdEf12345", ChannelRef(invite_hash="AbCdEf12345")),
    ("https://t.me/joinchat/AAAAAEk9Zx", ChannelRef(invite_hash="AAAAAEk9Zx")),
    ("tg://resolve?domain=music_channel", ChannelRef(username="music_channel")),
    ("tg://join?invite=AbCdEf12345", ChannelRef(invite_hash="AbCdEf12345")),
    ("https://t.me/c/123456/789", None),        # private post link: nothing to join with
    ("https://t.me/addlist/abcdef", None),       # folder links are not supported
    ("https://example.com/music", None),
    ("@ab", None),                               # too short for a username
    ("hello", None),
])
def test_parse_ref(text, expected):
    assert parse_ref(text) == expected


def test_parse_refs_many_and_deduplicated():
    text = "اینا رو اضافه کن:\n@chan_one https://t.me/chan_one t.me/+Hash_12345 ،@chan_two،"
    assert parse_refs(text) == [
        ChannelRef(username="chan_one"),
        ChannelRef(invite_hash="Hash_12345"),
        ChannelRef(username="chan_two"),
    ]


def test_ref_link():
    assert ChannelRef(username="abc_d").link == "https://t.me/abc_d"
    assert ChannelRef(invite_hash="XyZ").link == "https://t.me/+XyZ"


def test_safe_name():
    assert safe_name('a/b\\c:d*e?"f<g>h|i') == "a_b_c_d_e_f_g_h_i"
    assert safe_name("  ...  ") == "file"
    long = "x" * 300 + ".mp3"
    assert len(safe_name(long)) == 120 and safe_name(long).endswith(".mp3")
    assert safe_name("کانال موزیک") == "کانال موزیک"


def test_is_mp3():
    assert is_mp3("audio/mpeg", None)
    assert is_mp3("application/octet-stream", "Song.MP3")
    assert not is_mp3("audio/ogg", "voice.ogg")
    assert not is_mp3("audio/x-m4a", "song.m4a")
    assert not is_mp3(None, None)


def test_track_file_name():
    assert track_file_name("song.mp3", "A", "B", 1) == "song.mp3"
    assert track_file_name(None, "Artist", "Title", 1) == "Artist - Title.mp3"
    assert track_file_name(None, None, "Title", 1) == "Title.mp3"
    assert track_file_name(None, None, None, 42) == "track_42.mp3"
    assert track_file_name("no_extension", None, None, 1) == "no_extension.mp3"


def test_unique_arcnames():
    assert unique_arcnames(["a.mp3", "A.mp3", "a.mp3", "b.mp3"]) == ["a.mp3", "A (2).mp3", "a (3).mp3", "b.mp3"]


def test_build_zip(tmp_path):
    files = []
    for i in range(3):
        p = tmp_path / f"{i}.mp3"
        p.write_bytes(bytes([i]) * 1000)
        files.append((str(p), "same.mp3"))
    dest = tmp_path / "out" / "batch.zip"
    size = build_zip(files, dest)
    assert size == dest.stat().st_size
    assert not (tmp_path / "out" / "batch.zip.part").exists()
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["same.mp3", "same (2).mp3", "same (3).mp3"]
        assert zf.read("same (3).mp3") == bytes([2]) * 1000
        assert zf.testzip() is None


def test_content_disposition_keeps_persian():
    header = content_disposition("موزیک_001.zip")
    assert header.startswith('attachment; filename="_001.zip"')
    assert "filename*=UTF-8''%D9%85" in header


def test_human_size():
    assert human_size(500) == "500 B"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"
    assert human_size(3 * 1024 ** 3) == "3.0 GB"
