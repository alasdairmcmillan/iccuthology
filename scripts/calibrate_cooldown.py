"""Calibrate the heuristic cross-run cooldown multipliers from history.

Methodology (leakage-aware):
  * Holdout is the ``holdout_tours`` most-recent tours (backtest.select_holdout);
    we calibrate ONLY on shows with show_index < holdout.start_index and
    showdate year >= TRAIN_MIN_YEAR (2009), matching the backtest's train slice.
  * Features are built with phishpred.features.build_features (walk-forward /
    leakage-free by construction).
  * We score every calibration row with a "no-penalty" variant of the heuristic:
    the repeat multipliers (m_prev_show, m_in_run and any cooldown) are forced to
    1.0, so the predicted probability reflects base * m_venue * m_due only,
    renormalized per show to that show's era K (config.era_for_year +
    features.mean_setlist_size, exactly as backtest.run_backtest does).
  * For each cohort we report n rows, the mean predicted prob (no-penalty model),
    the mean empirical play rate y, and the implied multiplier mean_y / mean_pred.
    The cross-run gap-2 and gap-3 implied multipliers (clamped to [0.01, 1.0],
    rounded to 2 dp) are the recommended COOLDOWN_GAP2 / COOLDOWN_GAP3.

Cohorts:
  (a) gap == 1                          (compare vs existing m_prev_show = 0.02)
  (b) in-run, gap >= 2                  (compare vs existing m_in_run   = 0.05)
  (c) cross-run gap == 2 (not in-run)   -> COOLDOWN_GAP2
  (d) cross-run gap == 3 (not in-run)   -> COOLDOWN_GAP3
  (e) reference gap in 4..6, cross-run  (a "back-to-baseline" sanity cohort)

Run:  ./.venv/Scripts/python.exe scripts/calibrate_cooldown.py [--db data/phish.db]
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd

from phishpred import config
from phishpred import features as feat
from phishpred.backtest import TRAIN_MIN_YEAR, select_holdout
from phishpred.features import RECENT_RATE_WINDOW
from phishpred.probs import renormalize_to_k


def _year(showdate) -> int:
    return int(str(showdate)[:4])


def no_penalty_scores(df: pd.DataFrame) -> np.ndarray:
    """base * m_venue * m_due with every repeat penalty forced to 1.0.

    Mirrors heuristic_scores exactly except m_prev_show / m_in_run / m_cooldown
    are all 1.0 -- i.e. what the heuristic would predict if it never suppressed
    repeats. This is the reference against which empirical repeat rates imply a
    multiplier.
    """
    recent_rate = df["plays_last_150"] / RECENT_RATE_WINDOW
    w_recent = ((4.0 - df["gap_ratio"]) / 3.0).clip(lower=0.0, upper=1.0)
    base = np.maximum(df["decayed_rate"], w_recent * recent_rate)
    m_venue = np.where(df["venue_gap"] <= 2, 0.3, 1.0)
    m_due = 1 + 0.3 * (df["gap_ratio"] - 1).clip(lower=0, upper=2)
    return (base * m_venue * m_due).to_numpy(dtype=float)


def build_calibration_frame(
    conn: sqlite3.Connection, half_life: int = 50, min_year: int = TRAIN_MIN_YEAR
) -> pd.DataFrame:
    """Feature frame restricted to the calibration slice, with a per-show
    renormalized no-penalty predicted probability column ``pred``.

    ``min_year`` narrows the slice further (e.g. 2021 = era-4-only, the slice
    the shipped constants are calibrated on — see models/heuristic.py; the
    cooldown is strongly era-dependent, so pooling eras dilutes it).
    """
    holdout = select_holdout(conn, holdout_tours=2)
    features_df = feat.build_features(conn, half_life=half_life)

    years = features_df["showdate"].map(_year)
    mask = (years >= min_year) & (features_df["show_index"] < holdout.start_index)
    cal = features_df[mask].copy()

    era_k_cache: dict[str, float] = {}

    def k_for_year(year: int) -> float:
        era = config.era_for_year(year)
        if era not in era_k_cache:
            era_k_cache[era] = float(feat.mean_setlist_size(conn, era))
        return era_k_cache[era]

    cal["_np_score"] = no_penalty_scores(cal)
    pred = np.empty(len(cal), dtype=float)
    for _, idx in cal.groupby("showid").groups.items():
        pos = cal.index.get_indexer(idx)
        k = k_for_year(_year(cal.loc[idx, "showdate"].iloc[0]))
        pred[pos] = renormalize_to_k(cal.loc[idx, "_np_score"].to_numpy(), k)
    cal["pred"] = pred
    return cal


@dataclass
class Cohort:
    label: str
    n: int
    mean_pred: float
    mean_y: float

    @property
    def implied(self) -> float:
        if self.mean_pred <= 0:
            return float("nan")
        return self.mean_y / self.mean_pred


def _cohort(cal: pd.DataFrame, label: str, mask: pd.Series) -> Cohort:
    sub = cal[mask]
    n = int(len(sub))
    mp = float(sub["pred"].mean()) if n else float("nan")
    my = float(sub["y"].astype(float).mean()) if n else float("nan")
    return Cohort(label=label, n=n, mean_pred=mp, mean_y=my)


def cohorts(cal: pd.DataFrame) -> list[Cohort]:
    gap = cal["gap"]
    in_run = cal["played_in_run"].astype(bool)
    prev = cal["played_prev_show"].astype(bool)
    cross_run = ~in_run & ~prev
    return [
        _cohort(cal, "(a) gap==1", prev),
        _cohort(cal, "(b) in-run gap>=2", in_run & (gap >= 2)),
        _cohort(cal, "(c) cross-run gap==2", cross_run & (gap == 2)),
        _cohort(cal, "(d) cross-run gap==3", cross_run & (gap == 3)),
        _cohort(cal, "(e) cross-run gap 4..6", cross_run & (gap >= 4) & (gap <= 6)),
    ]


def clamp_round(x: float, lo: float = 0.01, hi: float = 1.0) -> float:
    return round(float(min(hi, max(lo, x))), 2)


def render(cohort_list: list[Cohort]) -> str:
    lines = []
    header = f"{'cohort':<24} {'n':>8} {'mean_pred':>10} {'mean_y':>10} {'implied':>9}"
    lines.append(header)
    lines.append("-" * len(header))
    for c in cohort_list:
        lines.append(
            f"{c.label:<24} {c.n:>8} {c.mean_pred:>10.4f} "
            f"{c.mean_y:>10.4f} {c.implied:>9.3f}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(config.DB_PATH))
    parser.add_argument("--half-life", type=int, default=50)
    parser.add_argument(
        "--min-year", type=int, default=TRAIN_MIN_YEAR,
        help="Restrict calibration rows to shows in this year or later "
             "(2021 = era-4-only, the slice the shipped constants use).",
    )
    args = parser.parse_args()

    from phishpred.db import get_connection

    conn = get_connection(args.db)
    cal = build_calibration_frame(conn, half_life=args.half_life, min_year=args.min_year)
    cl = cohorts(cal)

    print(f"calibration rows: {len(cal)}  (year >= {args.min_year}, pre-holdout)")
    print(f"half_life = {args.half_life}")
    print()
    print(render(cl))
    print()

    gap2 = next(c for c in cl if c.label.startswith("(c)"))
    gap3 = next(c for c in cl if c.label.startswith("(d)"))
    print(f"COOLDOWN_GAP2 = {clamp_round(gap2.implied)}  "
          f"(implied {gap2.implied:.3f}, n={gap2.n})")
    print(f"COOLDOWN_GAP3 = {clamp_round(gap3.implied)}  "
          f"(implied {gap3.implied:.3f}, n={gap3.n})")


if __name__ == "__main__":
    main()
