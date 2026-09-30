# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Members of a shared household use rás to see what media is coming, what is already available, and when to start a requested download.

## Product Purpose

rás connects a household's Plex watchlist and library to release tracking, tracker search, downloads, and storage management. The dashboard lets household members review the automation and intervene at deliberate decision points.

## Positioning

Approvals are a timing decision, not a task backlog. A following season waits on a separate "Ready when you are" shelf until someone chooses to start it. New titles and releases that cross the size gate require their own approval.

## Operating Context

The household uses Plex, TMDB release dates, private trackers, qBittorrent, and local storage drives. The interface covers Dashboard, Queue, Calendar, Library, Activity, and Settings. It receives live state over a WebSocket and supports light and dark themes.

## Capabilities and Constraints

- Preserve current workflows, content, and user facing terminology.
- Keep `rás` as the human facing name. Internal `conduit` identifiers and paths are compatibility constraints.
- Keep the month calendar with release posters and the alternate agenda view.
- Show the actual reason for search and automation outcomes rather than guessing.
- Keep actions that can start downloads or remove files explicit.

## Brand Commitments

The name and existing three stream mark are established.

## Evidence on Hand

`README.md`, `ARCHITECTURE.md`, and `REDESIGN.md` describe the product and its earlier design decisions. `src/conduit/static/` contains the running interface and current copy.

## Product Principles

- Make the next household decision clear without pressuring someone to clear the approval shelf.
- Distinguish a continued series from a new title.
- Show useful context before an action that spends tracker credit or storage.
- Explain uncertain, blocked, and failed states truthfully.
- Keep everyday monitoring quick while retaining detailed inspection when needed.
