# rás

rás watches your Plex watchlist and library, works out what you are
missing, finds the best matching release on your trackers, hands it to
qBittorrent on whichever drive has room, and shows you the whole pipeline
live.

It is a ground-up rewrite of an earlier Plex -> Aither -> qBittorrent script.
Same job, different everything: async throughout, a pluggable tracker layer,
declarative quality profiles, deterministic torrent tracking by info-hash, and
a dashboard that needs no build step.

> **About the name.** `rás` is Icelandic for *conduit*, which is what the
> project was called first. The brand moved and the code stayed: the Python
> package, `config/conduit.toml`, `data/conduit.db` and the `CONDUIT_*`
> environment variables all still read `conduit`. That is deliberate. Renaming
> them would orphan every torrent already tagged and tracked in qBittorrent.

---

## Quick start

1. **Install Python 3.11+** (tick *Add python.exe to PATH*).
2. **Get the code.** Clone it, or use *Code -> Download ZIP* and unpack it.

   ```bash
   git clone https://github.com/Ratatoskkk/ras.git
   ```

3. **Copy `.env.example` to `.env`** and fill it in.
4. **Double-click `run.bat`.**

That is the whole setup. Dependencies install themselves on first run, the
database creates itself, and a tray icon appears. The dashboard is at
<http://localhost:5050>.

The `.bat` and `.vbs` helpers are for Windows. macOS and Linux run the same
app through `python start.py run` -- see **Command line** below.

If anything looks wrong, run **`check.bat`** -- it validates your config and
probes Plex, TMDB, every tracker and qBittorrent, telling you exactly which
one is unhappy.

### Starting with Windows

Double-click **`install_startup.bat`**. rás will start hidden whenever you
log in, with only the tray icon showing. No admin rights needed -- it just puts
a shortcut in your own Startup folder.

**`uninstall_startup.bat`** removes it again. Neither script touches your
database, settings or downloads.

### Configuration

`.env` holds **credentials and paths** only:

```env
PLEX_URL=http://localhost:32400
PLEX_TOKEN=...                  # https://support.plex.tv/articles/204059436
TMDB_API_KEY=...                # https://developer.themoviedb.org
AITHER_API_KEY=...
QBITTORRENT_URL=http://localhost:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=...
DOWNLOAD_DIRS=D:\Torrents,E:\Torrent
```

Everything else -- quality profiles, size gates, task intervals, which trackers
to use -- lives in `config/conduit.toml`, is editable from the **Settings**
page, and is picked up within seconds without a restart.

---

## Private trackers: what will and will not be downloaded

On a private tracker every grab costs download credit, so this is worth being
precise about. rás refuses to grab something for five independent reasons,
each of which is enough on its own.

**1. It is already in Plex.** The library is mirrored locally, matched by TMDB
id. Anything present is never turned into a "want", so the tracker is never
even queried for it -- no search, no grab.

**2. You have already watched that season.** Fully watched seasons are skipped
entirely (`skip_watched_seasons`).

**3. That exact release was seen before.** Every `(tracker, torrent id)` that
has ever been grabbed, denied or blocklisted is excluded from candidates, and a
unique database index makes a duplicate impossible even under a race.

**4. That torrent is already in qBittorrent.** Before adding anything, rás
computes the info-hash and checks the client. A match is *adopted*, not
re-downloaded -- so even a lost database costs you nothing.

**5. You did not approve it.** Season packs, multi-season grabs and anything
over the size gate wait for a click.

The approval gates above describe the default policy. Setting
`auto_approve_below_gb` above zero explicitly bypasses the size and pack gates
for releases below that size. `require_approval_for_everything` still requires
a click for every grab.

### The one real gap

De-duplication is keyed on TMDB ids, so anything **Plex has not matched** is
invisible to it. A folder Plex has filed as its own unmatched entry looks like
missing media, and rás would happily fetch it again. The usual culprit is a
season split into its own folder -- Plex files `Modern Family S03` as a separate,
unmatched show, and the 21 episodes inside it stop counting as owned.

Nothing can fix that automatically without guessing, so rás refuses to guess
and **tells you instead**:

- a warning card at the top of the **Dashboard**, listing each unmatched entry
  and how many episodes it hides;
- a line in the activity timeline when the number changes -- once, not on every
  half-hourly index;
- `check.bat`, which reports it before anything is running.

Fix each one in Plex (*⋯ → Match*, then *Merge* into the right title), then
**Rescan watched state** on the Library page. The card disappears when the
library is clean.

### Recommended first run

Turn **`require_approval_for_everything`** on in Settings before you let it
run. Nothing whatsoever reaches qBittorrent until you click Approve, so rás
acts as a recommendation engine and you decide what is worth the credit. Turn
it off again once you trust its picks.

It ships **off**, because the size and season-pack gates already catch the
expensive grabs. On a private tracker the cautious first week is still worth
it.

### How much back catalogue to chase

rás follows every series you have watched in Plex and treats their missing
episodes as wanted. On a large library that is a *lot* of history, so two
settings decide how far back it reaches.

**`assume_prior_seasons_watched`** -- people watch in order, so if you have
started season 3, seasons 1 and 2 were almost certainly seen elsewhere.
Everything up to the furthest point you have reached is treated as watched.

**`backlog_mode`** -- what to do about seasons *after* the one you are on that
have already aired:

| Mode | Means |
|---|---|
| `all` | Every missing episode, however old |
| `current_season` | Finish the season you are on, then keep up |
| `upcoming_only` | Only what has not aired yet, plus the fresh window |

"The season you are on" comes from what you have watched in Plex. For a series
you have **never** watched — one you just put on the watchlist — that is the
first season: adding something to the watchlist is an explicit request, so
`current_season` starts you at the beginning rather than giving you nothing.
With `sequential_seasons` on, the next season then unlocks as you approach the
end of the one you are watching.

Measured on a real 94-series library where the owner is mid-way through
several long-running shows:

| Mode | Episodes wanted |
|---|---|
| `all` | 1,149 |
| `current_season` | 36 |
| `upcoming_only` | 15 |

Changing the mode re-tidies the existing list in both directions -- narrowing
stands wants down, widening brings them back.

**`sequential_seasons`** pairs with `current_season` to solve the storage
problem directly: hold only the season you are watching, and pull the next one
in as you approach the end of it. `sequential_lead_episodes` controls how much
warning you get (1 = when you start the second-to-last episode).

Two more levers: **`max_seasons_back`** caps how many seasons of any show are
considered, and **Ignore** on the Library page stops following a title
entirely (with **Clear ignore list** to undo the lot).

---

## What it does

**Reads your watchlist as an inbox.** Anything you add is turned into a
monitored title plus a concrete list of what is missing, and only *then*
removed from the watchlist. Nothing is dropped because a tracker happened to
be down.

**Follows what you actually watch.** Start a series in Plex and rás picks
it up on its own -- no watchlist round trip needed.

**Knows what you already have.** The Plex library is mirrored locally, so
every "do I need this?" decision is a local lookup rather than a network call.

**Tracks releases before they exist.** TMDB air dates and *digital/physical*
movie release dates drive a calendar; the moment something is out, rás
starts hunting for it, hard for the first week and steadily thereafter.

**Picks releases by rules you can read.** Quality profiles score resolution,
source, HDR flavour, codec, audio and release group, then apply size windows,
seeder floors and blocked terms. Every candidate is either accepted with a
score or rejected with a named reason.

**Explains itself.** The *Releases* button on any title shows every release
the trackers have, its score breakdown, and -- for the rejects -- the exact rule
that excluded it. No more guessing why the 4K remux was passed over.

**Asks before doing anything big.** Approvals are grouped into one card per
title rather than a wall of individual prompts.

**Treats the approval list as a shelf, not an inbox.** The next season of
something you already watch is not the same decision as a title you just added,
so it gets its own section — *Ready when you are* — showing how far through the
previous season you actually are:

> **Modern Family** · Season 3 ready · 40 GB
> ▓▓▓▓▓▓▓▓▓▓ 24/24 through season 2 &nbsp; **[ Start season 3 ]**

Leaving it sitting there is a valid answer. Nothing downloads until you say so,
which makes it a usable "I am ready to watch this now" gate rather than a queue
to be cleared. Whether something counts as a continuation is worked out from
your library — if an earlier season is already on disk, it is one.

**Places downloads intelligently.** It fetches the .torrent itself, reads the
true payload size, and picks the drive with the most room -- never dipping into
the space you told it to reserve.

**Shows your tracker standing.** Ratio, buffer, upload/download totals,
seeding count and hit-and-runs sit on the dashboard, so you can see what a
grab costs before you approve it.

**Lets you say "I've already seen that."** Any wanted episode can be marked as
watched, which stops rás looking for it *without* blocklisting anything.
The series stays followed and future episodes still land. Three scopes:

- **Seen** on a calendar row -- that one episode.
- **Seen ⤒** -- that episode and every earlier one in the season.
- **Seen all** on the Library page -- everything outstanding for a title.

This is deliberately a different state from *Ignore*: a policy stand-down is
reversible and comes back when the rules widen, whereas "I watched it" sticks.

**Fetches the next season just before you need it.** With
`sequential_seasons` on, reaching the second-to-last episode of a season
queues the following one. A twenty-season show arrives one season at a time
instead of filling your drive at once -- the point being that you never store
more than you are about to watch.

**Cleans up after you, without earning a hit-and-run.** Once Plex says you have
watched something it appears under *Reclaim space*, but only becomes deletable
once it has met the seeding requirement (5 days by default, or a ratio target
if you set one). Items still seeding are listed with a live countdown and
progress bar, and the delete is refused with an explanation rather than
silently doing nothing. A season pack that lands automatically retires the
individual episodes it replaces.

---

## The dashboard

| Page | What it is for |
|---|---|
| **Dashboard** | Live status: what is downloading, what needs approving, drive usage, recent activity |
| **Queue** | Every download with filters, retries, removals, and full history |
| **Calendar** | A month grid with posters on the day each release lands — click a day for the detail. *Agenda* mode keeps the old countdown list, with the aired-but-missing backlog behind a toggle |
| **Library** | Everything followed, with per-title search, release inspection, and the reclaim-space view |
| **Activity** | The event timeline, the application log, and per-task health with manual triggers |
| **Settings** | Policy, profiles, trackers and timings -- validated before they are saved |

State is pushed over a WebSocket, so nothing polls. Progress bars interpolate
between server updates from the reported speed, so they move smoothly instead
of stepping. Light and dark themes follow your system by default.

---

## Command line

```bash
python start.py run              # start the server
python start.py run --tray       # ...with a system-tray icon
python start.py run --open       # ...and open the dashboard
python start.py check            # validate config and probe every service
python start.py config           # print the effective configuration
```

`--port`, `--host`, `--no-tasks` (serve the UI without background jobs) and
`--allow-multiple` are available on `run`.

---

## Quality profiles

Three ship by default; the Settings page edits them directly.

| Profile | For |
|---|---|
| `uhd-remux` | 4K HDR remux first, falling back to 4K web and 1080p remux |
| `uhd-efficient` | 4K but bandwidth-conscious -- prefers web-dl over huge remuxes |
| `hd-balanced` | 1080p only: small, fast, universally playable |

A profile is a set of scored attributes plus hard limits:

```toml
[[profiles]]
name = "uhd-remux"
min_score = 400
max_size_per_episode_gb = 90
seeder_floor = 1
blocked_terms = ["full disc", "bd50", "bd25", "hdcam", "telesync"]

  [[profiles.resolutions]]
  value = "2160p"
  score = 1000

  [[profiles.sources]]
  value = "remux"
  score = 500

  [[profiles.dynamic_range]]
  value = "dv_hdr10plus"
  score = 260
```

A release must clear `min_score`, avoid every blocked term, fit the size
window and meet the seeder floor. Anything whose resolution or source is not
listed in the profile is rejected outright -- so a profile is a whitelist, not
a suggestion.

`max_size_per_episode_gb` applies to packs too: a 900 GB ten-episode season is
90 GB per episode, and is judged on that.

---

## Adding another tracker

rás speaks UNIT3D, which most private trackers run. It uses the documented
filter API properly: `tmdbId` / `imdbId` / `categories[]` / `seasonNumber` /
`episodeNumber` narrow the query server side, `alive=1` drops seederless
torrents before they are sent, `startYear`/`endYear` bound name-based searches,
and `perPage` respects the server's cap of 100. `/api/user` supplies the
account panel.

Adding a tracker is a config entry and an env var -- no code:

```toml
[[indexers]]
name = "Blutopia"
type = "unit3d"
base_url = "https://blutopia.cc"
api_key_env = "BLUTOPIA_API_KEY"
priority = 2
rate_limit_per_minute = 30
score_bonus = 0
```

```env
BLUTOPIA_API_KEY=...
```

Trackers are queried concurrently and their results ranked together. One being
down, rate-limited or slow never blocks the others.

---

## Project layout

```
ras/
|-- start.py                 # launcher shim (no install required)
|-- run.bat / check.bat / start_hidden.vbs
|-- install_startup.bat / uninstall_startup.bat
|-- config/conduit.toml      # behaviour -- created on first run, edited from the UI
|-- .env                     # credentials and paths
|-- data/conduit.db          # SQLite database
|-- logs/conduit.jsonl       # rotating structured log
|-- src/conduit/
|   |-- config.py            # settings + quality profiles
|   |-- logs.py              # console, file and in-memory logging
|   |-- clients/             # Plex, TMDB, qBittorrent, tracker clients
|   |-- domain/              # release parsing, scoring, decisions (pure, no I/O)
|   |-- db/                  # schema migrations and repositories
|   |-- services/            # watchlist, calendar, search, queue, monitor, janitor
|   |-- web/                 # FastAPI app, REST API, WebSocket
|   `-- static/              # the dashboard (plain ES modules and CSS)
`-- tests/                   # 267 tests, no network required
```

---

## Development

```bash
pip install -r requirements.txt
python -m pytest              # 267 tests, ~30 seconds, fully offline
python -m ruff check src tests
```

The domain layer performs no I/O, so parsing, scoring and grab decisions are
tested with plain function calls. Services are tested against a real SQLite
database with stubbed clients, and the API is tested through ASGI without a
socket.

The dashboard is plain ES modules -- edit a file under `src/conduit/static/`
and reload. There is no bundler, no `node_modules`, and no build step.

Interactive API docs live at `/api/docs` while the server is running.

---

## How this differs from the original

| | Original | rás |
|---|---|---|
| Concurrency | Synchronous, thread-pool scheduler | Async end to end; one event loop |
| Plex access | `plexapi`, one request per show and season | Direct HTTP, whole library in ~2 calls |
| Torrent identity | Fuzzy title matching against client names | Info-hash computed before the torrent is added |
| Quality rules | Hard-coded `if` ladder | Declarative profiles, editable in the UI |
| Release parsing | Four regexes | Full parser: multi-episode, DV/HDR10+ layering, date-based episodes, editions |
| Failure handling | `except: return None` | Typed errors, retries with backoff, rate limits, circuit breakers |
| "Why nothing?" | Silent | Every rejection recorded with its rule and shown in the UI |
| Trackers | Aither only | Any UNIT3D tracker, several at once |
| Config changes | Edit code, restart | Edit in the UI, applied in seconds |
| Schema changes | Ad-hoc `ALTER TABLE` on boot | Numbered, recorded migrations |
| Front end | SvelteKit + npm build step | Plain ES modules, no build |
| Live updates | 5 s polling *and* a separate SSE stream | One WebSocket, pushed |
| Access control | Prefix check that let `172.99.x` through | Correct range arithmetic, optional token |
| Tests | A handful | Offline regression suite |

---

## Notes

- rás removes items from your Plex watchlist once it has recorded them --
  that is what stops the same title being processed forever. Turn it off with
  *Remove items from the Plex watchlist once handled* in Settings.
- The dashboard is restricted to your local network by default. Set
  `CONDUIT_AUTH_MODE=token` and `CONDUIT_API_TOKEN` to require a shared secret
  as well, or `both` for both.
- [ARCHITECTURE.md](ARCHITECTURE.md) explains why each part is built the way
  it is, and what the decision bought.

---

## License

MIT. See [LICENSE](LICENSE).
