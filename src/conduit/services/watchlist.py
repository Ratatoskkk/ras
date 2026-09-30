"""Plex watchlist sync.

The watchlist is treated as an *inbox*, not as state. An item is read, turned
into a monitored media row plus the specific things we still need, and only
then removed -- so nothing is ever dropped because a tracker was down at the
wrong moment, which is exactly what could happen when the reference project
removed items before knowing whether it had found anything.
"""

from __future__ import annotations

from datetime import UTC, date

from ..domain.models import MediaType, WantedState
from ..logs import get_logger
from ..util.text import episode_code
from .context import Conduit

log = get_logger("watchlist")


async def sync_watchlist(ctx: Conduit) -> dict[str, int]:
    if ctx.plex is None:
        return {"seen": 0}

    entries = await ctx.plex.watchlist()
    await ctx.db.set_meta("watchlist_checked_at", _now())
    if not entries:
        ctx.bus.publish("watchlist.synced", seen=0, added=0)
        return {"seen": 0, "added": 0}

    added = 0
    failed = 0
    for entry in entries:
        try:
            media_id = await _ingest(ctx, entry)
        except ValueError as exc:
            failed += 1
            log.warning("watchlist item is not ready", extra={"title": entry.title, "err": str(exc)})
            continue
        except Exception as exc:
            failed += 1
            log.exception("watchlist item failed", extra={"title": entry.title})
            await ctx.record(
                "watchlist", f"Could not process {entry.title}: {exc}", level="error"
            )
            continue

        if media_id is None:
            continue
        added += 1
        if ctx.config.policy.auto_remove_from_watchlist and entry.rating_key:
            # The item is already safely recorded; a failed removal is worth a
            # log line, never a reason to abandon the rest of the watchlist.
            try:
                await ctx.plex.remove_from_watchlist(entry.rating_key)
            except Exception as exc:
                log.warning(
                    "could not remove from watchlist",
                    extra={"title": entry.title, "err": str(exc)},
                )

    if added:
        # Something new became wanted. Waiting out the search interval means a
        # title you just added sits there for up to half an hour doing nothing
        # visible, which reads as broken -- ask for a search now instead.
        ctx.request_run("search-full")

    ctx.bus.publish("watchlist.synced", seen=len(entries), added=added, failed=failed)
    log.info(
        "watchlist synced", extra={"seen": len(entries), "added": added, "failed": failed}
    )
    return {"seen": len(entries), "added": added, "failed": failed}


async def _ingest(ctx: Conduit, entry) -> int | None:
    """Turn one watchlist row into monitored media plus concrete wants."""
    tmdb_id = entry.tmdb_id
    is_movie = entry.media_type == "movie"
    media_type = MediaType.MOVIE if is_movie else MediaType.SHOW

    if not tmdb_id and entry.imdb_id and ctx.tmdb:
        found = await ctx.tmdb.find_by_external_id(entry.imdb_id)
        bucket = "movie_results" if is_movie else "tv_results"
        results = (found or {}).get(bucket) or []
        if results:
            tmdb_id = str(results[0].get("id"))

    title = entry.grandparent_title or entry.parent_title or entry.title
    details = None
    if tmdb_id and ctx.tmdb:
        details = (
            await ctx.tmdb.movie(tmdb_id) if is_movie else await ctx.tmdb.show(tmdb_id)
        )

    if details:
        title = details.get("title") or details.get("name") or title

    media_id = await ctx.repos.media.upsert(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        year=entry.year or _year(details),
        overview=(details or {}).get("overview"),
        poster_path=(details or {}).get("poster_path"),
        backdrop_path=(details or {}).get("backdrop_path"),
        tmdb_status=(details or {}).get("status"),
        imdb_id=entry.imdb_id or (details or {}).get("imdb_id"),
        source="watchlist",
        plex_rating_key=entry.rating_key,
    )
    await ctx.repos.media.set_flags(media_id, monitored=True, ignored=False)

    if is_movie:
        await _want_movie(ctx, media_id, tmdb_id, title)
    elif entry.media_type == "episode" and entry.season is not None:
        await ctx.repos.wanted.upsert(
            media_id=media_id,
            season=entry.season,
            episode=entry.episode,
            title=entry.title,
            state=WantedState.SEARCHING,
        )
        await ctx.record(
            "watchlist",
            f"Watchlisted {title} {episode_code(entry.season, entry.episode)}",
            media_id=media_id,
        )
    elif entry.media_type == "season" and entry.season is not None:
        await _want_season(ctx, media_id, tmdb_id, title, entry.season)
    else:
        # A whole series: the calendar task expands it into episodes on its
        # next pass, and we nudge it so that happens now rather than in an hour.
        from .calendar import refresh_media  # local import avoids a cycle

        await refresh_media(ctx, await ctx.repos.media.get(media_id), require_details=True)
        await ctx.record("watchlist", f"Now following {title}", media_id=media_id)
        return media_id

    return media_id


async def _want_movie(ctx: Conduit, media_id: int, tmdb_id: str | None, title: str) -> None:
    if tmdb_id and await ctx.repos.library.has_movie(tmdb_id):
        wanted_id = await ctx.repos.wanted.upsert(
            media_id=media_id, season=None, episode=None, title=title,
            state=WantedState.DOWNLOADED,
        )
        await ctx.repos.wanted.set_state(wanted_id, WantedState.DOWNLOADED,
                                         reason="present in library")
        await ctx.record("watchlist", f"{title} is already in your library", media_id=media_id)
        return

    air_date = None
    found = None
    if tmdb_id and ctx.tmdb:
        found, _ = await ctx.tmdb.movie_release_date(tmdb_id)
        air_date = found.isoformat() if found else None

    state = (
        WantedState.SEARCHING if found is None or found <= date.today()
        else WantedState.WAITING
    )
    await ctx.repos.wanted.upsert(
        media_id=media_id, season=None, episode=None, title=title,
        air_date=air_date, state=state,
    )
    when = f"expected {air_date}" if air_date else "release date unknown"
    await ctx.record("watchlist", f"Wanted: {title} ({when})", media_id=media_id)


async def _want_season(
    ctx: Conduit, media_id: int, tmdb_id: str | None, title: str, season: int
) -> None:
    episodes: list[dict] = []
    if tmdb_id and ctx.tmdb:
        data = await ctx.tmdb.season(tmdb_id, season)
        episodes = (data or {}).get("episodes") or []

    if not episodes:
        await ctx.repos.wanted.upsert(
            media_id=media_id, season=season, episode=None,
            title=f"Season {season}", state=WantedState.SEARCHING,
        )
        return

    have = await ctx.repos.library.have_episodes(tmdb_id) if tmdb_id else set()
    for raw in episodes:
        number = int(raw.get("episode_number") or 0)
        if number <= 0 or (season, number) in have:
            continue
        await ctx.repos.wanted.upsert(
            media_id=media_id,
            season=season,
            episode=number,
            title=str(raw.get("name") or ""),
            air_date=(raw.get("air_date") or None),
        )
    await ctx.record(
        "watchlist", f"Wanted: {title} Season {season} ({len(episodes)} episodes)",
        media_id=media_id,
    )


def _year(details: dict | None) -> int | None:
    if not details:
        return None
    raw = details.get("release_date") or details.get("first_air_date") or ""
    return int(raw[:4]) if raw[:4].isdigit() else None


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
