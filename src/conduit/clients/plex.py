"""Async Plex client -- Discover (watchlist) and the local Media Server.

Deliberately written against Plex's HTTP API instead of using ``plexapi``.
``plexapi`` is synchronous and lazily fetches each object, so indexing a
1,500-episode library costs one request *per show* plus one per season. Here
the whole episode list arrives in a couple of paginated calls with the fields
we do not need excluded, which turns a minutes-long scan into a second or two.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..domain.models import LibraryItem, WatchlistEntry
from ..logs import get_logger
from ..util.resilience import TransientError
from .http import HttpService

log = get_logger("plex")

DISCOVER_BASE = "https://discover.provider.plex.tv"
METADATA_BASE = "https://metadata.provider.plex.tv"

# Plex library "type" ids.
TYPE_MOVIE = 1
TYPE_SHOW = 2
TYPE_SEASON = 3
TYPE_EPISODE = 4

PAGE_SIZE = 500
# Plex Discover is stricter than a local server and 400s above 100.
WATCHLIST_PAGE_SIZE = 100

# Payload we never read. Excluding it roughly halves the transfer.
EXCLUDE_ELEMENTS = "Genre,Country,Role,Director,Writer,Producer,Similar,Collection,Chapter,Marker,Review,Rating,Image,UltraBlurColors,Extras,Related"
EXCLUDE_FIELDS = "summary,tagline,art,theme,titleSort,originalTitle,studio,contentRating"


def _client_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Token": token,
        # ASCII on purpose: HTTP headers are latin-1, and the client identifier
        # is what Plex keys the device on -- renaming it would register a new
        # device against the account.
        "X-Plex-Product": "ras",
        "X-Plex-Version": "1.0",
        "X-Plex-Client-Identifier": "conduit-0000-0000-0001",
        "X-Plex-Platform": "Windows",
        "X-Plex-Device": "Windows",
        "X-Plex-Device-Name": "ras",
    }


def _guid_map(item: dict[str, Any]) -> dict[str, str]:
    """``[{"id": "tmdb://157744"}, ...]`` -> ``{"tmdb": "157744", ...}``."""
    out: dict[str, str] = {}
    for entry in item.get("Guid") or []:
        raw = entry.get("id", "") if isinstance(entry, dict) else str(entry)
        if "://" in raw:
            scheme, _, value = raw.partition("://")
            out[scheme] = value
    guid = item.get("guid", "")
    if isinstance(guid, str) and "://" in guid and not guid.startswith("plex://"):
        scheme, _, value = guid.partition("://")
        out.setdefault(scheme, value.split("?")[0])
    return out


def _container(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        container = payload.get("MediaContainer")
        if isinstance(container, dict):
            return container
    return {}


def _metadata(payload: Any) -> list[dict[str, Any]]:
    items = _container(payload).get("Metadata")
    return items if isinstance(items, list) else []


def _first_part(item: dict[str, Any]) -> dict[str, Any]:
    for media in item.get("Media") or []:
        for part in media.get("Part") or []:
            return {
                "file": part.get("file"),
                "size": part.get("size") or 0,
                "resolution": media.get("videoResolution"),
            }
    return {}


class PlexClient:
    """Talks to both Plex clouds and the local server."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.token = token
        headers = _client_headers(token)
        self.server = HttpService("plex-server", base_url, headers=headers, timeout=timeout)
        self.discover = HttpService(
            "plex-discover", DISCOVER_BASE, headers=headers, timeout=timeout
        )
        self.metadata = HttpService(
            "plex-metadata", METADATA_BASE, headers=headers, timeout=timeout
        )

    async def aclose(self) -> None:
        await asyncio.gather(
            self.server.aclose(), self.discover.aclose(), self.metadata.aclose()
        )

    def health(self) -> list[dict[str, Any]]:
        return [self.server.health(), self.discover.health(), self.metadata.health()]

    # -- Discover / watchlist ----------------------------------------------
    async def watchlist(self) -> list[WatchlistEntry]:
        """Every item on the account watchlist, with external ids resolved.

        Plex returns only its own opaque guid in the list response, so ids are
        filled in from the per-item metadata endpoint -- fetched concurrently
        rather than one at a time.
        """
        raw_items: list[dict[str, Any]] = []
        start = 0
        while True:
            payload = await self.discover.get_json(
                "/library/sections/watchlist/all",
                params={
                    "includeCollections": 0,
                    "includeExternalMedia": 1,
                    "X-Plex-Container-Start": start,
                    # Discover rejects anything above 100 with a 400.
                    "X-Plex-Container-Size": WATCHLIST_PAGE_SIZE,
                },
            )
            container = _container(payload)
            batch = container.get("Metadata") or []
            raw_items.extend(batch)
            start += len(batch)
            total = container.get("totalSize")
            if total is not None and start >= int(total):
                break
            if not batch:
                if total is not None:
                    raise TransientError("Plex returned an incomplete watchlist page")
                break
            if total is None and len(batch) < WATCHLIST_PAGE_SIZE:
                break

        if not raw_items:
            return []

        entries = [self._watchlist_entry(item) for item in raw_items]
        needs_ids = [e for e in entries if not e.tmdb_id]
        if needs_ids:
            results = await asyncio.gather(
                *(self._resolve_ids(e.rating_key) for e in needs_ids),
                return_exceptions=True,
            )
            for entry, ids in zip(needs_ids, results, strict=True):
                if isinstance(ids, dict):
                    entry.tmdb_id = ids.get("tmdb") or entry.tmdb_id
                    entry.imdb_id = ids.get("imdb") or entry.imdb_id
                    entry.tvdb_id = ids.get("tvdb") or entry.tvdb_id
        return entries

    def _watchlist_entry(self, item: dict[str, Any]) -> WatchlistEntry:
        ids = _guid_map(item)
        return WatchlistEntry(
            rating_key=str(item.get("ratingKey", "")),
            guid=str(item.get("guid", "")),
            title=item.get("title", "Unknown"),
            media_type=item.get("type", "movie"),
            year=item.get("year"),
            tmdb_id=ids.get("tmdb"),
            imdb_id=ids.get("imdb"),
            tvdb_id=ids.get("tvdb"),
            parent_title=item.get("parentTitle"),
            grandparent_title=item.get("grandparentTitle"),
            season=item.get("parentIndex") if item.get("type") == "episode" else item.get("index"),
            episode=item.get("index") if item.get("type") == "episode" else None,
            thumb=item.get("thumb"),
        )

    async def _resolve_ids(self, rating_key: str) -> dict[str, str]:
        for service in (self.discover, self.metadata):
            try:
                payload = await service.get_json(
                    f"/library/metadata/{rating_key}", allow_404=True
                )
            except Exception:
                continue
            items = _metadata(payload)
            if items:
                ids = _guid_map(items[0])
                if ids:
                    return ids
        return {}

    async def remove_from_watchlist(self, rating_key: str) -> bool:
        """Remove an item once it has been dealt with."""
        try:
            await self.discover.request(
                "PUT",
                "/actions/removeFromWatchlist",
                params={"ratingKey": rating_key},
                allow_404=True,
            )
            return True
        except Exception as exc:
            log.warning(
                "could not remove from watchlist",
                extra={"rating_key": rating_key, "err": str(exc)},
            )
            return False

    # -- Local server -------------------------------------------------------
    async def sections(self) -> list[dict[str, Any]]:
        payload = await self.server.get_json("/library/sections")
        directories = _container(payload).get("Directory")
        return directories if isinstance(directories, list) else []

    async def _paged(self, section_key: str, item_type: int) -> list[dict[str, Any]]:
        """Fetch every item of a type in a section, one page at a time."""
        collected: list[dict[str, Any]] = []
        start = 0
        while True:
            payload = await self.server.get_json(
                f"/library/sections/{section_key}/all",
                params={
                    "type": item_type,
                    "includeGuids": 1,
                    "excludeElements": EXCLUDE_ELEMENTS,
                    "excludeFields": EXCLUDE_FIELDS,
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": PAGE_SIZE,
                },
            )
            container = _container(payload)
            batch = container.get("Metadata") or []
            collected.extend(batch)
            start += len(batch)
            total = container.get("totalSize")
            if total is not None and start >= int(total):
                break
            if not batch:
                if total is not None:
                    raise TransientError(f"Plex returned an incomplete page for section {section_key}")
                break
            if total is None and len(batch) < PAGE_SIZE:
                break
        return collected

    async def index_library(self) -> list[LibraryItem]:
        """Build a complete snapshot of what Plex holds.

        Movies come straight from the movie sections. For shows the trick is
        to pull shows and episodes separately: shows carry the TMDB guid,
        episodes carry watched state, and ``grandparentRatingKey`` joins them.
        """
        sections = await self.sections()
        items: list[LibraryItem] = []

        movie_keys = [s["key"] for s in sections if s.get("type") == "movie"]
        show_keys = [s["key"] for s in sections if s.get("type") == "show"]

        movie_pages = await asyncio.gather(
            *(self._paged(key, TYPE_MOVIE) for key in movie_keys)
        )
        for page in movie_pages:
            for raw in page:
                part = _first_part(raw)
                ids = _guid_map(raw)
                items.append(
                    LibraryItem(
                        kind="movie",
                        rating_key=str(raw.get("ratingKey", "")),
                        title=raw.get("title", ""),
                        tmdb_id=ids.get("tmdb"),
                        watched=int(raw.get("viewCount") or 0) > 0,
                        view_count=int(raw.get("viewCount") or 0),
                        resolution=part.get("resolution"),
                        file_path=part.get("file"),
                        size_bytes=float(part.get("size") or 0),
                        added_at=str(raw.get("addedAt") or ""),
                    )
                )

        for key in show_keys:
            shows, episodes = await asyncio.gather(
                self._paged(key, TYPE_SHOW), self._paged(key, TYPE_EPISODE)
            )

            show_tmdb: dict[str, str | None] = {}
            for raw in shows:
                rating_key = str(raw.get("ratingKey", ""))
                ids = _guid_map(raw)
                show_tmdb[rating_key] = ids.get("tmdb")
                items.append(
                    LibraryItem(
                        kind="show",
                        rating_key=rating_key,
                        title=raw.get("title", ""),
                        tmdb_id=ids.get("tmdb"),
                        show_tmdb_id=ids.get("tmdb"),
                        watched=int(raw.get("viewedLeafCount") or 0)
                        >= int(raw.get("leafCount") or 0) > 0,
                        view_count=int(raw.get("viewedLeafCount") or 0),
                        added_at=str(raw.get("addedAt") or ""),
                    )
                )

            for raw in episodes:
                part = _first_part(raw)
                parent = str(raw.get("grandparentRatingKey", ""))
                items.append(
                    LibraryItem(
                        kind="episode",
                        rating_key=str(raw.get("ratingKey", "")),
                        title=raw.get("grandparentTitle", raw.get("title", "")),
                        show_tmdb_id=show_tmdb.get(parent),
                        season=raw.get("parentIndex"),
                        episode=raw.get("index"),
                        watched=int(raw.get("viewCount") or 0) > 0,
                        view_count=int(raw.get("viewCount") or 0),
                        resolution=part.get("resolution"),
                        file_path=part.get("file"),
                        size_bytes=float(part.get("size") or 0),
                        added_at=str(raw.get("addedAt") or ""),
                    )
                )

        log.info(
            "library indexed",
            extra={
                "movies": sum(1 for i in items if i.kind == "movie"),
                "shows": sum(1 for i in items if i.kind == "show"),
                "episodes": sum(1 for i in items if i.kind == "episode"),
            },
        )
        return items

    async def refresh_sections(self, media_type: str) -> list[str]:
        """Ask Plex to scan the sections that could contain a new file."""
        wanted = {"show"} if media_type in ("show", "episode", "season") else {"movie"}
        refreshed: list[str] = []
        for section in await self.sections():
            if section.get("type") in wanted:
                try:
                    await self.server.request(
                        "GET", f"/library/sections/{section['key']}/refresh", allow_404=True
                    )
                    refreshed.append(section.get("title", section["key"]))
                except Exception as exc:
                    log.warning(
                        "section refresh failed",
                        extra={"section": section.get("title"), "err": str(exc)},
                    )
        return refreshed
