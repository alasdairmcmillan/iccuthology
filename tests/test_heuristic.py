"""Unit tests for phishpred.models.heuristic — synthetic DataFrames only, no DB."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from phishpred.features import FEATURE_COLUMNS
from phishpred.models.heuristic import heuristic_predict, heuristic_scores

DEFAULTS = {
    "decayed_rate": 0.1,
    "gap": 10,
    "gap_ratio": 1.0,
    "played_prev_show": 0,
    "played_in_run": 0,
    "venue_gap": 999,
    "plays_this_tour": 0,
    "plays_last_10": 0,
    "plays_last_50": 0,
    "plays_last_150": 0,
    "song_age_shows": 100,
    "era_rate": 0.1,
    "is_original": 1,
}


def make_row(**overrides) -> dict:
    row = dict(DEFAULTS)
    row.update(overrides)
    return row


def make_df(rows, showid=1, start_songid=1) -> pd.DataFrame:
    """Build a feature frame with FEATURE_COLUMNS + id columns from a list of
    per-row override dicts."""
    records = []
    for i, overrides in enumerate(rows):
        r = make_row(**overrides)
        r["showid"] = overrides.pop("showid", showid) if "showid" in overrides else showid
        r["songid"] = start_songid + i
        records.append(r)
    df = pd.DataFrame(records)
    # Ensure all FEATURE_COLUMNS are present (contract requirement).
    for col in FEATURE_COLUMNS:
        assert col in df.columns
    return df


def test_all_feature_columns_present_sanity():
    df = make_df([{}])
    for col in FEATURE_COLUMNS:
        assert col in df.columns


# ---------------------------------------------------------------------------
# Multiplier branch tests
# ---------------------------------------------------------------------------

def test_m_prev_show_multiplier():
    df = make_df([
        {"played_prev_show": 1, "played_in_run": 0},
        {"played_prev_show": 0, "played_in_run": 0},
    ])
    result = heuristic_scores(df)
    assert result.loc[0, "m_prev_show"] == pytest.approx(0.02)
    assert result.loc[1, "m_prev_show"] == pytest.approx(1.0)


def test_m_in_run_multiplier_fires_only_without_prev_show():
    df = make_df([
        {"played_prev_show": 0, "played_in_run": 1},  # in_run fires
        {"played_prev_show": 1, "played_in_run": 1},  # prev_show wins, in_run suppressed
        {"played_prev_show": 0, "played_in_run": 0},  # neither fires
    ])
    result = heuristic_scores(df)
    assert result.loc[0, "m_in_run"] == pytest.approx(0.05)
    assert result.loc[1, "m_in_run"] == pytest.approx(1.0)
    assert result.loc[2, "m_in_run"] == pytest.approx(1.0)


def test_in_run_not_double_penalized_with_prev_show():
    """When both played_prev_show and played_in_run are true, only the
    prev-show multiplier (0.02) should apply -- m_in_run must stay 1.0, not
    stack an additional 0.05 penalty."""
    df = make_df([{"played_prev_show": 1, "played_in_run": 1, "decayed_rate": 0.4}])
    result = heuristic_scores(df)
    row = result.loc[0]
    assert row["m_prev_show"] == pytest.approx(0.02)
    assert row["m_in_run"] == pytest.approx(1.0)
    # Overall score should reflect only the 0.02 penalty (times venue/due),
    # not an additional 0.05 stacked on top.
    expected_score = row["decayed_rate"] * 0.02 * 1.0 * row["m_venue"] * row["m_due"]
    assert row["score"] == pytest.approx(expected_score)


def test_m_venue_multiplier():
    df = make_df([
        {"venue_gap": 0},
        {"venue_gap": 2},
        {"venue_gap": 3},
        {"venue_gap": 999},
    ])
    result = heuristic_scores(df)
    assert result.loc[0, "m_venue"] == pytest.approx(0.3)
    assert result.loc[1, "m_venue"] == pytest.approx(0.3)
    assert result.loc[2, "m_venue"] == pytest.approx(1.0)
    assert result.loc[3, "m_venue"] == pytest.approx(1.0)


def test_m_due_multiplier_formula_and_clipping():
    df = make_df([
        {"gap_ratio": 1.0},   # clip(0,0,2) -> 0 -> m_due = 1.0
        {"gap_ratio": 0.2},   # clip(-0.8,0,2) -> 0 -> m_due = 1.0 (below-median gap doesn't discount)
        {"gap_ratio": 2.0},   # clip(1,0,2) -> 1 -> m_due = 1.3
        {"gap_ratio": 3.0},   # clip(2,0,2) -> 2 -> m_due = 1.6
        {"gap_ratio": 10.0},  # clip(9,0,2) -> 2 -> m_due = 1.6 (clipped, no runaway boost)
    ])
    result = heuristic_scores(df)
    assert result.loc[0, "m_due"] == pytest.approx(1.0)
    assert result.loc[1, "m_due"] == pytest.approx(1.0)
    assert result.loc[2, "m_due"] == pytest.approx(1.3)
    assert result.loc[3, "m_due"] == pytest.approx(1.6)
    assert result.loc[4, "m_due"] == pytest.approx(1.6)


# ---------------------------------------------------------------------------
# Blended base rate: recent-rate floor gated by w_recent
# ---------------------------------------------------------------------------

def test_steady_rare_song_gets_recent_rate_floor():
    """A steady-but-rare rotation song (a few plays in the last 150 shows,
    gap_ratio near 1) whose decayed_rate has sagged to near zero must be scored
    from the long-window floor, not the sagging decayed_rate."""
    df = make_df([
        {"decayed_rate": 0.001, "plays_last_150": 4, "gap_ratio": 1.5},
    ])
    result = heuristic_scores(df)
    row = result.loc[0]

    rate = 4 / 150
    w = (4.0 - 1.5) / 3.0  # 5/6 — mid-fade, still mostly floored
    assert row["recent_rate"] == pytest.approx(rate)
    assert row["w_recent"] == pytest.approx(w)
    # No multipliers fire except m_due = 1 + 0.3*0.5 = 1.15; base == w * rate.
    assert row["score"] == pytest.approx(w * rate * 1.15)
    assert row["score"] > 0.001  # decisively above what decayed_rate alone gives


def test_w_recent_fade_boundaries():
    """w_recent = clip((4 - gap_ratio)/3, 0, 1): full floor while gap_ratio<=1,
    linear fade, dead at gap_ratio>=4."""
    df = make_df([
        {"gap_ratio": 0.5},
        {"gap_ratio": 1.0},
        {"gap_ratio": 2.5},
        {"gap_ratio": 4.0},
        {"gap_ratio": 10.0},
    ])
    result = heuristic_scores(df)
    assert result.loc[0, "w_recent"] == pytest.approx(1.0)
    assert result.loc[1, "w_recent"] == pytest.approx(1.0)
    assert result.loc[2, "w_recent"] == pytest.approx(0.5)
    assert result.loc[3, "w_recent"] == pytest.approx(0.0)
    assert result.loc[4, "w_recent"] == pytest.approx(0.0)


def test_long_dormant_song_gets_no_floor():
    """A long-dormant song (gap_ratio >= 4) gets NO floor even with a nonzero
    long-window play count: base == decayed_rate, so score is exactly
    decayed_rate * m_due (the capped due boost stays the only bust-out path)."""
    df = make_df([
        {"decayed_rate": 0.0004, "plays_last_150": 30, "gap_ratio": 8.0},
    ])
    result = heuristic_scores(df)
    row = result.loc[0]
    assert row["w_recent"] == pytest.approx(0.0)
    # m_due clipped at 1.6; base must be decayed_rate, not 30/150.
    assert row["score"] == pytest.approx(0.0004 * 1.6)


def test_blend_never_lowers_score_vs_decayed_rate_alone():
    """base = max(decayed_rate, w*recent_rate) >= decayed_rate elementwise, so
    the blended score is never below the pure-decayed_rate score."""
    rows = []
    for dr in (0.0, 0.001, 0.05, 0.3):
        for p150 in (0, 2, 10, 60):
            for gr in (0.5, 1.0, 2.5, 5.0, 7.0):
                rows.append({
                    "decayed_rate": dr, "plays_last_150": p150, "gap_ratio": gr,
                    "played_prev_show": int(dr > 0.2), "venue_gap": 1 if p150 > 5 else 999,
                })
    df = make_df(rows)
    result = heuristic_scores(df)
    old_score = (
        result["decayed_rate"] * result["m_prev_show"] * result["m_in_run"]
        * result["m_venue"] * result["m_due"]
    )
    assert (result["score"] >= old_score - 1e-15).all()


# ---------------------------------------------------------------------------
# Score arithmetic on hand-computed rows
# ---------------------------------------------------------------------------

def test_score_arithmetic_hand_computed():
    df = make_df([
        # played_prev_show, low venue_gap, due boost -> stack all multipliers
        {
            "decayed_rate": 0.5,
            "played_prev_show": 1,
            "played_in_run": 0,
            "venue_gap": 1,
            "gap_ratio": 2.5,
        },
        # nothing fires -> score == decayed_rate
        {
            "decayed_rate": 0.25,
            "played_prev_show": 0,
            "played_in_run": 0,
            "venue_gap": 50,
            "gap_ratio": 1.0,
        },
        # only in_run fires, moderate due boost
        {
            "decayed_rate": 0.1,
            "played_prev_show": 0,
            "played_in_run": 1,
            "venue_gap": 999,
            "gap_ratio": 1.5,
        },
    ])
    result = heuristic_scores(df)

    # Row 0: m_prev_show=0.02, m_in_run=1.0, m_venue=0.3 (gap<=2),
    # m_due = 1 + 0.3*clip(1.5,0,2) = 1 + 0.3*1.5 = 1.45
    expected0 = 0.5 * 0.02 * 1.0 * 0.3 * 1.45
    assert result.loc[0, "score"] == pytest.approx(expected0)

    # Row 1: all multipliers 1.0 -> score == decayed_rate
    assert result.loc[1, "score"] == pytest.approx(0.25)

    # Row 2: m_prev_show=1.0, m_in_run=0.05, m_venue=1.0 (gap=999),
    # m_due = 1 + 0.3*clip(0.5,0,2) = 1.15
    expected2 = 0.1 * 1.0 * 0.05 * 1.0 * 1.15
    assert result.loc[2, "score"] == pytest.approx(expected2)


def test_heuristic_scores_returns_new_columns():
    df = make_df([{}])
    result = heuristic_scores(df)
    for col in ("recent_rate", "w_recent", "m_prev_show", "m_in_run", "m_venue",
                "m_due", "score"):
        assert col in result.columns


# ---------------------------------------------------------------------------
# Non-mutation of input
# ---------------------------------------------------------------------------

def test_heuristic_scores_does_not_mutate_input():
    df = make_df([
        {"played_prev_show": 1, "venue_gap": 1, "gap_ratio": 3.0},
        {"played_prev_show": 0, "played_in_run": 1, "venue_gap": 999},
    ])
    original = df.copy(deep=True)
    _ = heuristic_scores(df)
    pd.testing.assert_frame_equal(df, original)


def test_heuristic_predict_does_not_mutate_input():
    df = make_df([
        {"played_prev_show": 1, "venue_gap": 1, "gap_ratio": 3.0},
        {"played_prev_show": 0, "played_in_run": 1, "venue_gap": 999},
    ])
    original = df.copy(deep=True)
    _ = heuristic_predict(df, k=2.0)
    pd.testing.assert_frame_equal(df, original)


# ---------------------------------------------------------------------------
# Per-show renormalization
# ---------------------------------------------------------------------------

def test_heuristic_predict_adds_prob_column():
    df = make_df([{"decayed_rate": 0.3}, {"decayed_rate": 0.1}])
    result = heuristic_predict(df, k=1.5)
    assert "prob" in result.columns
    assert (result["prob"] >= 0).all()


def test_heuristic_predict_per_show_renormalization_two_shows():
    """Two distinct shows in a single frame: each show's probabilities must be
    renormalized independently to sum to (approximately) k, respecting the
    0.99 cap, regardless of the other show's scores."""
    rows_show1 = [
        make_row(decayed_rate=0.5, played_prev_show=0, venue_gap=999, gap_ratio=1.0)
        for _ in range(5)
    ]
    rows_show2 = [
        make_row(decayed_rate=0.05, played_prev_show=0, venue_gap=999, gap_ratio=1.0)
        for _ in range(5)
    ]
    for i, r in enumerate(rows_show1):
        r["showid"] = 1
        r["songid"] = i + 1
    for i, r in enumerate(rows_show2):
        r["showid"] = 2
        r["songid"] = i + 100

    df = pd.DataFrame(rows_show1 + rows_show2)

    k = 2.0
    result = heuristic_predict(df, k=k)

    show1 = result[result["showid"] == 1]
    show2 = result[result["showid"] == 2]

    assert show1["prob"].sum() == pytest.approx(k, rel=1e-6)
    assert show2["prob"].sum() == pytest.approx(k, rel=1e-6)
    assert (result["prob"] <= 0.99 + 1e-9).all()


def test_heuristic_predict_per_show_renormalization_respects_cap():
    """A show with one dominant score and many near-zero scores: cap must be
    respected (no prob exceeds 0.99) while the sum still hits k as closely as
    the cap allows."""
    rows = [make_row(decayed_rate=10.0, played_prev_show=0, venue_gap=999, gap_ratio=1.0)]
    rows += [
        make_row(decayed_rate=0.001, played_prev_show=0, venue_gap=999, gap_ratio=1.0)
        for _ in range(4)
    ]
    for i, r in enumerate(rows):
        r["showid"] = 1
        r["songid"] = i + 1
    df = pd.DataFrame(rows)

    k = 3.0
    result = heuristic_predict(df, k=k)
    assert (result["prob"] <= 0.99 + 1e-9).all()
    assert result["prob"].sum() <= k + 1e-6


def test_heuristic_predict_single_show_no_groupby_needed():
    df = make_df([{"decayed_rate": 0.4}, {"decayed_rate": 0.2}, {"decayed_rate": 0.05}])
    k = 1.2
    result = heuristic_predict(df, k=k)
    assert result["prob"].sum() == pytest.approx(k, rel=1e-6)
