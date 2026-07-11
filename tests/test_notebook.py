"""Unit tests for phishpred.models.notebook — synthetic DataFrames only, no DB."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from phishpred.models.notebook import (
    NOTEBOOK_COOLDOWN_SHOWS,
    notebook_predict,
    notebook_scores,
)


def make_df(rows, showid=1, start_songid=1) -> pd.DataFrame:
    records = []
    for i, overrides in enumerate(rows):
        r = {"gap": 10, "plays_last_50": 5}
        r.update(overrides)
        r["showid"] = overrides.pop("showid", showid) if "showid" in overrides else showid
        r["songid"] = start_songid + i
        records.append(r)
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Cooldown exclusion logic
# ---------------------------------------------------------------------------

def test_cooldown_constant_is_three():
    assert NOTEBOOK_COOLDOWN_SHOWS == 3


def test_gap_within_cooldown_scores_zero():
    df = make_df([
        {"gap": 1, "plays_last_50": 40},
        {"gap": 2, "plays_last_50": 40},
        {"gap": 3, "plays_last_50": 40},
    ])
    result = notebook_scores(df)
    assert (result["score"] == 0.0).all()


def test_gap_past_cooldown_scores_plays_last_50():
    df = make_df([
        {"gap": 4, "plays_last_50": 17},
        {"gap": 20, "plays_last_50": 3},
    ])
    result = notebook_scores(df)
    assert result.loc[0, "score"] == pytest.approx(17)
    assert result.loc[1, "score"] == pytest.approx(3)


def test_gap_boundary_exactly_at_cooldown_is_excluded():
    # gap > NOTEBOOK_COOLDOWN_SHOWS is the eligibility rule; gap == 3 must
    # still be excluded (strictly greater-than, not >=).
    df = make_df([{"gap": NOTEBOOK_COOLDOWN_SHOWS, "plays_last_50": 99}])
    result = notebook_scores(df)
    assert result.loc[0, "score"] == pytest.approx(0.0)


def test_notebook_scores_does_not_mutate_input():
    df = make_df([{"gap": 1, "plays_last_50": 10}, {"gap": 5, "plays_last_50": 20}])
    original = df.copy(deep=True)
    _ = notebook_scores(df)
    pd.testing.assert_frame_equal(df, original)


# ---------------------------------------------------------------------------
# Per-show renormalization / prediction
# ---------------------------------------------------------------------------

def test_notebook_predict_adds_prob_column():
    df = make_df([{"gap": 10, "plays_last_50": 5}, {"gap": 10, "plays_last_50": 1}])
    result = notebook_predict(df, k=1.5)
    assert "prob" in result.columns
    assert (result["prob"] >= 0).all()


def test_notebook_predict_all_zero_score_show_renormalizes_safely():
    """A show where every candidate is inside the cooldown window (all-zero
    score) must renormalize without NaN/inf/division-by-zero — falling back
    to the uniform k/n split, per probs.renormalize_to_k's zero-vector path."""
    df = make_df([
        {"gap": 1, "plays_last_50": 40},
        {"gap": 2, "plays_last_50": 40},
        {"gap": 3, "plays_last_50": 40},
    ])
    k = 1.2
    result = notebook_predict(df, k=k)
    assert np.isfinite(result["prob"]).all()
    assert (result["prob"] > 0).all()
    expected = k / len(df)
    assert result["prob"].to_numpy() == pytest.approx(np.full(len(df), expected))
    assert result["prob"].sum() == pytest.approx(k, rel=1e-6)


def test_notebook_predict_per_show_renormalization_two_shows():
    rows_show1 = [
        {"gap": 10, "plays_last_50": 5, "showid": 1, "songid": i}
        for i in range(1, 6)
    ]
    rows_show2 = [
        {"gap": 10, "plays_last_50": 1, "showid": 2, "songid": 100 + i}
        for i in range(5)
    ]
    df = pd.DataFrame(rows_show1 + rows_show2)

    k = 2.0
    result = notebook_predict(df, k=k)

    show1 = result[result["showid"] == 1]
    show2 = result[result["showid"] == 2]

    assert show1["prob"].sum() == pytest.approx(k, rel=1e-6)
    assert show2["prob"].sum() == pytest.approx(k, rel=1e-6)
    assert (result["prob"] <= 0.99 + 1e-9).all()


def test_notebook_predict_single_show_no_groupby_needed():
    df = make_df([
        {"gap": 10, "plays_last_50": 8},
        {"gap": 10, "plays_last_50": 2},
        {"gap": 1, "plays_last_50": 50},  # cooldown-excluded despite high count
    ])
    k = 1.2
    result = notebook_predict(df, k=k)
    assert result["prob"].sum() == pytest.approx(k, rel=1e-6)
    # cooldown row must never outrank/out-score eligible rows
    assert result.loc[2, "score"] == 0.0
