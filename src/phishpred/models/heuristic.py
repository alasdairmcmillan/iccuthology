"""Heuristic baseline scorer. See CONTRACTS.md section `models/heuristic.py`."""
from __future__ import annotations

import numpy as np
import pandas as pd

from phishpred.features import FEATURE_COLUMNS  # noqa: F401  (contract requires import)
from phishpred.probs import renormalize_to_k


def heuristic_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Score each (song, show) candidate row via the fixed heuristic formula.

    score = decayed_rate * m_prev_show * m_in_run * m_venue * m_due
      m_prev_show = 0.02 if played_prev_show else 1.0
      m_in_run    = 0.05 if played_in_run (and not prev show) else 1.0
      m_venue     = 0.3 if venue_gap <= 2 else 1.0
      m_due       = 1 + 0.3 * clip(gap_ratio - 1, 0, 2)

    Returns a copy of `df` with added columns: m_prev_show, m_in_run, m_venue,
    m_due, score. Fully vectorized (no row loops); does not mutate the input.
    """
    out = df.copy()

    played_prev_show = out["played_prev_show"].astype(bool)
    played_in_run = out["played_in_run"].astype(bool)

    m_prev_show = np.where(played_prev_show, 0.02, 1.0)
    m_in_run = np.where(played_in_run & ~played_prev_show, 0.05, 1.0)
    m_venue = np.where(out["venue_gap"] <= 2, 0.3, 1.0)
    m_due = 1 + 0.3 * (out["gap_ratio"] - 1).clip(lower=0, upper=2)

    out["m_prev_show"] = m_prev_show
    out["m_in_run"] = m_in_run
    out["m_venue"] = m_venue
    out["m_due"] = m_due
    out["score"] = out["decayed_rate"] * m_prev_show * m_in_run * m_venue * m_due

    return out


def heuristic_predict(df: pd.DataFrame, k: float) -> pd.DataFrame:
    """heuristic_scores + `prob` column via probs.renormalize_to_k(score, k).

    Renormalization is applied per show (groupby showid) so that each show's
    probabilities sum to (approximately) k independent of other shows present
    in `df`.
    """
    out = heuristic_scores(df)

    if "showid" in out.columns and out["showid"].nunique() > 1:
        probs = np.empty(len(out), dtype=float)
        for _, idx in out.groupby("showid").groups.items():
            pos = out.index.get_indexer(idx)
            probs[pos] = renormalize_to_k(out.loc[idx, "score"].to_numpy(), k)
        out["prob"] = probs
    else:
        out["prob"] = renormalize_to_k(out["score"].to_numpy(), k)

    return out
