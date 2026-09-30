"""UNIT3D tracker client (Aither, BLU, ATH, and every other UNIT3D site).

Design notes worth knowing:

* Searches are filtered **server side** by TMDB id, category and season. The
  reference project pulled 100 unfiltered rows and sifted them with regexes;
  asking the tracker for ``seasonNumber=3`` returns a handful instead.
* The ``files`` array on a full-disc listing can hold thousands of entries. It
  is dropped the moment a row is parsed, so neither memory nor the cache ever
  carries it.
* Results are cached with a TTL, keyed by the query -- a restart or a second
  task asking the same question costs nothing.
"""

from __future__ import annotations

from typing import Any

from ...config import IndexerConfig
from ...domain.models import Release
from ...logs import get_logger
from ...util.resilience import PermanentError, RetryPolicy, TransientError
from ..http import HttpService
from ..tmdb import CacheProtocol
from .base import SearchQuery

log = get_logger("unit3d")

CATEGORY_MOVIE = 1
CATEGORY_TV = 2

SEARCH_TTL = 600  # seconds
EMPTY_TTL = 180   # cache "nothing found" briefly so polling stays cheap
PER_PAGE = 100
MAX_SEARCH_PAGES = 100
ACCOUNT_TIMEOUT = 5.0  # a dashboard panel must never make anyone wait
TORRENT_FETCH_TIMEOUT = 90.0  # nothing waits on this, and losing it is expensive

# Fields we never use, dropped before anything is cached or held in memory.
_DROP_ATTRS = ("files", "description", "bd_info", "media_info")


class Unit3dIndexer:
    def __init__(
        self,
        config: IndexerConfig,
        api_key: str,
        *,
        cache: CacheProtocol | None = None,
    ) -> None:
        self.config = config
        self.name = config.name
        self.priority = config.priority
        self.score_bonus = config.score_bonus
        self.api_key = api_key
        self.cache = cache
        self.http = HttpService(
            f"indexer:{config.name}",
            config.base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=config.timeout_seconds,
            rate_per_minute=config.rate_limit_per_minute,
            verify=config.verify_ssl,
            # Two attempts, not three: with a 5 s connect timeout that bounds a
            # dead tracker at roughly 11 s instead of 33 s.
            retry_policy=RetryPolicy(attempts=2, base_delay=1.0, max_delay=8.0),
            breaker_threshold=3,
            breaker_recovery=120.0,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def health(self) -> dict[str, Any]:
        return {**self.http.health(), "indexer": self.name, "enabled": self.config.enabled}

    # -- search -------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Release]:
        if not self.api_key:
            raise PermanentError(
                f"{self.name}: no API key. Set {self.config.api_key_env} in .env."
            )
        if not (query.tmdb_id or query.imdb_id or query.title):
            return []

        cache_key = f"idx:{self.name}:{query.cache_key}"
        if self.cache is not None:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                return [_release_from_cache(self.name, row, self.score_bonus) for row in cached]

        rows = await self._fetch(query)

        # A season-scoped query can come back empty simply because the tracker
        # tags packs differently. Retry once without the season filter so the
        # local parser gets a chance to find the pack itself.
        if not rows and query.season is not None:
            rows = await self._fetch(
                SearchQuery(
                    media_type=query.media_type,
                    tmdb_id=query.tmdb_id,
                    imdb_id=query.imdb_id,
                    title=query.title,
                    year=query.year,
                )
            )

        if self.cache is not None:
            await self.cache.set(cache_key, rows, SEARCH_TTL if rows else EMPTY_TTL)
        return [_release_from_cache(self.name, row, self.score_bonus) for row in rows]

    async def _fetch(self, query: SearchQuery) -> list[dict[str, Any]]:
        # Parameter names are UNIT3D's, verbatim. `perPage` is capped at 100
        # server side and `sortField` is validated against a fixed list -- an
        # unknown value returns 422, not a silent fallback.
        params: dict[str, Any] = {
            "perPage": PER_PAGE,
            "sortField": "seeders",
            "sortDirection": "desc",
        }
        if query.tmdb_id:
            params["tmdbId"] = str(query.tmdb_id)
        elif query.imdb_id:
            # UNIT3D stores IMDb ids as integers, so the "tt" prefix must go.
            params["imdbId"] = str(query.imdb_id).removeprefix("tt")
        else:
            params["name"] = query.title
            # Only meaningful for a name search; with an id the year is noise.
            if query.year:
                params["startYear"] = query.year
                params["endYear"] = query.year

        params["categories[]"] = (
            CATEGORY_MOVIE if query.media_type == "movie" else CATEGORY_TV
        )
        if query.season is not None:
            params["seasonNumber"] = query.season
        if query.episode is not None:
            params["episodeNumber"] = query.episode
        if self.config.only_alive:
            # Excludes seederless torrents at the source: fewer rows over the
            # wire, and nothing unplayable ever reaches the scorer.
            params["alive"] = 1

        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for page in range(1, MAX_SEARCH_PAGES + 1):
            params["page"] = page
            payload = await self.http.get_json("/api/torrents/filter", params=params)
            page_rows = [_slim(item) for item in _rows(payload)]
            meta = payload.get("meta")
            if isinstance(meta, dict) and meta.get("current_page") not in (None, page, str(page)):
                raise TransientError("UNIT3D returned a mismatched search page")
            new_rows = [row for row in page_rows if row["id"] not in seen_ids]
            if page_rows and not new_rows:
                raise TransientError("UNIT3D repeated a search page")
            rows.extend(new_rows)
            seen_ids.update(row["id"] for row in new_rows)
            links = payload.get("links")
            if not isinstance(links, dict) or not links.get("next"):
                return rows
        raise TransientError("UNIT3D search exceeded the page limit")

    # -- account ------------------------------------------------------------
    async def account(self) -> dict[str, Any] | None:
        """Ratio, buffer and hit-and-run count for this tracker.

        On a private tracker these numbers decide whether a grab is affordable,
        so they belong on the dashboard rather than in a browser tab.

        One attempt only, and a short timeout: this is a cosmetic panel that a
        human is waiting on, so it must fail fast rather than retry thoroughly.
        """
        response = await self.http.request(
            "GET",
            "/api/user",
            allow_404=True,
            policy=RetryPolicy(attempts=1),
            timeout=ACCOUNT_TIMEOUT,
        )
        if response is None:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", payload)
        if isinstance(data, dict):
            data = data.get("attributes", data)
        if not isinstance(data, dict):
            return None
        return {
            "indexer": self.name,
            "username": data.get("username"),
            "group": data.get("group"),
            "uploaded": data.get("uploaded"),
            "downloaded": data.get("downloaded"),
            "ratio": data.get("ratio"),
            "buffer": data.get("buffer"),
            "seeding": data.get("seeding"),
            "leeching": data.get("leeching"),
            "seedbonus": data.get("seedbonus"),
            "hit_and_runs": data.get("hit_and_runs"),
        }

    # -- download -----------------------------------------------------------
    async def fetch_torrent(self, release: Release) -> bytes | None:
        """Download the .torrent so its info-hash can be computed locally.

        Given its own generous timeout rather than the search timeout. A
        season pack's metadata carries a piece hash for every piece, so the
        file is large and the tracker has to build it -- 20 seconds is a
        realistic read on a busy evening, and timing out here costs the
        info-hash precision the whole download path depends on.
        """
        url = release.download_url
        if not url:
            return None
        content = await self.http.get_bytes(
            url, allow_404=True, timeout=TORRENT_FETCH_TIMEOUT
        )
        if not content:
            return None
        if not content.startswith(b"d"):
            # Trackers return an HTML error page when a download slot is
            # exhausted or the rsskey is stale -- do not hand that to the client.
            log.warning(
                "torrent download did not return bencode",
                extra={"indexer": self.name, "release": release.name[:80]},
            )
            return None
        return content


# ---------------------------------------------------------------------------
def _rows(payload: Any) -> list[dict[str, Any]]:
    """UNIT3D has shipped both ``data: [...]`` and ``data: {data: [...]}``."""
    if not isinstance(payload, dict):
        raise TransientError("UNIT3D returned an unexpected search payload")
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise TransientError("UNIT3D returned an unexpected search payload")
    return data


def _slim(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes")
    if not isinstance(attributes, dict) or not item.get("id"):
        raise TransientError("UNIT3D returned an unexpected search payload")
    attrs = {k: v for k, v in attributes.items() if k not in _DROP_ATTRS}
    attrs["id"] = str(item["id"])
    return attrs


def _freeleech(value: Any) -> bool:
    """UNIT3D reports freeleech as ``"0%"`` / ``"100%"`` or a bare number."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value > 0
    if isinstance(value, str):
        stripped = value.strip().rstrip("%")
        try:
            return float(stripped) > 0
        except ValueError:
            return stripped.lower() in ("yes", "true")
    return False


def _release_from_cache(indexer: str, attrs: dict[str, Any], bonus: int) -> Release:
    meta = attrs.get("meta") or {}
    return Release(
        indexer=indexer,
        indexer_id=str(attrs.get("id", "")),
        name=str(attrs.get("name", "")),
        size_bytes=int(attrs.get("size") or 0),
        download_url=str(attrs.get("download_link") or ""),
        details_url=str(attrs.get("details_link") or ""),
        seeders=int(attrs.get("seeders") or 0),
        leechers=int(attrs.get("leechers") or 0),
        times_completed=int(attrs.get("times_completed") or 0),
        freeleech=_freeleech(attrs.get("freeleech")),
        internal=bool(attrs.get("internal")),
        tmdb_id=str(attrs["tmdb_id"]) if attrs.get("tmdb_id") else None,
        imdb_id=str(attrs["imdb_id"]) if attrs.get("imdb_id") else None,
        category=str(attrs.get("category") or ""),
        type_name=str(attrs.get("type") or ""),
        resolution_name=str(attrs.get("resolution") or ""),
        uploaded_at=attrs.get("created_at"),
        indexer_score_bonus=bonus,
        raw={"poster": meta.get("poster"), "uploader": attrs.get("uploader"),
             "num_file": attrs.get("num_file")},
    )
