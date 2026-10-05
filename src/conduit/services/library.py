"""Plex library indexing.

The snapshot supplies watched state, episode ownership and cleanup decisions.
Movie requests verify cached copies with Plex before treating them as owned.
One scan replaces the hundreds of show and season lookups per cycle.
"""

from __future__ import annotations

from datetime import UTC

from ..db.database import Row
from ..domain.models import EventLevel, LibraryItem
from ..logs import get_logger
from .context import Conduit

log = get_logger("library")


async def existing_movie(ctx: Conduit, tmdb_id: str) -> Row | None:
    """Check cached movie copies with Plex before counting them as owned."""
    for row in await ctx.repos.library.movie_copies(tmdb_id):
        if ctx.plex is None or await ctx.plex.movie_file_exists(row["rating_key"]):
            return row
        await ctx.repos.library.remove_movie_copy(row["rating_key"])
    return None


async def index_library(ctx: Conduit) -> dict[str, int]:
    """Refresh the local mirror of the Plex library."""
    if ctx.plex is None:
        return {"items": 0}

    items = await ctx.plex.index_library()
    if not items:
        log.warning("Plex returned an empty library; keeping the previous index")
        return {"items": 0}

    count = await ctx.repos.library.replace_all(items)
    breakdown = {
        "movies": sum(1 for i in items if i.kind == "movie"),
        "shows": sum(1 for i in items if i.kind == "show"),
        "episodes": sum(1 for i in items if i.kind == "episode"),
        "watched": sum(1 for i in items if i.watched),
    }
    await ctx.db.set_meta("library_indexed_at", _now())
    unmatched = await _report_unmatched(ctx, items)
    ctx.bus.publish("library.indexed", count=count, **breakdown)
    log.info("library index updated", extra={**breakdown, "unmatched": unmatched})
    return {"items": count, **breakdown, "unmatched": unmatched}


def count_unmatched(items: list[LibraryItem]) -> int:
    """How many entries Plex has not matched to a TMDB id.

    These are the one thing de-duplication cannot see: every "do I have this
    already?" check is a TMDB-id lookup, so an unmatched entry looks like
    missing media and gets bought again. Counted per *entry*, since one
    unmatched show usually drags all of its episodes with it.
    """
    return sum(
        1
        for i in items
        if (i.kind == "movie" and not i.tmdb_id) or (i.kind == "show" and not i.tmdb_id)
    )


async def _report_unmatched(ctx: Conduit, items: list[LibraryItem]) -> int:
    """Log every pass, but only write to the timeline when the number moves.

    The index runs every half hour; an event each time would bury the
    timeline in a warning the user has already seen and cannot act on twice.
    """
    unmatched = count_unmatched(items)
    previous = int(await ctx.db.get_meta("library_unmatched", 0) or 0)
    await ctx.db.set_meta("library_unmatched", unmatched)

    if unmatched:
        log.warning(
            "library entries Plex has not matched; Conduit cannot tell you own them",
            extra={"count": unmatched},
        )
    if unmatched == previous:
        return unmatched

    if unmatched:
        await ctx.record(
            "library",
            f"{unmatched} library entr{'y' if unmatched == 1 else 'ies'} have no TMDB match "
            f"— Conduit cannot tell you already own them, and may pay to fetch them again. "
            f"Fix them in Plex with Match, then re-index.",
            level=EventLevel.WARNING,
        )
    elif previous:
        await ctx.record(
            "library", "Every library entry is matched again", level=EventLevel.SUCCESS
        )
    return unmatched


async def track_watched_shows(ctx: Conduit) -> int:
    """Start monitoring any series the user has actually started watching.

    This is what makes new episodes appear without touching the watchlist:
    if you have watched something, you presumably want the next one.
    """
    config = ctx.config
    if not config.calendar.track_watched_shows:
        return 0

    tmdb_ids = await ctx.repos.library.watched_show_tmdb_ids()
    if not tmdb_ids:
        return 0

    known = {
        str(row["tmdb_id"])
        for row in await ctx.repos.media.list_all(media_type="show")
        if row["tmdb_id"]
    }
    ignored = await ctx.repos.media.ignored_tmdb_ids()
    new_ids = tmdb_ids - known - ignored
    if not new_ids:
        return 0

    added = 0
    for tmdb_id in sorted(new_ids):
        details = await ctx.tmdb.show(tmdb_id) if ctx.tmdb else None
        title = (details or {}).get("name") or f"TMDB {tmdb_id}"
        media_id = await ctx.repos.media.upsert(
            media_type="show",
            tmdb_id=tmdb_id,
            title=title,
            year=_year(details),
            overview=(details or {}).get("overview"),
            poster_path=(details or {}).get("poster_path"),
            backdrop_path=(details or {}).get("backdrop_path"),
            tmdb_status=(details or {}).get("status"),
            source="library",
        )
        await ctx.record(
            "monitor",
            f"Now following {title} -- you have been watching it",
            media_id=media_id,
            data={"tmdb_id": tmdb_id},
        )
        added += 1

    log.info("started following watched shows", extra={"count": added})
    return added


def _year(details: dict | None) -> int | None:
    if not details:
        return None
    raw = details.get("first_air_date") or details.get("release_date") or ""
    return int(raw[:4]) if raw[:4].isdigit() else None


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
