"""Versioned schema migrations.

The reference project detected drift with ``PRAGMA table_info`` and bolted on
``ALTER TABLE`` calls inline, which meant nobody could tell what version a
database was at. Here each migration is a numbered, immutable step recorded in
``schema_migrations``; upgrades are ordered, idempotent and auditable.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = []


def _migration(version: int, name: str, sql: str) -> None:
    MIGRATIONS.append((version, name, sql))


_migration(
    1,
    "initial",
    """
    -- A tracked title. One row per movie or series.
    CREATE TABLE media (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        media_type    TEXT NOT NULL CHECK (media_type IN ('movie', 'show')),
        tmdb_id       TEXT,
        imdb_id       TEXT,
        tvdb_id       TEXT,
        title         TEXT NOT NULL,
        sort_title    TEXT NOT NULL DEFAULT '',
        year          INTEGER,
        overview      TEXT,
        poster_path   TEXT,
        backdrop_path TEXT,
        tmdb_status   TEXT,
        profile       TEXT,
        source        TEXT NOT NULL DEFAULT 'watchlist',
        monitored     INTEGER NOT NULL DEFAULT 1,
        ignored       INTEGER NOT NULL DEFAULT 0,
        plex_rating_key TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX idx_media_tmdb ON media(media_type, tmdb_id) WHERE tmdb_id IS NOT NULL;
    CREATE INDEX idx_media_sort ON media(sort_title);

    -- Something we want but do not have: a movie, a whole season, or an episode.
    CREATE TABLE wanted (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        media_id       INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        season         INTEGER,
        episode        INTEGER,
        title          TEXT NOT NULL DEFAULT '',
        air_date       TEXT,
        state          TEXT NOT NULL DEFAULT 'waiting',
        reason         TEXT,
        search_attempts INTEGER NOT NULL DEFAULT 0,
        last_search_at TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX idx_wanted_unique
        ON wanted(media_id, IFNULL(season, -1), IFNULL(episode, -1));
    CREATE INDEX idx_wanted_state ON wanted(state, air_date);

    -- A release we decided to grab, and everything that happened to it.
    CREATE TABLE downloads (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        media_id         INTEGER REFERENCES media(id) ON DELETE SET NULL,
        wanted_id        INTEGER REFERENCES wanted(id) ON DELETE SET NULL,
        display_title    TEXT NOT NULL,
        release_name     TEXT NOT NULL DEFAULT '',
        indexer          TEXT NOT NULL DEFAULT '',
        indexer_id       TEXT NOT NULL DEFAULT '',
        info_hash        TEXT,
        download_url     TEXT,
        size_bytes       REAL NOT NULL DEFAULT 0,
        season           INTEGER,
        episode_from     INTEGER,
        episode_to       INTEGER,
        is_season_pack   INTEGER NOT NULL DEFAULT 0,
        resolution       TEXT,
        source           TEXT,
        dynamic_range    TEXT,
        video_codec      TEXT,
        audio            TEXT,
        release_group    TEXT,
        score            INTEGER NOT NULL DEFAULT 0,
        seeders          INTEGER NOT NULL DEFAULT 0,
        state            TEXT NOT NULL DEFAULT 'pending_approval',
        progress         REAL NOT NULL DEFAULT 0,
        eta_seconds      INTEGER NOT NULL DEFAULT -1,
        speed_bps        REAL NOT NULL DEFAULT 0,
        save_path        TEXT,
        content_path     TEXT,
        error            TEXT,
        watched          INTEGER NOT NULL DEFAULT 0,
        archived         INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
        grabbed_at       TEXT,
        completed_at     TEXT
    );
    CREATE INDEX idx_downloads_state ON downloads(state);
    CREATE INDEX idx_downloads_hash ON downloads(info_hash);
    CREATE UNIQUE INDEX idx_downloads_release ON downloads(indexer, indexer_id)
        WHERE indexer_id != '';

    -- Releases we must never grab again (failed, denied, manually blocked).
    CREATE TABLE blocklist (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        indexer    TEXT NOT NULL,
        indexer_id TEXT NOT NULL,
        info_hash  TEXT,
        title      TEXT,
        reason     TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX idx_blocklist_release ON blocklist(indexer, indexer_id);

    -- Snapshot of what Plex actually has, so decisions never need a live call.
    CREATE TABLE library_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL CHECK (kind IN ('movie', 'show', 'episode')),
        rating_key  TEXT NOT NULL,
        tmdb_id     TEXT,
        show_tmdb_id TEXT,
        title       TEXT NOT NULL DEFAULT '',
        season      INTEGER,
        episode     INTEGER,
        watched     INTEGER NOT NULL DEFAULT 0,
        view_count  INTEGER NOT NULL DEFAULT 0,
        resolution  TEXT,
        file_path   TEXT,
        size_bytes  REAL NOT NULL DEFAULT 0,
        added_at    TEXT,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX idx_library_rating_key ON library_items(rating_key);
    CREATE INDEX idx_library_lookup ON library_items(show_tmdb_id, season, episode);
    CREATE INDEX idx_library_tmdb ON library_items(kind, tmdb_id);

    -- Append-only activity timeline, surfaced in the dashboard.
    CREATE TABLE events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL DEFAULT (datetime('now')),
        level       TEXT NOT NULL DEFAULT 'info',
        category    TEXT NOT NULL,
        message     TEXT NOT NULL,
        media_id    INTEGER,
        download_id INTEGER,
        data        TEXT
    );
    CREATE INDEX idx_events_ts ON events(id DESC);
    CREATE INDEX idx_events_media ON events(media_id, id DESC);

    -- Key/value store for run state and counters.
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """,
)

_migration(
    2,
    "search_cache",
    """
    -- Short-lived cache of indexer responses. Survives restarts, which the
    -- reference project's in-process dict could not, so a crash loop no longer
    -- means re-querying the tracker for everything.
    CREATE TABLE search_cache (
        cache_key  TEXT PRIMARY KEY,
        payload    TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL
    );
    CREATE INDEX idx_search_cache_expiry ON search_cache(expires_at);
    """,
)

_migration(
    3,
    "task_runs",
    """
    -- Per-task health, so the dashboard can show which background job is
    -- failing instead of leaving the user to guess.
    CREATE TABLE task_runs (
        name          TEXT PRIMARY KEY,
        last_start    TEXT,
        last_finish   TEXT,
        last_duration REAL,
        last_error    TEXT,
        run_count     INTEGER NOT NULL DEFAULT 0,
        error_count   INTEGER NOT NULL DEFAULT 0,
        enabled       INTEGER NOT NULL DEFAULT 1
    );
    """,
)

_migration(
    4,
    "download_missing_since",
    """
    -- When a tracked download was first not found in the client. qBittorrent
    -- adds by URL asynchronously and empties its list briefly on restart, so
    -- one missed sighting is not evidence of anything; giving up needs to be
    -- based on how *long* it has been gone.
    ALTER TABLE downloads ADD COLUMN missing_since TEXT;
    """,
)

_migration(
    5,
    "wanted_first_search_at",
    """
    ALTER TABLE wanted ADD COLUMN first_search_at TEXT;
    UPDATE wanted SET first_search_at = last_search_at
        WHERE search_attempts > 0 AND last_search_at IS NOT NULL;
    """,
)

_migration(
    6,
    "download_wanted_count",
    """
    ALTER TABLE downloads ADD COLUMN wanted_count INTEGER NOT NULL DEFAULT 0;
    UPDATE downloads SET wanted_count = (
        SELECT COUNT(*) FROM wanted
        WHERE wanted.reason = 'grabbed #' || downloads.id
    );
    """,
)

SCHEMA_VERSION = max(v for v, _, _ in MIGRATIONS)
