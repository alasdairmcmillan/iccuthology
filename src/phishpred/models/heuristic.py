"""Heuristic baseline scorer. See CONTRACTS.md section `models/heuristic.py`."""
from __future__ import annotations

import numpy as np
import pandas as pd

from phishpred.features import FEATURE_COLUMNS, RECENT_RATE_WINDOW  # noqa: F401  (contract requires FEATURE_COLUMNS import)
from phishpred.probs import renormalize_to_k

# --------------------------------------------------------------------------- #
# Cross-run cooldown multipliers (CALIBRATED — do not hand-tune)
# --------------------------------------------------------------------------- #
# Phish rarely repeats a song within ~3 shows even ACROSS run boundaries (prior
# art: phish.net "Trey's Notebook" hard-excludes anything played in the last 3
# shows). m_cooldown suppresses cross-run repeats at gap 2 / gap 3 that the
# existing m_prev_show (gap==1) and m_in_run (within-run) penalties don't touch.
#
# Calibrated by scripts/calibrate_cooldown.py on 2026-07-11 (build_features
# H=50; shows before the 2-tour holdout). For each cohort the script compares
# the empirical play rate to the no-penalty heuristic's per-show renormalized
# predicted rate; the implied multiplier is mean_y / mean_pred.
#
# ERA 4 ONLY (--min-year 2021; 101,397 rows) — the values shipped below,
# because the model predicts current-era shows and the cooldown is strongly
# era-dependent:
#   cohort                     n       mean_pred  mean_y   implied
#   (c) cross-run gap==2     2,950     0.0967     0.0546   0.564  -> COOLDOWN_GAP2
#   (d) cross-run gap==3     3,527     0.0990     0.0675   0.682  -> COOLDOWN_GAP3
#   (e) cross-run gap 4..6   9,552     0.0987     0.1305   1.322  (reference)
#
# Pooled 2009+ (296,427 rows) for contrast: gap==2 implied 0.688, gap==3
# implied 1.084 (i.e. NO suppression at gap 3 pooled) — era 3.0's faster
# rotation dilutes the modern-era signal, which is why the era-4 slice governs.
# Independent confirmation: docs/rotation-stats-deepdive.md measures era-4
# across-run repeat rates at 0.43x (gap 2) / 0.51x (gap 3) of the gap-4..6
# baseline via a raw-rate method. Reported alongside but unchanged: existing
# gap==1 penalty vs era-4 implied 0.23 (constant 0.02) and within-run vs
# implied 0.015 (constant 0.05).
#
# Walk-forward validated 2026-07-11: constants recalibrated on 2021-2024 only
# (implied 0.598/0.709) and evaluated on the held-out 63 shows of 2025-2026
# improve every metric over no-cooldown (Hit@20 5.19 -> 5.48, Brier 0.03522 ->
# 0.03497) and land within noise of the values below, so the shipped full-slice
# calibration generalizes out-of-sample.
COOLDOWN_GAP2 = 0.56
COOLDOWN_GAP3 = 0.68


def heuristic_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Score each (song, show) candidate row via the fixed heuristic formula.

    score = base * m_prev_show * m_in_run * m_cooldown * m_venue * m_due
      recent_rate = plays_last_150 / RECENT_RATE_WINDOW    (long-window empirical rate)
      w_recent    = clip((4 - gap_ratio) / 3, 0, 1)        (1 while gap_ratio<=1, fades to 0 at 4)
      base        = max(decayed_rate, w_recent * recent_rate)
      m_prev_show = 0.02 if played_prev_show else 1.0
      m_in_run    = 0.05 if played_in_run (and not prev show) else 1.0
      m_cooldown  = COOLDOWN_GAP2 if gap == 2, COOLDOWN_GAP3 if gap == 3
                    (only when NOT played_prev_show and NOT played_in_run), else 1.0
      m_venue     = 0.3 if venue_gap <= 2 else 1.0
      m_due       = 1 + 0.3 * clip(gap_ratio - 1, 0, 2)

    Multiplier precedence is exclusive: gap==1 -> m_prev_show only; in-run
    (gap>=2) -> m_in_run only; cross-run gap 2/3 -> m_cooldown only. The
    cooldown captures Phish's cross-run reluctance to repeat within ~3 shows;
    COOLDOWN_GAP2 / COOLDOWN_GAP3 are calibrated constants (see module header).

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
    m_in_run, m_cooldown, m_venue, m_due, score. Fully vectorized (no row loops);
    does not mutate the input.
    """
    out = df.copy()

    played_prev_show = out["played_prev_show"].astype(bool)
    played_in_run = out["played_in_run"].astype(bool)
    gap = out["gap"]

    recent_rate = out["plays_last_150"] / RECENT_RATE_WINDOW
    w_recent = ((4.0 - out["gap_ratio"]) / 3.0).clip(lower=0.0, upper=1.0)
    base = np.maximum(out["decayed_rate"], w_recent * recent_rate)

    m_prev_show = np.where(played_prev_show, 0.02, 1.0)
    m_in_run = np.where(played_in_run & ~played_prev_show, 0.05, 1.0)

    # Cross-run cooldown: fires only for gap 2/3 that are neither the immediate
    # prev-show repeat (gap==1) nor a within-run repeat, keeping the multiplier
    # precedence exclusive (gap==1 -> prev_show only; in-run -> in_run only).
    cross_run = ~played_prev_show & ~played_in_run
    m_cooldown = np.select(
        [cross_run & (gap == 2), cross_run & (gap == 3)],
        [COOLDOWN_GAP2, COOLDOWN_GAP3],
        default=1.0,
    )

    m_venue = np.where(out["venue_gap"] <= 2, 0.3, 1.0)
    m_due = 1 + 0.3 * (out["gap_ratio"] - 1).clip(lower=0, upper=2)

    out["recent_rate"] = recent_rate
    out["w_recent"] = w_recent
    out["m_prev_show"] = m_prev_show
    out["m_in_run"] = m_in_run
    out["m_cooldown"] = m_cooldown
    out["m_venue"] = m_venue
    out["m_due"] = m_due
    out["score"] = base * m_prev_show * m_in_run * m_cooldown * m_venue * m_due

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
