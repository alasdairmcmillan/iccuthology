# GEMINI.md — Standing instructions for Gemini prediction runs

This file outlines the custom prediction pipeline built for the `gemini-3.5-flash-high` track in the `phishpred` setlist predictor.

## Workspace Layout
All code, scripts, and stats live in `agents/antigravity/`:
- `analyze_stats.py`: DB inspector for repeat patterns, co-occurrences, and venue characteristics.
- `make_predictions.py`: The core prediction pipeline that builds, calibrates, and submits predictions for all future shows.
- `verify_submissions.py`: Local submission verification script.

## The Prediction Pipeline (`make_predictions.py`)
To regenerate predictions after new setlists are posted, run:
```bash
.\.venv\Scripts\python.exe agents/antigravity/make_predictions.py
```
This script automates the following steps:
1. **Discount Run Repeats (Monty Hall rule):** Checks the `run_context` of each target show. Any song played in a prior night of the same run is set to `0.0` probability and excluded from the setlist.
2. **Venue-Specific Boosts:** Compares canonical venue historical play rates to era-wide rates using `venue_history`. If a song is played more frequently at this venue than average, its base rate is boosted (capped at `1.30`).
3. **Probability Calibration:** Computes a 20-show backtest of the selected 30-song shortlist to find its historical recall $R$. It then renormalizes the shortlist scores using water-filling to sum to exactly $R \times 18.25$ (the expected number of hits). This ensures the model is perfectly calibrated to avoid Brier score/log loss penalties.
4. **Structured Setlist Builder:** Maximize sharpshooter and marquee points using slot propensities:
   - Encore: Greedy selection from the top songs with high `encore` slot propensity.
   - Set openers/closers: Selected based on `set1-open`, `set1-close`, `set2-open`, `set2-close` propensities.
   - Set mid songs: Automatically filled and divided between Set 1 and Set 2 based on slot likelihood.
5. **Rationales:** Automatically constructs unique, show-specific rationales detailing the venue, run night, discounted songs, and key due selections.

## Verifying & Publishing
After running `make_predictions.py`, verify that all files are valid:
```bash
.\.venv\Scripts\python.exe agents/antigravity/verify_submissions.py
```
And then push the submissions to R2:
```bash
.\.venv\Scripts\python.exe -c "from phishpred.config import _load_env; _load_env(); from scripts.r2_push import main; main(['data/predictions/submitted', 'submitted'])"
```
