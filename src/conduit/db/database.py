"""Async SQLite access layer.

One connection, WAL journaling, foreign keys on, and a write lock. aiosqlite
already serialises statements onto a single worker thread, so a single
connection is both correct and the fastest option at this scale -- and it
removes the thread-local connection juggling the reference project needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from ..logs import get_logger
from .schema import MIGRATIONS, SCHEMA_VERSION

log = get_logger("db")

Row = dict[str, Any]


class Database:
    """Thin, typed-enough wrapper around a single aiosqlite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._tx_lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        try:
            await self._migrate()
        except Exception:
            # Never leave the worker thread running behind a failed boot.
            await self.close()
            raise

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self._conn

    # -- migrations ---------------------------------------------------------
    async def _migrate(self) -> None:
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name    TEXT NOT NULL,
                   applied_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
        )
        cursor = await self.conn.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in await cursor.fetchall()}
        await cursor.close()

        pending = [m for m in sorted(MIGRATIONS) if m[0] not in applied]
        if not pending:
            log.debug("schema up to date", extra={"version": SCHEMA_VERSION})
            return

        for version, name, sql in pending:
            log.info("applying migration", extra={"version": version, "migration": name})
            # BEGIN/COMMIT live inside the script: sqlite3.executescript issues
            # its own implicit commit, so an outer transaction would be closed
            # from under us and the matching COMMIT would fail.
            script = (
                "BEGIN;\n"
                f"{sql}\n"
                "INSERT INTO schema_migrations (version, name) VALUES "
                f"({int(version)}, '{name.replace(chr(39), chr(39) * 2)}');\n"
                "COMMIT;"
            )
            try:
                await self.conn.executescript(script)
            except Exception:
                with contextlib.suppress(Exception):
                    await self.conn.execute("ROLLBACK")
                log.exception("migration failed", extra={"version": version, "migration": name})
                raise

    # -- queries ------------------------------------------------------------
    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else default

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a statement and return ``lastrowid`` (or rowcount for updates)."""
        cursor = await self.conn.execute(sql, params)
        result = cursor.lastrowid or cursor.rowcount
        await cursor.close()
        return result

    async def execute_many(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        rows = list(seq)
        if not rows:
            return
        await self.conn.executemany(sql, rows)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Explicit transaction. Nested use is not supported (and not needed).

        The lock keeps two coroutines from issuing ``BEGIN`` on the shared
        connection. Cancellation is a ``BaseException``, so it must roll back
        too -- otherwise the connection stays inside an open transaction that
        every later write silently joins and never commits.
        """
        async with self._tx_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.conn.execute("ROLLBACK"))
                raise
            else:
                await self.conn.execute("COMMIT")

    # -- helpers ------------------------------------------------------------
    async def get_meta(self, key: str, default: Any = None) -> Any:
        raw = await self.fetch_value("SELECT value FROM meta WHERE key = ?", (key,))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw

    async def set_meta(self, key: str, value: Any) -> None:
        await self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    async def vacuum(self) -> None:
        await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await self.conn.execute("VACUUM")

    async def stats(self) -> Row:
        size = self.path.stat().st_size if self.path.exists() else 0
        counts = {}
        for table in ("media", "wanted", "downloads", "library_items", "events"):
            counts[table] = await self.fetch_value(f"SELECT COUNT(*) FROM {table}", default=0)
        return {"path": str(self.path), "size_bytes": size, "counts": counts,
                "schema_version": SCHEMA_VERSION}


@asynccontextmanager
async def connect(path: Path) -> AsyncIterator[Database]:
    db = Database(path)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
