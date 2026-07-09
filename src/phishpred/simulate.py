"""Forward Monte-Carlo setlist simulator. See phish-predictor-modes-plan.md §1.

Given the calibrated per-candidate probabilities for a future show, sample a
setlist, fold it into a *copy* of the running feature state (`_State` from
`features.py`), recompute probabilities for the next night, sample again, and
walk to the end of a horizon. Repeat for `n_sims` independent simulations.
Modes 1 (tour), 2 (run), and 4 (chaser) are reductions over `SimResult.samples`.

No-repeat-within-a-run is emergent via the `played_in_run` feature (already a
heavy down-weight in both heuristic and LR); `SimConfig.strict_no_repeat` adds
a hard mask (probability -> 0) for songs already sampled earlier in the same
run, matching the band's near-absolute live practice.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import features
from .config import era_for_year


@dataclass
class SimConfig:
    n_sims: int = 2000
    seed: int = 0
    strict_no_repeat: bool = True     # hard mask: P->0 for songs already played earlier in the SAME run
    model: str = "heuristic"          # "heuristic" | "lr" | "gbm"
    half_life: int = 50
    length_control: bool = False      # reserved: Plackett-Luce/top-K length control (not implemented)


@dataclass
class SimResult:
    horizon_showids: list[int]                 # ordered future showids simulated
    horizon_dates: list[str]                   # parallel showdates
    horizon_venueids: list[int]                # parallel canonical venueids
    songs_meta: dict[int, tuple[str, str]]      # songid -> (slug, name)
    # samples[m][t] = set of songids sampled for sim m, horizon position t
    samples: list[list[set[int]]] = field(default_factory=list)
    config: SimConfig = field(default_factory=SimConfig)


def _horizon_steps(
    conn: sqlite3.Connection, horizon_showids: list[int], max_index: int
) -> list[dict]:
    """Per-horizon-show scheduling metadata: effective index, venue/tour/era,
    and run_start_index, computed the same way features.py does.

    Effective indexes come from `features.future_show_ids` (global rank among
    all not-yet-indexed shows, matching `features_for_future_show`). Two
    horizon shows are in the same run iff they are adjacent in that global
    future ordering (rank differs by 1) and share a canonical venueid — the
    same "consecutive show_index, same venue" rule `build_features` applies.
    A show that starts a fresh run picks up run context from any already
    -indexed shows immediately preceding it at the same venue via
    `features.future_run_start`; if there are none, the run starts at that
    show's own effective index.
    """
    meta_rows = features.show_meta(conn, horizon_showids)
    future_ids = features.future_show_ids(conn)
    rank_of = {sid: i for i, sid in enumerate(future_ids)}

    steps: list[dict] = []
    prev_venue: int | None = None
    prev_rank: int | None = None
    run_start_index: int | None = None

    for showid in horizon_showids:
        row = meta_rows.get(showid)
        if row is None:
            raise ValueError(f"showid {showid} not found in shows")
        rank = rank_of.get(showid)
        if rank is None:
            raise ValueError(
                f"showid {showid} is not a future show (show_index NULL, exclude=0, "
                "dated after the last indexed show)"
            )
        eff_index = max_index + 1 + rank
        venueid = row["venueid"]
        showdate = row["showdate"]
        tourid = row["tourid"]
        era = era_for_year(int(showdate[:4]))

        continues_run = (
            prev_rank is not None and venueid == prev_venue and rank == prev_rank + 1
        )
        if not continues_run:
            pre = features.future_run_start(conn, showid, venueid)
            run_start_index = pre if pre is not None else eff_index

        steps.append(
            {
                "showid": showid,
                "showdate": showdate,
                "venueid": venueid,
                "tourid": tourid,
                "era": era,
                "index": eff_index,
                "run_start_index": run_start_index,
            }
        )
        prev_venue = venueid
        prev_rank = rank

    return steps


def _train_model(conn: sqlite3.Connection, model: str, half_life: int):
    import phishpred.models.ml as ml_mod

    hist = features.build_features(conn, half_life=half_life)
    hist = hist[hist["showdate"].astype(str).str.slice(0, 4).astype(int) >= 2009]
    show_indexes = sorted(hist["show_index"].dropna().unique())
    if show_indexes:
        n_valid = max(1, int(round(len(show_indexes) * 0.15)))
        valid_indexes = set(show_indexes[-n_valid:])
        valid_mask = hist["show_index"].isin(valid_indexes)
        train_df, valid_df = hist[~valid_mask], hist[valid_mask]
    else:
        train_df, valid_df = hist, hist.iloc[0:0]

    if model == "lr":
        return ml_mod.train_lr(train_df, valid_df)
    return ml_mod.train_gbm(train_df, valid_df)


def _score(model: str, trained, frame: pd.DataFrame, k: float) -> pd.DataFrame:
    if model == "heuristic":
        import phishpred.models.heuristic as heuristic_mod

        return heuristic_mod.heuristic_predict(frame, k)
    import phishpred.models.ml as ml_mod

    return ml_mod.ml_predict(trained, frame, k)


def simulate_horizon(
    conn: sqlite3.Connection, horizon_showids: list[int], config: SimConfig | None = None
) -> SimResult:
    """Run config.n_sims forward Monte-Carlo simulations over the given ordered
    list of future showids (each must exist in `shows`, show_index NULL, exclude=0).
    Deterministic given config.seed (use numpy.random.default_rng(seed))."""
    config = config or SimConfig()
    if not horizon_showids:
        return SimResult(
            horizon_showids=[], horizon_dates=[], horizon_venueids=[],
            songs_meta={}, samples=[[] for _ in range(config.n_sims)], config=config,
        )

    base_state, D, max_index = features.build_state_to_now(conn, config.half_life)
    if base_state is None:
        raise ValueError("no indexed history to seed the simulator")
    r = 0.5 ** (1.0 / config.half_life)

    steps = _horizon_steps(conn, horizon_showids, max_index)

    trained = _train_model(conn, config.model, config.half_life) if config.model != "heuristic" else None

    k_cache: dict[str, float] = {}

    def k_for(era: str) -> float:
        if era not in k_cache:
            k_cache[era] = features.mean_setlist_size(conn, era)
        return k_cache[era]

    songs_meta = {sid: (slug, name) for sid, (slug, name, _iso) in base_state.songs_meta.items()}

    rng = np.random.default_rng(config.seed)
    sim_rngs = rng.spawn(config.n_sims)

    samples: list[list[set[int]]] = []
    for sim_rng in sim_rngs:
        state = base_state.copy()
        sim_samples: list[set[int]] = []

        for step in steps:
            D_t = (r ** (step["index"] - max_index)) * (D + 1.0)
            frame = features.emit_candidate_frame(
                state, index=step["index"], showid=step["showid"], showdate=step["showdate"],
                venueid=step["venueid"], tourid=step["tourid"], era=step["era"],
                run_start_index=step["run_start_index"], D=D_t,
            )
            k = k_for(step["era"])
            pred = _score(config.model, trained, frame, k)

            probs = pred["prob"].to_numpy(dtype=float)
            if config.strict_no_repeat:
                in_run = pred["played_in_run"].to_numpy(dtype=bool)
                probs = np.where(in_run, 0.0, probs)

            draws = sim_rng.random(len(probs)) < probs
            chosen = {int(sid) for sid, drawn in zip(pred["songid"].to_numpy(), draws) if drawn}

            sim_samples.append(chosen)
            state.apply_show(step["index"], step["venueid"], step["tourid"], step["era"], chosen)

        samples.append(sim_samples)

    return SimResult(
        horizon_showids=[s["showid"] for s in steps],
        horizon_dates=[s["showdate"] for s in steps],
        horizon_venueids=[s["venueid"] for s in steps],
        songs_meta=songs_meta,
        samples=samples,
        config=config,
    )
