"""TMDB metadata client.

Two things make this materially cheaper than the reference implementation:

* Season data is requested with ``append_to_response=season/1,season/2,...``,
  so a whole series arrives in one HTTP call instead of one per season.
* Responses go through a persistent, TTL'd cache, so a restart does not throw
  away everything the app already knows.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Protocol

from ..logs import get_logger
from .http import HttpService

log = get_logger("tmdb")

TMDB_BASE = "https://api.themoviedb.org/3"

# TMDB release types. Digital and physical are what actually matter for us --
# a theatrical date tells you nothing about when a rip will exist.
RELEASE_PREMIERE = 1
RELEASE_LIMITED = 2
RELEASE_THEATRICAL = 3
RELEASE_DIGITAL = 4
RELEASE_PHYSICAL = 5

# How long each kind of answer stays fresh.
TTL_ENDED = 30 * 86400
TTL_RETURNING = 6 * 3600
TTL_MOVIE = 12 * 3600
TTL_SEASON = 6 * 3600


class CacheProtocol(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...


class TmdbClient:
    def __init__(
        self, api_key: str, *, cache: CacheProtocol | None = None, timeout: float = 15.0
    ) -> None:
        self.api_key = api_key.strip()
        self.cache = cache
        self.http = HttpService(
            "tmdb",
            TMDB_BASE,
            headers={"Accept": "application/json"},
            timeout=timeout,
            rate_per_minute=600,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def health(self) -> dict[str, Any]:
        return self.http.health()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # -- plumbing -----------------------------------------------------------
    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        ttl: int | Callable[[dict[str, Any]], int] = TTL_MOVIE,
    ) -> dict[str, Any] | None:
        if not self.api_key:
            return None
        query = {"api_key": self.api_key, **(params or {})}
        cache_key = f"tmdb:{path}:{sorted((k, v) for k, v in query.items() if k != 'api_key')}"
        if self.cache is not None:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                return cached
        payload = await self.http.get_json(path, params=query, allow_404=True)
        if payload is not None and self.cache is not None:
            await self.cache.set(cache_key, payload, ttl(payload) if callable(ttl) else ttl)
        return payload

    # -- movies -------------------------------------------------------------
    async def movie(self, tmdb_id: str | int) -> dict[str, Any] | None:
        return await self._get(
            f"/movie/{tmdb_id}", {"append_to_response": "release_dates"}, ttl=TTL_MOVIE
        )

    async def movie_release_date(self, tmdb_id: str | int) -> tuple[date | None, str]:
        """Earliest date a rip could plausibly exist, plus which window it came from.

        Digital and physical dates win. If a film only has theatrical dates we
        fall back to the earliest of those -- but the caller is told, so it can
        keep polling rather than assume the release has landed.
        """
        data = await self.movie(tmdb_id)
        if not data:
            return None, "unknown"

        results = (data.get("release_dates") or {}).get("results") or []
        buckets: dict[int, list[date]] = {}
        for country in results:
            for entry in country.get("release_dates") or []:
                parsed = _parse_date(entry.get("release_date"))
                if parsed:
                    buckets.setdefault(int(entry.get("type") or 0), []).append(parsed)

        for kinds, label in (
            ((RELEASE_DIGITAL, RELEASE_PHYSICAL), "home"),
            ((RELEASE_THEATRICAL, RELEASE_LIMITED, RELEASE_PREMIERE), "theatrical"),
        ):
            dates = [d for k in kinds for d in buckets.get(k, [])]
            if dates:
                return min(dates), label

        fallback = _parse_date(data.get("release_date"))
        return fallback, "primary" if fallback else "unknown"

    # -- tv -----------------------------------------------------------------
    async def show(self, tmdb_id: str | int) -> dict[str, Any] | None:
        # A finished series will never gain another episode, so it is cached
        # for far longer than a returning one. Decided when the answer is
        # stored, rather than by re-writing the entry on every cache hit.
        return await self._get(
            f"/tv/{tmdb_id}",
            ttl=lambda data: TTL_ENDED if _has_ended(data) else TTL_RETURNING,
        )

    async def show_with_seasons(
        self, tmdb_id: str | int, season_numbers: list[int] | None = None
    ) -> dict[str, Any] | None:
        """Series details with every requested season inlined.

        ``append_to_response`` takes up to 20 sub-requests, so a 40-season show
        costs two calls instead of forty.
        """
        base = await self.show(tmdb_id)
        if not base:
            return None

        if season_numbers is None:
            season_numbers = [
                int(s.get("season_number", 0))
                for s in base.get("seasons") or []
                if int(s.get("season_number", -1)) > 0
            ]
        if not season_numbers:
            return base

        merged = dict(base)
        # Episode lists are the bulk of the payload, and an ended series'
        # never change -- no reason to refetch them every six hours.
        ttl = TTL_ENDED if _has_ended(base) else TTL_SEASON
        for chunk_start in range(0, len(season_numbers), 20):
            chunk = season_numbers[chunk_start : chunk_start + 20]
            append = ",".join(f"season/{n}" for n in chunk)
            payload = await self._get(
                f"/tv/{tmdb_id}", {"append_to_response": append}, ttl=ttl
            )
            if payload:
                for number in chunk:
                    key = f"season/{number}"
                    if key in payload:
                        merged[key] = payload[key]
        return merged

    async def season(self, tmdb_id: str | int, season_number: int) -> dict[str, Any] | None:
        return await self._get(f"/tv/{tmdb_id}/season/{season_number}", ttl=TTL_SEASON)

    # -- lookup -------------------------------------------------------------
    async def find_by_external_id(
        self, external_id: str, source: str = "imdb_id"
    ) -> dict[str, Any] | None:
        """Resolve a TMDB id from an IMDb/TVDB id, for watchlist items missing one."""
        return await self._get(
            f"/find/{external_id}", {"external_source": source}, ttl=TTL_ENDED
        )

    async def search(self, query: str, media_type: str = "movie", year: int | None = None):
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if year:
            params["year" if media_type == "movie" else "first_air_date_year"] = year
        payload = await self._get(f"/search/{media_type}", params, ttl=TTL_MOVIE)
        return (payload or {}).get("results") or []


def _has_ended(data: dict[str, Any]) -> bool:
    return data.get("status") in ("Ended", "Canceled")


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    text = value.split("T")[0].strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_date(value: Any) -> date | None:
    """Public alias -- services parse TMDB date strings too."""
    return _parse_date(value)
