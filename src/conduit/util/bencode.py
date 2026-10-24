"""Minimal bencode decoder plus torrent info-hash extraction.

Why this exists: the reference project matched qBittorrent torrents back to its
own database rows by fuzzy-matching *titles*, which breaks on any release whose
name differs from the Plex title. Conduit fetches the .torrent file, computes
its info-hash locally, and hands qBittorrent a torrent whose identity it
already knows. Matching then becomes an exact hash lookup that cannot drift.
"""

from __future__ import annotations

import hashlib
from typing import Any


class BencodeError(ValueError):
    """Raised when a payload is not valid bencode."""


def decode(data: bytes) -> Any:
    """Decode a bencoded payload. Raises :class:`BencodeError` on bad input.

    Trailing bytes past the top-level value are tolerated -- some trackers
    append them -- because the info-hash only depends on the ``info`` dict.
    """
    value, _ = _decode_at(data, 0)
    return value


def _decode_at(data: bytes, i: int) -> tuple[Any, int]:
    if i >= len(data):
        raise BencodeError("unexpected end of input")
    marker = data[i : i + 1]

    # A truncated payload otherwise surfaces as a bare ValueError from
    # bytes.index or int(), which callers cannot tell from a real bug.
    try:
        if marker == b"i":
            end = data.index(b"e", i)
            return int(data[i + 1 : end]), end + 1

        if marker == b"l":
            i += 1
            items: list[Any] = []
            while data[i : i + 1] != b"e":
                value, i = _decode_at(data, i)
                items.append(value)
            return items, i + 1

        if marker == b"d":
            i += 1
            mapping: dict[bytes, Any] = {}
            while data[i : i + 1] != b"e":
                key, i = _decode_at(data, i)
                if not isinstance(key, bytes):
                    raise BencodeError("dictionary key must be a byte string")
                value, i = _decode_at(data, i)
                mapping[key] = value
            return mapping, i + 1

        if marker.isdigit():
            colon = data.index(b":", i)
            length = int(data[i:colon])
            start = colon + 1
            if start + length > len(data):
                raise BencodeError(f"string at offset {i} runs past the end of the payload")
            return data[start : start + length], start + length
    except ValueError as exc:
        if isinstance(exc, BencodeError):
            raise
        raise BencodeError(f"malformed bencode at offset {i}: {exc}") from exc

    raise BencodeError(f"invalid bencode marker {marker!r} at offset {i}")


def encode(value: Any) -> bytes:
    """Encode back to bencode. Only needed to re-serialise the ``info`` dict."""
    if isinstance(value, bool):
        raise BencodeError("bool is not representable in bencode")
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return str(len(raw)).encode() + b":" + raw
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    if isinstance(value, dict):
        out = [b"d"]
        for key in sorted(value, key=lambda k: k if isinstance(k, bytes) else str(k).encode()):
            out.append(encode(key))
            out.append(encode(value[key]))
        out.append(b"e")
        return b"".join(out)
    raise BencodeError(f"cannot encode {type(value).__name__}")


def _decode_with_info_span(data: bytes) -> tuple[dict[bytes, Any], tuple[int, int]]:
    """Decode a torrent and locate the raw bytes of its ``info`` value.

    The info-hash is the SHA-1 of those exact bytes. Re-encoding the parsed
    dict only reproduces them when the original keys were canonically sorted,
    and real trackers do serve torrents that are not -- qBittorrent would then
    know the torrent by a different hash than the one recorded here.
    """
    if data[:1] != b"d":
        raise BencodeError("torrent file is not a dictionary")
    i = 1
    mapping: dict[bytes, Any] = {}
    span: tuple[int, int] | None = None
    while data[i : i + 1] != b"e":
        if i >= len(data):
            raise BencodeError("unexpected end of input")
        key, i = _decode_at(data, i)
        if not isinstance(key, bytes):
            raise BencodeError("dictionary key must be a byte string")
        start = i
        value, i = _decode_at(data, i)
        mapping[key] = value
        if key == b"info":
            span = (start, i)
    if span is None or not isinstance(mapping.get(b"info"), dict):
        raise BencodeError("torrent file has no info dictionary")
    return mapping, span


def info_hash(torrent_bytes: bytes) -> str:
    """SHA-1 info-hash (hex, lowercase) -- the id qBittorrent uses for v1 torrents."""
    _, (start, end) = _decode_with_info_span(torrent_bytes)
    return hashlib.sha1(torrent_bytes[start:end]).hexdigest()


def torrent_summary(torrent_bytes: bytes) -> dict[str, Any]:
    """Name, total size and file count, straight from the torrent metadata.

    Trackers occasionally report a size that excludes padding files; reading it
    from the torrent itself gives an accurate figure for the disk-space check.
    """
    parsed, (start, end) = _decode_with_info_span(torrent_bytes)
    info = parsed[b"info"]

    name = info.get(b"name", b"")
    if isinstance(name, bytes):
        name = name.decode("utf-8", "replace")

    files = info.get(b"files")
    if isinstance(files, list):
        total = sum(int(f.get(b"length", 0)) for f in files if isinstance(f, dict))
        count = len(files)
    else:
        total = int(info.get(b"length", 0))
        count = 1

    return {
        "name": name,
        "size_bytes": total,
        "file_count": count,
        "info_hash": hashlib.sha1(torrent_bytes[start:end]).hexdigest(),
    }
