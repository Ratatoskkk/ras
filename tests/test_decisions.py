"""Decision engine: what we want, what we grab, and what needs a human."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from conduit.domain import decisions
from conduit.domain.parser import parse
from conftest import make_release

GIB = 1024**3


class TestApprovalGating:
    def test_oversized_release_needs_approval(self, policy) -> None:
        parsed = parse("Movie 2020 2160p UHD BluRay REMUX HDR HEVC-GRP")
        result = decisions.needs_approval(parsed, 150 * GIB, policy)
        assert result.required
        assert "100 GB gate" in result.reason

    def test_season_pack_needs_approval_by_default(self, policy) -> None:
        parsed = parse("Show S02 2160p WEB-DL DV HDR10 H.265-GRP")
        assert decisions.needs_approval(parsed, 40 * GIB, policy).required

    def test_multi_season_grab_needs_approval(self, policy) -> None:
        parsed = parse("Show S02E01 1080p WEB-DL H.264-GRP")
        assert decisions.needs_approval(parsed, 5 * GIB, policy, distinct_seasons=3).required

    def test_single_episode_goes_straight_through(self, policy) -> None:
        parsed = parse("Show S02E01 2160p WEB-DL DV HDR10 H.265-GRP")
        assert not decisions.needs_approval(parsed, 8 * GIB, policy).required

    def test_auto_approve_threshold_short_circuits_everything(self, policy) -> None:
        policy.auto_approve_below_gb = 200
        parsed = parse("Show S02 2160p WEB-DL DV HDR10 H.265-GRP")
        assert not decisions.needs_approval(parsed, 150 * GIB, policy).required

    def test_approve_everything_gates_even_a_tiny_single_episode(self, policy) -> None:
        """The private-tracker safety switch: no grab happens without a click."""
        policy.require_approval_for_everything = True
        parsed = parse("Show S02E01 1080p WEB-DL H.264-GRP")
        result = decisions.needs_approval(parsed, 0.5 * GIB, policy)
        assert result.required
        assert "every grab" in result.reason

    def test_approve_everything_outranks_the_auto_approve_shortcut(self, policy) -> None:
        policy.require_approval_for_everything = True
        policy.auto_approve_below_gb = 500
        parsed = parse("Show S02E01 1080p WEB-DL H.264-GRP")
        assert decisions.needs_approval(parsed, 1 * GIB, policy).required

    def test_complete_series_always_needs_approval(self, policy) -> None:
        parsed = parse("Show.S01-S05.COMPLETE.1080p.BluRay.x265-GRP")
        assert decisions.needs_approval(parsed, 1 * GIB, policy).required


class TestMatching:
    def test_tmdb_id_is_authoritative(self) -> None:
        release = make_release("Totally Different Name 2020 2160p WEB-DL-GRP", tmdb_id="27205")
        assert decisions.matches_target(
            release, parse(release.name), media_type="movie", tmdb_id="27205",
            title="Inception", season=None, episode=None,
        )

    def test_wrong_tmdb_id_is_refused_despite_a_matching_title(self) -> None:
        release = make_release("Inception 2010 2160p WEB-DL-GRP", tmdb_id="999999")
        assert not decisions.matches_target(
            release, parse(release.name), media_type="movie", tmdb_id="27205",
            title="Inception", season=None, episode=None,
        )

    def test_title_fallback_tolerates_punctuation(self) -> None:
        release = make_release("Mission Impossible Dead Reckoning 2023 2160p WEB-DL-GRP")
        assert decisions.matches_target(
            release, parse(release.name), media_type="movie", tmdb_id=None,
            title="Mission: Impossible - Dead Reckoning", season=None, episode=None, year=2023,
        )

    def test_tv_release_is_not_accepted_for_a_movie(self) -> None:
        release = make_release("Some Title S01E01 1080p WEB-DL-GRP", tmdb_id=None)
        assert not decisions.matches_target(
            release, parse(release.name), media_type="movie", tmdb_id=None,
            title="Some Title", season=None, episode=None,
        )

    def test_season_pack_satisfies_an_episode_request(self) -> None:
        release = make_release("Silo S02 2160p WEB-DL-GRP", tmdb_id="125988")
        assert decisions.matches_target(
            release, parse(release.name), media_type="show", tmdb_id="125988",
            title="Silo", season=2, episode=7,
        )

    def test_other_season_does_not_satisfy(self) -> None:
        release = make_release("Silo S03 2160p WEB-DL-GRP", tmdb_id="125988")
        assert not decisions.matches_target(
            release, parse(release.name), media_type="show", tmdb_id="125988",
            title="Silo", season=2, episode=7,
        )


class TestShowPlanning:
    SEASONS = {
        1: [{"episode_number": n, "name": f"E{n}", "air_date": "2020-01-01"} for n in range(1, 4)],
        2: [{"episode_number": n, "name": f"E{n}", "air_date": "2021-01-01"} for n in range(1, 4)],
    }

    def test_skips_episodes_already_in_the_library(self, policy) -> None:
        wants = decisions.plan_show_wants(
            self.SEASONS, have={(1, 1), (1, 2), (1, 3)}, watched=set(), policy=policy
        )
        assert {(w.season, w.episode) for w in wants} == {(2, 1), (2, 2), (2, 3)}

    def test_skips_fully_watched_seasons(self, policy) -> None:
        watched = {(1, 1), (1, 2), (1, 3)}
        wants = decisions.plan_show_wants(
            self.SEASONS, have=set(), watched=watched, policy=policy
        )
        assert all(w.season == 2 for w in wants)

    def test_can_be_told_not_to_skip_watched_seasons(self, policy) -> None:
        policy.skip_watched_seasons = False
        wants = decisions.plan_show_wants(
            self.SEASONS, have=set(), watched={(1, 1), (1, 2), (1, 3)}, policy=policy
        )
        assert any(w.season == 1 for w in wants)

    def test_max_seasons_back_limits_the_backlog(self, policy) -> None:
        wants = decisions.plan_show_wants(
            self.SEASONS, have=set(), watched=set(), policy=policy, max_seasons_back=1
        )
        assert {w.season for w in wants} == {2}

    def test_watching_season_3_implies_seasons_1_and_2(self, policy) -> None:
        """People watch in order, so a season-3 view means 1-2 were seen."""
        policy.assume_prior_seasons_watched = True
        seasons = {
            n: [{"episode_number": e, "name": f"E{e}", "air_date": "2020-01-01"}
                for e in range(1, 4)]
            for n in (1, 2, 3)
        }
        wants = decisions.plan_show_wants(
            seasons, have=set(), watched={(3, 1)}, policy=policy
        )
        # Only what comes after the point reached.
        assert {(w.season, w.episode) for w in wants} == {(3, 2), (3, 3)}

    def test_the_assumption_is_off_unless_asked_for(self, policy) -> None:
        seasons = {
            n: [{"episode_number": e, "name": f"E{e}", "air_date": "2020-01-01"}
                for e in range(1, 4)]
            for n in (1, 2, 3)
        }
        wants = decisions.plan_show_wants(
            seasons, have=set(), watched={(3, 1)}, policy=policy
        )
        assert {w.season for w in wants} == {1, 2, 3}

    def test_high_water_mark_orders_by_season_then_episode(self) -> None:
        assert decisions.watched_high_water({(2, 99), (3, 2)}) == (3, 2)
        assert decisions.watched_high_water(set()) is None

    def test_later_seasons_are_still_wanted_after_the_assumption(self, policy) -> None:
        policy.assume_prior_seasons_watched = True
        seasons = {
            n: [{"episode_number": 1, "name": "E1", "air_date": "2020-01-01"}]
            for n in (1, 2, 3, 4, 5)
        }
        wants = decisions.plan_show_wants(
            seasons, have=set(), watched={(3, 1)}, policy=policy
        )
        assert {w.season for w in wants} == {4, 5}

    def _long_running(self):
        """S1-S4 aired long ago; S5 is still airing."""
        old = "2015-01-01"
        return {
            1: [{"episode_number": 1, "name": "a", "air_date": old}],
            2: [{"episode_number": 1, "name": "b", "air_date": old}],
            3: [{"episode_number": 1, "name": "c", "air_date": old},
                {"episode_number": 2, "name": "d", "air_date": old}],
            4: [{"episode_number": 1, "name": "e", "air_date": old}],
            5: [{"episode_number": 1, "name": "f",
                 "air_date": (date.today() + timedelta(days=5)).isoformat()}],
        }

    def test_backlog_all_chases_everything_after_the_high_water_mark(self, policy) -> None:
        policy.assume_prior_seasons_watched = True
        policy.backlog_mode = "all"
        wants = decisions.plan_show_wants(
            self._long_running(), have=set(), watched={(3, 1)}, policy=policy
        )
        assert {(w.season, w.episode) for w in wants} == {(3, 2), (4, 1), (5, 1)}

    def test_backlog_current_season_does_not_run_ahead(self, policy) -> None:
        policy.assume_prior_seasons_watched = True
        policy.backlog_mode = "current_season"
        wants = decisions.plan_show_wants(
            self._long_running(), have=set(), watched={(3, 1)}, policy=policy
        )
        # Finishes season 3, skips the aired season 4, still takes the future one.
        assert {(w.season, w.episode) for w in wants} == {(3, 2), (5, 1)}

    def test_a_never_watched_series_starts_at_its_first_season(self, policy) -> None:
        """The real failure this was written for.

        A series added to the watchlist with `backlog_mode = current_season`
        and no watch history produced *no wants at all*: "the season you are
        on" was read from the watch history, there was none, so every aired
        episode was excluded. The title sat there forever, and the UI blamed
        the quality profile. Adding something to the watchlist is an explicit
        request -- the season you are on is the first one.
        """
        policy.assume_prior_seasons_watched = True
        policy.backlog_mode = "current_season"
        wants = decisions.plan_show_wants(
            self._long_running(), have=set(), watched=set(), policy=policy
        )
        seasons = {w.season for w in wants}
        assert 1 in seasons, "a watchlisted series must produce something to search for"
        # Still does not drag in the whole back catalogue -- season 1 plus
        # anything not yet aired.
        assert seasons <= {1, 5}

    def test_a_never_watched_series_is_untouched_under_all(self, policy) -> None:
        policy.backlog_mode = "all"
        wants = decisions.plan_show_wants(
            self._long_running(), have=set(), watched=set(), policy=policy
        )
        assert {w.season for w in wants} == {1, 2, 3, 4, 5}

    def test_a_never_watched_series_still_takes_nothing_under_upcoming_only(
        self, policy
    ) -> None:
        policy.backlog_mode = "upcoming_only"
        wants = decisions.plan_show_wants(
            self._long_running(), have=set(), watched=set(), policy=policy
        )
        assert {w.season for w in wants} == {5}

    def test_backlog_upcoming_only_takes_nothing_old(self, policy) -> None:
        policy.assume_prior_seasons_watched = True
        policy.backlog_mode = "upcoming_only"
        wants = decisions.plan_show_wants(
            self._long_running(), have=set(), watched={(3, 1)}, policy=policy
        )
        assert {(w.season, w.episode) for w in wants} == {(5, 1)}

    def test_recently_aired_survives_upcoming_only(self, policy) -> None:
        """An episode from three days ago is 'keeping up', not backlog."""
        policy.backlog_mode = "upcoming_only"
        seasons = {
            1: [{"episode_number": 1, "name": "recent",
                 "air_date": (date.today() - timedelta(days=3)).isoformat()}]
        }
        wants = decisions.plan_show_wants(
            seasons, have=set(), watched=set(), policy=policy, backlog_grace_days=7
        )
        assert len(wants) == 1

    def _five_seasons(self, per_season=5):
        old = "2015-01-01"
        return {
            n: [{"episode_number": e, "name": f"s{n}e{e}", "air_date": old}
                for e in range(1, per_season + 1)]
            for n in range(1, 6)
        }

    def test_next_season_unlocks_one_episode_from_the_end(self, policy) -> None:
        """Storage-friendly sequential watching: fetch S+1 as you finish S."""
        seasons = self._five_seasons()
        # Watched up to S02E04 of a five-episode season -> S03 unlocks.
        assert decisions.unlocked_next_season(
            seasons, {(2, 4)}, lead_episodes=1
        ) == 3
        # Only up to S02E03 -> not yet.
        assert decisions.unlocked_next_season(
            seasons, {(2, 3)}, lead_episodes=1
        ) is None

    def test_last_season_unlocks_nothing(self, policy) -> None:
        seasons = self._five_seasons()
        assert decisions.unlocked_next_season(seasons, {(5, 5)}, lead_episodes=1) is None

    def test_sequential_pulls_the_next_season_past_current_season_mode(self, policy) -> None:
        policy.assume_prior_seasons_watched = True
        policy.backlog_mode = "current_season"
        policy.sequential_seasons = True
        policy.sequential_lead_episodes = 1
        seasons = self._five_seasons()

        wants = decisions.plan_show_wants(
            seasons, have=set(), watched={(2, 4)}, policy=policy
        )
        got = {(w.season, w.episode) for w in wants}
        # Finishes season 2 and pulls in the whole of season 3 -- but not 4 or 5.
        assert (2, 5) in got
        assert {k for k in got if k[0] == 3} == {(3, e) for e in range(1, 6)}
        assert not [k for k in got if k[0] > 3]

    def test_sequential_off_leaves_the_next_season_alone(self, policy) -> None:
        policy.assume_prior_seasons_watched = True
        policy.backlog_mode = "current_season"
        policy.sequential_seasons = False
        wants = decisions.plan_show_wants(
            self._five_seasons(), have=set(), watched={(2, 4)}, policy=policy
        )
        assert {w.season for w in wants} == {2}

    def test_sequential_lead_can_be_widened(self, policy) -> None:
        seasons = self._five_seasons()
        # With two episodes of lead, S02E03 of five is enough.
        assert decisions.unlocked_next_season(
            seasons, {(2, 3)}, lead_episodes=2
        ) == 3

    def test_season_length_ignores_specials(self) -> None:
        seasons = {1: [{"episode_number": 0}, {"episode_number": 1}, {"episode_number": 2}]}
        assert decisions.season_length(seasons, 1) == 2

    def test_all_episode_keys_skips_specials(self) -> None:
        seasons = {
            0: [{"episode_number": 1}],
            1: [{"episode_number": 1}, {"episode_number": 2}, {"episode_number": 0}],
        }
        assert decisions.all_episode_keys(seasons) == {(1, 1), (1, 2)}

    def test_specials_are_ignored(self, policy) -> None:
        seasons = {0: [{"episode_number": 1, "name": "Special", "air_date": "2019-01-01"}]}
        assert decisions.plan_show_wants(seasons, have=set(), watched=set(), policy=policy) == []


class TestGrabTargets:
    def test_many_missing_episodes_becomes_a_season_pack(self, policy) -> None:
        targets = decisions.plan_grab_targets([(1, 1), (1, 2), (1, 3), (1, 4)], policy)
        assert len(targets) == 1
        assert targets[0].is_pack
        assert targets[0].episode_count == 4

    def test_one_straggler_stays_a_single_episode(self, policy) -> None:
        """Re-downloading a 70 GB pack to fill one gap is the bug this prevents."""
        targets = decisions.plan_grab_targets([(1, 7)], policy)
        assert len(targets) == 1
        assert not targets[0].is_pack
        assert targets[0].episode == 7

    def test_pack_preference_can_be_switched_off(self, policy) -> None:
        policy.prefer_season_packs = False
        targets = decisions.plan_grab_targets([(1, 1), (1, 2), (1, 3), (1, 4)], policy)
        assert len(targets) == 4
        assert all(not t.is_pack for t in targets)

    def test_seasons_are_planned_independently(self, policy) -> None:
        targets = decisions.plan_grab_targets([(1, 1), (1, 2), (1, 3), (2, 9)], policy)
        assert [t.is_pack for t in targets] == [True, False]


class TestTiming:
    def test_aired_items_are_searchable(self) -> None:
        assert decisions.should_search(date.today() - timedelta(days=1))
        assert not decisions.should_search(date.today() + timedelta(days=2))

    def test_unknown_date_is_worth_a_look(self) -> None:
        assert decisions.should_search(None)

    def test_lead_time_starts_the_search_early(self) -> None:
        assert decisions.should_search(date.today(), lead_hours=48)

    def test_a_lead_under_a_day_still_counts(self) -> None:
        evening = datetime(2026, 3, 9, 18, 0)
        morning = datetime(2026, 3, 9, 6, 0)
        # Twelve hours before a midnight premiere is the previous evening.
        assert decisions.search_cutoff(12, now=evening) == date(2026, 3, 10)
        assert decisions.search_cutoff(12, now=morning) == date(2026, 3, 9)
        assert decisions.search_cutoff(0, now=evening) == date(2026, 3, 9)


def test_display_titles_read_naturally() -> None:
    pack = parse("Silo S02 2160p WEB-DL-GRP")
    single = parse("Silo S02E07 2160p WEB-DL-GRP")
    span = parse("The.Bear.S02E01-E03.1080p.WEB-DL-NTb")
    assert decisions.display_title("Silo", pack) == "Silo (Season 2)"
    assert decisions.display_title("Silo", single) == "Silo (S02E07)"
    assert decisions.display_title("The Bear", span) == "The Bear (S02E01-E03)"


def test_rejection_summary_names_the_dominant_rule(profile) -> None:
    from conduit.domain import scoring

    scored = scoring.rank(
        [make_release(f"Movie {n} 2020 480p DVDRip XviD-GRP", seeders=0) for n in range(3)],
        profile,
    )
    summary = decisions.summarise_rejections(scored)
    assert "3 releases rejected" in summary
    assert "seeders" in summary or "resolutions" in summary


def test_empty_result_says_so() -> None:
    assert decisions.summarise_rejections([]) == "no releases found on any indexer"
