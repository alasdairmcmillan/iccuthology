# GEMINI.md — Standing instructions for Gemini prediction runs

This file outlines the custom prediction pipelines built for the Gemini
tracks (`gemini-pro` and `gemini-flash` — unversioned identity labels) in the `phishpred` setlist predictor.

## Workspace Layout
All code, scripts, and stats live in `agents/antigravity/`:
- `predict_gemini_flash.py`: Core prediction pipeline for the `gemini-flash` track.
- `predict_gemini_pro.py`: Core prediction pipeline for the `gemini-pro` track.
- `verify_submissions_flash.py`: Local submission verification script for `gemini-flash`.
- `verify_submissions_pro.py`: Local submission verification script for `gemini-pro`.

## The Prediction Pipeline
To regenerate or update predictions for all future shows, run the appropriate script:
```bash
python -m uv run python agents/antigravity/predict_gemini_pro.py
```
This script automates the following steps:
1. **Chronological Joint-Consistency & Multi-Night Run Exclusions:** Tracks completed shows (e.g. MSG Nights 1 & 2) and excludes all played songs from subsequent nights of the run (`played_in_run=1` -> 0.0 probability). Also tracks simulated setlists chronologically across multi-night stands (MSG 5-night stand, Fenway 2-night stand, Dick's 3-night stand).
2. **Venue-Specific Historical Boosts:** Incorporates venue play rates (e.g. Fenway Park and Dick's Sporting Goods Park) to boost songs with historically high venue frequencies.
3. **Probability Calibration:** Calibrates 30-song shortlists so the sum of probabilities equals ~7.50 expected hits (recall × average distinct songs in setlist).
4. **Structured Setlist Builder:** Maximize marquee and sharpshooter points using `slot_propensities`:
   - Set 1 Opener / Set 1 Closer / Set 2 Opener / Set 2 Closer / Encore alignment.
   - Zero duplicate songs per setlist.
5. **Show Rationales:** Generates unique, 2-5 sentence show-specific rationales detailing venue history, run position, joint consistency exclusions, and rotation context.

## Verifying & Publishing
After running the prediction pipeline, verify that all files are valid:
```bash
python -m uv run python agents/antigravity/verify_submissions_pro.py
```
And push the submissions to R2:
```bash
python -m uv run --extra deploy python -c "from phishpred.config import _load_env; _load_env(); from scripts.r2_push import main; main(['data/predictions/submitted', 'submitted'])"
```
