"""Release calendar: keeps `wanted` in step with TMDB and the library.

Responsibilities:

* expand monitored series into per-episode wants, skipping what Plex already
  has and (optionally) seasons the user has fully watched;
* keep movie release dates current, preferring digital/physical over
  theatrical, because a theatrical date says nothing about when a rip exists;
* promote wants from *waiting* to *searching* the moment they air, then slow
  repeated misses to daily searches.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from ..domain import decisions
from ..domain.models import WantedState
from ..logs import get_logger
from .context import Conduit

log = get_logger("calendar")

CONCURRENCY = 4


async def refresh_calendar(ctx: Conduit) -> dict[str, int]:
    """Full pass over everything we monitor."""
    media_rows = list(await ctx.repos.media.list_all(monitored_only=True))
    if not media_rows:
        return await _promote_and_expire(ctx, {"media": 0, "wants": 0})

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def worker(row) -> int:
        async with semaphore:
            try:
                return await refresh_media(ctx, row)
            except Exception:
                log.exception("calendar refresh failed", extra={"title": row["title"]})
                return 0

    counts = await asyncio.gather(*(worker(row) for row in media_rows))
    summary = {"media": len(media_rows), "wants": sum(counts)}
    return await _promote_and_expire(ctx, summary)


async def refresh_media(ctx: Conduit, row, *, require_details: bool = False) -> int:
    """Recompute what a single title still needs. Returns wants created."""
    if row is None or ctx.tmdb is None:
        if require_details:
            raise ValueError("TMDB is unavailable for this watchlist title")
        return 0
    tmdb_id = row.get("tmdb_id")
    if not tmdb_id:
        if require_details:
            raise ValueError("No TMDB match for this watchlist title")
        return 0

    if row["media_type"] == "movie":
        return await _refresh_movie(ctx, row, str(tmdb_id))
    return await _refresh_show(ctx, row, str(tmdb_id), require_details=require_details)


# ---------------------------------------------------------------------------
async def _refresh_movie(ctx: Conduit, row, tmdb_id: str) -> int:
    media_id = int(row["id"])
    library_row = await ctx.repos.library.has_movie(tmdb_id)
    if library_row:
        wanted_id = await ctx.repos.wanted.upsert(
            media_id=media_id, season=None, episode=None, title=row["title"],
            state=WantedState.DOWNLOADED,
        )
        await ctx.repos.wanted.set_state(wanted_id, WantedState.DOWNLOADED,
                                         reason="present in library")
        return 0

    found, _ = await ctx.tmdb.movie_release_date(tmdb_id)
    air_date = found.isoformat() if found else None
    state = (
        WantedState.SEARCHING
        if found is None or found <= date.today()
        else WantedState.WAITING
    )
    await ctx.repos.wanted.upsert(
        media_id=media_id, season=None, episode=None, title=row["title"],
        air_date=air_date, state=state,
    )
    return 1


async def _refresh_show(
    ctx: Conduit, row, tmdb_id: str, *, require_details: bool = False
) -> int:
    media_id = int(row["id"])
    config = ctx.config

    details = await ctx.tmdb.show_with_seasons(tmdb_id)
    if not details:
        if require_details:
            raise ValueError(f"TMDB has no episode data for {row['title']}")
        return 0

    if details.get("status") and details.get("status") != row.get("tmdb_status"):
        await ctx.repos.media.upsert(
            media_type="show", tmdb_id=tmdb_id, title=details.get("name") or row["title"],
            tmdb_status=details.get("status"),
            poster_path=details.get("poster_path"),
            backdrop_path=details.get("backdrop_path"),
        )

    seasons: dict[int, list[dict]] = {}
    for key, value in details.items():
        if key.startswith("season/") and isinstance(value, dict):
            number = int(key.split("/", 1)[1])
            seasons[number] = value.get("episodes") or []

    if not seasons or not any(seasons.values()):
        if require_details:
            raise ValueError(f"TMDB has no episodes for {row['title']}")
        return 0

    have = await ctx.repos.library.have_episodes(tmdb_id)
    watched = await ctx.repos.library.watched_episodes(tmdb_id)

    wants = decisions.plan_show_wants(
        seasons,
        have=have,
        watched=watched,
        policy=config.policy,
        max_seasons_back=config.calendar.max_seasons_back,
        backlog_grace_days=config.calendar.fresh_window_days,
    )

    created = 0
    for want in wants:
        air_iso = want.air_date.isoformat() if want.air_date else None
        state = (
            WantedState.SEARCHING
            if decisions.should_search(
                want.air_date, lead_hours=config.calendar.pre_air_lead_hours
            )
            else WantedState.WAITING
        )
        await ctx.repos.wanted.upsert(
            media_id=media_id,
            season=want.season,
            episode=want.episode,
            title=want.title,
            air_date=air_iso,
            state=state,
        )
        created += 1

    # Anything we already have on disk is no longer wanted.
    await ctx.repos.wanted.mark_present(media_id, have | watched)

    # Anything the planner deliberately left out -- already watched, assumed
    # watched, or beyond max_seasons_back -- is stood down, so a rule change
    # tidies the existing list instead of only affecting future additions.
    unwanted = decisions.all_episode_keys(seasons) - {
        (w.season, w.episode) for w in wants
    } - have - watched
    if unwanted:
        retired = await ctx.repos.wanted.retire(
            media_id, unwanted, reason="already watched or out of scope"
        )
        if retired:
            log.debug(
                "wants stood down", extra={"media_id": media_id, "count": retired}
            )
    return created


async def _promote_and_expire(ctx: Conduit, summary: dict[str, int]) -> dict[str, int]:
    config = ctx.config
    today = date.today().isoformat()
    promoted = await ctx.repos.wanted.promote_due(today)
    slowed = await ctx.repos.wanted.expire_stale(
        config.calendar.give_up_days_tv,
        config.calendar.give_up_days_movie,
        max_attempts=config.policy.max_search_attempts,
        fresh_days=config.calendar.fresh_window_days,
    )
    await ctx.db.set_meta("calendar_refreshed_at", datetime.now(UTC).isoformat())

    if promoted:
        log.info("wants became searchable", extra={"count": promoted})
    if slowed:
        log.info("wants switched to daily search", extra={"count": slowed})

    summary.update({"promoted": promoted, "expired": slowed})
    ctx.bus.publish("calendar.refreshed", **summary)
    return summary
