"""The grabber: find the best release for everything we still want.

Flow per title: group outstanding wants into the cheapest set of searches
(a pack when several episodes are missing, singles when one is), ask every
tracker at once, parse and score the results, then either create a download or
record *why* nothing qualified. That last part is the difference between a
dashboard that says "waiting" forever and one that tells you the only release
was a 220 GB full disc your profile blocks.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date
from typing import Any

from ..clients.indexers.base import SearchQuery
from ..domain import decisions, scoring
from ..domain.models import (
    DownloadState,
    EventLevel,
    ParsedRelease,
    Rejection,
    Release,
    WantedState,
)
from ..domain.parser import parse_release
from ..logs import get_logger
from ..util.text import episode_code, human_size
from .context import Conduit

log = get_logger("search")

DEFAULT_LIMIT = 60
CONCURRENCY = 3


async def run_search(
    ctx: Conduit, *, fresh_only: bool = False, limit: int = DEFAULT_LIMIT
) -> dict[str, int]:
    """Search for everything that is due. ``fresh_only`` targets recent airings."""
    if not len(ctx.indexers):
        log.warning("no indexers configured; skipping search")
        return {"searched": 0, "grabbed": 0}

    await ctx.repos.wanted.promote_due(date.today().isoformat())
    fresh_days = ctx.config.calendar.fresh_window_days if fresh_only else None
    wants = await ctx.repos.wanted.due_for_search(
        limit=limit, fresh_days=fresh_days,
        lead_hours=ctx.config.calendar.pre_air_lead_hours,
    )
    if not wants:
        return {"searched": 0, "grabbed": 0}

    by_media: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in wants:
        by_media[int(row["media_id"])].append(row)

    known = await ctx.repos.downloads.known_release_keys()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    grabbed = 0

    async def worker(media_id: int, rows: list[dict[str, Any]]) -> int:
        async with semaphore:
            try:
                return await _search_media(ctx, media_id, rows, known)
            except Exception:
                log.exception("search failed", extra={"media_id": media_id})
                return 0

    results = await asyncio.gather(
        *(worker(media_id, rows) for media_id, rows in by_media.items())
    )
    grabbed = sum(results)

    ctx.bus.publish("search.finished", searched=len(wants), grabbed=grabbed, fresh=fresh_only)
    log.info(
        "search pass complete",
        extra={"wants": len(wants), "titles": len(by_media), "grabbed": grabbed,
               "mode": "fresh" if fresh_only else "full"},
    )
    return {"searched": len(wants), "grabbed": grabbed, "titles": len(by_media)}


async def search_media_now(ctx: Conduit, media_id: int) -> dict[str, int]:
    """Manual 'search this title now' from the dashboard."""
    rows = [
        row
        for row in await ctx.repos.wanted.for_media(media_id)
        if row["state"] in (WantedState.SEARCHING, WantedState.WAITING,
                            WantedState.UNAVAILABLE)
    ]
    if not rows:
        # Distinct from "searched and found nothing good". Reporting this as a
        # quality-profile miss sent a real investigation in the wrong direction.
        return {
            "searched": 0,
            "grabbed": 0,
            "reason": "Nothing is outstanding for this title — no episodes are "
                      "currently wanted, so there was nothing to search for.",
        }
    media = await ctx.repos.media.get(media_id)
    for row in rows:
        row.update(
            {
                "media_type": media["media_type"],
                "tmdb_id": media["tmdb_id"],
                "media_title": media["title"],
                "year": media["year"],
                "profile": media["profile"],
                "poster_path": media["poster_path"],
            }
        )
    known = await ctx.repos.downloads.known_release_keys()
    grabbed = await _search_media(ctx, media_id, rows, known)
    return {"searched": len(rows), "grabbed": grabbed}


async def preview_media(ctx: Conduit, media_id: int, season: int | None = None) -> dict[str, Any]:
    """Score every candidate for a title without grabbing anything.

    This is the answer to "why didn't it take the 4K remux?" -- the dashboard
    shows each release with its score breakdown and the exact rule that
    rejected it.
    """
    media = await ctx.repos.media.get(media_id)
    if not media:
        return {"error": "not found", "candidates": []}

    profile = (
        ctx.config.profile(media["profile"])
        if media.get("profile")
        else ctx.config.profile_for(media["media_type"])
    )
    query = SearchQuery(
        media_type=media["media_type"],
        tmdb_id=str(media["tmdb_id"]) if media["tmdb_id"] else None,
        title=media["title"],
        year=media["year"],
        season=season,
    )
    releases = await ctx.indexers.search(query)
    outstanding = [
        row for row in await ctx.repos.wanted.for_media(media_id)
        if row["state"] in (WantedState.WAITING, WantedState.SEARCHING,
                            WantedState.UNAVAILABLE)
    ]
    season_sizes = (
        await _known_season_sizes(ctx, media_id, query.tmdb_id)
        if media["media_type"] == "show" else {}
    )
    planned_packs: set[int] = set()
    if media["media_type"] == "show":
        missing = [
            (int(row["season"]), int(row["episode"]))
            for row in outstanding
            if row["season"] is not None and row["episode"] is not None
        ]
        planned_packs = {
            int(target.season)
            for target in decisions.plan_grab_targets(
                missing, ctx.config.policy, season_sizes=season_sizes
            )
            if target.is_pack and target.season is not None
        }
        planned_packs.update(
            int(row["season"]) for row in outstanding
            if row["season"] is not None and row["episode"] is None
        )
    known = await ctx.repos.downloads.known_release_keys()
    parsed = {release.key: parse_release(release) for release in releases}
    coverage = {
        release.key: (
            outstanding if media["media_type"] == "movie" else [
                row for row in outstanding
                if row["season"] is not None
                and (season is None or row["season"] == season)
                and parsed[release.key].covers(row["season"], row["episode"])
            ]
        )
        for release in releases
    }
    scored = scoring.rank(
        releases, profile,
        episode_counts={
            release.key: _content_episode_count(
                parsed[release.key], season_sizes, len(coverage[release.key])
            )
            for release in releases
        },
    )
    for item in scored:
        if not decisions.matches_target(
            item.release, item.parsed,
            media_type=media["media_type"], tmdb_id=query.tmdb_id,
            title=media["title"], season=None, episode=None, year=media["year"],
        ):
            item.rejections.append(Rejection("match", "does not match this title"))
        if item.release.key in known:
            item.rejections.append(Rejection("seen", "already selected, denied or blocked"))
        if not coverage[item.release.key]:
            detail = ("title is not currently wanted" if media["media_type"] == "movie"
                      else "does not cover an outstanding episode")
            item.rejections.append(Rejection("wanted", detail))
        if item.parsed.is_complete_series and not planned_packs:
            item.rejections.append(Rejection("pack", "no season pack is currently planned"))
        elif (item.parsed.is_season_pack and not item.parsed.is_complete_series
              and item.parsed.season not in planned_packs):
            item.rejections.append(Rejection("pack", "single episodes are planned for this season"))
    return {
        "media": {"id": media_id, "title": media["title"], "type": media["media_type"]},
        "profile": profile.name,
        "query": query.describe(),
        "candidates": [item.summary() for item in scored],
        "total": len(releases),
    }


# ---------------------------------------------------------------------------
async def _search_media(
    ctx: Conduit,
    media_id: int,
    rows: list[dict[str, Any]],
    known: set[tuple[str, str]],
) -> int:
    first = rows[0]
    media_type = first["media_type"]
    tmdb_id = first.get("tmdb_id")
    title = first.get("media_title") or ""
    year = first.get("year")
    profile = ctx.config.profile(first.get("profile")) if first.get("profile") else \
        ctx.config.profile_for(media_type)
    season_sizes = (
        await _known_season_sizes(ctx, media_id, str(tmdb_id) if tmdb_id else None)
        if media_type == "show" else {}
    )

    if media_type == "movie":
        targets = [decisions.GrabTarget(season=None, episode=None, episode_count=1, label=title)]
        want_lookup = {(None, None): rows[0]}
    else:
        missing = [
            (int(r["season"]), int(r["episode"]))
            for r in rows
            if r.get("season") is not None and r.get("episode") is not None
        ]
        want_lookup = {
            (r.get("season"), r.get("episode")): r for r in rows
        }
        if missing:
            targets = decisions.plan_grab_targets(
                missing, ctx.config.policy, season_sizes=season_sizes
            )
        else:
            targets = [
                decisions.GrabTarget(
                    season=r.get("season"), episode=None,
                    episode_count=season_sizes.get(r.get("season"), 1),
                    label=f"Season {r.get('season')}",
                )
                for r in rows
                if r.get("season") is not None
            ]

    distinct_seasons = len({t.season for t in targets if t.season is not None})
    grabbed = 0
    queue = list(targets)

    while queue:
        target = queue.pop(0)
        found = await _search_target(
            ctx,
            media_id=media_id,
            media_type=media_type,
            tmdb_id=str(tmdb_id) if tmdb_id else None,
            title=title,
            year=year,
            profile=profile,
            target=target,
            want_lookup=want_lookup,
            known=known,
            distinct_seasons=distinct_seasons,
            season_sizes=season_sizes,
        )
        if found:
            grabbed += 1
            continue

        # A season pack that does not exist yet (a currently-airing season, or
        # one nobody has packed) must not block the episodes that *are* there.
        if target.is_pack and target.season is not None:
            # Filter before sorting: a title can hold both season-level wants
            # (episode NULL) and episode-level ones, and sorting a mix of None
            # and int raises.
            episodes = sorted(
                key[1]
                for key in want_lookup
                if key[0] == target.season and key[1] is not None
            )
            singles = [
                decisions.GrabTarget(season=target.season, episode=episode, episode_count=1,
                                     label=episode_code(target.season, episode))
                for episode in episodes
            ]
            if singles:
                log.debug(
                    "no pack available, falling back to individual episodes",
                    extra={"title": title, "season": target.season, "episodes": len(singles)},
                )
                queue.extend(singles)
    return grabbed


async def _search_target(
    ctx: Conduit,
    *,
    media_id: int,
    media_type: str,
    tmdb_id: str | None,
    title: str,
    year: int | None,
    profile,
    target: decisions.GrabTarget,
    want_lookup: dict,
    known: set[tuple[str, str]],
    distinct_seasons: int,
    season_sizes: dict[int, int],
) -> bool:
    affected = _affected_wants(want_lookup, target)
    if not await ctx.repos.wanted.claimable([int(row["id"]) for row in affected]):
        return False
    query = SearchQuery(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        season=target.season,
        episode=target.episode,
    )
    releases = await ctx.indexers.search(query)

    if not releases:
        await _record_miss(ctx, media_id, title, target, affected, "no releases on any tracker")
        return False

    # Keyed by (indexer, id): torrent ids are only unique within a tracker, so
    # keying on the id alone silently mixes up releases once a second tracker
    # is configured.
    parsed_map = {r.key: parse_release(r) for r in releases}
    candidates: list[Release] = [
        r
        for r in releases
        if decisions.matches_target(
            r,
            parsed_map[r.key],
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            season=target.season,
            episode=target.episode,
            year=year,
        )
        and not (target.episode is not None and
                 (parsed_map[r.key].is_season_pack or parsed_map[r.key].is_complete_series))
    ]
    candidates = [r for r in candidates if r.key not in known]

    if not candidates:
        await _record_miss(
            ctx, media_id, title, target, affected,
            f"{len(releases)} releases found, none matched {target.label or 'the request'}",
        )
        return False

    scored = scoring.rank(
        candidates,
        profile,
        episode_counts={
            r.key: _content_episode_count(
                parsed_map[r.key], season_sizes, target.episode_count
            )
            for r in candidates
        },
    )
    winner = scoring.best(scored, profile)
    if winner is None:
        await _record_miss(
            ctx, media_id, title, target, affected, decisions.summarise_rejections(scored)
        )
        return False

    covered = (
        affected if media_type == "movie" else [
            row for (season, episode), row in want_lookup.items()
            if season is not None and winner.parsed.covers(season, episode)
        ]
    )

    created = await _create_download(
        ctx,
        media_id=media_id,
        media_title=title,
        winner=winner,
        target=target,
        affected=covered,
        distinct_seasons=distinct_seasons,
        profile_name=profile.name,
        alternatives=[s.summary() for s in scored[:8]],
    )
    if created:
        known.add(winner.release.key)
    return created


def _affected_wants(want_lookup: dict, target: decisions.GrabTarget) -> list[dict[str, Any]]:
    """Which want rows this grab would satisfy."""
    if target.season is None:
        return list(want_lookup.values())
    if target.episode is not None:
        row = want_lookup.get((target.season, target.episode))
        return [row] if row else []
    return [
        row
        for key, row in want_lookup.items()
        if key[0] == target.season
    ]


async def _known_season_sizes(
    ctx: Conduit, media_id: int, tmdb_id: str | None
) -> dict[int, int]:
    keys = {
        (int(row["season"]), int(row["episode"]))
        for row in await ctx.repos.wanted.for_media(media_id)
        if row["season"] is not None and row["episode"] is not None
    }
    if tmdb_id:
        keys.update(await ctx.repos.library.have_episodes(tmdb_id))
    episodes: dict[int, set[int]] = defaultdict(set)
    for season, episode in keys:
        episodes[season].add(episode)
    return {
        season: max(len(numbers), max(numbers))
        for season, numbers in episodes.items()
    }


def _content_episode_count(
    parsed: ParsedRelease, season_sizes: dict[int, int], fallback: int
) -> int:
    if parsed.is_complete_series:
        return max(sum(season_sizes.values()), fallback, 1)
    if parsed.is_season_pack and parsed.season is not None:
        return max(season_sizes.get(parsed.season, 0), fallback, 1)
    return max(len(parsed.episodes), fallback, 1)


async def _create_download(
    ctx: Conduit,
    *,
    media_id: int,
    media_title: str,
    winner,
    target: decisions.GrabTarget,
    affected: list[dict[str, Any]],
    distinct_seasons: int,
    profile_name: str,
    alternatives: list[dict[str, Any]],
) -> bool:
    release: Release = winner.release
    parsed = winner.parsed
    policy = ctx.config.policy

    approval = decisions.needs_approval(
        parsed, float(release.size_bytes), policy, distinct_seasons=distinct_seasons
    )
    state = DownloadState.PENDING_APPROVAL if approval.required else DownloadState.QUEUED
    display = decisions.display_title(
        media_title, parsed, None if len(parsed.episodes) > 1 else target
    )

    async with ctx.download_decision_lock:
        wanted_ids = [int(row["id"]) for row in affected]
        if not await ctx.repos.wanted.claimable(wanted_ids):
            return False
        existing = await ctx.repos.downloads.by_release(release.indexer, release.indexer_id)
        if existing:
            return False

        download_id = await ctx.repos.downloads.create(
            media_id=media_id,
            wanted_id=wanted_ids[0],
            display_title=display,
            release_name=release.name,
            indexer=release.indexer,
            indexer_id=release.indexer_id,
            download_url=release.download_url,
            size_bytes=float(release.size_bytes),
            season=target.season if target.season is not None else parsed.season,
            episode_from=parsed.episode_from if not target.is_pack else None,
            episode_to=parsed.episode_to if not target.is_pack else None,
            is_season_pack=parsed.is_season_pack or target.is_pack,
            resolution=parsed.resolution,
            source=parsed.source,
            dynamic_range=parsed.dynamic_range,
            video_codec=parsed.video_codec,
            audio=parsed.audio,
            release_group=parsed.release_group,
            score=winner.score,
            seeders=release.seeders,
            state=state,
            wanted_count=len(wanted_ids),
        )

        for wanted_id in wanted_ids:
            await ctx.repos.wanted.set_state(
                wanted_id, WantedState.GRABBED, reason=f"grabbed #{download_id}"
            )

    detail = (
        f"{parsed.resolution or '?'} {parsed.source or '?'} "
        f"{parsed.dynamic_range} · {human_size(release.size_bytes)} · "
        f"{release.seeders} seeders · score {winner.score}"
    )
    if approval.required:
        message = f"Needs approval: {display} — {approval.reason}"
        level = EventLevel.WARNING
    else:
        message = f"Grabbed {display} — {detail}"
        level = EventLevel.SUCCESS

    await ctx.record(
        "grab",
        message,
        level=level,
        media_id=media_id,
        download_id=download_id,
        data={
            "release": release.name,
            "indexer": release.indexer,
            "profile": profile_name,
            "score": winner.score,
            "breakdown": winner.breakdown,
            "size_bytes": release.size_bytes,
            "approval_reason": approval.reason,
            "alternatives": alternatives,
        },
    )
    ctx.bus.publish(
        "download.created", download_id=download_id, state=str(state), title=display
    )
    log.info(
        "release selected",
        extra={"title": display, "release": release.name[:90], "score": winner.score,
               "state": str(state)},
    )
    return True


async def _record_miss(
    ctx: Conduit,
    media_id: int,
    title: str,
    target: decisions.GrabTarget,
    affected: list[dict[str, Any]],
    reason: str,
) -> None:
    changed = 0
    for row in affected:
        changed += await ctx.repos.wanted.record_search_miss(int(row["id"]), reason)
    if not changed:
        return
    label = f"{title} {target.label}".strip()
    log.debug("nothing grabbed", extra={"title": label, "reason": reason})
    await ctx.record(
        "search",
        f"No release for {label}: {reason}",
        level=EventLevel.DEBUG,
        media_id=media_id,
        publish=False,
    )
