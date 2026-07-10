"""Heuristic baseline scorer. See CONTRACTS.md section `models/heuristic.py`."""
from __future__ import annotations

import numpy as np
import pandas as pd

from phishpred.features import FEATURE_COLUMNS, RECENT_RATE_WINDOW  # noqa: F401  (contract requires FEATURE_COLUMNS import)
from phishpred.probs import renormalize_to_k


def heuristic_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Score each (song, show) candidate row via the fixed heuristic formula.

    score = base * m_prev_show * m_in_run * m_venue * m_due
      recent_rate = plays_last_150 / RECENT_RATE_WINDOW    (long-window empirical rate)
      w_recent    = clip((4 - gap_ratio) / 3, 0, 1)        (1 while gap_ratio<=1, fades to 0 at 4)
      base        = max(decayed_rate, w_recent * recent_rate)
      m_prev_show = 0.02 if played_prev_show else 1.0
      m_in_run    = 0.05 if played_in_run (and not prev show) else 1.0
      m_venue     = 0.3 if venue_gap <= 2 else 1.0
      m_due       = 1 + 0.3 * clip(gap_ratio - 1, 0, 2)

    The base rate blends the exponentially-decayed rate (half-life 50 shows) with
    a floor drawn from the long ~150-show empirical rate. The floor keeps
    steady-but-rare rotation songs (roughly 1-3 plays/year) alive mid-cycle, when
    decayed_rate sags hard just before the song comes due. The w_recent() gate
    kills the floor once a song is far beyond its own median gap
    (gap_ratio >= 4): long-dormant songs (hundreds of plays but nothing in years)
    stay governed by decayed_rate + the capped m_due, which remains the ONLY
    bust-out mechanism. Because base >= decayed_rate elementwise, the blend never
    lowers a song's score relative to decayed_rate alone.

    Returns a copy of `df` with added columns: recent_rate, w_recent, m_prev_show,
    m_in_run, m_venue, m_due, score. Fully vectorized (no row loops); does not
    mutate the input.
    """
    out = df.copy()

    played_prev_show = out["played_prev_show"].astype(bool)
    played_in_run = out["played_in_run"].astype(bool)

    recent_rate = out["plays_last_150"] / RECENT_RATE_WINDOW
    w_recent = ((4.0 - out["gap_ratio"]) / 3.0).clip(lower=0.0, upper=1.0)
    base = np.maximum(out["decayed_rate"], w_recent * recent_rate)

    m_prev_show = np.where(played_prev_show, 0.02, 1.0)
    m_in_run = np.where(played_in_run & ~played_prev_show, 0.05, 1.0)
    m_venue = np.where(out["venue_gap"] <= 2, 0.3, 1.0)
    m_due = 1 + 0.3 * (out["gap_ratio"] - 1).clip(lower=0, upper=2)

    out["recent_rate"] = recent_rate
    out["w_recent"] = w_recent
    out["m_prev_show"] = m_prev_show
    out["m_in_run"] = m_in_run
    out["m_venue"] = m_venue
    out["m_due"] = m_due
    out["score"] = base * m_prev_show * m_in_run * m_venue * m_due

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
