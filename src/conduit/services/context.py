"""The shared application context.

One object owns every connection, repository and piece of configuration, and
is handed to every service, task and API route. That is what makes the whole
app testable: swap the clients on a context and the rest of the code neither
knows nor cares.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..clients import PlexClient, QBittorrentClient, TmdbClient
from ..clients.indexers import IndexerPool, build_indexer
from ..config import AppConfig, ConfigStore, Settings, get_settings
from ..db.database import Database
from ..db.repo import Repos
from ..domain.models import EventLevel
from ..logs import get_logger
from .bus import EventBus

log = get_logger("context")


class Conduit:
    """Everything the application needs, assembled once at startup."""

    def __init__(
        self,
        settings: Settings | None = None,
        config_store: ConfigStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.config_store = config_store or ConfigStore()
        self.db = Database(self.settings.database_path)
        self.repos: Repos = None  # type: ignore[assignment]
        self.bus = EventBus()
        self.download_decision_lock = asyncio.Lock()
        self.queue_dispatch_lock = asyncio.Lock()
        self.started_at = datetime.now(UTC)

        self.plex: PlexClient | None = None
        self.tmdb: TmdbClient | None = None
        self.qbt: QBittorrentClient | None = None
        self.indexers = IndexerPool([])

        # Set by the web app once the supervisor exists. Lets a service ask for
        # another task to run now instead of waiting out its interval, without
        # services having to know the supervisor exists.
        self.request_run: Callable[[str], bool] = lambda _name: False

        self._indexer_signature: tuple = ()
        # asyncio keeps only a weak reference to a running task, so a
        # fire-and-forget close can be collected before it finishes.
        self._closing: set[asyncio.Task] = set()

    # -- config -------------------------------------------------------------
    @property
    def config(self) -> AppConfig:
        return self.config_store.current

    def refresh_config(self) -> bool:
        """Reload conduit.toml if it changed, rebuilding trackers as needed."""
        if not self.config_store.refresh():
            return False
        log.info("configuration reloaded")
        self.rebuild_indexers()
        self.bus.publish("config.reloaded")
        return True

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        await self.db.connect()
        self.repos = Repos(self.db)

        self.plex = PlexClient(self.settings.plex_url, self.settings.plex_token)
        self.tmdb = TmdbClient(self.settings.tmdb_api_key, cache=self.repos.cache)
        self.qbt = QBittorrentClient(
            self.settings.qbittorrent_url,
            self.settings.qbittorrent_username,
            self.settings.qbittorrent_password,
        )
        self.rebuild_indexers()

        await self.db.set_meta("started_at", self.started_at.isoformat())
        log.info(
            "context ready",
            extra={
                "database": str(self.settings.database_path),
                "indexers": len(self.indexers),
                "profiles": len(self.config.profiles),
            },
        )

    async def stop(self) -> None:
        closers = [c.aclose() for c in (self.plex, self.tmdb, self.qbt) if c is not None]
        closers.append(self.indexers.aclose())
        await asyncio.gather(*closers, return_exceptions=True)
        await self.db.close()
        log.info("context closed")

    def rebuild_indexers(self) -> None:
        """(Re)create tracker clients from config, only when they changed."""
        enabled = self.config.enabled_indexers()
        signature = tuple(
            (i.name, i.type, i.base_url, i.api_key_env, i.rate_limit_per_minute, i.priority,
             i.score_bonus, i.verify_ssl, i.timeout_seconds, i.only_alive)
            for i in enabled
        )
        if signature == self._indexer_signature and self.indexers.indexers:
            return

        old = self.indexers
        built = []
        for spec in enabled:
            api_key = self.settings.tracker_api_key(spec.api_key_env)
            if not api_key:
                log.warning(
                    "indexer disabled: no API key",
                    extra={"indexer": spec.name, "env": spec.api_key_env},
                )
                continue
            try:
                built.append(build_indexer(spec, api_key, cache=self.repos.cache if self.repos else None))
            except Exception as exc:
                log.error(
                    "could not build indexer",
                    extra={"indexer": spec.name, "err": str(exc)},
                )
        self.indexers = IndexerPool(built)
        self._indexer_signature = signature
        if old.indexers:
            task = asyncio.create_task(_close_quietly(old))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        log.info("indexers ready", extra={"count": len(built),
                                          "names": ", ".join(i.name for i in built)})

    # -- shared helpers -----------------------------------------------------
    async def record(
        self,
        category: str,
        message: str,
        *,
        level: str = EventLevel.INFO,
        media_id: int | None = None,
        download_id: int | None = None,
        data: dict[str, Any] | None = None,
        publish: bool = True,
    ) -> None:
        """Write one line to the activity timeline and tell every dashboard."""
        await self.repos.events.add(
            category, message, level=level, media_id=media_id,
            download_id=download_id, data=data,
        )
        if publish:
            self.bus.publish(
                "event",
                level=str(level),
                category=category,
                message=message,
                media_id=media_id,
                download_id=download_id,
                data=data or {},
            )

    async def health(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "plex": self.plex.health() if self.plex else [],
            "tmdb": self.tmdb.health() if self.tmdb else {},
            "qbittorrent": self.qbt.health() if self.qbt else {},
            "indexers": self.indexers.health(),
            "database": await self.db.stats(),
            "uptime_seconds": (datetime.now(UTC) - self.started_at).total_seconds(),
        }
        return checks


async def _close_quietly(pool: IndexerPool) -> None:
    with contextlib.suppress(Exception):
        await pool.aclose()
