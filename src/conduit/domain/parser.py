"""Release-name parser.

The reference project made grab decisions from four loose regexes over the
release name, which meant ``S01E01-E03`` multi-episode packs, ``DV HDR10+``
layering, date-based episodes and hybrid remuxes were all invisible to it.

This parser extracts the full attribute set, in the order the scene actually
writes it, and then lets indexer-supplied hints fill any remaining gaps. It is
pure: give it a string, get a :class:`ParsedRelease`.
"""

from __future__ import annotations

import contextlib
import re
from datetime import date

from .models import ParsedRelease, Release

# --- structural ------------------------------------------------------------
RE_SEPARATORS = re.compile(r"[._]+")
RE_MULTISPACE = re.compile(r"\s{2,}")
RE_YEAR = re.compile(r"(?:^|[\s(\[])((?:19|20)\d{2})(?=[\s)\]]|$)")

RE_SE = re.compile(
    r"\bS(?P<season>\d{1,3})\s*(?:E|EP|X)(?P<ep>\d{1,4})"
    r"(?P<extra>(?:\s*-\s*(?:E|EP)?\d{1,4}|\s*(?:E|EP)\d{1,4})*)",
    re.IGNORECASE,
)
RE_XFORMAT = re.compile(r"\b(?P<season>\d{1,2})x(?P<ep>\d{2,3})\b", re.IGNORECASE)
RE_SEASON_RANGE = re.compile(r"\bS(?P<from>\d{1,3})\s*-\s*S?(?P<to>\d{1,3})\b", re.IGNORECASE)
RE_SEASON_ONLY = re.compile(r"\bS(?P<season>\d{1,3})\b(?!\s*(?:E|EP)\d)", re.IGNORECASE)
RE_SEASON_WORD = re.compile(r"\bSeasons?\s+(?P<season>\d{1,3})\b", re.IGNORECASE)
RE_SEASON_WORD_RANGE = re.compile(
    r"\bSeasons?\s+(?P<from>\d{1,3})\s*(?:-|to|through)\s*(?P<to>\d{1,3})\b", re.IGNORECASE
)
RE_DATE_EPISODE = re.compile(r"\b(?P<y>(?:19|20)\d{2})[.\- ](?P<m>\d{2})[.\- ](?P<d>\d{2})\b")
RE_COMPLETE = re.compile(r"\b(complete|the\s+complete)\s+(series|collection)\b", re.IGNORECASE)
RE_EXTRA_EP = re.compile(r"(?:E|EP)?(\d{1,4})", re.IGNORECASE)

RE_GROUP = re.compile(r"-\s*(?P<group>[A-Za-z0-9@#$._]{2,25})\s*$")
RE_GROUP_BRACKET = re.compile(r"[\[(]\s*(?P<group>[A-Za-z0-9@#$_]{2,25})\s*[\])]\s*$")

# --- attribute vocabularies ------------------------------------------------
# Ordered longest/most-specific first: the first hit wins.
RESOLUTIONS: list[tuple[str, re.Pattern[str]]] = [
    ("2160p", re.compile(r"\b(2160p|4k|uhd\s?bluray|ultrahd|3840x2160)\b", re.IGNORECASE)),
    ("1080p", re.compile(r"\b(1080p|1920x1080)\b", re.IGNORECASE)),
    ("1080i", re.compile(r"\b1080i\b", re.IGNORECASE)),
    ("720p", re.compile(r"\b(720p|1280x720)\b", re.IGNORECASE)),
    ("576p", re.compile(r"\b576[pi]\b", re.IGNORECASE)),
    ("480p", re.compile(r"\b480[pi]\b", re.IGNORECASE)),
]

SOURCES: list[tuple[str, re.Pattern[str]]] = [
    ("remux", re.compile(r"\bremux\b", re.IGNORECASE)),
    ("bluray", re.compile(r"\b(blu[\s.-]?ray|bdrip|brrip|bd25|bd50|bd66|bd100|uhd\s?bd)\b", re.IGNORECASE)),
    ("webdl", re.compile(r"\bweb[\s.-]?dl\b", re.IGNORECASE)),
    ("webrip", re.compile(r"\bweb[\s.-]?rip\b", re.IGNORECASE)),
    ("web", re.compile(r"\bweb\b", re.IGNORECASE)),
    ("hdtv", re.compile(r"\b(hdtv|pdtv|sdtv|dsr)\b", re.IGNORECASE)),
    ("dvd", re.compile(r"\b(dvd|dvdrip|dvd5|dvd9)\b", re.IGNORECASE)),
    ("cam", re.compile(r"\b(cam|camrip|hdcam|hdts|telesync|telecine|screener|workprint)\b", re.IGNORECASE)),
]

VIDEO_CODECS: list[tuple[str, re.Pattern[str]]] = [
    ("av1", re.compile(r"\bav1\b", re.IGNORECASE)),
    ("hevc", re.compile(r"\b(hevc|[hx][\s.-]?265)\b", re.IGNORECASE)),
    ("avc", re.compile(r"\b(avc|[hx][\s.-]?264)\b", re.IGNORECASE)),
    ("vc1", re.compile(r"\bvc[\s.-]?1\b", re.IGNORECASE)),
    ("mpeg2", re.compile(r"\bmpeg[\s.-]?2\b", re.IGNORECASE)),
    ("xvid", re.compile(r"\b(xvid|divx)\b", re.IGNORECASE)),
]

RE_DV = re.compile(r"\b(dv|dovi|dolby[\s.-]?vision)\b", re.IGNORECASE)
# No trailing \b: "HDR10+" ends on a non-word character, so a boundary assertion
# after the plus never matches.
RE_HDR10PLUS = re.compile(r"\bhdr10\s?\+|\bhdr10plus\b|\bhdr\s?\+|\bhdr10p\b", re.IGNORECASE)
RE_HDR10 = re.compile(r"\bhdr10\b", re.IGNORECASE)
RE_HDR = re.compile(r"\bhdr\b", re.IGNORECASE)
RE_HLG = re.compile(r"\bhlg\b", re.IGNORECASE)
RE_SDR = re.compile(r"\bsdr\b", re.IGNORECASE)

RE_ATMOS = re.compile(r"\batmos\b", re.IGNORECASE)
AUDIO_CODECS: list[tuple[str, re.Pattern[str]]] = [
    ("dts_x", re.compile(r"\bdts[\s.:-]?x\b", re.IGNORECASE)),
    ("truehd", re.compile(r"\b(true[\s.-]?hd|thd)\b", re.IGNORECASE)),
    ("dts_hd_ma", re.compile(r"\bdts[\s.-]?hd[\s.-]?ma\b|\bdtshd[\s.-]?ma\b|\bdca[\s.-]?ma\b", re.IGNORECASE)),
    ("dts_hd", re.compile(r"\bdts[\s.-]?hd\b", re.IGNORECASE)),
    ("flac", re.compile(r"\bflac\b", re.IGNORECASE)),
    # \d? catches "DDP5" (left behind when "DDP5.1" splits on dots). "DD+" gets
    # no trailing \b -- a plus followed by a space is not a word boundary.
    ("dd_plus", re.compile(r"\b(?:ddp\d?\b|dd\s?\+|e[\s.-]?ac[\s.-]?3\d?\b|eac3\d?\b)", re.IGNORECASE)),
    ("dts", re.compile(r"\bdts\b", re.IGNORECASE)),
    ("ac3", re.compile(r"\b(ac[\s.-]?3|dd\d?)\b(?!\+)", re.IGNORECASE)),
    ("aac", re.compile(r"\baac\b", re.IGNORECASE)),
    ("opus", re.compile(r"\bopus\b", re.IGNORECASE)),
    ("mp3", re.compile(r"\bmp3\b", re.IGNORECASE)),
]
RE_CHANNELS = re.compile(r"\b([1-9])[\s.]([01])\b")

RE_FULL_DISC = re.compile(
    r"\b(full[\s.-]?disc|bd25|bd50|bd66|bd100|complete[\s.-]?blu[\s.-]?ray|iso|untouched|avc[\s.-]?remux\b(?=.*\bdisc\b))\b",
    re.IGNORECASE,
)
RE_REPACK = re.compile(r"\brepack\d?\b", re.IGNORECASE)
RE_PROPER = re.compile(r"\bproper\d?\b", re.IGNORECASE)
RE_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)

EDITIONS: list[tuple[str, re.Pattern[str]]] = [
    ("imax_enhanced", re.compile(r"\bimax[\s.-]?enhanced\b", re.IGNORECASE)),
    ("imax", re.compile(r"\bimax\b", re.IGNORECASE)),
    ("extended", re.compile(r"\b(extended|eec|extended[\s.-]?cut)\b", re.IGNORECASE)),
    ("directors_cut", re.compile(r"\b(director'?s?[\s.-]?cut|dc)\b", re.IGNORECASE)),
    ("theatrical", re.compile(r"\btheatrical\b", re.IGNORECASE)),
    ("uncut", re.compile(r"\b(uncut|unrated|uncensored)\b", re.IGNORECASE)),
    ("remastered", re.compile(r"\b(remaster(ed)?|restored|criterion)\b", re.IGNORECASE)),
    ("open_matte", re.compile(r"\bopen[\s.-]?matte\b", re.IGNORECASE)),
]

# Everything that signals "the title stopped here".
RE_TITLE_STOP = re.compile(
    r"\b(?:S\d{1,3}(?:E\d{1,4})?|Season|\d{1,2}x\d{2}|2160p|1080[pi]|720p|576[pi]|480[pi]|4K|UHD|"
    r"BluRay|Blu-Ray|BDRip|BRRip|WEB-DL|WEBRip|WEB|HDTV|DVDRip|DVD|REMUX|COMPLETE|"
    r"(?:19|20)\d{2})\b",
    re.IGNORECASE,
)


def _normalise(name: str) -> str:
    """Dots and underscores become spaces so word boundaries behave."""
    text = RE_SEPARATORS.sub(" ", name)
    return RE_MULTISPACE.sub(" ", text).strip()


def _match_first(text: str, table: list[tuple[str, re.Pattern[str]]]) -> str | None:
    for value, pattern in table:
        if pattern.search(text):
            return value
    return None


def _dynamic_range(text: str) -> str:
    """Layered HDR formats: a DV+HDR10+ disc is not the same as plain HDR."""
    dv = bool(RE_DV.search(text))
    hdr10plus = bool(RE_HDR10PLUS.search(text))
    hdr10 = bool(RE_HDR10.search(text))
    hdr = bool(RE_HDR.search(text))

    if dv and hdr10plus:
        return "dv_hdr10plus"
    if dv and (hdr10 or hdr):
        return "dv_hdr10"
    if dv:
        return "dv"
    if hdr10plus:
        return "hdr10plus"
    if hdr10:
        return "hdr10"
    if hdr:
        return "hdr"
    if RE_HLG.search(text):
        return "hlg"
    if RE_SDR.search(text):
        return "sdr"
    return "sdr"


def _audio(text: str) -> str | None:
    codec = _match_first(text, AUDIO_CODECS)
    if codec is None:
        return None
    if RE_ATMOS.search(text):
        if codec == "truehd":
            return "truehd_atmos"
        if codec in ("dd_plus", "ac3"):
            return "eac3_atmos"
    return codec


def _release_group(original: str) -> str | None:
    bracket = RE_GROUP_BRACKET.search(original.strip())
    if bracket:
        return bracket.group("group")
    match = RE_GROUP.search(original.strip())
    if not match:
        return None
    group = match.group("group").strip(" .")
    # Guard against swallowing an attribute: "...DTS-HD" or "...WEB-DL".
    if group.lower() in {"hd", "dl", "ma", "x", "1", "5", "265", "264", "e"}:
        return None
    return group or None


def _episodes(match: re.Match[str]) -> list[int]:
    """Expand ``S01E01-E03`` / ``S01E01E02`` into an explicit episode list."""
    first = int(match.group("ep"))
    extra = match.groupdict().get("extra") or ""
    numbers = [int(n) for n in RE_EXTRA_EP.findall(extra) if n]
    if not numbers:
        return [first]
    last = max(numbers)
    if "-" in extra and last > first:
        return list(range(first, last + 1))
    return sorted({first, *numbers})


RE_TRAILING_COMPLETE = re.compile(
    r"\s*\b(the\s+)?complete\s+(series|collection|seasons?)\b\s*$", re.IGNORECASE
)
RE_LEADING_COMPLETE = re.compile(
    r"^\s*\b(the\s+)?complete\s+(series|collection|seasons?)\b\s*", re.IGNORECASE
)


def _title_before(text: str, index: int) -> str:
    raw = text[:index].strip(" -.[](){}")
    raw = RE_TRAILING_COMPLETE.sub("", raw)
    raw = RE_LEADING_COMPLETE.sub("", raw)
    return RE_MULTISPACE.sub(" ", raw).strip(" -.")


def parse(name: str, release: Release | None = None) -> ParsedRelease:
    """Parse a release name, optionally refined by indexer-supplied hints."""
    original = (name or "").strip()
    text = _normalise(original)
    parsed = ParsedRelease(raw_name=original)

    # --- season / episode ---------------------------------------------------
    stop_index: int | None = None

    range_match = RE_SEASON_RANGE.search(text) or RE_SEASON_WORD_RANGE.search(text)
    se_match = RE_SE.search(text) or RE_XFORMAT.search(text)
    date_match = RE_DATE_EPISODE.search(text)

    if range_match:
        parsed.season = int(range_match.group("from"))
        parsed.season_to = int(range_match.group("to"))
        parsed.is_season_pack = True
        parsed.is_complete_series = True
        stop_index = range_match.start()
    elif se_match:
        parsed.season = int(se_match.group("season"))
        parsed.episodes = (
            _episodes(se_match) if "extra" in se_match.groupdict() else [int(se_match.group("ep"))]
        )
        stop_index = se_match.start()
    else:
        season_match = RE_SEASON_WORD.search(text) or RE_SEASON_ONLY.search(text)
        if season_match:
            parsed.season = int(season_match.group("season"))
            parsed.is_season_pack = True
            stop_index = season_match.start()
        elif date_match:
            try:
                parsed.air_date = date(
                    int(date_match.group("y")), int(date_match.group("m")), int(date_match.group("d"))
                )
                stop_index = date_match.start()
            except ValueError:
                pass

    if RE_COMPLETE.search(text):
        parsed.is_complete_series = True
        parsed.is_season_pack = True

    # --- year ---------------------------------------------------------------
    # A year at position 0 *is* the title (``1923``, ``2012``), never metadata,
    # and a date-based episode's year belongs to the air date, not the show.
    candidates = [
        m
        for m in RE_YEAR.finditer(text)
        if m.start() > 0 and not (date_match and m.start(1) >= date_match.start())
    ]
    year_match = candidates[0] if candidates else None
    if year_match:
        parsed.year = int(year_match.group(1))

    # --- title --------------------------------------------------------------
    if stop_index is None:
        if year_match:
            stop_index = year_match.start()
        else:
            stop = RE_TITLE_STOP.search(text)
            stop_index = stop.start() if stop else len(text)
    elif year_match and 0 < year_match.start() < stop_index:
        stop_index = year_match.start()
    parsed.title = _title_before(text, stop_index)

    # --- quality attributes -------------------------------------------------
    parsed.resolution = _match_first(text, RESOLUTIONS)
    parsed.source = _match_first(text, SOURCES)
    parsed.video_codec = _match_first(text, VIDEO_CODECS)
    parsed.dynamic_range = _dynamic_range(text)
    parsed.audio = _audio(text)
    parsed.edition = _match_first(text, EDITIONS)

    channels = RE_CHANNELS.search(text)
    if channels:
        parsed.audio_channels = f"{channels.group(1)}.{channels.group(2)}"

    parsed.is_full_disc = bool(RE_FULL_DISC.search(text))
    parsed.is_repack = bool(RE_REPACK.search(text))
    parsed.is_proper = bool(RE_PROPER.search(text))
    parsed.is_hybrid = bool(RE_HYBRID.search(text))
    parsed.release_group = _release_group(original)

    if release is not None:
        _apply_hints(parsed, release)
    return parsed


def _apply_hints(parsed: ParsedRelease, release: Release) -> None:
    """Trust the tracker's own classification where the name is ambiguous.

    UNIT3D publishes an authoritative ``type`` (Remux / Full Disc / Encode /
    WEB-DL) and ``resolution`` per torrent. Those beat guessing from the name --
    a "Full Disc" listing whose name never says so would otherwise slip past
    every blocklist.
    """
    hint_type = (release.type_name or "").strip().lower()
    if hint_type:
        if "full disc" in hint_type or hint_type in ("disc", "bd50", "bd25"):
            parsed.is_full_disc = True
            parsed.source = "bluray"
        elif "remux" in hint_type:
            parsed.source = "remux"
        elif hint_type in ("web-dl", "webdl", "web dl"):
            parsed.source = "webdl"
        elif hint_type in ("webrip", "web rip"):
            parsed.source = "webrip"
        elif hint_type == "hdtv":
            parsed.source = "hdtv"
        elif hint_type == "encode" and parsed.source in (None, "web"):
            parsed.source = "bluray"

    hint_res = (release.resolution_name or "").strip().lower()
    if hint_res:
        normalised = {"4k": "2160p", "uhd": "2160p"}.get(hint_res, hint_res)
        if any(normalised == value for value, _ in RESOLUTIONS):
            parsed.resolution = normalised

    if not parsed.year and release.raw.get("release_year"):
        with contextlib.suppress(TypeError, ValueError):
            parsed.year = int(release.raw["release_year"])


def parse_release(release: Release) -> ParsedRelease:
    """Convenience wrapper: parse straight from an indexer result."""
    return parse(release.name, release)
