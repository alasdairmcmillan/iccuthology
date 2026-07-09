"""Unit tests for phishpred.simulate — the forward Monte-Carlo setlist simulator.

No network. Small in-memory DBs, built the same way tests/test_features.py does.
Covers the plan's acceptance criteria: marginal recovery, no-repeat-within-a-run,
determinism, and the samples[] shape contract.
"""
from __future__ import annotations

import pytest

from phishpred import db
from phishpred.features import features_for_future_show, mean_setlist_size
from phishpred.models.heuristic import heuristic_predict
from phishpred.simulate import SimConfig, simulate_horizon

# ---------------------------------------------------------------------------
# Scenario 1: single future show, for marginal-recovery + determinism + shape.
# ---------------------------------------------------------------------------
# Six songs with varied history so several land at meaningfully different
# probabilities; one future show at a brand-new venue (no run bleed-through).

_M1_VENUES = [(1, "Alpha", 0), (2, "Gamma", 0)]
_M1_SONGS = [
    (101, "tweezer", "Tweezer", 1),
    (102, "yem", "YEM", 1),
    (103, "wilson", "Wilson", 1),
    (104, "gin", "Bathtub Gin", 1),
]
_M1_SHOWS = [
    (1, 0, "2010-06-01", 1, 100),
    (2, 1, "2010-06-02", 1, 100),
    (3, 2, "2010-06-03", 1, 100),
    (4, 3, "2010-06-04", 1, 100),
    (5, 4, "2010-06-10", 1, 100),  # gap before this show, breaks played_prev_show
]
_M1_SETLISTS = {
    1: [101, 102, 103],
    2: [101, 102, 103],
    3: [101, 102],
    4: [101],
    5: [104],  # filler night right before the future show
}
# Future show at a NEW venue (2) -> no preceding run context, clean marginals.
_M1_FUTURE = (6, None, "2010-06-20", 2, 100)


def _populate(conn, venues, songs, shows, setlists, future=None):
    for vid, name, alias in venues:
        conn.execute(
            "INSERT INTO venues (venueid, name, alias) VALUES (?,?,?)", (vid, name, alias)
        )
    for sid, slug, name, iso in songs:
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,?)",
            (sid, slug, name, iso),
        )
    for showid, idx, date, vid, tour in shows:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
            "VALUES (?,?,?,?,?,0)",
            (showid, date, vid, tour, idx),
        )
    if future is not None:
        futures = future if isinstance(future, list) else [future]
        for showid, idx, date, vid, tour in futures:
            conn.execute(
                "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
                "VALUES (?,?,?,?,?,0)",
                (showid, date, vid, tour, idx),
            )
    for showid, songs_ in setlists.items():
        for pos, songid in enumerate(songs_):
            conn.execute(
                "INSERT INTO performances (showid, songid, set_label, position) "
                "VALUES (?,?,?,?)",
                (showid, songid, "1", pos),
            )
    conn.commit()


@pytest.fixture()
def single_show_conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    _populate(c, _M1_VENUES, _M1_SONGS, _M1_SHOWS, _M1_SETLISTS, _M1_FUTURE)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. Marginal recovery: MC inclusion rate ~= calibrated per-song prob.
# ---------------------------------------------------------------------------

def test_marginal_recovery_matches_heuristic_prob(single_show_conn):
    conn = single_show_conn
    feat_df = features_for_future_show(conn, showid=6, half_life=50)
    k = mean_setlist_size(conn, era="3.0")
    calibrated = heuristic_predict(feat_df, k)
    expected = dict(zip(calibrated["songid"], calibrated["prob"]))

    config = SimConfig(n_sims=3000, seed=42, strict_no_repeat=False, model="heuristic")
    result = simulate_horizon(conn, [6], config)
    assert len(result.samples) == 3000

    n = len(result.samples)
    for songid, p in expected.items():
        inclusion_rate = sum(1 for sim in result.samples if songid in sim[0]) / n
        assert inclusion_rate == pytest.approx(p, abs=0.03), (
            f"song {songid}: expected ~{p:.3f}, observed {inclusion_rate:.3f}"
        )


def test_marginal_recovery_covers_a_high_and_low_prob_song(single_show_conn):
    conn = single_show_conn
    feat_df = features_for_future_show(conn, showid=6, half_life=50)
    k = mean_setlist_size(conn, era="3.0")
    calibrated = heuristic_predict(feat_df, k)
    probs = dict(zip(calibrated["songid"], calibrated["prob"]))
    assert max(probs.values()) - min(probs.values()) > 0.1  # scenario has real spread


# ---------------------------------------------------------------------------
# 2. Shape / contract.
# ---------------------------------------------------------------------------

def test_shape_and_songid_contract(single_show_conn):
    conn = single_show_conn
    config = SimConfig(n_sims=25, seed=1, model="heuristic")
    result = simulate_horizon(conn, [6], config)

    assert len(result.samples) == 25
    known_songids = set(result.songs_meta.keys())
    assert known_songids  # non-empty
    for sim in result.samples:
        assert len(sim) == 1  # one horizon show
        for songid_set in sim:
            assert songid_set <= known_songids

    assert result.horizon_showids == [6]
    assert result.horizon_dates == ["2010-06-20"]
    assert result.horizon_venueids == [2]


# ---------------------------------------------------------------------------
# 3. Determinism.
# ---------------------------------------------------------------------------

def test_determinism_same_seed_same_samples(single_show_conn):
    conn = single_show_conn
    config = SimConfig(n_sims=50, seed=7, model="heuristic")
    a = simulate_horizon(conn, [6], config)
    b = simulate_horizon(conn, [6], config)
    assert a.samples == b.samples


def test_determinism_different_seed_differs(single_show_conn):
    conn = single_show_conn
    a = simulate_horizon(conn, [6], SimConfig(n_sims=50, seed=1, model="heuristic"))
    b = simulate_horizon(conn, [6], SimConfig(n_sims=50, seed=2, model="heuristic"))
    assert a.samples != b.samples


# ---------------------------------------------------------------------------
# Scenario 2: three-night same-venue future run, for no-repeat behavior.
# ---------------------------------------------------------------------------

_M2_VENUES = [(1, "Alpha", 0), (2, "Beta", 0)]
_M2_SONGS = [
    (101, "tweezer", "Tweezer", 1),
    (102, "yem", "YEM", 1),
    (103, "wilson", "Wilson", 1),
    (104, "filler", "Filler", 1),
]
_M2_SHOWS = [
    (1, 0, "2010-06-01", 1, 100),
    (2, 1, "2010-06-02", 1, 100),
    (3, 2, "2010-06-03", 1, 100),
    (4, 3, "2010-06-04", 1, 100),
    (5, 4, "2010-06-05", 1, 100),  # filler-only night right before the run
]
_M2_SETLISTS = {
    1: [101, 102, 103],
    2: [101, 102, 103],
    3: [101, 102, 103],
    4: [101, 102, 103],
    5: [104],
}
# Three-night run at a brand-new venue (2), consecutive future showids/dates.
_M2_FUTURE = [
    (10, None, "2010-06-10", 2, 100),
    (11, None, "2010-06-11", 2, 100),
    (12, None, "2010-06-12", 2, 100),
]


@pytest.fixture()
def run_conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    _populate(c, _M2_VENUES, _M2_SONGS, _M2_SHOWS, _M2_SETLISTS, _M2_FUTURE)
    yield c
    c.close()


def _repeats(samples):
    """Count of (sim, song) pairs where the song appears in >1 horizon night."""
    total = 0
    for sim in samples:
        counts: dict[int, int] = {}
        for night in sim:
            for songid in night:
                counts[songid] = counts.get(songid, 0) + 1
        total += sum(1 for c in counts.values() if c > 1)
    return total


def test_strict_no_repeat_hard_masks_within_run(run_conn):
    config = SimConfig(n_sims=500, seed=3, strict_no_repeat=True, model="heuristic")
    result = simulate_horizon(run_conn, [10, 11, 12], config)

    assert _repeats(result.samples) == 0
    # sanity: songs are actually being sampled at all (test isn't vacuous).
    total_picks = sum(len(night) for sim in result.samples for night in sim)
    assert total_picks > 0


def test_soft_no_repeat_allows_rare_cross_night_repeats(run_conn):
    config = SimConfig(n_sims=2000, seed=3, strict_no_repeat=False, model="heuristic")
    result = simulate_horizon(run_conn, [10, 11, 12], config)

    repeats = _repeats(result.samples)
    total_picks = sum(len(night) for sim in result.samples for night in sim)
    assert total_picks > 0
    # Soft penalty (m_prev_show / m_in_run multipliers) makes repeats rare but
    # not impossible -- some should occur, and they should be a small minority.
    assert 0 < repeats < total_picks * 0.5


def test_run_grouping_detected_for_three_night_horizon(run_conn):
    from phishpred.simulate import _horizon_steps
    from phishpred.features import build_state_to_now

    _, _, max_index = build_state_to_now(run_conn, half_life=50)
    steps = _horizon_steps(run_conn, [10, 11, 12], max_index)
    assert steps[0]["run_start_index"] == steps[0]["index"]
    assert steps[1]["run_start_index"] == steps[0]["index"]
    assert steps[2]["run_start_index"] == steps[0]["index"]
