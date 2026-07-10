"""Unit tests for phishpred.modes -- prediction modes 1 (tour), 2 (run), and
4 (chaser). See phish-predictor-modes-plan.md sections 2, 3, 5 and
CONTRACTS.md. No network; small in-memory DB built the same way
tests/test_features.py and tests/test_simulate.py do, with a fixed small
n_sims and seed for determinism.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from phishpred import db, features
from phishpred.config import era_for_year
from phishpred.models.heuristic import heuristic_predict
from phishpred.modes import (
    BUSTOUT_GAP_RATIO_THRESHOLD,
    LIKELY_THRESHOLD,
    LOCK_THRESHOLD,
    ChaserReport,
    RunReport,
    TourReport,
    _bucket_for,
    chaser_mode,
    resolve_run,
    resolve_song,
    resolve_tour_horizon,
    run_mode,
    tour_mode,
)
from phishpred.simulate import SimConfig

# ---------------------------------------------------------------------------
# Shared fixture DB
# ---------------------------------------------------------------------------
# History is dated 2022 (era "4.0" starts 2021) so it shares an era with the
# 2026 future shows -- mean_setlist_size(era) would otherwise be 0 for the
# future era and every probability would come out zero.

VENUES = [(1, "Alpha", "AlphaCity", 0), (2, "Beta", "BetaCity", 0), (3, "Gamma", "GammaCity", 0)]

SONGS = [
    (101, "tweezer", "Tweezer", 1),
    (102, "yem", "YEM", 1),
    (103, "wilson", "Wilson", 1),
    (104, "gin", "Bathtub Gin", 1),      # rare + overdue -> bustout-watch candidate
    (105, "filler", "Filler", 1),
    (106, "mikes-song", "Mike's Song", 1),   # never played -> never a MC candidate
    (107, "mikes-groove", "Mike's Groove", 1),  # for resolve_song ambiguity
]

HIST_SHOWS = [
    (1, 0, "2022-06-01", 1, 1),
    (2, 1, "2022-06-02", 1, 1),
    (3, 2, "2022-06-03", 1, 1),
    (4, 3, "2022-06-10", 2, 1),
    (5, 4, "2022-06-11", 2, 1),
    (6, 5, "2022-06-20", 3, 1),
    (7, 6, "2022-07-01", 1, 2),
    (8, 7, "2022-07-02", 1, 2),
    (9, 8, "2022-07-10", 2, 2),
    (10, 9, "2022-07-11", 2, 2),
]

HIST_SETLISTS = {
    1: [101, 103, 104, 102],   # Bathtub Gin play #1
    2: [101, 102],
    3: [101, 103],
    4: [101, 104, 102],        # Bathtub Gin play #2 (gap 3 -> median historical gap 3)
    5: [101, 103],
    6: [101, 102],
    7: [101, 103, 102],
    8: [101, 105, 102],        # Filler's only play
    9: [101, 102],
    10: [101, 103, 102],
}

# Rest-of-2026 tour (default horizon), a same-year "fall" tour, a 3-night run
# at Beta, and one next-year show that the default (current-year) filter must
# exclude.
FUTURE = [
    (1101, "2026-07-10", 1, 10, "2026 Summer Tour"),
    (1102, "2026-07-11", 1, 10, "2026 Summer Tour"),
    (1103, "2026-07-17", 3, 10, "2026 Summer Tour"),
    (1104, "2026-07-18", 3, 10, "2026 Summer Tour"),
    (1105, "2026-07-25", 1, 10, "2026 Summer Tour"),
    (1110, "2026-08-01", 2, 10, "2026 Summer Tour"),  # run night 1 (Beta)
    (1111, "2026-08-02", 2, 10, "2026 Summer Tour"),  # run night 2 (Beta)
    (1112, "2026-08-03", 2, 10, "2026 Summer Tour"),  # run night 3 (Beta)
    (1106, "2026-10-01", 1, 11, "2026 Fall Tour"),
    (1107, "2026-10-02", 1, 11, "2026 Fall Tour"),
    (1108, "2027-01-05", 1, 12, "2027 Winter Tour"),  # next year -> excluded by default
]

TOUR_ONLY = [1101, 1102, 1103, 1104, 1105]
RUN_SHOWIDS = [1110, 1111, 1112]
DEFAULT_HORIZON = [1101, 1102, 1103, 1104, 1105, 1110, 1111, 1112, 1106, 1107]


def _populate(conn):
    for vid, name, city, alias in VENUES:
        conn.execute(
            "INSERT INTO venues (venueid, name, city, alias) VALUES (?,?,?,?)", (vid, name, city, alias)
        )
    for sid, slug, name, iso in SONGS:
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,?)",
            (sid, slug, name, iso),
        )
    for showid, idx, showdate, vid, tour in HIST_SHOWS:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
            "VALUES (?,?,?,?,?,0)",
            (showid, showdate, vid, tour, idx),
        )
    for showid, showdate, vid, tourid, tour_name in FUTURE:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, tour_name, show_index, exclude) "
            "VALUES (?,?,?,?,?,NULL,0)",
            (showid, showdate, vid, tourid, tour_name),
        )
    for showid, songs in HIST_SETLISTS.items():
        for pos, songid in enumerate(songs):
            conn.execute(
                "INSERT INTO performances (showid, songid, set_label, position) VALUES (?,?,?,?)",
                (showid, songid, "1", pos),
            )
    conn.commit()


@pytest.fixture()
def conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    _populate(c)
    yield c
    c.close()


SMALL_CONFIG = SimConfig(n_sims=300, seed=123, model="heuristic")


# ---------------------------------------------------------------------------
# resolve_tour_horizon
# ---------------------------------------------------------------------------

def test_resolve_tour_horizon_default_is_rest_of_current_year(conn):
    # Freeze "today" to 2026-07-09 -- DEFAULT_HORIZON's fixture shows are all
    # dated 2026 (show 1108 is deliberately 2027 to prove the year filter
    # works), so this would silently break once the real clock rolls into
    # 2027 otherwise. Same pattern as tests/test_mcp.py's upcoming_shows fix.
    with patch("phishpred.modes.date") as mock_date:
        mock_date.today.return_value = date(2026, 7, 9)
        assert resolve_tour_horizon(conn) == DEFAULT_HORIZON


def test_resolve_tour_horizon_matches_future_show_ids_order(conn):
    with patch("phishpred.modes.date") as mock_date:
        mock_date.today.return_value = date(2026, 7, 9)
        full_order = features.future_show_ids(conn)
        horizon = resolve_tour_horizon(conn)
    # Subsequence check: the horizon preserves future_show_ids' relative order.
    positions = [full_order.index(sid) for sid in horizon]
    assert positions == sorted(positions)


def test_resolve_tour_horizon_named_tour_filter(conn):
    assert resolve_tour_horizon(conn, tour="fall") == [1106, 1107]
    assert resolve_tour_horizon(conn, tour="summer") == [
        1101, 1102, 1103, 1104, 1105, 1110, 1111, 1112,
    ]


def test_resolve_tour_horizon_explicit_year(conn):
    assert resolve_tour_horizon(conn, year=2027) == [1108]
    assert resolve_tour_horizon(conn, year=2026) == DEFAULT_HORIZON


# ---------------------------------------------------------------------------
# resolve_run
# ---------------------------------------------------------------------------

def test_resolve_run_explicit_dates(conn):
    result = resolve_run(conn, dates=["2026-08-01", "2026-08-02", "2026-08-03"])
    assert result == RUN_SHOWIDS


def test_resolve_run_venue_and_nights(conn):
    assert resolve_run(conn, venue="beta", nights=3) == RUN_SHOWIDS
    assert resolve_run(conn, venue="BETA", nights=2) == RUN_SHOWIDS[:2]
    assert resolve_run(conn, venue="betacity", nights=1) == RUN_SHOWIDS[:1]


def test_resolve_run_requires_dates_or_venue(conn):
    with pytest.raises(ValueError):
        resolve_run(conn)


# ---------------------------------------------------------------------------
# resolve_song
# ---------------------------------------------------------------------------

def test_resolve_song_exact_slug(conn):
    assert resolve_song(conn, "wilson") == (103, "wilson", "Wilson")


def test_resolve_song_substring_unique(conn):
    assert resolve_song(conn, "tweez") == (101, "tweezer", "Tweezer")


def test_resolve_song_ambiguous_raises_with_candidates(conn):
    with pytest.raises(ValueError) as excinfo:
        resolve_song(conn, "mike")
    msg = str(excinfo.value)
    assert "Mike's Song" in msg
    assert "Mike's Groove" in msg


def test_resolve_song_none_raises(conn):
    with pytest.raises(ValueError):
        resolve_song(conn, "zzznotasong")


# ---------------------------------------------------------------------------
# _bucket_for (pure threshold logic)
# ---------------------------------------------------------------------------

def test_bucket_thresholds():
    assert _bucket_for(LOCK_THRESHOLD, None) == "lock"
    assert _bucket_for(0.95, None) == "lock"
    assert _bucket_for(LIKELY_THRESHOLD, None) == "likely"
    assert _bucket_for(0.7, None) == "likely"
    assert _bucket_for(0.3, BUSTOUT_GAP_RATIO_THRESHOLD) == "bustout-watch"
    assert _bucket_for(0.3, 5.0) == "bustout-watch"
    assert _bucket_for(0.3, 1.0) == "longshot"
    assert _bucket_for(0.3, None) == "longshot"
    # Lock/likely take priority over gap_ratio even when overdue.
    assert _bucket_for(0.95, 5.0) == "lock"


# ---------------------------------------------------------------------------
# tour_mode
# ---------------------------------------------------------------------------

def test_tour_mode_expected_plays_and_p_at_least_one_in_range(conn):
    report = tour_mode(conn, TOUR_ONLY, SMALL_CONFIG)
    assert isinstance(report, TourReport)
    assert report.horizon_showids == TOUR_ONLY
    assert report.rows  # non-empty
    for row in report.rows:
        assert 0.0 <= row.p_at_least_one <= 1.0
        assert row.expected_plays >= 0.0
        # Expected plays cannot exceed the number of horizon shows.
        assert row.expected_plays <= len(TOUR_ONLY) + 1e-9


def test_tour_mode_frequent_song_has_high_p_at_least_one(conn):
    report = tour_mode(conn, TOUR_ONLY, SMALL_CONFIG)
    by_slug = {row.slug: row for row in report.rows}
    # Tweezer was played in every historical show -> should be near-certain
    # to appear at least once across a 5-show future horizon.
    assert by_slug["tweezer"].p_at_least_one > 0.7


def test_tour_mode_bucket_assignment_is_self_consistent(conn):
    report = tour_mode(conn, TOUR_ONLY, SMALL_CONFIG)
    for row in report.rows:
        assert row.bucket == _bucket_for(row.p_at_least_one, row.gap_ratio)
        assert row.bucket in {"lock", "likely", "longshot", "bustout-watch"}


def test_tour_mode_bathtub_gin_flagged_overdue(conn):
    report = tour_mode(conn, TOUR_ONLY, SMALL_CONFIG)
    by_slug = {row.slug: row for row in report.rows}
    gin = by_slug["gin"]
    # Bathtub Gin: 2 historical plays with gap 3, then a 7-show absence into
    # the horizon -> gap_ratio ~2.33, comfortably over the bustout threshold.
    assert gin.gap_ratio == pytest.approx(7 / 3, rel=1e-6)
    assert gin.gap_ratio >= BUSTOUT_GAP_RATIO_THRESHOLD


def test_tour_mode_sorted_by_expected_plays_desc(conn):
    report = tour_mode(conn, TOUR_ONLY, SMALL_CONFIG)
    expected = [row.expected_plays for row in report.rows]
    assert expected == sorted(expected, reverse=True)


def test_tour_mode_analytic_sanity_check_present_and_labeled(conn):
    report = tour_mode(conn, TOUR_ONLY, SMALL_CONFIG)
    for row in report.rows:
        assert row.analytic_p >= 0.0

    # Cross-check the Sigma-of-marginals arithmetic directly against
    # features_for_future_show + heuristic_predict for one song (Tweezer).
    expected_total = 0.0
    for showid in TOUR_ONLY:
        feat_df = features.features_for_future_show(conn, showid, SMALL_CONFIG.half_life)
        year = int(str(feat_df["showdate"].iloc[0])[:4])
        k = features.mean_setlist_size(conn, era_for_year(year))
        pred_df = heuristic_predict(feat_df, k)
        row = pred_df[pred_df["slug"] == "tweezer"]
        if not row.empty:
            expected_total += float(row["prob"].iloc[0])

    by_slug = {row.slug: row for row in report.rows}
    assert by_slug["tweezer"].analytic_p == pytest.approx(expected_total, rel=1e-9)

    # The rendered table and header must label the analytic column as an
    # approximation (plan §2: "ship it as a labeled approximation").
    text = report.render()
    assert "Analytic" in text
    assert "approximation" in text.lower()


def test_tour_mode_render_json_round_trips(conn):
    report = tour_mode(conn, TOUR_ONLY, SMALL_CONFIG)
    text = report.render(json_out=True)
    payload = json.loads(text)
    assert payload["model"] == "heuristic"
    assert len(payload["rows"]) == len(report.rows)
    for row in payload["rows"]:
        assert set(("song", "slug", "expected_plays", "p_at_least_one", "dist", "bucket")) <= set(
            row.keys()
        )


def test_tour_mode_render_text_non_empty(conn):
    report = tour_mode(conn, TOUR_ONLY, SMALL_CONFIG)
    text = report.render()
    assert text.strip()
    assert "Tweezer" in text


# ---------------------------------------------------------------------------
# run_mode
# ---------------------------------------------------------------------------

def test_run_mode_default_is_strict_no_repeat(conn):
    report = run_mode(conn, RUN_SHOWIDS, SMALL_CONFIG)
    assert isinstance(report, RunReport)
    assert report.strict_no_repeat is True
    assert report.run_showids == RUN_SHOWIDS


def test_run_mode_p_at_least_one_bounded_by_per_night_rates(conn):
    report = run_mode(conn, RUN_SHOWIDS, SMALL_CONFIG)
    assert report.rows
    for row in report.rows:
        total = sum(row.per_night_probs)
        assert row.p_at_least_one <= total + 1e-9
        assert row.p_at_least_one >= max(row.per_night_probs) - 1e-9


def test_run_mode_most_likely_night_is_valid_index(conn):
    report = run_mode(conn, RUN_SHOWIDS, SMALL_CONFIG)
    for row in report.rows:
        assert row.most_likely_night_index in (0, 1, 2)
        assert row.most_likely_night_date in report.run_dates


def test_run_mode_sorted_by_p_at_least_one_desc(conn):
    report = run_mode(conn, RUN_SHOWIDS, SMALL_CONFIG)
    probs = [row.p_at_least_one for row in report.rows]
    assert probs == sorted(probs, reverse=True)


def test_run_mode_soft_no_repeat_shows_joint_not_naive_sum(conn):
    # With soft (non-strict) no-repeat, a song can appear on more than one
    # night within a single simulation, so summing per-night marginals
    # overcounts vs the true joint P(>=1 in run) -- the plan's "would
    # triple-count Harry Hood" scenario.
    soft_config = SimConfig(n_sims=2000, seed=5, model="heuristic", strict_no_repeat=False)
    report = run_mode(conn, RUN_SHOWIDS, soft_config)
    top = report.rows[0]
    naive_sum = sum(top.per_night_probs)
    assert naive_sum > top.p_at_least_one + 0.05  # clearly overcounts


def test_run_mode_render_json_round_trips(conn):
    report = run_mode(conn, RUN_SHOWIDS, SMALL_CONFIG)
    text = report.render(json_out=True)
    payload = json.loads(text)
    assert payload["strict_no_repeat"] is True
    assert len(payload["rows"]) == len(report.rows)


def test_run_mode_render_text_non_empty(conn):
    report = run_mode(conn, RUN_SHOWIDS, SMALL_CONFIG)
    text = report.render()
    assert text.strip()
    assert "P(>=1 in run)" in text


# ---------------------------------------------------------------------------
# chaser_mode
# ---------------------------------------------------------------------------

def test_chaser_mode_never_played_song_always_misses(conn):
    # "Mike's Song" has zero historical performances -> never a MC candidate,
    # so it can never be sampled: a deterministic (seed-independent) miss.
    report = chaser_mode(conn, "mikes-song", TOUR_ONLY, SMALL_CONFIG)
    assert isinstance(report, ChaserReport)
    assert report.p_not_within_horizon == pytest.approx(1.0)
    assert report.modal_show_date is None
    assert report.median_show_date is None
    assert report.expected_shows_until_next_play is None
    assert report.historical_play_count == 0
    assert report.low_signal_caveat is True
    assert sum(entry.probability for entry in report.distribution) == pytest.approx(0.0)


def test_chaser_mode_frequent_song_has_low_miss_probability(conn):
    report = chaser_mode(conn, "tweezer", TOUR_ONLY, SMALL_CONFIG)
    assert report.p_not_within_horizon < 0.3
    assert report.modal_show_date in report.horizon_dates
    assert report.median_show_date in report.horizon_dates
    assert report.expected_shows_until_next_play is not None
    assert report.expected_shows_until_next_play >= 1.0


def test_chaser_mode_distribution_sums_with_miss_prob_to_at_most_one(conn):
    report = chaser_mode(conn, "gin", TOUR_ONLY, SMALL_CONFIG)
    total = sum(entry.probability for entry in report.distribution) + report.p_not_within_horizon
    assert total <= 1.0 + 1e-9
    assert total == pytest.approx(1.0, abs=1e-9)


def test_chaser_mode_low_signal_caveat_for_rare_song(conn):
    # Bathtub Gin: only 2 historical plays, comfortably under the threshold.
    report = chaser_mode(conn, "gin", TOUR_ONLY, SMALL_CONFIG)
    assert report.historical_play_count == 2
    assert report.low_signal_caveat is True
    text = report.render()
    assert "CAVEAT" in text


def test_chaser_mode_render_json_round_trips(conn):
    report = chaser_mode(conn, "wilson", TOUR_ONLY, SMALL_CONFIG)
    text = report.render(json_out=True)
    payload = json.loads(text)
    assert payload["song"] == "Wilson"
    assert len(payload["distribution"]) == len(TOUR_ONLY)


def test_chaser_mode_render_text_non_empty(conn):
    report = chaser_mode(conn, "wilson", TOUR_ONLY, SMALL_CONFIG)
    text = report.render()
    assert text.strip()
    assert "Wilson" in text


def test_chaser_mode_unresolvable_song_raises(conn):
    with pytest.raises(ValueError):
        chaser_mode(conn, "zzznotasong", TOUR_ONLY, SMALL_CONFIG)
