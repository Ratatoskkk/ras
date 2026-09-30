"""Indexer abstraction.

Trackers are pluggable rather than hard-coded. Aither happens to run UNIT3D,
and so do dozens of other private trackers -- pointing Conduit at a second one
is a four-line entry in ``conduit.toml``, not a code change.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ...domain.models import Release
from ...logs import get_logger
from ...util.resilience import TransientError

log = get_logger("indexer")


@dataclass(slots=True, frozen=True)
class SearchQuery:
    """What we are looking for. Indexers translate this to their own params."""

    media_type: str  # movie | show
    tmdb_id: str | None = None
    imdb_id: str | None = None
    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def cache_key(self) -> str:
        return (
            f"{self.media_type}:{self.tmdb_id or ''}:{self.imdb_id or ''}:"
            f"{self.title.lower()}:{self.year or ''}:{self.season if self.season is not None else ''}:"
            f"{self.episode if self.episode is not None else ''}"
        )

    def describe(self) -> str:
        from ...util.text import episode_code

        code = episode_code(self.season, self.episode)
        label = self.title or f"tmdb:{self.tmdb_id}"
        return f"{label} {code}".strip()


@runtime_checkable
class Indexer(Protocol):
    name: str
    priority: int
    score_bonus: int

    async def search(self, query: SearchQuery) -> list[Release]: ...
    async def fetch_torrent(self, release: Release) -> bytes | None: ...
    async def account(self) -> dict[str, Any] | None: ...
    async def aclose(self) -> None: ...
    def health(self) -> dict[str, Any]: ...


class IndexerPool:
    """Fans a query out across every enabled tracker, concurrently.

    One tracker being down, rate-limited or slow must not stop the others --
    failures are logged and the surviving results are returned.
    """

    def __init__(self, indexers: list[Indexer]) -> None:
        self.indexers = sorted(indexers, key=lambda i: i.priority)

    def __len__(self) -> int:
        return len(self.indexers)

    def get(self, name: str) -> Indexer | None:
        return next((i for i in self.indexers if i.name == name), None)

    async def search(self, query: SearchQuery) -> list[Release]:
        if not self.indexers:
            return []
        results = await asyncio.gather(
            *(indexer.search(query) for indexer in self.indexers), return_exceptions=True
        )
        releases: list[Release] = []
        succeeded = 0
        for indexer, result in zip(self.indexers, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                log.warning(
                    "indexer search failed",
                    extra={"indexer": indexer.name, "query": query.describe(),
                           "err": str(result)},
                )
                continue
            succeeded += 1
            releases.extend(result)
        if not succeeded:
            raise TransientError(f"Every tracker failed while searching for {query.describe()}")
        return releases

    async def fetch_torrent(self, release: Release) -> bytes | None:
        indexer = self.get(release.indexer)
        if indexer is None:
            return None
        return await indexer.fetch_torrent(release)

    async def accounts(self) -> list[dict[str, Any]]:
        """Account standing on every tracker. Failures are simply omitted."""
        if not self.indexers:
            return []
        results = await asyncio.gather(
            *(i.account() for i in self.indexers), return_exceptions=True
        )
        return [r for r in results if isinstance(r, dict)]

    async def aclose(self) -> None:
        await asyncio.gather(
            *(i.aclose() for i in self.indexers), return_exceptions=True
        )

    def health(self) -> list[dict[str, Any]]:
        return [i.health() for i in self.indexers]
