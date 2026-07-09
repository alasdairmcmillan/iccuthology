"""Tests for phishpred.models.ml — no DB, no network. Synthetic separable data."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from phishpred.features import FEATURE_COLUMNS
from phishpred.models import ml

SEED = 12345


def _make_dataset(n: int = 2000, seed: int = SEED) -> pd.DataFrame:
    """Synthetic separable frame: y is a logistic function of 2-3 features.

    ``played_prev_show`` is constructed as a strong *negative* driver so the LR
    coefficient sign check has real signal.
    """
    rng = np.random.default_rng(seed)
    data = {c: rng.normal(size=n) for c in FEATURE_COLUMNS}
    df = pd.DataFrame(data)

    # Realistic-ish ranges for the three drivers we actually use.
    df["decayed_rate"] = rng.uniform(0.0, 1.0, size=n)
    df["gap_ratio"] = rng.uniform(0.0, 3.0, size=n)
    df["played_prev_show"] = rng.integers(0, 2, size=n).astype(float)

    logit = 3.0 * df["decayed_rate"] + 1.2 * df["gap_ratio"] - 4.5 * df["played_prev_show"]
    prob = 1.0 / (1.0 + np.exp(-logit))
    df["y"] = (rng.uniform(size=n) < prob).astype(int)
    return df


def _split(df: pd.DataFrame, valid_frac: float = 0.25):
    n_valid = int(len(df) * valid_frac)
    return df.iloc[:-n_valid].copy(), df.iloc[-n_valid:].copy()


@pytest.fixture(scope="module")
def dataset() -> pd.DataFrame:
    return _make_dataset()


def test_train_lr_predict_scores_in_range(dataset):
    train_df, valid_df = _split(dataset)
    model = ml.train_lr(train_df, valid_df, seed=SEED)

    assert model.name == "lr"
    scores = model.predict_scores(valid_df)
    assert isinstance(scores, np.ndarray)
    assert len(scores) == len(valid_df)
    assert np.all(scores >= 0.0) and np.all(scores <= 1.0)


def test_train_gbm_predict_scores_in_range(dataset):
    train_df, valid_df = _split(dataset)
    # Tiny params so the test runs fast.
    model = ml.train_gbm(train_df, valid_df, seed=SEED, params={"n_estimators": 20})

    assert model.name == "gbm"
    scores = model.predict_scores(valid_df)
    assert len(scores) == len(valid_df)
    assert np.all(scores >= 0.0) and np.all(scores <= 1.0)


def test_isotonic_is_monotone(dataset):
    train_df, valid_df = _split(dataset)
    model = ml.train_lr(train_df, valid_df, seed=SEED)

    raw = np.linspace(0.0, 1.0, 100)
    calibrated = model.isotonic.predict(raw)
    # Isotonic regression is non-decreasing.
    assert np.all(np.diff(calibrated) >= -1e-9)


def test_lr_coefficient_sign_sanity(dataset):
    train_df, valid_df = _split(dataset)
    model = ml.train_lr(train_df, valid_df, seed=SEED)

    assert set(FEATURE_COLUMNS).issubset(model.coefficients.keys())
    # played_prev_show was built strongly negative; decayed_rate positive.
    assert model.coefficients["played_prev_show"] < 0.0
    assert model.coefficients["decayed_rate"] > 0.0


def test_ml_predict_per_show_sums_to_k(dataset):
    train_df, valid_df = _split(dataset)
    model = ml.train_lr(train_df, valid_df, seed=SEED)

    # Build a prediction frame with several shows, ~30 candidate rows each.
    rng = np.random.default_rng(7)
    rows_per_show, n_shows = 30, 5
    pred = _make_dataset(n=rows_per_show * n_shows, seed=99)
    pred["showid"] = np.repeat(np.arange(n_shows), rows_per_show)

    k = 20.0
    out = ml.ml_predict(model, pred, k)
    assert "prob" in out.columns

    for _, g in out.groupby("showid"):
        # Each show's probabilities renormalize to ~k (cap may pull slightly below).
        assert g["prob"].sum() == pytest.approx(k, abs=1e-6)
        assert np.all((g["prob"] >= 0.0) & (g["prob"] <= 0.99 + 1e-9))
    _ = rng  # silence unused


def test_predict_scores_handles_nan_and_inf(dataset):
    train_df, valid_df = _split(dataset)
    model = ml.train_lr(train_df, valid_df, seed=SEED)

    dirty = valid_df.copy()
    dirty.iloc[0, dirty.columns.get_loc("gap")] = np.inf
    dirty.iloc[1, dirty.columns.get_loc("decayed_rate")] = np.nan
    scores = model.predict_scores(dirty)
    assert np.all(np.isfinite(scores))
    assert np.all((scores >= 0.0) & (scores <= 1.0))
