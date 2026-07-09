"""ML models — calibrated logistic regression and LightGBM. See CONTRACTS.md.

Both models produce *calibrated* per-row probabilities (isotonic on a validation
slice) via a common wrapper. ``ml_predict`` then renormalizes to K per show.

Training data (per contract): rows with show year >= 2009 (era 3.0+). Callers
(backtest / predict) are responsible for the era/time split; the fitters here
train on whatever ``train_df`` / ``valid_df`` they are handed. Fixed seeds.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from phishpred.features import FEATURE_COLUMNS


@runtime_checkable
class CalibratedSongModel(Protocol):
    """Structural type for a trained, calibrated per-song model."""

    name: str

    def predict_scores(self, df: pd.DataFrame) -> np.ndarray:  # pragma: no cover
        """Calibrated per-row probabilities in [0, 1], pre-renormalization."""
        ...


def _prepare_X(df: pd.DataFrame, feature_columns: list[str] = FEATURE_COLUMNS) -> np.ndarray:
    """Feature matrix with defensive NaN/inf handling.

    ``is_original`` NaN -> 0.5 is expected to be done upstream; this is still a
    guard so a stray NaN/inf never blows up a fit or predict. inf is replaced
    with a large finite value, NaN with 0.
    """
    X = df.loc[:, list(feature_columns)].to_numpy(dtype=float)
    return np.nan_to_num(X, nan=0.0, posinf=1.0e9, neginf=-1.0e9)


def _y(df: pd.DataFrame) -> np.ndarray:
    return df["y"].to_numpy(dtype=float).round().astype(int)


class _CalibratedModel:
    """Wraps a fitted sklearn/lightgbm classifier + an isotonic calibrator.

    ``predict_scores`` returns ``isotonic(model.predict_proba(X)[:, 1])``.
    """

    def __init__(
        self,
        name: str,
        estimator: Any,
        isotonic: Any,
        feature_columns: list[str],
        coefficients: dict[str, float] | None = None,
    ) -> None:
        self.name = name
        self.estimator = estimator
        self.isotonic = isotonic
        self.feature_columns = list(feature_columns)
        self.coefficients: dict[str, float] = coefficients or {}

    def _raw_proba(self, df: pd.DataFrame) -> np.ndarray:
        X = _prepare_X(df, self.feature_columns)
        proba = self.estimator.predict_proba(X)
        # positive class is column 1 for a two-class classifier
        return np.asarray(proba)[:, 1]

    def predict_scores(self, df: pd.DataFrame) -> np.ndarray:
        raw = self._raw_proba(df)
        calibrated = self.isotonic.predict(raw)
        calibrated = np.nan_to_num(calibrated, nan=0.0, posinf=1.0, neginf=0.0)
        return np.clip(calibrated, 0.0, 1.0)


def _fit_isotonic(estimator: Any, valid_df: pd.DataFrame, feature_columns: list[str]) -> Any:
    from sklearn.isotonic import IsotonicRegression

    Xval = _prepare_X(valid_df, feature_columns)
    yval = _y(valid_df)
    raw_val = np.asarray(estimator.predict_proba(Xval))[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(raw_val, yval)
    return iso


def train_lr(train_df: pd.DataFrame, valid_df: pd.DataFrame, seed: int = 42) -> _CalibratedModel:
    """Scaled LogisticRegression + isotonic calibration on the valid slice.

    Coefficients (in standardized-feature space) are exposed on
    ``.coefficients`` for driver explanations. Sign sanity: for real data
    ``played_prev_show`` should come out strongly negative.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    Xtr = _prepare_X(train_df)
    ytr = _y(train_df)

    pipe = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("lr", LogisticRegression(max_iter=2000, random_state=seed)),
        ]
    )
    pipe.fit(Xtr, ytr)

    iso = _fit_isotonic(pipe, valid_df, FEATURE_COLUMNS)

    lr = pipe.named_steps["lr"]
    coefficients = {
        feat: float(coef) for feat, coef in zip(FEATURE_COLUMNS, lr.coef_[0])
    }
    return _CalibratedModel("lr", pipe, iso, FEATURE_COLUMNS, coefficients)


_GBM_DEFAULTS: dict[str, Any] = {
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_child_samples": 50,
    "deterministic": True,
    "n_jobs": 4,
    "verbose": -1,
}


def train_gbm(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    seed: int = 42,
    params: dict[str, Any] | None = None,
) -> _CalibratedModel:
    """LightGBM classifier + isotonic calibration on the valid slice.

    ``params`` overrides the reasonable defaults (tests pass e.g.
    ``n_estimators=20`` to keep the suite fast).
    """
    import lightgbm as lgb

    settings: dict[str, Any] = dict(_GBM_DEFAULTS)
    settings["random_state"] = seed
    if params:
        settings.update(params)

    Xtr = _prepare_X(train_df)
    ytr = _y(train_df)

    clf = lgb.LGBMClassifier(**settings)
    clf.fit(Xtr, ytr)

    iso = _fit_isotonic(clf, valid_df, FEATURE_COLUMNS)
    return _CalibratedModel("gbm", clf, iso, FEATURE_COLUMNS)


def ml_predict(model: CalibratedSongModel, df: pd.DataFrame, k: float) -> pd.DataFrame:
    """Add a calibrated ``score`` and a per-show renormalized ``prob`` column.

    ``prob`` is produced by ``probs.renormalize_to_k`` applied independently
    within each ``showid`` group so each show's probabilities sum to ~k.
    """
    from phishpred.probs import renormalize_to_k

    out = df.copy()
    out["score"] = np.asarray(model.predict_scores(out), dtype=float)

    if "showid" in out.columns and out["showid"].nunique() > 1:
        probs = np.empty(len(out), dtype=float)
        for _, idx in out.groupby("showid").groups.items():
            pos = out.index.get_indexer(idx)
            probs[pos] = renormalize_to_k(out.loc[idx, "score"].to_numpy(), k)
        out["prob"] = probs
    else:
        out["prob"] = renormalize_to_k(out["score"].to_numpy(), k)

    return out
