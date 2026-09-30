"""Repositories -- every SQL statement in the app lives here.

Services talk to repositories, never to the connection. That keeps SQL out of
business logic (the reference project executed ad-hoc SQL from three different
modules, including the web layer) and gives the test suite a single seam.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from ..domain.models import DownloadState, EventLevel, LibraryItem, WantedState
from ..logs import get_logger
from .database import Database, Row

log = get_logger("repo")


class BaseRepo:
    def __init__(self, db: Database) -> None:
        self.db = db


# ---------------------------------------------------------------------------
class MediaRepo(BaseRepo):
    """Movies and series we are tracking."""

    async def upsert(
        self,
        *,
        media_type: str,
        tmdb_id: str | None,
        title: str,
        year: int | None = None,
        overview: str | None = None,
        poster_path: str | None = None,
        backdrop_path: str | None = None,
        tmdb_status: str | None = None,
        imdb_id: str | None = None,
        source: str = "watchlist",
        plex_rating_key: str | None = None,
    ) -> int:
        from ..util.text import normalize_title

        existing = await self.find(media_type, tmdb_id, title)
        sort_title = normalize_title(title)
        if existing:
            await self.db.execute(
                """UPDATE media SET title = ?, sort_title = ?, year = COALESCE(?, year),
                       overview = COALESCE(?, overview), poster_path = COALESCE(?, poster_path),
                       backdrop_path = COALESCE(?, backdrop_path),
                       tmdb_status = COALESCE(?, tmdb_status), imdb_id = COALESCE(?, imdb_id),
                       plex_rating_key = COALESCE(?, plex_rating_key),
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (title, sort_title, year, overview, poster_path, backdrop_path,
                 tmdb_status, imdb_id, plex_rating_key, existing["id"]),
            )
            return int(existing["id"])

        return await self.db.execute(
            """INSERT INTO media
                 (media_type, tmdb_id, imdb_id, title, sort_title, year, overview,
                  poster_path, backdrop_path, tmdb_status, source, plex_rating_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (media_type, tmdb_id, imdb_id, title, sort_title, year, overview,
             poster_path, backdrop_path, tmdb_status, source, plex_rating_key),
        )

    async def find(self, media_type: str, tmdb_id: str | None, title: str = "") -> Row | None:
        if tmdb_id:
            row = await self.db.fetch_one(
                "SELECT * FROM media WHERE media_type = ? AND tmdb_id = ?",
                (media_type, str(tmdb_id)),
            )
            if row:
                return row
        if title:
            from ..util.text import normalize_title

            return await self.db.fetch_one(
                "SELECT * FROM media WHERE media_type = ? AND sort_title = ? AND tmdb_id IS NULL",
                (media_type, normalize_title(title)),
            )
        return None

    async def get(self, media_id: int) -> Row | None:
        return await self.db.fetch_one("SELECT * FROM media WHERE id = ?", (media_id,))

    async def list_all(self, media_type: str | None = None, monitored_only: bool = False) -> list[Row]:
        clauses, params = [], []
        if media_type:
            clauses.append("media_type = ?")
            params.append(media_type)
        if monitored_only:
            clauses.append("monitored = 1 AND ignored = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self.db.fetch_all(f"SELECT * FROM media {where} ORDER BY sort_title", params)

    async def set_flags(
        self, media_id: int, *, monitored: bool | None = None, ignored: bool | None = None,
        profile: str | None = None,
    ) -> None:
        sets, params = [], []
        if monitored is not None:
            sets.append("monitored = ?")
            params.append(int(monitored))
        if ignored is not None:
            sets.append("ignored = ?")
            params.append(int(ignored))
        if profile is not None:
            sets.append("profile = ?")
            params.append(profile or None)
        if not sets:
            return
        params.append(media_id)
        await self.db.execute(
            f"UPDATE media SET {', '.join(sets)}, updated_at = datetime('now') WHERE id = ?",
            params,
        )

    async def clear_ignored(self) -> int:
        """Un-ignore every title. Returns how many were brought back."""
        cursor = await self.db.conn.execute(
            "UPDATE media SET ignored = 0, monitored = 1, updated_at = datetime('now') "
            "WHERE ignored = 1"
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def ignored_tmdb_ids(self) -> set[str]:
        rows = await self.db.fetch_all(
            "SELECT tmdb_id FROM media WHERE ignored = 1 AND tmdb_id IS NOT NULL"
        )
        return {str(r["tmdb_id"]) for r in rows}

    async def delete(self, media_id: int) -> None:
        await self.db.execute("DELETE FROM media WHERE id = ?", (media_id,))


# ---------------------------------------------------------------------------
class WantedRepo(BaseRepo):
    """Movies / seasons / episodes we still need."""

    # One statement, not a SELECT followed by an INSERT or an UPDATE. A full
    # calendar recompute upserts one row per missing episode -- over a thousand
    # on a large library -- so halving the round trips halves the pass.
    # The conflict target repeats the expression index from migration 1.
    _UPSERT = """
        INSERT INTO wanted (media_id, season, episode, title, air_date, state)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (media_id, IFNULL(season, -1), IFNULL(episode, -1)) DO UPDATE SET
            air_date = COALESCE(excluded.air_date, wanted.air_date),
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE wanted.title END,
            state = CASE WHEN excluded.state = 'waiting'
                              AND wanted.state IN ('waiting', 'searching', 'unavailable')
                         THEN 'waiting'
                         WHEN wanted.state = 'ignored'
                              OR (wanted.state = 'waiting' AND excluded.state = 'searching')
                         THEN excluded.state ELSE wanted.state END,
            reason = CASE WHEN excluded.state = 'waiting'
                              AND wanted.state IN ('waiting', 'searching', 'unavailable')
                          THEN NULL ELSE wanted.reason END,
            search_attempts = CASE WHEN excluded.state = 'waiting'
                              AND wanted.state IN ('waiting', 'searching', 'unavailable')
                                   THEN 0 ELSE wanted.search_attempts END,
            first_search_at = CASE WHEN excluded.state = 'waiting'
                              AND wanted.state IN ('waiting', 'searching', 'unavailable')
                                   THEN NULL ELSE wanted.first_search_at END,
            last_search_at = CASE WHEN excluded.state = 'waiting'
                              AND wanted.state IN ('waiting', 'searching', 'unavailable')
                                  THEN NULL ELSE wanted.last_search_at END,
            updated_at = datetime('now')
        RETURNING id
    """

    async def upsert(
        self,
        *,
        media_id: int,
        season: int | None,
        episode: int | None,
        title: str = "",
        air_date: str | None = None,
        state: str = WantedState.WAITING,
    ) -> int:
        # fetch_all, not fetch_one: SQLite only guarantees a RETURNING
        # statement's writes are complete once it has been stepped to the end.
        rows = await self.db.fetch_all(
            self._UPSERT, (media_id, season, episode, title, air_date, str(state))
        )
        return int(rows[0]["id"])

    async def get(self, wanted_id: int) -> Row | None:
        return await self.db.fetch_one("SELECT * FROM wanted WHERE id = ?", (wanted_id,))

    async def set_state(
        self, wanted_id: int, state: str, reason: str | None = None, bump_attempt: bool = False
    ) -> None:
        await self.db.execute(
            f"""UPDATE wanted SET state = ?, reason = ?,
                    search_attempts = search_attempts + {1 if bump_attempt else 0},
                    last_search_at = CASE WHEN ? THEN datetime('now') ELSE last_search_at END,
                    first_search_at = CASE WHEN ? THEN COALESCE(first_search_at, datetime('now'))
                                           ELSE first_search_at END,
                    updated_at = datetime('now')
                WHERE id = ?""",
            (state, reason, int(bump_attempt), int(bump_attempt), wanted_id),
        )

    async def reopen_for_download(self, download_id: int, wanted_id: int | None) -> int:
        removed_reason = f"download #{download_id} was removed"
        cursor = await self.db.conn.execute(
            """UPDATE wanted SET state = ?, reason = ?,
                   updated_at = datetime('now')
               WHERE state = ? AND reason = ?""",
            (WantedState.SEARCHING, removed_reason,
             WantedState.GRABBED, f"grabbed #{download_id}"),
        )
        count = cursor.rowcount
        await cursor.close()
        if count == 0 and wanted_id is not None:
            cursor = await self.db.conn.execute(
                """UPDATE wanted SET state = ?, reason = ?,
                       updated_at = datetime('now')
                   WHERE id = ? AND state IN (?, ?)""",
                (WantedState.SEARCHING, removed_reason, wanted_id,
                 WantedState.GRABBED, WantedState.WAITING),
            )
            count = cursor.rowcount
            await cursor.close()
        return max(count, 0)

    async def reclaim_for_retry(
        self, download_id: int, expected_count: int, legacy_wanted_id: int | None = None
    ) -> int:
        removed_reason = f"download #{download_id} was removed"
        count = await self.db.fetch_value(
            "SELECT COUNT(*) FROM wanted WHERE state = ? AND reason = ?",
            (WantedState.SEARCHING, removed_reason),
            default=0,
        )
        if not count and not expected_count and legacy_wanted_id is not None:
            cursor = await self.db.conn.execute(
                """UPDATE wanted SET state = ?, reason = ?, updated_at = datetime('now')
                   WHERE id = ? AND state = ? AND reason = 'download was removed'""",
                (WantedState.GRABBED, f"grabbed #{download_id}", legacy_wanted_id,
                 WantedState.SEARCHING),
            )
            count = cursor.rowcount
            await cursor.close()
            return max(count, 0)
        if not count or (expected_count and count != expected_count):
            return 0
        cursor = await self.db.conn.execute(
            """UPDATE wanted SET state = ?, reason = ?, updated_at = datetime('now')
               WHERE state = ? AND reason = ?""",
            (WantedState.GRABBED, f"grabbed #{download_id}",
             WantedState.SEARCHING, removed_reason),
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def still_claimed_for_download(self, download_id: int, expected_count: int) -> bool:
        count = await self.db.fetch_value(
            "SELECT COUNT(*) FROM wanted WHERE state = ? AND reason = ?",
            (WantedState.GRABBED, f"grabbed #{download_id}"),
            default=0,
        )
        return count == expected_count

    async def record_search_miss(self, wanted_id: int, reason: str) -> bool:
        cursor = await self.db.conn.execute(
            """UPDATE wanted SET reason = ?, search_attempts = search_attempts + 1,
                   last_search_at = datetime('now'),
                   first_search_at = COALESCE(first_search_at, datetime('now')),
                   updated_at = datetime('now')
               WHERE id = ? AND state IN (?, ?, ?)""",
            (reason, wanted_id, WantedState.WAITING, WantedState.SEARCHING,
             WantedState.UNAVAILABLE),
        )
        changed = cursor.rowcount > 0
        await cursor.close()
        return changed

    async def promote_due(self, now_iso: str) -> int:
        """Move ``waiting`` items whose air date has passed into ``searching``."""
        cursor = await self.db.conn.execute(
            """UPDATE wanted SET state = ?, updated_at = datetime('now')
               WHERE state = ? AND air_date IS NOT NULL AND air_date <= ?""",
            (WantedState.SEARCHING, WantedState.WAITING, now_iso),
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def due_for_search(
        self, limit: int = 200, fresh_days: int | None = None,
        lead_hours: int = 0,
    ) -> list[Row]:
        """Searchable wants, joined to their media row.

        ``fresh_days`` restricts to recently aired items -- the aggressive
        short-interval poll that catches same-day releases. The limit selects
        titles; all matching wants for those titles must be searched together.
        """
        cutoff = (date.today() + timedelta(hours=lead_hours)).isoformat()
        clause = """(
            w.state = ? OR (
                w.state = ? AND (w.last_search_at IS NULL
                                 OR w.last_search_at <= datetime('now', '-1 day'))
            )) AND (w.air_date IS NULL OR w.air_date <= ?)"""
        params: list[Any] = [
            WantedState.SEARCHING, WantedState.UNAVAILABLE, cutoff,
        ]
        if fresh_days is not None:
            clause += " AND w.air_date >= ?"
            params.append((date.today() - timedelta(days=fresh_days)).isoformat())
        params.append(limit)
        selected = await self.db.fetch_all(
            f"""SELECT w.*, m.media_type, m.tmdb_id, m.title AS media_title, m.year,
                       m.poster_path, m.profile, m.monitored, m.ignored
                FROM wanted w JOIN media m ON m.id = w.media_id
                WHERE {clause} AND m.monitored = 1 AND m.ignored = 0
                ORDER BY w.last_search_at IS NOT NULL, w.last_search_at, w.air_date DESC
                LIMIT ?""",
            params,
        )
        if not selected:
            return []
        media_ids = list(dict.fromkeys(int(row["media_id"]) for row in selected))
        marks = ", ".join("?" * len(media_ids))
        return await self.db.fetch_all(
            f"""SELECT w.*, m.media_type, m.tmdb_id, m.title AS media_title, m.year,
                       m.poster_path, m.profile, m.monitored, m.ignored
                FROM wanted w JOIN media m ON m.id = w.media_id
                WHERE w.media_id IN ({marks}) AND {clause}
                  AND m.monitored = 1 AND m.ignored = 0
                ORDER BY w.last_search_at IS NOT NULL, w.last_search_at, w.air_date DESC""",
            [*media_ids, *params[:-1]],
        )

    async def upcoming(self, limit: int = 500) -> list[Row]:
        """What is coming, soonest first -- then TBA, then the aired backlog.

        Ordering matters more than it looks: a library with years of unwatched
        back catalogue would otherwise fill the entire result set with episodes
        from 1999 and push next week's releases past the limit.
        """
        return await self.db.fetch_all(
            """SELECT w.*, m.media_type, m.tmdb_id, m.title AS media_title, m.year,
                      m.poster_path, m.tmdb_status, m.ignored, m.monitored
               FROM wanted w JOIN media m ON m.id = w.media_id
               WHERE w.state IN (?, ?) AND m.ignored = 0
               ORDER BY
                 CASE WHEN w.air_date IS NULL THEN 1
                      WHEN w.air_date >= date('now') THEN 0
                      ELSE 2 END,
                 CASE WHEN w.air_date >= date('now') THEN w.air_date END ASC,
                 w.air_date DESC,
                 m.sort_title, w.season, w.episode
               LIMIT ?""",
            (WantedState.WAITING, WantedState.SEARCHING, limit),
        )

    async def for_media(self, media_id: int) -> list[Row]:
        return await self.db.fetch_all(
            "SELECT * FROM wanted WHERE media_id = ? ORDER BY season, episode", (media_id,)
        )

    async def claimable(self, wanted_ids: list[int]) -> bool:
        if not wanted_ids:
            return False
        marks = ", ".join("?" * len(wanted_ids))
        count = await self.db.fetch_value(
            f"""SELECT COUNT(*) FROM wanted WHERE id IN ({marks})
                AND state IN (?, ?, ?)""",
            [*wanted_ids, WantedState.WAITING, WantedState.SEARCHING,
             WantedState.UNAVAILABLE],
            default=0,
        )
        return count == len(wanted_ids)

    async def set_state_for_media(
        self, media_id: int, state: str, only_states: tuple[str, ...] | None = None
    ) -> None:
        params: list[Any] = [state, media_id]
        clause = ""
        if only_states:
            clause = f"AND state IN ({', '.join('?' * len(only_states))})"
            params.extend(only_states)
        await self.db.execute(
            f"UPDATE wanted SET state = ?, updated_at = datetime('now') "
            f"WHERE media_id = ? {clause}",
            params,
        )

    async def mark_covered(self, media_id: int, season: int, episodes: list[int] | None) -> None:
        """Mark a season (or specific episodes) as satisfied by a grab."""
        if episodes:
            placeholders = ", ".join("?" * len(episodes))
            await self.db.execute(
                f"""UPDATE wanted SET state = ?, updated_at = datetime('now')
                    WHERE media_id = ? AND season = ? AND episode IN ({placeholders})
                      AND state IN (?, ?, ?)""",
                [WantedState.GRABBED, media_id, season, *episodes,
                 WantedState.WAITING, WantedState.SEARCHING, WantedState.UNAVAILABLE],
            )
        else:
            await self.db.execute(
                """UPDATE wanted SET state = ?, updated_at = datetime('now')
                   WHERE media_id = ? AND season = ? AND state IN (?, ?, ?)""",
                (WantedState.GRABBED, media_id, season,
                 WantedState.WAITING, WantedState.SEARCHING, WantedState.UNAVAILABLE),
            )

    async def retire(
        self, media_id: int, keys: set[tuple[int, int]], reason: str
    ) -> int:
        """Stand down wants that are no longer wanted.

        Used when the rules change underneath us -- a season turns out to be
        watched, or files appeared in Plex -- so the pending list reflects
        reality instead of accumulating.
        """
        if not keys:
            return 0
        rows = [
            (WantedState.IGNORED, reason, media_id, season, episode)
            for season, episode in keys
        ]
        await self.db.execute_many(
            """UPDATE wanted SET state = ?, reason = ?, updated_at = datetime('now')
               WHERE media_id = ? AND season = ? AND episode = ? AND state IN
                     ('waiting', 'searching', 'unavailable')""",
            rows,
        )
        return len(keys)

    async def mark_present(self, media_id: int, keys: set[tuple[int, int]]) -> None:
        """Wants whose files have appeared in Plex are no longer outstanding.

        One batched statement rather than reading every want for the title and
        writing back the ones that matched -- the calendar calls this per show
        on every pass.
        """
        if not keys:
            return
        await self.db.execute_many(
            """UPDATE wanted SET state = ?, reason = 'present in library',
                   updated_at = datetime('now')
               WHERE media_id = ? AND season = ? AND episode = ?
                 AND state NOT IN (?, ?)""",
            [
                (WantedState.DOWNLOADED, media_id, season, episode,
                 WantedState.DOWNLOADED, WantedState.IGNORED)
                for season, episode in keys
            ],
        )

    async def mark_watched(self, wanted_ids: list[int], watched: bool = True) -> int:
        """Record that the user has already seen these episodes (or undo it)."""
        if not wanted_ids:
            return 0
        marks = ", ".join("?" * len(wanted_ids))
        if watched:
            params: list[Any] = [WantedState.WATCHED, "marked as already seen", *wanted_ids]
            clause = ""
        else:
            params = [WantedState.SEARCHING, None, *wanted_ids, WantedState.WATCHED]
            clause = "AND state = ?"
        cursor = await self.db.conn.execute(
            f"""UPDATE wanted SET state = ?, reason = ?, updated_at = datetime('now')
                WHERE id IN ({marks}) {clause}""",
            params,
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def mark_watched_for_media(
        self, media_id: int, season: int | None = None, up_to_episode: int | None = None
    ) -> int:
        """Mark a whole title, a season, or everything up to an episode as seen."""
        clauses = ["media_id = ?", "state IN ('waiting', 'searching', 'unavailable', 'ignored')"]
        params: list[Any] = [WantedState.WATCHED, "marked as already seen", media_id]
        if season is not None:
            clauses.append("season = ?")
            params.append(season)
            if up_to_episode is not None:
                clauses.append("episode <= ?")
                params.append(up_to_episode)
        cursor = await self.db.conn.execute(
            f"""UPDATE wanted SET state = ?, reason = ?, updated_at = datetime('now')
                WHERE {' AND '.join(clauses)}""",
            params,
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def clear_ignored(self, media_id: int | None = None) -> int:
        """Bring ignored wants back into play."""
        clause, params = "", [WantedState.SEARCHING, WantedState.IGNORED]
        if media_id is not None:
            clause = "AND media_id = ?"
            params.append(media_id)
        cursor = await self.db.conn.execute(
            f"""UPDATE wanted SET state = ?, reason = NULL, updated_at = datetime('now')
                WHERE state = ? {clause}""",
            params,
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def expire_stale(
        self, tv_days: int, movie_days: int, max_attempts: int = 60,
        fresh_days: int = 7,
    ) -> int:
        """Slow polling for wants we have hunted for and never found.

        The clock runs from when *we* started looking, not from the air date --
        otherwise every back-catalogue episode of a newly followed series would
        be written off before a single search ran.
        """
        cursor = await self.db.conn.execute(
            """UPDATE wanted SET state = ?, reason = 'searched repeatedly, no release found',
                   updated_at = datetime('now')
                WHERE state = ? AND search_attempts > 0 AND first_search_at IS NOT NULL
                  AND (air_date IS NULL OR air_date <= date('now'))
                  AND (
                    (search_attempts >= ?
                     AND first_search_at < datetime('now', ?)
                     AND (air_date IS NULL OR air_date < date('now', ?)))
                    OR (episode IS NOT NULL AND first_search_at < datetime('now', ?))
                    OR (episode IS NULL AND first_search_at < datetime('now', ?))
                 )""",
            (WantedState.UNAVAILABLE, WantedState.SEARCHING, max_attempts,
             f"-{fresh_days} days", f"-{fresh_days} days",
             f"-{tv_days} days", f"-{movie_days} days"),
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def counts_by_media(self) -> dict[int, dict[str, int]]:
        """Per-title want totals, for the library list.

        Without this the Library rows show nothing that changes when you mark
        episodes as seen, which makes a working action look broken.
        """
        rows = await self.db.fetch_all(
            """SELECT media_id,
                      SUM(state IN ('waiting', 'searching', 'unavailable')) AS outstanding,
                      SUM(state = 'watched') AS seen,
                      SUM(state IN ('grabbed', 'downloaded')) AS have,
                      COUNT(*) AS total
               FROM wanted GROUP BY media_id"""
        )
        return {
            int(r["media_id"]): {
                "outstanding": int(r["outstanding"] or 0),
                "seen": int(r["seen"] or 0),
                "have": int(r["have"] or 0),
                "total": int(r["total"] or 0),
            }
            for r in rows
        }

    async def counts_by_state(self) -> dict[str, int]:
        rows = await self.db.fetch_all("SELECT state, COUNT(*) AS n FROM wanted GROUP BY state")
        return {r["state"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
class DownloadRepo(BaseRepo):
    """Grabs: pending approval, queued, in flight, finished."""

    COLUMNS = (
        "media_id", "wanted_id", "display_title", "release_name", "indexer", "indexer_id",
        "info_hash", "download_url", "size_bytes", "season", "episode_from", "episode_to",
        "is_season_pack", "resolution", "source", "dynamic_range", "video_codec", "audio",
        "release_group", "score", "seeders", "state", "wanted_count",
    )

    async def create(self, **values: Any) -> int:
        payload = {c: values.get(c) for c in self.COLUMNS}
        payload["display_title"] = values["display_title"]  # required, never NULL
        payload["state"] = values.get("state", DownloadState.PENDING_APPROVAL)
        payload["is_season_pack"] = int(bool(values.get("is_season_pack")))
        payload["size_bytes"] = float(values.get("size_bytes") or 0)
        payload["score"] = int(values.get("score") or 0)
        payload["seeders"] = int(values.get("seeders") or 0)
        payload["wanted_count"] = int(values.get("wanted_count") or 0)
        for key in ("release_name", "indexer", "indexer_id"):
            payload[key] = values.get(key) or ""
        cols = ", ".join(payload)
        marks = ", ".join("?" * len(payload))
        return await self.db.execute(
            f"INSERT INTO downloads ({cols}) VALUES ({marks})", list(payload.values())
        )

    async def get(self, download_id: int) -> Row | None:
        return await self.db.fetch_one("SELECT * FROM downloads WHERE id = ?", (download_id,))

    async def by_hash(self, info_hash: str) -> Row | None:
        return await self.db.fetch_one(
            "SELECT * FROM downloads WHERE info_hash = ?", (info_hash.lower(),)
        )

    async def by_release(self, indexer: str, indexer_id: str) -> Row | None:
        return await self.db.fetch_one(
            "SELECT * FROM downloads WHERE indexer = ? AND indexer_id = ?", (indexer, indexer_id)
        )

    async def by_ids(self, ids: list[int]) -> list[Row]:
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        return await self.db.fetch_all(
            f"SELECT * FROM downloads WHERE id IN ({marks})", list(ids)
        )

    async def known_release_keys(self) -> set[tuple[str, str]]:
        """Every release we have already acted on -- the de-dupe set."""
        rows = await self.db.fetch_all(
            "SELECT indexer, indexer_id FROM downloads WHERE indexer_id != '' "
            "UNION SELECT indexer, indexer_id FROM blocklist"
        )
        return {(r["indexer"], str(r["indexer_id"])) for r in rows}

    async def list_by_state(self, *states: str) -> list[Row]:
        marks = ", ".join("?" * len(states))
        return await self.db.fetch_all(
            f"""SELECT d.*, m.poster_path, m.tmdb_id, m.media_type, m.title AS media_title
                FROM downloads d LEFT JOIN media m ON m.id = d.media_id
                WHERE d.state IN ({marks}) AND d.archived = 0 ORDER BY d.created_at""",
            list(states),
        )

    async def dashboard(self, limit: int = 300) -> list[Row]:
        return await self.db.fetch_all(
            """SELECT d.*, m.poster_path, m.tmdb_id, m.media_type, m.title AS media_title
               FROM downloads d LEFT JOIN media m ON m.id = d.media_id
               WHERE d.archived = 0 AND d.state != ?
               ORDER BY
                   CASE d.state
                       WHEN 'downloading' THEN 0
                       WHEN 'pending_approval' THEN 1
                       WHEN 'queued' THEN 2
                       WHEN 'no_space' THEN 3
                       WHEN 'failed' THEN 4
                       ELSE 5 END,
                   d.created_at DESC
               LIMIT ?""",
            (DownloadState.DENIED, limit),
        )

    async def history(self, limit: int = 200, offset: int = 0, query: str = "") -> list[Row]:
        clause, params = "", []
        if query:
            clause = "AND d.display_title LIKE ?"
            params.append(f"%{query}%")
        params.extend([limit, offset])
        return await self.db.fetch_all(
            f"""SELECT d.*, m.poster_path, m.tmdb_id, m.media_type
                FROM downloads d LEFT JOIN media m ON m.id = d.media_id
                WHERE d.state IN ('completed', 'denied', 'failed', 'cancelled') {clause}
                ORDER BY COALESCE(d.completed_at, d.updated_at) DESC LIMIT ? OFFSET ?""",
            params,
        )

    async def active_count(self) -> int:
        return await self.db.fetch_value(
            "SELECT COUNT(*) FROM downloads WHERE state = ? AND archived = 0",
            (DownloadState.DOWNLOADING,),
            default=0,
        )

    async def set_state(
        self, download_id: int, state: str, *, error: str | None = None,
        save_path: str | None = None,
    ) -> None:
        stamps = ""
        if state == DownloadState.DOWNLOADING:
            stamps = ", grabbed_at = COALESCE(grabbed_at, datetime('now'))"
        elif state == DownloadState.COMPLETED:
            stamps = ", completed_at = datetime('now'), progress = 1.0, eta_seconds = 0"
        await self.db.execute(
            f"""UPDATE downloads SET state = ?, error = ?,
                    save_path = COALESCE(?, save_path),
                    updated_at = datetime('now'){stamps}
                WHERE id = ?""",
            (state, error, save_path, download_id),
        )

    async def mark_missing(self, download_id: int) -> None:
        """Note that the client did not have this download. Idempotent -- the
        first sighting is the one that starts the clock."""
        await self.db.execute(
            "UPDATE downloads SET missing_since = COALESCE(missing_since, datetime('now')) "
            "WHERE id = ?",
            (download_id,),
        )

    async def clear_missing(self, download_id: int) -> None:
        await self.db.execute(
            "UPDATE downloads SET missing_since = NULL WHERE id = ?", (download_id,)
        )

    async def missing_beyond(self, seconds: int) -> list[Row]:
        """Downloads the client has not had for longer than the grace window."""
        return await self.db.fetch_all(
            """SELECT * FROM downloads
               WHERE state = ? AND archived = 0 AND missing_since IS NOT NULL
                 AND missing_since <= datetime('now', ?)""",
            (DownloadState.DOWNLOADING, f"-{int(seconds)} seconds"),
        )

    async def set_hash(self, download_id: int, info_hash: str) -> None:
        await self.db.execute(
            "UPDATE downloads SET info_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (info_hash.lower(), download_id),
        )

    async def set_verified_size(
        self, download_id: int, size_bytes: float, *, require_approval: bool = False
    ) -> None:
        if require_approval:
            await self.db.execute(
                """UPDATE downloads SET size_bytes = ?, state = ?,
                       updated_at = datetime('now') WHERE id = ?""",
                (size_bytes, DownloadState.PENDING_APPROVAL, download_id),
            )
        else:
            await self.db.execute(
                "UPDATE downloads SET size_bytes = ?, updated_at = datetime('now') WHERE id = ?",
                (size_bytes, download_id),
            )

    async def update_progress(self, rows: list[tuple[float, int, float, str, int]]) -> None:
        """Batch progress write: (progress, eta, speed, content_path, id)."""
        await self.db.execute_many(
            """UPDATE downloads SET progress = ?, eta_seconds = ?, speed_bps = ?,
                   content_path = COALESCE(?, content_path), updated_at = datetime('now')
               WHERE id = ?""",
            rows,
        )

    async def approve(self, download_id: int) -> bool:
        cursor = await self.db.conn.execute(
            "UPDATE downloads SET state = ?, updated_at = datetime('now') "
            "WHERE id = ? AND state = ? AND archived = 0",
            (DownloadState.QUEUED, download_id, DownloadState.PENDING_APPROVAL),
        )
        changed = cursor.rowcount > 0
        await cursor.close()
        return changed

    async def approve_many(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ", ".join("?" * len(ids))
        cursor = await self.db.conn.execute(
            f"UPDATE downloads SET state = ?, updated_at = datetime('now') "
            f"WHERE id IN ({marks}) AND state = ? AND archived = 0",
            [DownloadState.QUEUED, *ids, DownloadState.PENDING_APPROVAL],
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def deny_many(self, ids: list[int]) -> list[Row]:
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        return await self.db.fetch_all(
            f"UPDATE downloads SET state = ?, updated_at = datetime('now') "
            f"WHERE id IN ({marks}) AND state = ? AND archived = 0 "
            f"RETURNING id, indexer, indexer_id, display_title",
            [DownloadState.DENIED, *ids, DownloadState.PENDING_APPROVAL],
        )

    async def pending_groups(self) -> list[Row]:
        return await self.db.fetch_all(
            """SELECT d.*, m.poster_path, m.tmdb_id, m.media_type, m.title AS media_title
               FROM downloads d LEFT JOIN media m ON m.id = d.media_id
               WHERE d.state = ? AND d.archived = 0
               ORDER BY m.sort_title, d.season, d.episode_from""",
            (DownloadState.PENDING_APPROVAL,),
        )

    async def queued(self) -> list[Row]:
        return await self.db.fetch_all(
            """SELECT * FROM downloads WHERE state IN (?, ?) AND archived = 0
               ORDER BY score DESC, created_at""",
            (DownloadState.QUEUED, DownloadState.NO_SPACE),
        )

    async def in_flight(self) -> list[Row]:
        return await self.db.fetch_all(
            "SELECT * FROM downloads WHERE state = ? AND archived = 0",
            (DownloadState.DOWNLOADING,),
        )

    async def completed(self, watched_only: bool = False) -> list[Row]:
        clause = "AND d.watched = 1" if watched_only else ""
        return await self.db.fetch_all(
            f"""SELECT d.*, m.poster_path, m.tmdb_id, m.media_type
                FROM downloads d LEFT JOIN media m ON m.id = d.media_id
                WHERE d.state = ? AND d.archived = 0 {clause}
                ORDER BY d.size_bytes DESC""",
            (DownloadState.COMPLETED,),
        )

    async def set_watched(self, download_id: int, watched: bool) -> None:
        await self.db.execute(
            "UPDATE downloads SET watched = ?, updated_at = datetime('now') WHERE id = ?",
            (int(watched), download_id),
        )

    async def set_watched_bulk(self, rows: list[tuple[int, int]]) -> None:
        await self.db.execute_many(
            "UPDATE downloads SET watched = ?, updated_at = datetime('now') WHERE id = ?", rows
        )

    async def archive(self, download_id: int) -> None:
        await self.db.execute(
            "UPDATE downloads SET archived = 1, updated_at = datetime('now') WHERE id = ?",
            (download_id,),
        )

    async def delete(self, download_id: int) -> None:
        await self.db.execute("DELETE FROM downloads WHERE id = ?", (download_id,))

    async def purge_history(self) -> int:
        """Drop every dead-end row. Returns how many actually went."""
        cursor = await self.db.conn.execute(
            "DELETE FROM downloads WHERE state IN (?, ?, ?)",
            (DownloadState.DENIED, DownloadState.FAILED, DownloadState.CANCELLED),
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def totals(self) -> Row:
        row = await self.db.fetch_one(
            """SELECT
                 COUNT(*) AS total,
                 SUM(state = 'downloading') AS downloading,
                 SUM(state = 'pending_approval') AS pending,
                 SUM(state = 'queued') AS queued,
                 SUM(state = 'completed') AS completed,
                 SUM(state = 'failed') AS failed,
                 SUM(state = 'no_space') AS no_space,
                 SUM(CASE WHEN state = 'completed' THEN size_bytes ELSE 0 END) AS completed_bytes
               FROM downloads WHERE archived = 0"""
        )
        return {k: (v or 0) for k, v in (row or {}).items()}


# ---------------------------------------------------------------------------
class BlocklistRepo(BaseRepo):
    async def add(
        self, indexer: str, indexer_id: str, title: str = "", reason: str = "",
        info_hash: str | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO blocklist (indexer, indexer_id, info_hash, title, reason)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (indexer, indexer_id) DO UPDATE SET reason = excluded.reason""",
            (indexer, str(indexer_id), info_hash, title, reason),
        )

    async def list_all(self, limit: int = 500) -> list[Row]:
        return await self.db.fetch_all(
            "SELECT * FROM blocklist ORDER BY id DESC LIMIT ?", (limit,)
        )

    async def remove(self, blocklist_id: int) -> None:
        await self.db.execute("DELETE FROM blocklist WHERE id = ?", (blocklist_id,))


# ---------------------------------------------------------------------------
class LibraryRepo(BaseRepo):
    """Cached view of the Plex library."""

    async def replace_all(self, items: list[LibraryItem]) -> int:
        """Swap the whole index atomically -- no half-updated state is visible."""
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM library_items")
            await conn.executemany(
                """INSERT INTO library_items
                     (kind, rating_key, tmdb_id, show_tmdb_id, title, season, episode,
                      watched, view_count, resolution, file_path, size_bytes, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (rating_key) DO UPDATE SET
                     watched = excluded.watched, view_count = excluded.view_count,
                     updated_at = datetime('now')""",
                [
                    (i.kind, i.rating_key, i.tmdb_id, i.show_tmdb_id, i.title, i.season,
                     i.episode, int(i.watched), i.view_count, i.resolution, i.file_path,
                     i.size_bytes, i.added_at)
                    for i in items
                ],
            )
        return len(items)

    async def have_episodes(self, show_tmdb_id: str) -> set[tuple[int, int]]:
        rows = await self.db.fetch_all(
            "SELECT season, episode FROM library_items "
            "WHERE kind = 'episode' AND show_tmdb_id = ? AND season IS NOT NULL",
            (str(show_tmdb_id),),
        )
        return {(r["season"], r["episode"]) for r in rows if r["episode"] is not None}

    async def watched_episodes(self, show_tmdb_id: str) -> set[tuple[int, int]]:
        rows = await self.db.fetch_all(
            "SELECT season, episode FROM library_items "
            "WHERE kind = 'episode' AND show_tmdb_id = ? AND watched = 1",
            (str(show_tmdb_id),),
        )
        return {(r["season"], r["episode"]) for r in rows if r["episode"] is not None}

    async def has_movie(self, tmdb_id: str) -> Row | None:
        return await self.db.fetch_one(
            "SELECT * FROM library_items WHERE kind = 'movie' AND tmdb_id = ?", (str(tmdb_id),)
        )

    async def movie_watched_map(self) -> dict[str, bool]:
        rows = await self.db.fetch_all(
            "SELECT tmdb_id, watched FROM library_items WHERE kind = 'movie' AND tmdb_id IS NOT NULL"
        )
        return {str(r["tmdb_id"]): bool(r["watched"]) for r in rows}

    async def season_progress(self, tmdb_ids: set[str]) -> dict[str, dict[int, dict[str, int]]]:
        """Episodes held and watched, per show, per season.

        Answers "do I already own an earlier season of this?", which is what
        separates *the next season of something you are watching* from *a new
        title you added*. One grouped query for every pending approval rather
        than a lookup per card.
        """
        if not tmdb_ids:
            return {}
        marks = ", ".join("?" * len(tmdb_ids))
        rows = await self.db.fetch_all(
            f"""SELECT show_tmdb_id, season,
                       COUNT(*) AS episodes, SUM(watched) AS watched
                FROM library_items
                WHERE kind = 'episode' AND season IS NOT NULL
                  AND show_tmdb_id IN ({marks})
                GROUP BY show_tmdb_id, season""",
            [str(i) for i in tmdb_ids],
        )
        out: dict[str, dict[int, dict[str, int]]] = {}
        for row in rows:
            out.setdefault(str(row["show_tmdb_id"]), {})[int(row["season"])] = {
                "episodes": int(row["episodes"] or 0),
                "watched": int(row["watched"] or 0),
            }
        return out

    async def show_progress(self) -> dict[str, dict[str, int]]:
        """Per-show episode/watched counts, for the library view."""
        rows = await self.db.fetch_all(
            """SELECT show_tmdb_id, COUNT(*) AS episodes, SUM(watched) AS watched
               FROM library_items WHERE kind = 'episode' AND show_tmdb_id IS NOT NULL
               GROUP BY show_tmdb_id"""
        )
        return {
            str(r["show_tmdb_id"]): {"episodes": r["episodes"], "watched": r["watched"] or 0}
            for r in rows
        }

    async def unmatched(self, limit: int = 50) -> list[Row]:
        """Library entries Plex has not matched to a TMDB id.

        Every "do I already have this?" decision is a TMDB-id lookup against
        this table, so an unmatched entry is invisible to de-duplication:
        Conduit will happily pay to fetch something already on the disk. Rare,
        but expensive on a private tracker, so it is worth showing rather than
        leaving to be discovered on an approval card.
        """
        return await self.db.fetch_all(
            """SELECT CASE WHEN kind = 'movie' THEN 'movie' ELSE 'show' END AS kind,
                      title,
                      SUM(kind = 'episode') AS episodes,
                      MIN(rating_key) AS rating_key
               FROM library_items
               WHERE (kind = 'movie'   AND tmdb_id      IS NULL)
                  OR (kind = 'show'    AND tmdb_id      IS NULL)
                  OR (kind = 'episode' AND show_tmdb_id IS NULL)
               GROUP BY 1, 2
               ORDER BY episodes DESC, title
               LIMIT ?""",
            (limit,),
        )

    async def watched_show_tmdb_ids(self) -> set[str]:
        rows = await self.db.fetch_all(
            "SELECT DISTINCT show_tmdb_id FROM library_items "
            "WHERE kind = 'episode' AND watched = 1 AND show_tmdb_id IS NOT NULL"
        )
        return {str(r["show_tmdb_id"]) for r in rows}


# ---------------------------------------------------------------------------
class EventRepo(BaseRepo):
    async def add(
        self,
        category: str,
        message: str,
        *,
        level: str = EventLevel.INFO,
        media_id: int | None = None,
        download_id: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        return await self.db.execute(
            """INSERT INTO events (level, category, message, media_id, download_id, data)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (level, category, message, media_id, download_id,
             json.dumps(data) if data else None),
        )

    async def recent(
        self, limit: int = 100, category: str | None = None, media_id: int | None = None,
        since_id: int = 0, download_id: int | None = None,
    ) -> list[Row]:
        clauses, params = ["id > ?"], [since_id]
        if category:
            clauses.append("category = ?")
            params.append(category)
        if media_id is not None:
            clauses.append("media_id = ?")
            params.append(media_id)
        if download_id is not None:
            clauses.append("download_id = ?")
            params.append(download_id)
        params.append(limit)
        rows = await self.db.fetch_all(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", params
        )
        for row in rows:
            if row.get("data"):
                try:
                    row["data"] = json.loads(row["data"])
                except json.JSONDecodeError:
                    row["data"] = None
        return rows

    async def prune(self, keep: int = 5000) -> int:
        cursor = await self.db.conn.execute(
            "DELETE FROM events WHERE id <= (SELECT MAX(id) - ? FROM events)", (keep,)
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)


# ---------------------------------------------------------------------------
class CacheRepo(BaseRepo):
    """Persistent, TTL'd cache for indexer and metadata responses."""

    async def get(self, key: str) -> Any | None:
        row = await self.db.fetch_one(
            "SELECT payload FROM search_cache WHERE cache_key = ? AND expires_at > datetime('now')",
            (key,),
        )
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        await self.db.execute(
            """INSERT INTO search_cache (cache_key, payload, expires_at)
               VALUES (?, ?, datetime('now', ?))
               ON CONFLICT (cache_key) DO UPDATE SET
                 payload = excluded.payload, expires_at = excluded.expires_at,
                 created_at = datetime('now')""",
            # A signed modifier: "+600 seconds" / "-10 seconds". Writing
            # "+-10 seconds" makes SQLite return NULL, not an error.
            (key, json.dumps(value), f"{int(ttl_seconds):+d} seconds"),
        )

    async def purge_expired(self) -> int:
        cursor = await self.db.conn.execute(
            "DELETE FROM search_cache WHERE expires_at <= datetime('now')"
        )
        count = cursor.rowcount
        await cursor.close()
        return max(count, 0)

    async def clear(self) -> None:
        await self.db.execute("DELETE FROM search_cache")


# ---------------------------------------------------------------------------
class TaskRepo(BaseRepo):
    """Per-task health, so failures are visible instead of silent."""

    async def start(self, name: str) -> None:
        await self.db.execute(
            """INSERT INTO task_runs (name, last_start, run_count)
               VALUES (?, datetime('now'), 1)
               ON CONFLICT (name) DO UPDATE SET
                 last_start = datetime('now'), run_count = task_runs.run_count + 1""",
            (name,),
        )

    async def finish(self, name: str, duration: float, error: str | None = None) -> None:
        await self.db.execute(
            """UPDATE task_runs SET last_finish = datetime('now'), last_duration = ?,
                   last_error = ?, error_count = error_count + ?
               WHERE name = ?""",
            (duration, error, 1 if error else 0, name),
        )

    async def all(self) -> list[Row]:
        return await self.db.fetch_all("SELECT * FROM task_runs ORDER BY name")


# ---------------------------------------------------------------------------
class Repos:
    """Bundle handed to every service."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.media = MediaRepo(db)
        self.wanted = WantedRepo(db)
        self.downloads = DownloadRepo(db)
        self.blocklist = BlocklistRepo(db)
        self.library = LibraryRepo(db)
        self.events = EventRepo(db)
        self.cache = CacheRepo(db)
        self.tasks = TaskRepo(db)
