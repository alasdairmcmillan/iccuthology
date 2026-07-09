# Phish Setlist Predictor

Calibrated per-song play probabilities for upcoming Phish shows, built on the
phish.net v5 API. See `phish-predictor-plan.md` for the design and
`CONTRACTS.md` for module interfaces.

## Setup

1. Copy `.env.example` to `.env` (or `.env.local`) and add your phish.net API
   key from https://phish.net/api/keys as `PHISHNET_API_KEY`.
2. Install dependencies: `python -m uv sync`

## Usage

```bash
python -m uv run phishpred ingest                 # full backfill 1983->now (cached to data/raw)
python -m uv run phishpred refresh                # incremental: current year + new shows
python -m uv run phishpred backtest               # heuristic vs LR vs GBM, H sweep, calibration
python -m uv run phishpred predict 2026-07-10 --model lr
python -m uv run phishpred predict --venue ruoff --next 3 --model lr
python -m uv run phishpred predict 2026-07-10 --json   # machine-readable output
python -m uv run phishpred build-features --half-life 50 --out data/features.parquet
```

Models: `heuristic` (multiplicative baseline), `lr` (calibrated logistic
regression — best Brier/log-loss in backtest), `gbm` (calibrated LightGBM).

**Predicting mid-run:** run `refresh` first so last night's setlist feeds the
`played_prev_show` / `played_in_run` features. Predictions for later nights of
a run cannot see earlier nights that haven't been played yet.

## Notes & known limitations

- All recency math uses show ordinals ("shows ago"), not calendar days.
- Venue identity is alias-canonicalized (Deer Creek / Verizon / Klipsch /
  Ruoff count as one venue).
- Isotonic calibration produces stepped probabilities, so ties in the output
  are expected.
- `predict` on an already-played date leaks that show's own history into its
  features (fine for real predictions; use `backtest` for honest retrospective
  scoring).
- ML driver strings currently rank |coef x raw value|, which over-weights
  large-scale features (gap, song_age_shows); treat them as rough hints.
- phish.net `times_played` counts distinct shows; `performances` keeps every
  row (sandwiched songs appear twice with the API's `gap=0` on the reprise row).

## Development

```bash
.venv/Scripts/python -m pytest -q     # 80 tests, no network required
```

Later phases (planned, not built): FastAPI + React frontend, Neon Postgres +
Azure Container Apps deploy, play-money prediction market.
