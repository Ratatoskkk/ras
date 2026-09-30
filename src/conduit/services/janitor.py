"""Housekeeping: watched detection, reclaiming space, keeping the DB tidy."""

from __future__ import annotations

import contextlib
from typing import Any

from ..domain.models import DownloadState, EventLevel, Release, TorrentStatus
from ..domain.parser import parse_release
from ..logs import get_logger
from ..util.text import human_duration, human_size
from .context import Conduit

log = get_logger("janitor")


async def sync_watched_flags(ctx: Conduit) -> dict[str, int]:
    """Mark completed downloads whose content the user has now watched.

    Derived entirely from the library index, so it costs one SQL pass instead
    of the reference project's per-title Plex search.
    """
    completed = await ctx.repos.downloads.completed()
    if not completed:
        return {"checked": 0, "watched": 0}

    movie_watched = await ctx.repos.library.movie_watched_map()
    # Both caches are per-show, not per-row: a ten-episode series otherwise
    # asked the same two questions ten times.
    show_cache: dict[str, set[tuple[int, int]]] = {}
    have_cache: dict[str, set[tuple[int, int]]] = {}
    updates: list[tuple[int, int]] = []

    for row in completed:
        tmdb_id = str(row.get("tmdb_id") or "")
        media_type = row.get("media_type") or "movie"
        watched = False

        if media_type == "movie":
            watched = movie_watched.get(tmdb_id, False)
        elif tmdb_id:
            if tmdb_id not in show_cache:
                show_cache[tmdb_id] = await ctx.repos.library.watched_episodes(tmdb_id)
            seen = show_cache[tmdb_id]
            season = row.get("season")
            if season is not None:
                if row.get("is_season_pack"):
                    if tmdb_id not in have_cache:
                        have_cache[tmdb_id] = await ctx.repos.library.have_episodes(tmdb_id)
                    seasons = _pack_seasons(row)
                    if seasons:
                        watched = True
                        for number in seasons:
                            season_keys = {k for k in have_cache[tmdb_id] if k[0] == number}
                            if not season_keys or not season_keys <= seen:
                                watched = False
                                break
                else:
                    start = row.get("episode_from")
                    end = row.get("episode_to") or start
                    if start is not None:
                        span = {(int(season), e) for e in range(int(start), int(end) + 1)}
                        watched = bool(span) and span <= seen

        if bool(row.get("watched")) != watched:
            updates.append((int(watched), int(row["id"])))

    if updates:
        await ctx.repos.downloads.set_watched_bulk(updates)
        ctx.bus.publish("cleanup.updated", changed=len(updates))

    total_watched = sum(1 for u in updates if u[0] == 1)
    log.debug(
        "watched flags synced",
        extra={"checked": len(completed), "changed": len(updates)},
    )
    return {"checked": len(completed), "changed": len(updates), "watched": total_watched}


def _pack_seasons(row: dict[str, Any]) -> set[int] | None:
    release_name = row.get("release_name") or ""
    if release_name:
        parsed = parse_release(Release(
            indexer=row.get("indexer") or "", indexer_id=str(row.get("indexer_id") or ""),
            name=release_name, size_bytes=int(row.get("size_bytes") or 0), download_url="",
        ))
        if parsed.season is not None and parsed.season_to is not None:
            return set(range(min(parsed.season, parsed.season_to),
                             max(parsed.season, parsed.season_to) + 1))
        if parsed.is_complete_series:
            return None
    season = row.get("season")
    return {int(season)} if season is not None else None


async def cleanup_candidates(ctx: Conduit) -> list[dict[str, Any]]:
    """Watched downloads, annotated with how far through seeding they are.

    Deleting a torrent before the tracker's seed requirement is met earns a
    hit-and-run, so every candidate carries its live seeding time and whether
    the goal has been reached. Nothing is hidden -- items still seeding are
    listed with a countdown so you can see what is coming.
    """
    policy = ctx.config.policy
    rows = await ctx.repos.downloads.completed(watched_only=True)
    if not rows:
        return []

    live: list[TorrentStatus] = []
    client_available = ctx.qbt is not None
    if ctx.qbt is not None:
        try:
            live = await ctx.qbt.torrents()
        except Exception as exc:
            client_available = False
            log.warning("could not read seeding state", extra={"err": str(exc)})

    required = policy.min_seed_days * 86400
    out: list[dict[str, Any]] = []

    for row in rows:
        torrent = _matching_torrent(ctx, row, live)
        seeded = float(torrent.seeding_time) if torrent else 0.0
        ratio = float(torrent.ratio) if torrent else 0.0

        # Gone from the client entirely: nothing left to seed, so nothing to wait for.
        orphaned = client_available and torrent is None
        by_time = seeded >= required
        by_ratio = policy.min_seed_ratio > 0 and ratio >= policy.min_seed_ratio
        satisfied = client_available and (orphaned or by_time or by_ratio)

        out.append(
            {
                **row,
                "drive_label": _drive_label(ctx, row.get("save_path") or ""),
                "human_size": human_size(float(row.get("size_bytes") or 0)),
                "seeding_seconds": int(seeded),
                "seeding_human": (
                    human_duration(seeded) if torrent else
                    "not in client" if client_available else "unavailable"
                ),
                "seed_ratio": round(ratio, 2),
                "seed_required_seconds": int(required),
                "seed_remaining_seconds": max(0, int(required - seeded)) if torrent else 0,
                "seed_progress": min(1.0, seeded / required) if required > 0 else 1.0,
                "seed_satisfied": satisfied,
                "seed_reason": (
                    "seeding state unavailable" if not client_available
                    else "no longer in qBittorrent" if orphaned
                    else "seed time met" if by_time
                    else f"ratio {ratio:.2f} met" if by_ratio
                    else f"{human_duration(required - seeded)} of seeding left"
                ),
                "in_client": torrent is not None,
            }
        )

    # Ready to reclaim first, then whatever frees the most space soonest.
    out.sort(key=lambda r: (not r["seed_satisfied"], -float(r.get("size_bytes") or 0)))
    return out


async def remove_download(
    ctx: Conduit, download_id: int, *, delete_files: bool = True,
    respect_seed_goal: bool = False,
) -> dict[str, Any]:
    """Remove a download from the client (optionally with its files) and archive it.

    ``respect_seed_goal`` refuses the delete while the tracker's seeding
    requirement is unmet. The reclaim UI sets it; an explicit removal from the
    queue does not, because that is a deliberate act on something you chose.
    """
    async with ctx.queue_dispatch_lock:
        return await _remove_download(
            ctx, download_id, delete_files=delete_files,
            respect_seed_goal=respect_seed_goal,
        )


async def _remove_download(
    ctx: Conduit, download_id: int, *, delete_files: bool,
    respect_seed_goal: bool,
) -> dict[str, Any]:
    row = await ctx.repos.downloads.get(download_id)
    if not row:
        return {"ok": False, "error": "not found"}

    removed = False
    info_hash = (row.get("info_hash") or "").lower()
    if ctx.qbt is None and (
        info_hash or row["state"] in (DownloadState.DOWNLOADING, DownloadState.COMPLETED)
    ):
        return {
            "ok": False,
            "error": "qBittorrent is unavailable; the download was not removed",
            "client_unavailable": True,
        }

    checked_torrent = None
    seed_state_checked = False
    if respect_seed_goal and not ctx.config.policy.allow_delete_before_seed_goal:
        blocker, checked_torrent, seed_state_checked = await _seed_blocker(ctx, row)
        if blocker:
            return {"ok": False, "error": blocker, "seed_blocked": True}

    if ctx.qbt is not None:
        if seed_state_checked:
            torrent = checked_torrent
        else:
            try:
                torrent = _matching_torrent(ctx, row, await ctx.qbt.torrents())
            except Exception as exc:
                log.warning("could not verify torrent removal", extra={"err": str(exc)})
                return {
                    "ok": False,
                    "error": "qBittorrent is unavailable; the download was not removed",
                    "client_unavailable": True,
                }
        if torrent:
            await ctx.qbt.delete([torrent.info_hash], delete_files=delete_files)
            removed = True

    await ctx.repos.downloads.archive(download_id)
    await ctx.record(
        "cleanup",
        f"Removed {row['display_title']}"
        + (" and its files" if delete_files and removed else ""),
        level=EventLevel.INFO,
        media_id=row.get("media_id"),
        download_id=download_id,
        data={"freed_bytes": float(row.get("size_bytes") or 0) if delete_files and removed else 0},
    )
    ctx.bus.publish("cleanup.removed", download_id=download_id)
    return {"ok": True, "client_removed": removed}


async def _seed_blocker(
    ctx: Conduit, row: dict[str, Any]
) -> tuple[str | None, TorrentStatus | None, bool]:
    """Return a seed blocker and the verified torrent for this download."""
    if ctx.qbt is None:
        return "Cannot verify seeding state while qBittorrent is unavailable.", None, False
    policy = ctx.config.policy
    try:
        torrents = await ctx.qbt.torrents()
    except Exception as exc:
        log.warning(
            "could not verify seeding state; refusing the delete",
            extra={"download_id": row["id"], "err": f"{type(exc).__name__}: {exc}"},
        )
        return "Cannot verify seeding state while qBittorrent is unavailable.", None, False
    torrent = _matching_torrent(ctx, row, torrents)
    if torrent is None:
        return None, None, True

    required = policy.min_seed_days * 86400
    if torrent.seeding_time >= required:
        return None, torrent, True
    if policy.min_seed_ratio > 0 and torrent.ratio >= policy.min_seed_ratio:
        return None, torrent, True
    remaining = human_duration(required - torrent.seeding_time)
    return (
        f"Still seeding: {remaining} short of the {policy.min_seed_days:g}-day "
        f"requirement. Deleting now risks a hit-and-run.",
        torrent,
        True,
    )


def _matching_torrent(
    ctx: Conduit, row: dict[str, Any], torrents: list[TorrentStatus]
) -> TorrentStatus | None:
    info_hash = (row.get("info_hash") or "").lower()
    tag = f"{ctx.config.policy.torrent_tag_prefix}_{row['id']}"
    return next(
        (torrent for torrent in torrents if info_hash and torrent.info_hash == info_hash),
        None,
    ) or next((torrent for torrent in torrents if tag in torrent.tags), None)


async def retire_superseded_episodes(
    ctx: Conduit, media_id: int, season: int | set[int], keep_download_id: int
) -> int:
    """A season pack landed -- drop the individual episodes it replaces.

    Only Conduit's own grabs for that season are touched, identified by
    database rows rather than by regex-matching names in the download client.
    """
    seasons = {season} if isinstance(season, int) else season
    if not seasons:
        return 0
    rows = await ctx.repos.downloads.list_by_state(
        DownloadState.DOWNLOADING, DownloadState.QUEUED,
        DownloadState.PENDING_APPROVAL, DownloadState.NO_SPACE,
        DownloadState.COMPLETED,
    )
    victims = [
        row
        for row in rows
        if int(row["id"]) != keep_download_id
        and row.get("media_id") == media_id
        and row.get("season") in seasons
        and not row.get("is_season_pack")
    ]
    if not victims:
        return 0

    retired = 0
    for row in victims:
        result = await remove_download(
            ctx, int(row["id"]), delete_files=True, respect_seed_goal=True
        )
        if result["ok"]:
            retired += 1

    if not retired:
        return 0

    label = (f"Season {next(iter(seasons))}" if len(seasons) == 1
             else f"Seasons {', '.join(str(number) for number in sorted(seasons))}")
    await ctx.record(
        "cleanup",
        f"{label} pack replaced {retired} individual episode download(s)",
        media_id=media_id,
        download_id=keep_download_id,
    )
    log.info("superseded episodes retired", extra={"count": retired, "seasons": sorted(seasons)})
    return retired


async def housekeeping(ctx: Conduit) -> dict[str, int]:
    """Periodic cleanup of superseded downloads, caches, events and the database."""
    retired = 0
    for row in await ctx.repos.downloads.list_by_state(DownloadState.COMPLETED):
        if row.get("is_season_pack") and row.get("media_id"):
            seasons = _pack_seasons(row)
            if seasons:
                retired += await retire_superseded_episodes(
                    ctx, int(row["media_id"]), seasons, int(row["id"])
                )
    purged = await ctx.repos.cache.purge_expired()
    pruned = await ctx.repos.events.prune(keep=5000)
    if pruned:
        with contextlib.suppress(Exception):
            await ctx.db.vacuum()
    log.debug("housekeeping done", extra={"cache_purged": purged, "events_pruned": pruned})
    return {"superseded_retired": retired, "cache_purged": purged, "events_pruned": pruned}


def _drive_label(ctx: Conduit, save_path: str) -> str:
    if not save_path:
        return "Unknown"
    normalised = save_path.rstrip("/\\").lower()
    for index, path in enumerate(ctx.settings.download_dirs):
        if normalised.startswith(str(path).rstrip("/\\").lower()):
            return f"Drive {index + 1}"
    return "Unknown"
