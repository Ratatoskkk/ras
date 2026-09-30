"""Stand-in clients so service tests exercise real logic without a network."""

from __future__ import annotations

from typing import Any

from conduit.clients.indexers.base import IndexerPool, SearchQuery
from conduit.domain.models import LibraryItem, Release, TorrentStatus, WatchlistEntry
from conduit.util import bencode


class FakeIndexer:
    name = "Fake"
    priority = 1
    score_bonus = 0

    def __init__(self, releases: list[Release] | None = None) -> None:
        self.releases = releases or []
        self.queries: list[SearchQuery] = []
        self.fail = False

    async def search(self, query: SearchQuery) -> list[Release]:
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("tracker unavailable")
        results = []
        for release in self.releases:
            from conduit.domain.parser import parse_release

            parsed = parse_release(release)
            if query.season is not None and not parsed.covers(
                query.season, query.episode
            ):
                continue
            results.append(release)
        return results

    async def account(self) -> dict[str, Any] | None:
        return {"indexer": self.name, "ratio": "6.98", "buffer": "100 TiB"}

    async def fetch_torrent(self, release: Release) -> bytes | None:
        return bencode.encode({
            b"announce": b"https://fake.test/announce",
            b"info": {
                b"name": release.name.encode(),
                b"length": int(release.size_bytes),
                b"piece length": 262144,
            },
        })

    async def aclose(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "state": "closed"}


def pool(releases: list[Release]) -> tuple[IndexerPool, FakeIndexer]:
    indexer = FakeIndexer(releases)
    return IndexerPool([indexer]), indexer


class FakeQbt:
    """Behaves like the parts of qBittorrent the services actually use."""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.torrents_list: list[TorrentStatus] = []
        self.deleted: list[tuple[list[str], bool]] = []
        self.file_adds_never_land = False
        # The real client answers "Ok." to a URL add and then fetches it in the
        # background, so the torrent can never materialise. Set this to model a
        # tracker that will not hand the file over.
        self.url_adds_never_land = False

    async def login(self, force: bool = False) -> None:
        return None

    async def ensure_category(self, name: str, save_path: str = "") -> None:
        return None

    async def torrents(self, **kwargs: Any) -> list[TorrentStatus]:
        tag = kwargs.get("tag")
        if tag:
            return [t for t in self.torrents_list if tag in t.tags]
        return list(self.torrents_list)

    async def torrents_by_hash(self, hashes: list[str]) -> dict[str, TorrentStatus]:
        wanted = {h.lower() for h in hashes}
        return {t.info_hash: t for t in self.torrents_list if t.info_hash in wanted}

    async def active_count(self) -> int:
        busy = {"downloading", "stalledDL", "metaDL", "queuedDL", "forcedDL"}
        return sum(1 for t in self.torrents_list if t.state in busy)

    async def add_torrent_file(self, content: bytes, **kwargs: Any) -> bool:
        summary = bencode.torrent_summary(content)
        self.added.append({"content": content, "info_hash": summary["info_hash"], **kwargs})
        if self.file_adds_never_land:
            return True
        self.torrents_list.append(
            TorrentStatus(
                info_hash=str(summary["info_hash"]),
                name=str(summary["name"]),
                state="downloading",
                progress=0.0,
                eta_seconds=600,
                dlspeed=5_000_000,
                size_bytes=float(summary["size_bytes"]),
                save_path=kwargs.get("save_path", ""),
                content_path="",
                tags=[t.strip() for t in kwargs.get("tags", "").split(",") if t.strip()],
                category=kwargs.get("category", ""),
            )
        )
        return True

    async def add_torrent_url(self, url: str, **kwargs: Any) -> bool:
        self.added.append({"url": url, **kwargs})
        if self.url_adds_never_land:
            return True  # accepted, then quietly fetched nothing
        self.torrents_list.append(
            TorrentStatus(
                info_hash=f"{abs(hash(url)):040x}"[:40],
                name=url.rsplit("/", 1)[-1], state="downloading", progress=0.0,
                eta_seconds=600, dlspeed=1_000_000, size_bytes=0.0,
                save_path=kwargs.get("save_path", ""), content_path="",
                tags=[t.strip() for t in kwargs.get("tags", "").split(",") if t.strip()],
                category=kwargs.get("category", ""),
            )
        )
        return True

    async def delete(self, hashes: list[str], delete_files: bool = False) -> None:
        self.deleted.append((hashes, delete_files))
        self.torrents_list = [t for t in self.torrents_list if t.info_hash not in hashes]

    async def aclose(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {"name": "qbittorrent", "state": "closed", "version": "fake"}

    def complete_all(self) -> None:
        for torrent in self.torrents_list:
            torrent.progress = 1.0
            torrent.state = "stalledUP"
            torrent.eta_seconds = 0


class FakePlex:
    def __init__(
        self,
        watchlist: list[WatchlistEntry] | None = None,
        library: list[LibraryItem] | None = None,
    ) -> None:
        self._watchlist = watchlist or []
        self._library = library or []
        self.removed: list[str] = []
        self.refreshed: list[str] = []

    async def watchlist(self) -> list[WatchlistEntry]:
        return list(self._watchlist)

    async def remove_from_watchlist(self, rating_key: str) -> bool:
        self.removed.append(rating_key)
        self._watchlist = [e for e in self._watchlist if e.rating_key != rating_key]
        return True

    async def index_library(self) -> list[LibraryItem]:
        return list(self._library)

    async def refresh_sections(self, media_type: str) -> list[str]:
        self.refreshed.append(media_type)
        return ["Films" if media_type == "movie" else "TV"]

    async def aclose(self) -> None:
        return None

    def health(self) -> list[dict[str, Any]]:
        return [{"name": "plex", "state": "closed"}]


class FakeTmdb:
    def __init__(self, shows: dict | None = None, movies: dict | None = None) -> None:
        self.shows = shows or {}
        self.movies = movies or {}

    async def show(self, tmdb_id: str | int) -> dict | None:
        return self.shows.get(str(tmdb_id))

    async def show_with_seasons(self, tmdb_id: str | int, season_numbers=None) -> dict | None:
        return self.shows.get(str(tmdb_id))

    async def season(self, tmdb_id: str | int, season_number: int) -> dict | None:
        show = self.shows.get(str(tmdb_id)) or {}
        return show.get(f"season/{season_number}")

    async def movie(self, tmdb_id: str | int) -> dict | None:
        return self.movies.get(str(tmdb_id))

    async def movie_release_date(self, tmdb_id: str | int):
        from datetime import date

        data = self.movies.get(str(tmdb_id)) or {}
        raw = data.get("digital_release")
        if not raw:
            return None, "unknown"
        return date.fromisoformat(raw), "home"

    async def find_by_external_id(self, external_id: str, source: str = "imdb_id") -> dict | None:
        return None

    async def aclose(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {"name": "tmdb", "state": "closed"}
