"""Unit tests for phishpred.slots.

Hand-built in-memory DB (mirrors tests/test_features.py's ``_populate`` style)
with shows whose set structure and positions are known, so slot classification,
propensities, and set-structure stats can be hand-computed and asserted exactly.
"""
from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from phishpred import config, db
from phishpred.slots import (
    SLOTS,
    classify_slot,
    sample_set_structure,
    set_structure_stats,
    slot_counts,
    slot_propensities,
)

# ---------------------------------------------------------------------------
# Hand-crafted history
# ---------------------------------------------------------------------------
# songid -> slug/name; all original, doesn't matter for slots.
SONGS = {
    1001: "opener",     # always set1 rank1 -> set1-open
    1002: "mid-a",       # set1-mid in shows 1 & 3 (era 3.0 & 4.0)
    1003: "closer",      # always set1 rank(last) -> set1-close
    1007: "mid-b",       # set1-mid in show 2 only
    1004: "set2-opener",  # always set2 rank1 -> set2-open
    1005: "set2-closer",  # always set2 rank(last) -> set2-close
    1006: "encore-song",  # always sole encore song ('e' or 'e2') -> encore
    2001: "single-set-song",  # single-song set1 -> classify as set1-open
    3001: "set4-open",   # set '4', rank1 of 2 -> set3-open
    3002: "set4-close",  # set '4', rank2 of 2 -> set3-close
    5001: "era-shift-song",  # set1-open in era 2.5, set2-close in era 4.0
    9999: "filler",      # dummy opener paired with 5001 in show 7
}

VENUES = [(1, "Venue", 0)]

# showid, year, showdate
SHOWS = {
    1: "2010-06-01",  # era 3.0
    2: "2010-06-02",  # era 3.0
    3: "2021-06-01",  # era 4.0
    4: "2010-07-01",  # era 3.0 -- single-song set1
    5: "2010-08-01",  # era 3.0 -- set '4'
    6: "2005-06-01",  # era 2.5 -- era-shift song, set1-open
    7: "2021-07-01",  # era 4.0 -- era-shift song, set2-close
}

# showid -> list of (songid, set_label) in position order
PERFORMANCES: dict[int, list[tuple[int, str]]] = {
    1: [
        (1001, "1"), (1002, "1"), (1003, "1"),
        (1004, "2"), (1005, "2"),
        (1006, "e"),
    ],
    2: [
        (1001, "1"), (1007, "1"), (1003, "1"),
        (1004, "2"), (1005, "2"),
        (1006, "e"),
    ],
    3: [
        (1001, "1"), (1002, "1"), (1003, "1"),
        (1004, "2"), (1005, "2"),
        (1006, "e2"),
    ],
    4: [
        (2001, "1"),
    ],
    5: [
        (1001, "1"),
        (1004, "2"),
        (3001, "4"), (3002, "4"),
    ],
    6: [
        (5001, "1"),
    ],
    7: [
        (1001, "1"),
        (9999, "2"), (5001, "2"),
    ],
}


def _populate(conn):
    for vid, name, alias in VENUES:
        conn.execute("INSERT INTO venues (venueid, name, alias) VALUES (?,?,?)", (vid, name, alias))
    for sid, slug in SONGS.items():
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,1)",
            (sid, slug, slug),
        )
    for idx, (showid, date) in enumerate(SHOWS.items()):
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
            "VALUES (?,?,?,?,?,0)",
            (showid, date, 1, 900, idx),
        )
    for showid, perf in PERFORMANCES.items():
        for pos, (songid, set_label) in enumerate(perf, start=1):
            conn.execute(
                "INSERT INTO performances (showid, songid, set_label, position) VALUES (?,?,?,?)",
                (showid, songid, set_label, pos),
            )
    conn.commit()


@pytest.fixture()
def conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    _populate(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# classify_slot
# ---------------------------------------------------------------------------

def test_classify_slot_open_mid_close():
    assert classify_slot("1", 1, 3) == "set1-open"
    assert classify_slot("1", 2, 3) == "set1-mid"
    assert classify_slot("1", 3, 3) == "set1-close"
    assert classify_slot("2", 1, 2) == "set2-open"
    assert classify_slot("2", 2, 2) == "set2-close"
    assert classify_slot("3", 2, 5) == "set3-mid"


def test_classify_slot_encore_ignores_rank():
    assert classify_slot("e", 1, 1) == "encore"
    assert classify_slot("e2", 1, 4) == "encore"
    assert classify_slot("e3", 4, 4) == "encore"


def test_classify_slot_set4_folds_into_set3():
    assert classify_slot("4", 1, 2) == "set3-open"
    assert classify_slot("4", 2, 2) == "set3-close"


def test_classify_slot_single_song_set_prefers_open():
    # rank == 1 == set_len: documented tie-break is "-open".
    assert classify_slot("1", 1, 1) == "set1-open"
    assert classify_slot("2", 1, 1) == "set2-open"


def test_slots_taxonomy_constant():
    assert "encore" in SLOTS
    assert "set1-open" in SLOTS and "set2-close" in SLOTS


# ---------------------------------------------------------------------------
# slot_counts
# ---------------------------------------------------------------------------

def test_slot_counts_matches_hand_computation(conn):
    counts = slot_counts(conn)

    # 1001: set1-open in shows 1,2,3 + single-song set1 in shows 5,7 -> 5.
    assert counts[1001] == {"set1-open": 5}
    # 1002: set1-mid in shows 1 and 3.
    assert counts[1002] == {"set1-mid": 2}
    # 1003: set1-close in shows 1,2,3.
    assert counts[1003] == {"set1-close": 3}
    # 1007: set1-mid in show 2 only.
    assert counts[1007] == {"set1-mid": 1}
    # 1004: set2-open in shows 1,2,3 + single-song set2 in show5 -> 4.
    assert counts[1004] == {"set2-open": 4}
    # 1005: set2-close in shows 1,2,3.
    assert counts[1005] == {"set2-close": 3}
    # 1006: encore in shows 1 ('e'), 2 ('e'), 3 ('e2') -> 3.
    assert counts[1006] == {"encore": 3}
    # 2001: single-song set1 in show 4 -> open.
    assert counts[2001] == {"set1-open": 1}
    # 3001/3002: set '4' with 2 songs -> set3-open / set3-close.
    assert counts[3001] == {"set3-open": 1}
    assert counts[3002] == {"set3-close": 1}
    # era-shift song: set1-open (show6) + set2-close (show7).
    assert counts[5001] == {"set1-open": 1, "set2-close": 1}


# ---------------------------------------------------------------------------
# slot_propensities
# ---------------------------------------------------------------------------

def test_propensities_raw_song_always_first_in_set1(conn):
    props = slot_propensities(conn, era_weighted=False)
    assert props[1001] == pytest.approx({"set1-open": 1.0})


def test_propensities_raw_song_always_encore(conn):
    props = slot_propensities(conn, era_weighted=False)
    assert props[1006] == pytest.approx({"encore": 1.0})


def test_propensities_sum_to_one(conn):
    for era_weighted in (False, True):
        props = slot_propensities(conn, era_weighted=era_weighted)
        for songid, dist in props.items():
            assert sum(dist.values()) == pytest.approx(1.0), (era_weighted, songid, dist)


def test_propensities_raw_mixed_slots(conn):
    # era-shift song: 1 occurrence each in two different slots -> 50/50 raw.
    props = slot_propensities(conn, era_weighted=False)
    assert props[5001] == pytest.approx({"set1-open": 0.5, "set2-close": 0.5})


def test_propensities_era_weighted_default_favors_recent_era(conn):
    # 5001: set1-open in era "2.5" (2005), set2-close in era "4.0" (2021).
    # _ERA_WEIGHTS: era index in config.ERAS -> 2**index.
    # "2.5" is index 2 -> weight 4; "4.0" is index 4 -> weight 16.
    # -> set1-open = 4/20 = 0.2, set2-close = 16/20 = 0.8.
    props = slot_propensities(conn, era_weighted=True)
    assert props[5001] == pytest.approx({"set1-open": 0.2, "set2-close": 0.8})


def test_propensities_half_life_years_overrides_era_buckets(conn):
    # Anchor year = max show year in the whole DB = 2021 (show 7).
    # 5001 appears at year 2005 (set1-open) and year 2021 (set2-close).
    # With half_life_years=16: weight(2005) = 0.5**(16/16) = 0.5;
    # weight(2021) = 0.5**0 = 1.0 -> total 1.5.
    props = slot_propensities(conn, era_weighted=True, half_life_years=16)
    assert props[5001] == pytest.approx({"set1-open": 0.5 / 1.5, "set2-close": 1.0 / 1.5})


def test_propensities_era_weighted_false_ignores_half_life(conn):
    props = slot_propensities(conn, era_weighted=False, half_life_years=1)
    assert props[5001] == pytest.approx({"set1-open": 0.5, "set2-close": 0.5})


def test_propensities_only_includes_observed_songs(conn):
    props = slot_propensities(conn)
    assert 999999 not in props
    assert set(props.keys()) == set(SONGS.keys())


# ---------------------------------------------------------------------------
# set_structure_stats
# ---------------------------------------------------------------------------

def test_set_structure_stats_era_3(conn):
    stats = set_structure_stats(conn, era="3.0")
    # Shows 1,2,4,5 are era 3.0 (year 2010).
    assert stats["n_shows"] == 4

    assert stats["num_sets_dist"] == Counter({2: 2, 1: 1, 3: 1})
    assert stats["num_encores_dist"] == Counter({1: 2, 0: 2})

    set1_lengths = [3, 3, 1, 1]  # shows 1,2,4,5
    assert stats["set_lengths"]["1"]["mean"] == pytest.approx(statistics.mean(set1_lengths))
    assert stats["set_lengths"]["1"]["std"] == pytest.approx(statistics.pstdev(set1_lengths))
    assert stats["set_lengths"]["1"]["hist"] == Counter(set1_lengths)

    set2_lengths = [2, 2, 1]  # shows 1,2,5
    assert stats["set_lengths"]["2"]["mean"] == pytest.approx(statistics.mean(set2_lengths))
    assert stats["set_lengths"]["2"]["hist"] == Counter(set2_lengths)

    set4_lengths = [2]  # show 5
    assert stats["set_lengths"]["4"]["mean"] == pytest.approx(2.0)
    assert stats["set_lengths"]["4"]["hist"] == Counter(set4_lengths)

    encore_lengths = [1, 1]  # shows 1,2 (combined e/e2/e3 per show)
    assert stats["set_lengths"]["encore"]["mean"] == pytest.approx(1.0)
    assert stats["set_lengths"]["encore"]["hist"] == Counter(encore_lengths)


def test_set_structure_stats_era_4(conn):
    stats = set_structure_stats(conn, era="4.0")
    # Shows 3, 7 are era 4.0 (year 2021).
    assert stats["n_shows"] == 2
    assert stats["num_sets_dist"] == Counter({2: 2})
    assert stats["num_encores_dist"] == Counter({1: 1, 0: 1})

    set1_lengths = [3, 1]  # show3, show7
    assert stats["set_lengths"]["1"]["hist"] == Counter(set1_lengths)
    set2_lengths = [2, 2]  # show3, show7
    assert stats["set_lengths"]["2"]["hist"] == Counter(set2_lengths)
    assert stats["set_lengths"]["encore"]["hist"] == Counter([1])  # only show3 has an encore


def test_set_structure_stats_no_era_filter_covers_all(conn):
    stats = set_structure_stats(conn)
    assert stats["n_shows"] == len(SHOWS)


def test_set_structure_stats_empty_era_returns_zeros(conn):
    stats = set_structure_stats(conn, era="1.0")
    assert stats["n_shows"] == 0
    assert stats["num_sets_dist"] == Counter()
    assert stats["set_lengths"]["encore"] == {"mean": 0.0, "std": 0.0, "hist": Counter()}


# ---------------------------------------------------------------------------
# sample_set_structure
# ---------------------------------------------------------------------------

def test_sample_set_structure_deterministic(conn):
    rng1 = np.random.default_rng(42)
    result1 = sample_set_structure(conn, "4.0", rng1)
    rng2 = np.random.default_rng(42)
    result2 = sample_set_structure(conn, "4.0", rng2)
    assert result1 == result2


def test_sample_set_structure_plausible_shape(conn):
    rng = np.random.default_rng(7)
    for _ in range(20):
        skeleton = sample_set_structure(conn, "4.0", rng)
        assert set(skeleton.keys()) <= {"1", "2", "e"}
        # era 4.0 fixture: set '2' is always length 2.
        if "2" in skeleton:
            assert skeleton["2"] == 2
        # era 4.0 fixture: set '1' is length 1 or 3.
        if "1" in skeleton:
            assert skeleton["1"] in (1, 3)
        # era 4.0 fixture: encore, when present, is always length 1.
        if "e" in skeleton:
            assert skeleton["e"] == 1


def test_sample_set_structure_mean_matches_empirical(conn):
    # set '1' length in era 4.0 fixture is 1 or 3 with equal empirical mass
    # (one observation each) -> expected mean 2.0. Sample many draws from a
    # single advancing rng and check convergence.
    rng = np.random.default_rng(123)
    draws = [sample_set_structure(conn, "4.0", rng).get("1") for _ in range(400)]
    draws = [d for d in draws if d is not None]
    assert draws  # sanity: set '1' appears in most/all draws
    assert statistics.mean(draws) == pytest.approx(2.0, abs=0.3)


def test_sample_set_structure_unknown_era_returns_empty(conn):
    rng = np.random.default_rng(1)
    assert sample_set_structure(conn, "1.0", rng) == {}


# ---------------------------------------------------------------------------
# Real-data smoke test (guarded, optional)
# ---------------------------------------------------------------------------

def test_real_db_smoke():
    if not Path(config.DB_PATH).exists():
        pytest.skip("data/phish.db not present")
    real_conn = db.get_connection(config.DB_PATH)
    try:
        props = slot_propensities(real_conn)
        stats = set_structure_stats(real_conn, era="4.0")
        assert props  # runs without error, non-empty
        assert stats["n_shows"] > 0
        # Documented era-4 means: Set1 ~9.2, Set2 ~7.3, encore ~2.1. Generous
        # tolerance -- this is a sanity check, not a precise regression test.
        if "1" in stats["set_lengths"]:
            assert stats["set_lengths"]["1"]["mean"] == pytest.approx(9.2, abs=3.0)
        if "2" in stats["set_lengths"]:
            assert stats["set_lengths"]["2"]["mean"] == pytest.approx(7.3, abs=3.0)
        assert stats["set_lengths"]["encore"]["mean"] == pytest.approx(2.1, abs=2.0)

        rng = np.random.default_rng(0)
        skeleton = sample_set_structure(real_conn, "4.0", rng)
        assert skeleton  # non-empty for a well-populated era
    finally:
        real_conn.close()
