"""Decision engine -- what to want, what to grab, what needs a human.

All pure functions over plain data. The services fetch the data and persist the
outcome; everything in between is here, where it can be tested exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..config import Policy
from ..util.text import episode_code, title_similarity
from .models import ParsedRelease, Release, ScoredRelease


# ---------------------------------------------------------------------------
# Matching indexer results to what we asked for
# ---------------------------------------------------------------------------
def matches_target(
    release: Release,
    parsed: ParsedRelease,
    *,
    media_type: str,
    tmdb_id: str | None,
    title: str,
    season: int | None,
    episode: int | None,
    year: int | None = None,
    title_threshold: float = 0.85,
) -> bool:
    """Does this release actually contain the thing we are looking for?

    TMDB id is authoritative when both sides have one. Falling back to title
    similarity (rather than the reference project's exact word-subset check)
    keeps releases like ``Mission Impossible - Dead Reckoning`` matchable
    against ``Mission: Impossible – Dead Reckoning``.
    """
    if tmdb_id and release.tmdb_id:
        if str(release.tmdb_id) != str(tmdb_id):
            return False
    elif title:
        if title_similarity(title, parsed.title or release.name) < title_threshold:
            return False
        if year and parsed.year and abs(parsed.year - year) > 1:
            return False

    if media_type == "movie":
        # A movie result carrying season/episode markers is a mislabelled TV rip.
        return parsed.season is None and not parsed.episodes

    if season is None:
        return True
    return parsed.covers(season, episode)


# ---------------------------------------------------------------------------
# Approval gating
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class ApprovalDecision:
    required: bool
    reason: str = ""


def needs_approval(
    parsed: ParsedRelease,
    size_bytes: float,
    policy: Policy,
    *,
    distinct_seasons: int = 1,
) -> ApprovalDecision:
    """Should a human sign this off before it hits the download client?"""
    # Checked before everything else, including the auto-approve shortcut:
    # "approve everything" has to mean everything or it is not a safety net.
    if policy.require_approval_for_everything:
        return ApprovalDecision(True, "manual approval is required for every grab")
    if policy.auto_approve_below_gb > 0 and size_bytes < policy.auto_approve_below_gb * 1024**3:
        return ApprovalDecision(False)
    if size_bytes > policy.approval_size_bytes:
        return ApprovalDecision(
            True, f"{size_bytes / 1024**3:.0f} GB exceeds the {policy.approval_size_threshold_gb:.0f} GB gate"
        )
    if parsed.is_complete_series:
        return ApprovalDecision(True, "complete-series pack")
    if policy.require_approval_for_multi_season and distinct_seasons > 1:
        return ApprovalDecision(True, f"{distinct_seasons} seasons queued at once")
    if policy.require_approval_for_season_packs and parsed.is_season_pack:
        return ApprovalDecision(True, "season pack")
    return ApprovalDecision(False)


# ---------------------------------------------------------------------------
# Planning what a series still needs
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EpisodeWant:
    season: int
    episode: int
    title: str
    air_date: date | None

    @property
    def code(self) -> str:
        return episode_code(self.season, self.episode)


def watched_high_water(watched: set[tuple[int, int]]) -> tuple[int, int] | None:
    """The furthest point reached in a series, as ``(season, episode)``.

    Tuple ordering does the work: ``(3, 2) > (2, 99)``, so the maximum is the
    latest episode watched, not merely the highest episode number.
    """
    return max(watched) if watched else None


def plan_show_wants(
    seasons: dict[int, list[dict]],
    *,
    have: set[tuple[int, int]],
    watched: set[tuple[int, int]],
    policy: Policy,
    today: date | None = None,
    max_seasons_back: int = 0,
    backlog_grace_days: int = 7,
) -> list[EpisodeWant]:
    """Episodes a series is missing.

    ``seasons`` maps season number to TMDB episode dicts. Anything already on
    disk is skipped; so is any fully watched season (``skip_watched_seasons``);
    and, when ``assume_prior_seasons_watched`` is on, so is everything up to
    the furthest point the user has reached.

    ``policy.backlog_mode`` then decides how much already-aired history to
    chase -- the difference between "keep me current" and "fetch all twenty
    seasons".
    """
    today = today or date.today()
    cutoff = today - timedelta(days=max(backlog_grace_days, 0))
    wants: list[EpisodeWant] = []

    watched_mark = watched_high_water(watched)
    high_water = watched_mark if policy.assume_prior_seasons_watched else None
    unlocked = (
        unlocked_next_season(
            seasons, watched, lead_episodes=policy.sequential_lead_episodes
        )
        if policy.sequential_seasons
        else None
    )

    numbers = sorted(n for n in seasons if n > 0)
    if max_seasons_back > 0:
        numbers = numbers[-max_seasons_back:]

    # Which season "current_season" means. Normally the one the watch history
    # has reached -- read from `watched` directly, since it is a fact about the
    # user regardless of whether `assume_prior_seasons_watched` is on.
    #
    # With no history at all there is no season you are on, and the previous
    # reading of that was "allow nothing": a series added to the watchlist
    # produced no wants whatsoever and never got searched for. Adding something
    # to the watchlist is an explicit request, so the honest reading of
    # "the season you are on" for a show you have not started is the first one.
    current_season = (
        watched_mark[0] if watched_mark else (numbers[0] if numbers else None)
    )

    for season_number in numbers:
        episodes = seasons.get(season_number) or []
        if not episodes:
            continue

        # Everything before the season we are up to is assumed seen.
        if high_water is not None and season_number < high_water[0]:
            continue

        keys = {(season_number, int(e.get("episode_number") or 0)) for e in episodes}
        keys.discard((season_number, 0))
        if policy.skip_watched_seasons and keys and keys <= watched:
            continue
        if keys and keys <= have:
            continue

        for raw in episodes:
            number = int(raw.get("episode_number") or 0)
            if number <= 0:
                continue
            key = (season_number, number)
            # Having the file always disqualifies it. Having *watched* it only
            # does so while the skip-watched policy is on -- otherwise turning
            # that policy off would still silently skip the same episodes.
            if key in have:
                continue
            if policy.skip_watched_seasons and key in watched:
                continue
            if high_water is not None and key <= high_water:
                continue

            air = _as_date(raw.get("air_date"))
            already_aired = air is not None and air < cutoff
            if already_aired and not _backlog_allows(
                policy, season_number, current_season, unlocked
            ):
                continue

            wants.append(
                EpisodeWant(
                    season=season_number,
                    episode=number,
                    title=str(raw.get("name") or ""),
                    air_date=_as_date(raw.get("air_date")),
                )
            )
    return wants


def _backlog_allows(
    policy: Policy,
    season: int,
    current_season: int | None,
    unlocked: int | None = None,
) -> bool:
    """May we chase this already-aired episode?"""
    # A just-in-time unlock overrides the backlog mode: you are about to run
    # out of the season you are on, so the next one is needed now regardless.
    if unlocked is not None and season == unlocked:
        return True
    if policy.backlog_mode == "all":
        return True
    if policy.backlog_mode == "upcoming_only":
        return False
    # current_season: finish the one you are on, but do not run ahead into
    # seasons you have not started.
    return current_season is not None and season == current_season


def season_length(seasons: dict[int, list[dict]], season: int) -> int:
    """How many real episodes a season has, ignoring specials."""
    return sum(
        1
        for raw in seasons.get(season) or []
        if int(raw.get("episode_number") or 0) > 0
    )


def unlocked_next_season(
    seasons: dict[int, list[dict]],
    watched: set[tuple[int, int]],
    *,
    lead_episodes: int = 1,
) -> int | None:
    """The season to pull in early because you are about to need it.

    Once you are within ``lead_episodes`` of the end of the season you are
    watching, the next one is unlocked. That is what lets someone work through
    a long series without ever storing more than a season or two of it.
    """
    high_water = watched_high_water(watched)
    if high_water is None:
        return None
    season, episode = high_water
    total = season_length(seasons, season)
    if total <= 0:
        return None
    if episode >= total - max(lead_episodes, 0):
        following = season + 1
        return following if seasons.get(following) else None
    return None


def all_episode_keys(seasons: dict[int, list[dict]]) -> set[tuple[int, int]]:
    """Every real episode a series has, ignoring specials."""
    return {
        (season, int(raw.get("episode_number") or 0))
        for season, episodes in seasons.items()
        if season > 0
        for raw in episodes or []
        if int(raw.get("episode_number") or 0) > 0
    }


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            year, month, day = value[:10].split("-")
            return date(int(year), int(month), int(day))
        except ValueError:
            return None
    return None


def should_search(air: date | None, *, today: date | None = None, lead_hours: int = 0) -> bool:
    """Has this aired (allowing an optional head start)?"""
    if air is None:
        return True  # unknown date: worth a look
    today = today or date.today()
    return air - timedelta(hours=lead_hours) <= today


def is_stale(
    air: date | None, attempts: int, policy: Policy, *, is_movie: bool,
    tv_days: int, movie_days: int, today: date | None = None,
) -> bool:
    """Stop chasing something that is never going to appear."""
    if attempts >= policy.max_search_attempts:
        return True
    if air is None:
        return False
    today = today or date.today()
    limit = movie_days if is_movie else tv_days
    return air + timedelta(days=limit) < today


# ---------------------------------------------------------------------------
# Choosing between a season pack and individual episodes
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class GrabTarget:
    """One search to run: a movie, a whole season, or a single episode."""

    season: int | None
    episode: int | None
    episode_count: int = 1
    label: str = ""

    @property
    def is_pack(self) -> bool:
        return self.season is not None and self.episode is None


def plan_grab_targets(
    missing: list[tuple[int, int]],
    policy: Policy,
    *,
    season_sizes: dict[int, int] | None = None,
) -> list[GrabTarget]:
    """Group missing episodes into the cheapest sensible set of searches.

    Three missing episodes of one season is a pack; one straggler is a single
    episode. The reference project always preferred packs, which meant a
    70 GB re-download to fill one gap.
    """
    season_sizes = season_sizes or {}
    by_season: dict[int, list[int]] = {}
    for season, episode in sorted(missing):
        by_season.setdefault(season, []).append(episode)

    targets: list[GrabTarget] = []
    for season, episodes in sorted(by_season.items()):
        # 0 means "we do not know how long this season is". Defaulting to the
        # number of missing episodes would make "we are missing all of them"
        # trivially true, and every lone straggler would pull a whole pack.
        total = season_sizes.get(season, 0)
        take_pack = (
            policy.prefer_season_packs
            and (
                len(episodes) >= max(policy.season_pack_min_missing, 1)
                or (total > 0 and len(episodes) >= total)
            )
        )
        if take_pack:
            targets.append(
                GrabTarget(
                    season=season,
                    episode=None,
                    episode_count=max(total, len(episodes), 1),
                    label=f"Season {season}",
                )
            )
        else:
            targets.extend(
                GrabTarget(season=season, episode=e, episode_count=1,
                           label=episode_code(season, e))
                for e in episodes
            )
    return targets


def display_title(media_title: str, parsed: ParsedRelease, target: GrabTarget | None = None) -> str:
    """Human-readable name for the dashboard and the download client tag."""
    if parsed.season_to is not None and parsed.season is not None:
        return f"{media_title} (Seasons {parsed.season}–{parsed.season_to})"
    if parsed.is_complete_series:
        return f"{media_title} (Complete Series)"
    if target is not None and target.season is not None:
        if target.episode is not None:
            return f"{media_title} ({episode_code(target.season, target.episode)})"
        return f"{media_title} (Season {target.season})"
    if parsed.season is not None:
        if parsed.episodes:
            first, last = min(parsed.episodes), max(parsed.episodes)
            if first != last:
                return f"{media_title} (S{parsed.season:02d}E{first:02d}-E{last:02d})"
            return f"{media_title} ({episode_code(parsed.season, first)})"
        return f"{media_title} (Season {parsed.season})"
    return media_title


def summarise_rejections(scored: list[ScoredRelease], limit: int = 4) -> str:
    """One line explaining why a search came back empty-handed."""
    if not scored:
        return "no releases found on any indexer"
    counts: dict[str, int] = {}
    for item in scored:
        for rejection in item.rejections:
            counts[rejection.rule] = counts.get(rejection.rule, 0) + 1
    if not counts:
        return f"{len(scored)} releases found but none selected"
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    detail = ", ".join(f"{rule} ({count})" for rule, count in ranked)
    return f"{len(scored)} releases rejected: {detail}"
