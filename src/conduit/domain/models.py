"""Core value objects and state enums."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any


class MediaType(StrEnum):
    MOVIE = "movie"
    SHOW = "show"


class WantedState(StrEnum):
    WAITING = "waiting"          # known, but not released yet
    SEARCHING = "searching"      # released, actively polling indexers
    GRABBED = "grabbed"          # a release was sent to the client
    DOWNLOADED = "downloaded"    # present in the library
    UNAVAILABLE = "unavailable"
    IGNORED = "ignored"          # stood down by a policy; revived if it changes
    # "I have already seen this." Deliberately distinct from IGNORED: a policy
    # stand-down is reversible and gets revived when the rules widen, whereas
    # a human saying they watched something must stick. It scopes to the
    # episode only -- the series stays followed and future releases still land.
    WATCHED = "watched"


class DownloadState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    NO_SPACE = "no_space"
    CANCELLED = "cancelled"

    @classmethod
    def active(cls) -> tuple[DownloadState, ...]:
        return (cls.PENDING_APPROVAL, cls.QUEUED, cls.DOWNLOADING, cls.NO_SPACE)

    @classmethod
    def occupies_slot(cls) -> tuple[DownloadState, ...]:
        return (cls.DOWNLOADING,)


class EventLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Indexer results
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Release:
    """A candidate torrent as reported by an indexer, before interpretation."""

    indexer: str
    indexer_id: str
    name: str
    size_bytes: int
    download_url: str
    details_url: str = ""
    seeders: int = 0
    leechers: int = 0
    times_completed: int = 0
    freeleech: bool = False
    internal: bool = False
    tmdb_id: str | None = None
    imdb_id: str | None = None
    category: str = ""
    type_name: str = ""
    resolution_name: str = ""
    uploaded_at: str | None = None
    indexer_score_bonus: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.indexer, self.indexer_id)


@dataclass(slots=True)
class ParsedRelease:
    """Structured attributes extracted from a release name (and API hints)."""

    raw_name: str
    title: str = ""
    year: int | None = None
    season: int | None = None
    season_to: int | None = None
    episodes: list[int] = field(default_factory=list)
    resolution: str | None = None
    source: str | None = None
    dynamic_range: str = "sdr"
    video_codec: str | None = None
    audio: str | None = None
    audio_channels: str | None = None
    release_group: str | None = None
    edition: str | None = None
    language: str | None = None
    is_season_pack: bool = False
    is_complete_series: bool = False
    is_full_disc: bool = False
    is_repack: bool = False
    is_proper: bool = False
    is_hybrid: bool = False
    air_date: date | None = None

    @property
    def episode_from(self) -> int | None:
        return min(self.episodes) if self.episodes else None

    @property
    def episode_to(self) -> int | None:
        return max(self.episodes) if self.episodes else None

    @property
    def is_episode(self) -> bool:
        return bool(self.episodes)

    def covers(self, season: int, episode: int | None) -> bool:
        """Does this release satisfy a specific season/episode want?"""
        if self.season_to is not None and self.season is not None:
            return min(self.season, self.season_to) <= season <= max(self.season, self.season_to)
        if self.is_complete_series:
            return True
        if self.season != season:
            return False
        if episode is None:
            return self.is_season_pack
        if self.is_season_pack:
            return True
        return episode in self.episodes


@dataclass(slots=True)
class Rejection:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


@dataclass(slots=True)
class ScoredRelease:
    """A release plus everything the scorer concluded about it."""

    release: Release
    parsed: ParsedRelease
    score: int = 0
    breakdown: dict[str, int] = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.rejections

    def summary(self) -> dict[str, Any]:
        return {
            "indexer": self.release.indexer,
            "indexer_id": self.release.indexer_id,
            "name": self.release.name,
            "size_bytes": self.release.size_bytes,
            "seeders": self.release.seeders,
            "score": self.score,
            "accepted": self.accepted,
            "resolution": self.parsed.resolution,
            "source": self.parsed.source,
            "dynamic_range": self.parsed.dynamic_range,
            "video_codec": self.parsed.video_codec,
            "audio": self.parsed.audio,
            "release_group": self.parsed.release_group,
            "season": self.parsed.season,
            "episodes": self.parsed.episodes,
            "is_season_pack": self.parsed.is_season_pack,
            "freeleech": self.release.freeleech,
            "internal": self.release.internal,
            "breakdown": self.breakdown,
            "rejections": [str(r) for r in self.rejections],
        }


# ---------------------------------------------------------------------------
# Plex / library
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class LibraryItem:
    """One thing Plex actually has on disk."""

    kind: str  # movie | show | episode
    rating_key: str
    title: str = ""
    tmdb_id: str | None = None
    show_tmdb_id: str | None = None
    season: int | None = None
    episode: int | None = None
    watched: bool = False
    view_count: int = 0
    resolution: str | None = None
    file_path: str | None = None
    size_bytes: float = 0.0
    added_at: str | None = None


@dataclass(slots=True)
class WatchlistEntry:
    """An item on the Plex Discover watchlist."""

    rating_key: str
    guid: str
    title: str
    media_type: str  # movie | show | season | episode
    year: int | None = None
    tmdb_id: str | None = None
    imdb_id: str | None = None
    tvdb_id: str | None = None
    parent_title: str | None = None
    grandparent_title: str | None = None
    season: int | None = None
    episode: int | None = None
    thumb: str | None = None


@dataclass(slots=True)
class TorrentStatus:
    """Live state of one torrent in the download client."""

    info_hash: str
    name: str
    state: str
    progress: float
    eta_seconds: int
    dlspeed: float
    size_bytes: float
    save_path: str
    content_path: str
    tags: list[str] = field(default_factory=list)
    category: str = ""
    completion_on: int = 0
    seeding_time: int = 0   # seconds spent seeding since completion
    ratio: float = 0.0
    uploaded: float = 0.0

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0 or self.state in {
            "uploading",
            "stalledUP",
            "pausedUP",
            "queuedUP",
            "forcedUP",
            "checkingUP",
        }

    @property
    def is_errored(self) -> bool:
        return self.state in {"error", "missingFiles"}
