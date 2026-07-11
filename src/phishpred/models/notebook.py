"""Trey's Notebook baseline. See CONTRACTS.md section `models/notebook.py`.

Reproduction of phish.net's "Trey's Notebook" prior art as a named backtest
baseline: rank candidate songs by trailing-year play count, hard-excluding
anything played within the last NOTEBOOK_COOLDOWN_SHOWS shows. This is a
backtest-only comparison point (see backtest.py) — it must never become
selectable in predict/publish/simulate paths.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from phishpred.probs import renormalize_to_k

# phish.net's Trey's Notebook: "not appeared in the last 3 shows".
NOTEBOOK_COOLDOWN_SHOWS = 3


def notebook_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Score each (song, show) candidate row via the Trey's Notebook rule.

    score = plays_last_50 if gap > NOTEBOOK_COOLDOWN_SHOWS else 0.0

    `plays_last_50` (our 50-show trailing window; era 4 averages ~46 shows per
    calendar year, so 50 shows ≈ 1.08 trailing years — see
    docs/rotation-stats-deepdive.md) is our shows-domain stand-in for
    phish.net's trailing-calendar-year play count: phish.net counts plays over
    the last 365 days, while our feature set only carries counts over fixed
    show-count windows. The two are not identical (a slow touring stretch
    spans fewer than 50 shows per year; a heavy stretch spans more), but the
    approximation preserves the same ranking intent — recent play frequency,
    gated by a hard cooldown.

    Returns a copy of `df` with an added `score` column. Does not mutate the
    input.
    """
    out = df.copy()
    gap = out["gap"]
    eligible = gap > NOTEBOOK_COOLDOWN_SHOWS
    out["score"] = np.where(eligible, out["plays_last_50"], 0.0)
    return out


def notebook_predict(df: pd.DataFrame, k: float) -> pd.DataFrame:
    """notebook_scores + `prob` column via probs.renormalize_to_k(score, k).

    Renormalization is applied per show (groupby showid) so that each show's
    probabilities sum to (approximately) k independent of other shows present
    in `df`, mirroring heuristic_predict. Shows where every candidate is
    cooldown-excluded (all-zero score) fall back to renormalize_to_k's
    uniform-split behavior for an all-zero vector (k/n each) rather than
    dividing by zero or producing NaN.
    """
    out = notebook_scores(df)

    if "showid" in out.columns and out["showid"].nunique() > 1:
        probs = np.empty(len(out), dtype=float)
        for _, idx in out.groupby("showid").groups.items():
            pos = out.index.get_indexer(idx)
            probs[pos] = renormalize_to_k(out.loc[idx, "score"].to_numpy(), k)
        out["prob"] = probs
    else:
        out["prob"] = renormalize_to_k(out["score"].to_numpy(), k)

    return out
