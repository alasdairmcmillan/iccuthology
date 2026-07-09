# Phish Setlist Predictor

Calibrated per-song play probabilities for upcoming Phish shows, built on the
phish.net v5 API. See `phish-predictor-plan.md` for the design and
`CONTRACTS.md` for module interfaces.

## Setup

1. Copy `.env.example` to `.env` (or `.env.local`) and add your phish.net API
   key from https://phish.net/api/keys as `PHISHNET_API_KEY`.
2. Install dependencies: `python -m uv sync`

## Usage

Activate the virtualenv, then call `phishpred` directly:

```powershell
# Windows PowerShell — activate once per shell:
.\.venv\Scripts\Activate.ps1
```

```bash
phishpred ingest                 # full backfill 1983->now (cached to data/raw)
phishpred refresh                # incremental: current year + new shows
phishpred backtest               # heuristic vs LR vs GBM, H sweep, calibration
phishpred predict 2026-07-10 --model lr
phishpred predict --venue ruoff --next 3 --model lr
phishpred predict 2026-07-10 --json   # machine-readable output
phishpred build-features --half-life 50 --out data/features.parquet
```

Without activating, prefix with the venv Python: `python -m phishpred.cli <command>`
(from the repo root, with `.\.venv\Scripts\python.exe` on PATH or called directly).
The `python -m uv run phishpred …` form only works where `uv` is installed in the
active interpreter — it is **not** inside this project's venv.

### Prediction modes

All ride on a forward Monte-Carlo simulator (`simulate.py`) that samples setlists
night-by-night over a horizon. Horizon defaults to the rest of the calendar year;
`--tour` restricts to a named tour.

```bash
phishpred tour                              # mode 1: expected plays / P(>=1) per song this year
phishpred tour --tour "summer" --top 25
phishpred run --dates 2026-07-10,2026-07-11,2026-07-12   # mode 2: P(hear >=1 across a run)
phishpred run --venue deer_creek --nights 3 --soft-no-repeat
phishpred chaser "harry hood"               # mode 4: when is the next play?
phishpred setlist 2026-07-10                # mode 5: full ordered setlist (deterministic sampler)
phishpred setlist 2026-07-10 --llm --provider anthropic  # LLM assembler (needs an API key)
phishpred llm-backtest --provider anthropic # benchmark the LLM-as-model vs heuristic/LR/GBM
```

Run mode enforces no-repeat-within-a-run with a hard mask by default
(`--soft-no-repeat` trusts the learned penalty instead). The LLM path is
model-agnostic — `--provider anthropic|openai|google|openai-compat` — reading
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` (see `.env.example`).

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
- Exact ordered-setlist accuracy has a hard ceiling: the deterministic sampler
  scores ~0.11 song-overlap (Hit@K) on recent shows (`setlist.evaluate_sampler`,
  leakage-free) — it trades accuracy for realistic variety. The `--llm` assembler
  is the candidate to beat it; `llm-backtest` measures LLM-as-model song-inclusion
  signal against LR/GBM on the same holdout.

## Development

```bash
.venv/Scripts/python -m pytest -q     # 80 tests, no network required
```

Later phases (planned, not built): FastAPI + React frontend, Neon Postgres +
Azure Container Apps deploy, play-money prediction market.
