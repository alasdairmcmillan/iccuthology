"""Walk-forward backtest harness. See CONTRACTS.md section `backtest.py`.

Compares the untrained Trey's Notebook prior-art baseline, the heuristic
baseline, and calibrated LR / GBM over a holdout of the most-recent complete
tours, sweeping the decayed-rate half-life. The expensive pieces (holdout
selection, feature build, train/valid split, model training, metrics) are
factored into standalone functions so tests can drive them with synthetic
data without touching a real database.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from phishpred import config

# Model column order used everywhere the report is built / rendered.
# "notebook" is the Trey's Notebook baseline (models/notebook.py) — untrained,
# H-invariant (identical rows across the half-life sweep; expected and cheap),
# backtest-only: no non-backtest module iterates MODEL_NAMES (verified — see
# CONTRACTS.md `backtest.py`), so it is safe to add here directly rather than
# introducing a separate backtest-scoped list.
MODEL_NAMES = ["notebook", "heuristic", "lr", "gbm"]

TRAIN_MIN_YEAR = 2009
VALID_FRACTION = 0.15
N_CALIBRATION_BUCKETS = 10


# --------------------------------------------------------------------------- #
# Metrics (pure functions, hand-testable)
# --------------------------------------------------------------------------- #
def brier_score(y_true: np.ndarray, prob: np.ndarray) -> float:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(prob, dtype=float)
    if len(y) == 0:
        return float("nan")
    return float(np.mean((p - y) ** 2))


def log_loss_score(y_true: np.ndarray, prob: np.ndarray) -> float:
    y = np.asarray(y_true, dtype=float)
    p = np.clip(np.asarray(prob, dtype=float), 1e-6, 1 - 1e-6)
    if len(y) == 0:
        return float("nan")
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def hit_at_k(df: pd.DataFrame, k: int) -> float:
    """Mean over shows of (# actually-played songs among the top-k by prob)."""
    if len(df) == 0:
        return float("nan")
    hits: list[float] = []
    for _, g in df.groupby("showid"):
        topk = g.nlargest(k, "prob")
        hits.append(float((topk["y"].to_numpy(dtype=float) >= 0.5).sum()))
    return float(np.mean(hits)) if hits else float("nan")


def calibration_table(
    y_true: np.ndarray, prob: np.ndarray, n_buckets: int = N_CALIBRATION_BUCKETS
) -> list[dict[str, float]]:
    """10 equal-width buckets over 0-100%: n rows, mean predicted, empirical rate."""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(prob, dtype=float)
    edges = np.linspace(0.0, 1.0, n_buckets + 1)
    rows: list[dict[str, float]] = []
    for i in range(n_buckets):
        lo, hi = edges[i], edges[i + 1]
        if i == n_buckets - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        n = int(mask.sum())
        rows.append(
            {
                "lo": float(lo),
                "hi": float(hi),
                "n": n,
                "mean_pred": float(p[mask].mean()) if n else 0.0,
                "empirical": float(y[mask].mean()) if n else 0.0,
            }
        )
    return rows


def compute_metrics(df: pd.DataFrame) -> dict[str, float]:
    """Brier / log loss / Hit@20 / Hit@25 plus counts over a prediction frame."""
    if len(df) == 0:
        return {
            "n_rows": 0,
            "n_shows": 0,
            "brier": float("nan"),
            "log_loss": float("nan"),
            "hit20": float("nan"),
            "hit25": float("nan"),
        }
    y = df["y"].to_numpy(dtype=float)
    p = df["prob"].to_numpy(dtype=float)
    return {
        "n_rows": int(len(df)),
        "n_shows": int(df["showid"].nunique()) if "showid" in df.columns else 0,
        "brier": brier_score(y, p),
        "log_loss": log_loss_score(y, p),
        "hit20": hit_at_k(df, 20),
        "hit25": hit_at_k(df, 25),
    }


# --------------------------------------------------------------------------- #
# Holdout selection
# --------------------------------------------------------------------------- #
@dataclass
class HoldoutSelection:
    tour_labels: list[str]
    showids: list[int]
    start_index: int
    n_shows: int
    date_range: tuple[str, str]

    @property
    def description(self) -> str:
        lo, hi = self.date_range
        tours = ", ".join(self.tour_labels) if self.tour_labels else "(none)"
        return (
            f"Holdout: {len(self.tour_labels)} most-recent tour(s) [{tours}] — "
            f"{self.n_shows} shows, show_index >= {self.start_index}, "
            f"dates {lo}..{hi}"
        )


def _tour_key(tourid: Any, tour_name: Any) -> tuple[str, Any] | None:
    """Stable tour identity: tourid if present, else tour_name. None if neither."""
    if tourid is not None:
        return ("id", tourid)
    if tour_name is not None and str(tour_name) != "":
        return ("name", tour_name)
    return None


def select_holdout(conn: sqlite3.Connection, holdout_tours: int = 2) -> HoldoutSelection:
    """Pick the ``holdout_tours`` most recent distinct tours (by max showdate).

    Selection is over indexed shows (exclude=0, show_index NOT NULL) that have
    performances; shows with a NULL tour are skipped from selection. The holdout
    set is every indexed show belonging to the chosen tours.
    """
    rows = conn.execute(
        """
        SELECT s.showid, s.showdate, s.show_index, s.tourid, s.tour_name,
               (SELECT COUNT(*) FROM performances p WHERE p.showid = s.showid) AS n_perf
        FROM shows s
        WHERE s.exclude = 0 AND s.show_index IS NOT NULL
        ORDER BY s.show_index
        """
    ).fetchall()

    tour_max_date: dict[tuple[str, Any], str] = {}
    tour_label: dict[tuple[str, Any], str] = {}
    for r in rows:
        if r["n_perf"] <= 0:
            continue
        key = _tour_key(r["tourid"], r["tour_name"])
        if key is None:
            continue
        d = r["showdate"]
        if key not in tour_max_date or d > tour_max_date[key]:
            tour_max_date[key] = d
        tour_label[key] = (
            str(r["tour_name"]) if r["tour_name"] not in (None, "") else f"tour {r['tourid']}"
        )

    # Most recent tours first; repr(key) breaks ties deterministically.
    ranked = sorted(tour_max_date.items(), key=lambda kv: (kv[1], repr(kv[0])), reverse=True)
    selected_keys = [k for k, _ in ranked[: max(0, holdout_tours)]]
    selected_set = set(selected_keys)

    holdout_rows = [
        r for r in rows if _tour_key(r["tourid"], r["tour_name"]) in selected_set
    ]
    showids = [int(r["showid"]) for r in holdout_rows]
    indexes = [int(r["show_index"]) for r in holdout_rows]
    dates = sorted(r["showdate"] for r in holdout_rows)

    return HoldoutSelection(
        tour_labels=[tour_label[k] for k in selected_keys],
        showids=showids,
        start_index=min(indexes) if indexes else 0,
        n_shows=len(showids),
        date_range=(dates[0], dates[-1]) if dates else ("", ""),
    )


# --------------------------------------------------------------------------- #
# Train / valid split + training
# --------------------------------------------------------------------------- #
def _year(showdate: Any) -> int:
    return int(str(showdate)[:4])


def split_train_valid(
    features_df: pd.DataFrame,
    holdout_start: int,
    min_year: int = TRAIN_MIN_YEAR,
    valid_frac: float = VALID_FRACTION,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Train = rows with year >= ``min_year`` and show_index < ``holdout_start``.

    The calibration/valid slice is the rows from the last ``valid_frac`` of
    distinct train show_indexes; the model is fit on the rest.
    """
    years = features_df["showdate"].map(_year)
    mask = (years >= min_year) & (features_df["show_index"] < holdout_start)
    train_all = features_df[mask]

    distinct = np.sort(train_all["show_index"].unique())
    n = len(distinct)
    if n <= 1:
        return train_all, train_all

    n_valid = max(1, int(round(n * valid_frac)))
    n_valid = min(n_valid, n - 1)  # keep at least one show for fitting
    valid_indexes = set(distinct[-n_valid:].tolist())

    is_valid = train_all["show_index"].isin(valid_indexes)
    return train_all[~is_valid], train_all[is_valid]


def train_models(
    fit_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    seed: int = 42,
    gbm_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Train the calibrated LR and GBM models (heuristic needs no training)."""
    from phishpred.models.ml import train_gbm, train_lr

    return {
        "lr": train_lr(fit_df, valid_df, seed=seed),
        "gbm": train_gbm(fit_df, valid_df, seed=seed, params=gbm_params),
    }


def predict_holdout(
    models: dict[str, Any],
    holdout_df: pd.DataFrame,
    k_for_year,
) -> dict[str, pd.DataFrame]:
    """Score every holdout show with each model, renormalizing per show to its era K.

    Returns ``{model_name: concatenated prediction frame}`` where each frame
    carries at least ``showid``, ``y`` and ``prob`` columns.
    """
    from phishpred.models.heuristic import heuristic_predict
    from phishpred.models.ml import ml_predict
    from phishpred.models.notebook import notebook_predict

    parts: dict[str, list[pd.DataFrame]] = {name: [] for name in MODEL_NAMES}
    if len(holdout_df) == 0:
        return {name: holdout_df.copy() for name in MODEL_NAMES}

    for _, show_rows in holdout_df.groupby("showid"):
        k = k_for_year(_year(show_rows["showdate"].iloc[0]))
        parts["notebook"].append(notebook_predict(show_rows, k))
        parts["heuristic"].append(heuristic_predict(show_rows, k))
        parts["lr"].append(ml_predict(models["lr"], show_rows, k))
        parts["gbm"].append(ml_predict(models["gbm"], show_rows, k))

    return {
        name: (pd.concat(chunks, ignore_index=True) if chunks else holdout_df.copy())
        for name, chunks in parts.items()
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
@dataclass
class BacktestReport:
    half_lives: tuple[int, ...]
    model_names: list[str]
    holdout_description: str
    results: dict[tuple[str, int], dict[str, float]] = field(default_factory=dict)
    calibration: dict[tuple[str, int], list[dict[str, float]]] = field(default_factory=dict)

    def render(self) -> str:
        lines: list[str] = []
        lines.append("Phish setlist backtest")
        lines.append("=" * 60)
        lines.append(self.holdout_description)
        lines.append("")

        header = f"{'model':<10} {'H':>4} {'rows':>7} {'shows':>6} " \
                 f"{'Brier':>9} {'LogLoss':>9} {'Hit@20':>8} {'Hit@25':>8}"
        lines.append("Metrics")
        lines.append("-" * len(header))
        lines.append(header)
        for name in self.model_names:
            for h in self.half_lives:
                m = self.results.get((name, h))
                if m is None:
                    continue
                lines.append(
                    f"{name:<10} {h:>4} {m['n_rows']:>7} {m['n_shows']:>6} "
                    f"{m['brier']:>9.4f} {m['log_loss']:>9.4f} "
                    f"{m['hit20']:>8.2f} {m['hit25']:>8.2f}"
                )
        lines.append("")

        for name in self.model_names:
            for h in self.half_lives:
                table = self.calibration.get((name, h))
                if not table:
                    continue
                lines.append(f"Calibration — {name} (H={h})")
                lines.append(f"{'bucket':<10} {'n':>7} {'pred':>8} {'empirical':>10}")
                for row in table:
                    label = f"{int(round(row['lo'] * 100)):>3d}-{int(round(row['hi'] * 100)):>3d}%"
                    lines.append(
                        f"{label:<10} {row['n']:>7} {row['mean_pred']:>8.3f} "
                        f"{row['empirical']:>10.3f}"
                    )
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.render()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_backtest(
    conn: sqlite3.Connection,
    half_lives: tuple[int, ...] = (25, 50, 100),
    holdout_tours: int = 2,
    seed: int = 42,
    gbm_params: dict[str, Any] | None = None,
) -> BacktestReport:
    """Full walk-forward backtest: notebook vs heuristic vs LR vs GBM across the H sweep."""
    from phishpred import features as feat

    holdout = select_holdout(conn, holdout_tours=holdout_tours)
    holdout_showids = set(holdout.showids)

    era_k_cache: dict[str, float] = {}

    def k_for_year(year: int) -> float:
        era = config.era_for_year(year)
        if era not in era_k_cache:
            era_k_cache[era] = float(feat.mean_setlist_size(conn, era))
        return era_k_cache[era]

    report = BacktestReport(
        half_lives=tuple(half_lives),
        model_names=list(MODEL_NAMES),
        holdout_description=holdout.description,
    )

    for h in half_lives:
        features_df = feat.build_features(conn, half_life=h)
        holdout_df = features_df[features_df["showid"].isin(holdout_showids)]
        fit_df, valid_df = split_train_valid(features_df, holdout.start_index)
        models = train_models(fit_df, valid_df, seed=seed, gbm_params=gbm_params)

        preds = predict_holdout(models, holdout_df, k_for_year)
        for name, pdf in preds.items():
            report.results[(name, h)] = compute_metrics(pdf)
            if len(pdf) > 0:
                report.calibration[(name, h)] = calibration_table(
                    pdf["y"].to_numpy(dtype=float), pdf["prob"].to_numpy(dtype=float)
                )
            else:
                report.calibration[(name, h)] = []

    return report
