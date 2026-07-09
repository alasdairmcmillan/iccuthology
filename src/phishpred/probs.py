"""Shared probability utilities."""
from __future__ import annotations

import numpy as np


def renormalize_to_k(scores: np.ndarray, k: float, cap: float = 0.99) -> np.ndarray:
    """Scale non-negative scores so they sum to k, capping each at `cap`.

    Water-filling: scale everything so the sum is k; anything above `cap` is
    pinned to `cap` and the remaining mass is redistributed over the uncapped
    entries. Converges in a handful of iterations.
    """
    s = np.asarray(scores, dtype=float).clip(min=0.0)
    n = len(s)
    if n == 0:
        return s
    k = min(float(k), n * cap)
    if s.sum() <= 0:
        return np.full(n, k / n)

    p = s * (k / s.sum())
    for _ in range(100):
        over = p > cap
        if not over.any():
            break
        residual = k - cap * over.sum()
        under_sum = p[~over].sum()
        p[over] = cap
        if under_sum <= 0 or residual <= 0:
            p[~over] = max(residual, 0.0) / max((~over).sum(), 1)
            break
        p[~over] *= residual / under_sum
    return np.clip(p, 0.0, cap)
