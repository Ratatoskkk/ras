"""Configuration for Conduit.

Two layers, deliberately separated:

* :class:`Settings` -- secrets and machine-specific paths, read from ``.env``
  and the process environment. Never written back to disk by the app.
* :class:`AppConfig` -- *behaviour*: quality profiles, policy thresholds, task
  intervals, tracker definitions. Read from ``config/conduit.toml``, validated,
  hot-reloadable, and safely writable from the dashboard.

Keeping them apart means the settings UI can never accidentally serialise a
password into a file that gets shared, and a bad profile edit can be rolled
back without touching credentials.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "conduit.toml"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

GIB = 1024**3


# ---------------------------------------------------------------------------
# Layer 1: environment / secrets
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _env_file_values(path: Path = PROJECT_ROOT / ".env") -> dict[str, str]:
    """Parse ``.env`` into a plain dict for lookups pydantic cannot model."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values



class Settings(BaseSettings):
    """Connection details and paths. Sourced from ``.env`` + environment."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    plex_url: str = "http://localhost:32400"
    plex_token: str = ""

    tmdb_api_key: str = ""

    qbittorrent_url: str = "http://localhost:8080"
    qbittorrent_username: str = "admin"
    qbittorrent_password: str = ""

    download_dirs_raw: str = Field(default="", alias="DOWNLOAD_DIRS")

    conduit_host: str = "0.0.0.0"
    conduit_port: int = 5050
    conduit_auth_mode: Literal["lan", "token", "both", "none"] = "lan"
    conduit_api_token: str = ""
    conduit_log_level: str = "INFO"

    database_path: Path = DATA_DIR / "conduit.db"

    @property
    def download_dirs(self) -> list[Path]:
        """Configured download roots, in declaration order.

        Accepts the modern ``DOWNLOAD_DIRS`` comma list and falls back to the
        legacy ``DOWNLOAD_DIR_1``/``DOWNLOAD_DIR_2`` pair so an existing .env
        from the old project keeps working.
        """
        raw = self.download_dirs_raw
        if not raw:
            legacy = [os.getenv(f"DOWNLOAD_DIR_{i}") for i in range(1, 6)]
            raw = ",".join(v for v in legacy if v)
        return [Path(p.strip()) for p in raw.split(",") if p.strip()]

    def tracker_api_key(self, env_var: str) -> str:
        """Look up a tracker key by env var name.

        Tracker names are user-defined, so their keys cannot be declared as
        fields here. pydantic-settings reads ``.env`` without exporting it to
        the process environment, so the file is consulted directly as well --
        otherwise adding a second tracker would only work if you also set a
        real environment variable.
        """
        value = os.getenv(env_var)
        if value:
            return value.strip()
        return _env_file_values().get(env_var, "").strip()

    def missing_required(self) -> list[str]:
        """Names of required settings that are empty. Used for the boot check."""
        missing = []
        if not self.plex_token:
            missing.append("PLEX_TOKEN")
        if not self.tmdb_api_key:
            missing.append("TMDB_API_KEY")
        if not self.qbittorrent_password:
            missing.append("QBITTORRENT_PASSWORD")
        if not self.download_dirs:
            missing.append("DOWNLOAD_DIRS")
        if self.conduit_auth_mode in ("token", "both") and not self.conduit_api_token:
            missing.append("CONDUIT_API_TOKEN")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Layer 2: behaviour / quality
# ---------------------------------------------------------------------------
class ScoredTerm(BaseModel):
    """A matchable attribute value and the score it contributes."""

    value: str
    score: int = 0

    @field_validator("value")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class QualityProfile(BaseModel):
    """Declarative rules for ranking releases.

    A release's score is the sum of its matching attribute scores plus
    tie-breaker bonuses. Anything scoring below ``min_score``, matching a
    blocked term, missing a required term, or falling outside the size window
    is rejected outright -- rejections are recorded with a reason so the UI can
    explain *why* nothing was grabbed.
    """

    name: str
    description: str = ""

    resolutions: list[ScoredTerm] = Field(default_factory=list)
    sources: list[ScoredTerm] = Field(default_factory=list)
    dynamic_range: list[ScoredTerm] = Field(default_factory=list)
    video_codecs: list[ScoredTerm] = Field(default_factory=list)
    audio: list[ScoredTerm] = Field(default_factory=list)
    groups: list[ScoredTerm] = Field(default_factory=list)

    required_terms: list[str] = Field(default_factory=list)
    blocked_terms: list[str] = Field(default_factory=list)

    @field_validator("required_terms", "blocked_terms")
    @classmethod
    def _lower_terms(cls, v: list[str]) -> list[str]:
        """Normalise once, here, so the scorer's inner loop is a plain
        substring test rather than a ``.lower()`` per release per term."""
        return [t.strip().lower() for t in v if t and t.strip()]

    min_score: int = 1
    min_size_gb: float = 0.0
    max_size_gb: float = 0.0  # 0 == unlimited
    max_size_per_episode_gb: float = 0.0  # 0 == unlimited; applies to packs too

    require_internal: bool = False
    allow_unknown_resolution: bool = False

    seeder_floor: int = 1
    seeder_bonus_per_10: int = 0
    freeleech_bonus: int = 0
    tie_break: Literal["size", "seeders", "score_only"] = "size"

    @property
    def max_size_bytes(self) -> float:
        return self.max_size_gb * GIB if self.max_size_gb > 0 else float("inf")

    @property
    def min_size_bytes(self) -> float:
        return self.min_size_gb * GIB

    def lookup(self, bucket: str, key: str | None) -> int | None:
        """Score for ``key`` within an attribute bucket, or ``None`` if absent."""
        if not key:
            return None
        terms: list[ScoredTerm] = getattr(self, bucket, [])
        key = key.lower()
        for term in terms:
            if term.value == key:
                return term.score
        return None


class Policy(BaseModel):
    """Operational guard rails."""

    max_active_downloads: int = 5
    approval_size_threshold_gb: float = 100.0
    require_approval_for_season_packs: bool = True
    require_approval_for_multi_season: bool = True
    auto_approve_below_gb: float = 0.0  # 0 == disabled

    # Nothing reaches the download client without a click. On a private
    # tracker every grab spends real download credit, so this is the switch
    # that makes Conduit a recommendation engine rather than an autopilot.
    require_approval_for_everything: bool = False

    auto_remove_from_watchlist: bool = True
    trigger_plex_refresh: bool = True
    skip_watched_seasons: bool = True

    # People watch series in order. If season 3 has been started, seasons 1-2
    # were almost certainly watched elsewhere -- treat everything up to the
    # furthest point reached as seen, so a newly followed show does not drag
    # in its entire back catalogue.
    assume_prior_seasons_watched: bool = False

    # How much of a series' already-aired backlog to chase.
    #   all             everything missing (a 20-season show means 20 seasons)
    #   current_season  finish the season you are on, then keep up
    #   upcoming_only   only what has not aired yet, plus the fresh window
    backlog_mode: Literal["all", "current_season", "upcoming_only"] = "all"

    # Fetch the next season just before you need it. As you approach the end
    # of the season you are watching, the following one is unlocked -- so a
    # twenty-season show arrives one season at a time instead of all at once.
    # Works with any backlog_mode; `current_season` is the intended pairing.
    sequential_seasons: bool = False
    # How many episodes from the end of a season to trigger on. 1 means "when
    # you start the second-to-last episode", giving one episode of lead time.
    sequential_lead_episodes: int = 1

    # Reserved for the "replace a file when a better release turns up" pass,
    # which is not implemented: `wanted` rows leave the searchable states once
    # they are grabbed, so nothing ever re-scores them. Kept as the hook (see
    # domain.scoring.compare) and deliberately *not* offered in the Settings
    # UI -- a switch that silently does nothing is worse than no switch.
    upgrade_existing: bool = False
    upgrade_margin: int = 150

    # Season packs are cheaper per episode but waste bandwidth when only one
    # episode is missing. Grab a pack once this many episodes of a season are
    # outstanding, otherwise take individual episodes.
    prefer_season_packs: bool = True
    season_pack_min_missing: int = 3
    max_search_attempts: int = 60

    reserve_free_space_gb: float = 20.0
    size_headroom_percent: float = 5.0

    # Private trackers punish early deletion. Nothing is offered for reclaim
    # until it has seeded this long; the dashboard shows the countdown and
    # refuses the delete rather than risking a hit-and-run.
    min_seed_days: float = 5.0
    # Optional escape hatch: a torrent that has returned this ratio counts as
    # satisfied regardless of time. 0 disables it (time-only, the safe default).
    min_seed_ratio: float = 0.0
    allow_delete_before_seed_goal: bool = False

    dry_run: bool = False
    torrent_category: str = "conduit"
    torrent_tag_prefix: str = "conduit"

    @property
    def approval_size_bytes(self) -> float:
        return self.approval_size_threshold_gb * GIB


class Intervals(BaseModel):
    """Task cadence in seconds. Every value drives the task supervisor."""

    # The Discover call is one cheap HTTP request that does nothing unless
    # something is on the list, so polling often costs almost nothing -- and
    # waiting a quarter of an hour for a title you just added feels broken.
    # Adding one also asks the search task to run immediately, so the wait
    # after a watchlist add is this interval, not the search interval.
    watchlist_sync: int = 30
    queue_dispatch: int = 30
    download_monitor: int = 15
    library_index: int = 1800
    calendar_refresh: int = 21600
    release_poll: int = 1800
    fresh_release_poll: int = 600
    watched_scan: int = 3600
    housekeeping: int = 86400


class CalendarConfig(BaseModel):
    """When to start searching and when to slow repeated misses."""

    fresh_window_days: int = 7
    give_up_days_tv: int = 45
    give_up_days_movie: int = 180
    pre_air_lead_hours: int = 0
    track_watched_shows: bool = True
    max_seasons_back: int = 0  # 0 == all seasons


class IndexerConfig(BaseModel):
    """A tracker definition. ``type`` selects the client implementation."""

    name: str
    type: Literal["unit3d"] = "unit3d"
    base_url: str
    api_key_env: str
    enabled: bool = True
    priority: int = 1
    rate_limit_per_minute: int = 30
    timeout_seconds: float = 20.0
    score_bonus: int = 0
    verify_ssl: bool = True
    # UNIT3D can exclude seederless torrents server side. Leaving this on
    # means dead releases never even reach the scorer.
    only_alive: bool = True

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class NotificationConfig(BaseModel):
    """Optional outbound webhooks. Disabled unless a URL is supplied."""

    webhook_url: str = ""
    on_grab: bool = True
    on_complete: bool = True
    on_error: bool = True
    on_approval_required: bool = True


class AppConfig(BaseModel):
    """The whole behavioural configuration, as loaded from TOML."""

    policy: Policy = Field(default_factory=Policy)
    intervals: Intervals = Field(default_factory=Intervals)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    indexers: list[IndexerConfig] = Field(default_factory=list)
    profiles: list[QualityProfile] = Field(default_factory=list)
    default_profile: str = ""
    movie_profile: str = ""
    tv_profile: str = ""

    @model_validator(mode="after")
    def _resolve_profiles(self) -> AppConfig:
        if not self.profiles:
            self.profiles = _builtin_profiles()
        names = {p.name for p in self.profiles}
        if self.default_profile not in names:
            self.default_profile = self.profiles[0].name
        if self.movie_profile not in names:
            self.movie_profile = self.default_profile
        if self.tv_profile not in names:
            self.tv_profile = self.default_profile
        return self

    def profile(self, name: str | None = None) -> QualityProfile:
        target = name or self.default_profile
        for p in self.profiles:
            if p.name == target:
                return p
        return self.profiles[0]

    def profile_for(self, media_type: str) -> QualityProfile:
        return self.profile(self.movie_profile if media_type == "movie" else self.tv_profile)

    def enabled_indexers(self) -> list[IndexerConfig]:
        return sorted(
            (i for i in self.indexers if i.enabled), key=lambda i: (i.priority, i.name)
        )


# ---------------------------------------------------------------------------
# Defaults + persistence
# ---------------------------------------------------------------------------
def _builtin_profiles() -> list[QualityProfile]:
    """Shipped profiles. Mirrors (and then improves on) the reference ranking."""
    common_blocked = [
        "full disc",
        "bd50",
        "bd25",
        "complete bluray",
        "avc remux",
        "3d",
        "hdcam",
        "hdts",
        "telesync",
        "telecine",
        "screener",
        "workprint",
    ]
    return [
        QualityProfile(
            name="uhd-remux",
            description="4K HDR remux first, graceful fallback to 4K web and 1080p remux.",
            resolutions=[
                ScoredTerm(value="2160p", score=1000),
                ScoredTerm(value="1080p", score=400),
                ScoredTerm(value="720p", score=100),
            ],
            sources=[
                ScoredTerm(value="remux", score=500),
                ScoredTerm(value="bluray", score=300),
                ScoredTerm(value="webdl", score=200),
                ScoredTerm(value="webrip", score=120),
                ScoredTerm(value="hdtv", score=40),
            ],
            dynamic_range=[
                ScoredTerm(value="dv_hdr10plus", score=260),
                ScoredTerm(value="dv_hdr10", score=250),
                ScoredTerm(value="dv", score=230),
                ScoredTerm(value="hdr10plus", score=220),
                ScoredTerm(value="hdr10", score=200),
                ScoredTerm(value="hdr", score=180),
                ScoredTerm(value="hlg", score=60),
                ScoredTerm(value="sdr", score=0),
            ],
            video_codecs=[
                ScoredTerm(value="hevc", score=60),
                ScoredTerm(value="av1", score=40),
                ScoredTerm(value="avc", score=20),
            ],
            audio=[
                ScoredTerm(value="truehd_atmos", score=120),
                ScoredTerm(value="dts_x", score=110),
                ScoredTerm(value="truehd", score=90),
                ScoredTerm(value="dts_hd_ma", score=85),
                ScoredTerm(value="eac3_atmos", score=70),
                ScoredTerm(value="dd_plus", score=45),
                ScoredTerm(value="dts", score=40),
                ScoredTerm(value="ac3", score=20),
                ScoredTerm(value="aac", score=10),
            ],
            groups=[
                ScoredTerm(value="framestor", score=40),
                ScoredTerm(value="3l", score=35),
                ScoredTerm(value="bmf", score=35),
                ScoredTerm(value="w4nk3r", score=30),
                ScoredTerm(value="hifi", score=30),
                ScoredTerm(value="flux", score=25),
                ScoredTerm(value="ntb", score=25),
            ],
            blocked_terms=common_blocked,
            min_score=400,
            max_size_per_episode_gb=90.0,
            seeder_floor=1,
            seeder_bonus_per_10=2,
            freeleech_bonus=15,
            tie_break="size",
        ),
        QualityProfile(
            name="uhd-efficient",
            description="4K but bandwidth-conscious: prefers web-dl over huge remuxes.",
            resolutions=[
                ScoredTerm(value="2160p", score=1000),
                ScoredTerm(value="1080p", score=500),
            ],
            sources=[
                ScoredTerm(value="webdl", score=400),
                ScoredTerm(value="bluray", score=300),
                ScoredTerm(value="webrip", score=200),
                ScoredTerm(value="remux", score=100),
            ],
            dynamic_range=[
                ScoredTerm(value="dv_hdr10plus", score=200),
                ScoredTerm(value="dv_hdr10", score=190),
                ScoredTerm(value="dv", score=180),
                ScoredTerm(value="hdr10plus", score=170),
                ScoredTerm(value="hdr10", score=150),
                ScoredTerm(value="hdr", score=140),
                ScoredTerm(value="sdr", score=0),
            ],
            video_codecs=[
                ScoredTerm(value="hevc", score=80),
                ScoredTerm(value="av1", score=70),
                ScoredTerm(value="avc", score=10),
            ],
            audio=[
                ScoredTerm(value="eac3_atmos", score=60),
                ScoredTerm(value="dd_plus", score=45),
                ScoredTerm(value="truehd_atmos", score=40),
                ScoredTerm(value="aac", score=15),
            ],
            blocked_terms=common_blocked,
            min_score=400,
            max_size_gb=40.0,
            max_size_per_episode_gb=12.0,
            seeder_floor=2,
            seeder_bonus_per_10=3,
            freeleech_bonus=20,
            tie_break="seeders",
        ),
        QualityProfile(
            name="hd-balanced",
            description="1080p only. Small, fast, universally playable.",
            resolutions=[
                ScoredTerm(value="1080p", score=1000),
                ScoredTerm(value="720p", score=300),
            ],
            sources=[
                ScoredTerm(value="bluray", score=300),
                ScoredTerm(value="webdl", score=280),
                ScoredTerm(value="remux", score=150),
                ScoredTerm(value="webrip", score=150),
                ScoredTerm(value="hdtv", score=50),
            ],
            dynamic_range=[ScoredTerm(value="sdr", score=10), ScoredTerm(value="hdr10", score=5)],
            video_codecs=[ScoredTerm(value="hevc", score=30), ScoredTerm(value="avc", score=25)],
            audio=[
                ScoredTerm(value="dts_hd_ma", score=40),
                ScoredTerm(value="dd_plus", score=30),
                ScoredTerm(value="ac3", score=20),
                ScoredTerm(value="aac", score=15),
            ],
            blocked_terms=common_blocked,
            min_score=400,
            max_size_gb=25.0,
            max_size_per_episode_gb=6.0,
            seeder_floor=2,
            seeder_bonus_per_10=3,
            tie_break="seeders",
        ),
    ]


def default_config() -> AppConfig:
    return AppConfig(
        indexers=[
            IndexerConfig(
                name="Aither",
                type="unit3d",
                base_url="https://aither.cc",
                api_key_env="AITHER_API_KEY",
                enabled=True,
                priority=1,
                rate_limit_per_minute=30,
            )
        ],
        profiles=_builtin_profiles(),
        default_profile="uhd-remux",
        movie_profile="uhd-remux",
        tv_profile="uhd-remux",
    )


def _to_toml_dict(config: AppConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json", exclude_defaults=False)
    # tomli_w cannot serialise None; scrub it.
    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj

    return scrub(data)


def save_config(config: AppConfig, path: Path = CONFIG_FILE) -> None:
    """Write config atomically so a crash mid-write cannot corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    header = (
        "# Conduit behaviour configuration.\n"
        "# Edited from the dashboard Settings page, or by hand -- changes are\n"
        "# picked up automatically without a restart.\n\n"
    )
    tmp.write_text(header + tomli_w.dumps(_to_toml_dict(config)), encoding="utf-8")
    tmp.replace(path)


def load_config(path: Path = CONFIG_FILE) -> AppConfig:
    """Load and validate config, writing the defaults out on first run."""
    if not path.exists():
        config = default_config()
        save_config(config, path)
        return config
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return AppConfig.model_validate(raw)


class ConfigStore:
    """Holds the live :class:`AppConfig` and reloads it when the file changes.

    The supervisor calls :meth:`refresh` on every tick, so editing the TOML by
    hand takes effect within seconds -- no restart, and no filesystem watcher
    dependency.
    """

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path = path
        self._config = load_config(path)
        self._mtime = self._current_mtime()

    def _current_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    @property
    def current(self) -> AppConfig:
        return self._config

    def refresh(self) -> bool:
        """Reload if the file changed on disk. Returns True when reloaded."""
        mtime = self._current_mtime()
        if mtime == self._mtime:
            return False
        try:
            self._config = load_config(self.path)
            self._mtime = mtime
            return True
        except Exception:  # keep serving the last good config
            self._mtime = mtime
            return False

    def replace(self, config: AppConfig) -> None:
        """Persist a new config (used by the settings API)."""
        save_config(config, self.path)
        self._config = config
        self._mtime = self._current_mtime()
