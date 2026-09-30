# Architecture

Why rás is built the way it is, and what each decision bought.

---

## Shape

```
                    +------------------------------------------+
   Plex Discover -->|  services/watchlist   the inbox          |
   Plex Server   -->|  services/library     what you have      |
   TMDB          -->|  services/calendar    what is coming     |
                    |            |                             |
                    |            v                             |
                    |  services/search      choose a release   |--> trackers
                    |            |                             |
                    |            v                             |
                    |  services/queue       place it on disk   |--> qBittorrent
                    |            |                             |
                    |            v                             |
                    |  services/monitor     follow it home     |
                    |  services/janitor     tidy up            |
                    +--------------+---------------------------+
                                   |  events + state
                                   v
                      web/  -->  WebSocket  -->  dashboard
```

Everything hangs off one context object (`services.context.Conduit` -- the
Python package keeps the original name; only the brand changed) holding the
database, repositories, clients, config and event bus. Services are plain coroutines
that take it. That is what makes the whole thing testable: swap the clients on
a context and nothing else notices.

---

## Layers

**`domain/`** -- pure functions and dataclasses. Release parsing, quality
scoring, and every "should I grab this?" decision. No I/O, no database, no
clients. This is deliberate: the rules that decide what lands on your disk are
the part most worth testing, and they are testable with plain function calls.

**`db/`** -- schema migrations and repositories. Every SQL statement in the
application lives here. The original executed ad-hoc SQL from three different
modules including the web layer; putting it behind repositories gives one
place to look and one seam to test against.

**`clients/`** -- one class per upstream, all async over `httpx`. Each is
wrapped in the same resilience stack (see below).

**`services/`** -- the actual jobs. Each does one thing end to end and reports
what it did through the event bus and the database.

**`web/`** -- FastAPI. Routes are thin adapters over services; no business
logic, no SQL.

---

## Decisions

### Async all the way down, and no `plexapi`

`plexapi` and `qbittorrent-api` are synchronous, so using them forces every
call onto a thread pool. Both APIs are simple HTTP, so rás talks to them
directly with `httpx`.

The bigger win is query shape. `plexapi` fetches lazily: indexing a
1,500-episode library costs a request per show plus one per season. rás
asks the server for every episode in a section at once, with unused fields
excluded:

```
GET /library/sections/{id}/all?type=4&includeGuids=1&excludeElements=...
```

Two requests instead of hundreds. Measured against a real library -- 119 films,
94 series, 1,451 episodes -- a full index takes about a second.

Shows carry the TMDB guid, episodes carry watched state, and
`grandparentRatingKey` joins them. That join is done locally.

### Torrents are identified by info-hash, not by name

The original added torrents to qBittorrent by URL, then tried to work out
which torrent was which by comparing words in the release name against the
Plex title. That breaks on any release whose name does not resemble the title --
which is most of them.

rás fetches the `.torrent` first, decodes it, and computes the SHA-1 of its
`info` dictionary (`util/bencode.py`, ~90 lines, no dependency). It then adds
the file to qBittorrent already knowing its hash. Every later lookup is an
exact match, with our own `conduit_<id>` tag as a fallback for torrents added
by URL.

On a private tracker this is also the last line of defence against paying for
something twice: a torrent already in the client is recognised by hash and
adopted rather than re-added.

Reading the torrent first has a second benefit: the true payload size is known
before the disk-space check, rather than trusting the tracker's figure.

### Never grabbing the same thing twice

Five independent barriers, in the order they apply:

1. **Library mirror** -- anything Plex holds (matched by TMDB id) never becomes
   a want, so it is never searched for.
2. **Watched seasons** -- skipped wholesale when `skip_watched_seasons` is on.
3. **Release keys** -- every `(indexer, indexer_id)` ever grabbed, denied or
   blocklisted is filtered out of candidates, backed by a unique index so a
   race cannot slip one through.
4. **Info-hash adoption** -- described above.
5. **Approval gating** -- optionally every grab, via
   `require_approval_for_everything`.

The known limitation is that (1) is keyed on TMDB ids, so media Plex has not
matched is invisible to it. Resolving it by title instead would mean guessing,
and a wrong guess on a private tracker costs real download credit -- so the
blind spot is *reported* rather than papered over: the index counts unmatched
entries on every pass, the dashboard shows them with the episode count they
hide, and the timeline records it once per change rather than once per pass.

### Quality profiles are data

Scoring is a declarative table of attribute values and their scores, plus hard
limits. The original had a hard-coded `if` ladder -- changing "prefer web-dl
over remux" meant editing Python.

More importantly, every release is either **accepted with a score** or
**rejected with a named rule**. Rejections are kept, not discarded, which is
what lets the UI answer "why didn't it take the 4K remux?" with the actual
reason instead of silence.

### Trackers are pluggable

Aither runs UNIT3D, and so do dozens of other private trackers. The indexer
layer is a `Protocol` with one UNIT3D implementation parameterised by base URL
and API key, so a second tracker is a config entry.

Searches are filtered **server side** using UNIT3D's documented parameters:
`tmdbId` / `imdbId`, `categories[]`, `seasonNumber`, `episodeNumber`, and
`alive=1` so seederless torrents never leave the server. Name-only fallbacks
are bounded with `startYear`/`endYear`. `perPage` honours the server's cap of
100, and `sortField` is one of the seven values UNIT3D validates against --
anything else returns 422, which maps cleanly onto `PermanentError`.

The original pulled 100 unfiltered rows and sifted them with regexes; asking
for `seasonNumber=3` returns a handful. Results are cached with a TTL in
SQLite, so a restart does not throw away what the app already knows.

`/api/user` gives ratio, buffer and hit-and-run count. On a private tracker
those numbers decide whether a grab is affordable, so they belong on the
dashboard rather than in a browser tab.

### Failures have types

The original wrapped everything in `try/except` returning `None`, so a
rate-limit response was indistinguishable from "no results" -- and the
scheduler would keep hammering.

`util/resilience.py` provides three primitives, applied to every client:

- **Retry** with exponential backoff and jitter, on transient errors only.
- **Rate limiting** via token bucket, per tracker, from config.
- **Circuit breaking**, so a dead service stops being called instead of timing
  out on every tick. Configuration errors (a bad API key) never trip it --
  that would take down everything else with it.

Status codes map to meaning: 429 -> `RateLimited` (honouring `Retry-After`),
401/403 -> `PermanentError` ("check your API key"), 5xx/timeout ->
`TransientError`.

**Retries are not free, and user-facing calls say so.** The first version used
a 10 s connect timeout and three attempts, so one unreachable tracker cost 33
seconds -- measured, not theorised. Anything a human waits on now overrides the
policy: the account panel gets a single attempt and a 5 s timeout, the endpoint
wraps it in an 8 s hard ceiling, and *failures are cached too*, so a dead
tracker is dialled once every two minutes rather than on every page load.
Background searches keep two attempts against a 5 s connect timeout, bounding
a dead host at roughly 11 s, and the breaker opens after three failures rather
than five.

### A task supervisor instead of APScheduler

APScheduler runs jobs on threads, swallows exceptions into a logger nobody
reads, and fixes intervals at registration time.

`services/supervisor.py` runs each task as an asyncio task that:

- re-reads its interval from config every cycle, so retuning needs no restart;
- applies startup delays and jitter, so six jobs do not stampede on boot;
- backs off exponentially while a task keeps failing;
- records duration, run count and last error per task -- surfaced in the UI
  with manual *Run* and *Pause* buttons;
- wakes early when triggered from the API;
- cancels cleanly on shutdown.

### Two configuration layers

`.env` holds credentials and paths. `config/conduit.toml` holds behaviour.

They are separate so the Settings UI can write configuration without any risk
of serialising a password into a file, and so a bad profile edit can be rolled
back without touching credentials. The TOML is validated with Pydantic
*before* it is written, and written atomically.

`ConfigStore` reloads on mtime change, checked by a cheap 15-second task, so
hand-editing the file works too. A file that fails to parse leaves the last
good config in place.

### One WebSocket

The original polled `/api/state` every five seconds *and* held an SSE
connection that re-derived progress independently. rás publishes to an
in-process bus; the WebSocket sends a full snapshot on connect and pushes
changes as they happen.

Progress is a special case: the monitor sends actual figures every ~15 s, and
the browser interpolates between them from the reported speed. The bars move
smoothly without the server sending more.

### A front end with no build step

The original needed Node, npm and a SvelteKit build, plus a rebuild after
every edit. rás's dashboard is plain ES modules and CSS, served directly.

The trade is no bundler-provided reactivity, so rendering is explicit: views
re-render on state change (which is infrequent) and high-frequency progress
updates touch only the specific elements they own. A tagged-template `html`
helper escapes every interpolation by default, so the absence of a framework
does not mean the absence of XSS protection.

Because filenames carry no content hash, static files are served `no-cache` --
the browser still revalidates cheaply, and an edited file is never stale.

### Migrations are numbered

The original detected drift with `PRAGMA table_info` and bolted on
`ALTER TABLE` calls at boot, so no database knew what version it was. Here
each migration is a numbered, immutable step recorded in `schema_migrations`,
applied in order inside a transaction.

---

## Data model

```
media      one movie or series          (tmdb_id, title, monitored, ignored, profile)
  |- wanted    something we need        (season, episode, air_date, state)
       |- downloads  a release we took  (info_hash, score, state, progress)

library_items   a mirror of what Plex holds -- the "do I need this?" oracle
blocklist       releases never to grab again
events          the activity timeline
search_cache    TTL'd tracker responses
task_runs       per-task health
```

`wanted` is the pivot. Everything upstream produces wants; everything
downstream consumes them.

```
waiting --(air date passes)--> searching --(release found)--> grabbed
                                   |                             |
                                   |                    (download completes)
                        (searched repeatedly,                    v
                         nothing found)                     downloaded
                                   v
                         unavailable (daily search)
```

Slow polling begins from **when we started looking**, not from the air date. The
distinction matters: dating it from the air date would slow the entire back
catalogue of a newly followed series before a single search ran.
The attempt limit applies after the fresh-release window. An `unavailable`
title gets a daily check indefinitely while it remains wanted; a manual search
can check it immediately. Known future air dates keep a title waiting until
the date arrives.

### Deciding how far back to reach

Two rules shrink a newly followed series from "twenty seasons" to "what you
actually need":

- **`assume_prior_seasons_watched`** takes the maximum `(season, episode)`
  tuple you have watched and treats everything at or below it as seen. Tuple
  ordering does the work -- `(3, 2) > (2, 99)` -- so the high-water mark is the
  latest episode watched, not the highest episode number.
- **`backlog_mode`** then decides what to do with already-aired episodes past
  that point: chase them all, finish only the season you are on, or take
  nothing older than the fresh window.

Both are applied by the same planner that creates wants, and every recompute
reconciles the existing list: episodes the rules now exclude are stood down,
and -- because `upsert` revives stood-down wants -- widening the rules brings
them back. Without that second half, narrowing the policy would be a one-way
door.

---

## Choosing what to grab

For a series with missing episodes:

1. Group missing episodes by season.
2. A season with several gaps becomes one pack search; a lone straggler stays
   a single-episode search. Re-downloading a 70 GB pack to fill one gap is the
   failure mode this avoids.
3. If no pack exists -- a currently-airing season, or one nobody has packed --
   fall back to individual episodes rather than giving up.
4. Score every candidate against the profile; take the winner.
5. Gate on approval: everything, or over the size threshold, or a season pack,
   a multi-season grab, or a complete-series pack.

`auto_approve_below_gb` is an explicit override of the size and pack gates for
smaller releases; `require_approval_for_everything` takes precedence over it.

Approvals are grouped by title in the UI, so a five-season backfill is one
card with *Approve all*, not five prompts.

---

## Testing

The offline test suite runs without network access in about 30 seconds.

- **Domain** -- parser, scoring and decisions as plain function calls. Every
  release name in the parser tests is a real one taken from a live tracker or
  a Plex library.
- **Repositories** -- against a real SQLite file, so the SQL is genuinely
  exercised (including the partial and expression indexes).
- **Services** -- real database, real business logic, stubbed clients. The fake
  qBittorrent decodes the torrents handed to it, so the info-hash path is
  tested for real.
- **Web** -- through ASGI, no socket. Includes the access-control matrix.

Deliberately covered because they are the things that broke before: private
address ranges (`172.32.0.1` is public, `172.31.255.255` is not), titles that
are also years (`1923`, `2012`), `DD+` and `HDR10+` at a token boundary,
multi-episode ranges, a Plex response that comes back empty, and -- because it
costs real money on a private tracker -- that nothing already in the library or
already in the download client is ever fetched again.
