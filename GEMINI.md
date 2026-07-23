# GEMINI.md — Standing instructions for Gemini prediction runs

This file outlines the custom prediction pipelines built for the Gemini tracks (`gemini-3.5-flash-high` and `gemini-3.6-flash-high`) in the `phishpred` setlist predictor.

## Workspace Layout
All code, scripts, and stats live in `agents/antigravity/`:
- `predict_gemini_3_5_flash_high.py`: Core prediction pipeline for `gemini-3.5-flash-high`.
- `predict_gemini_3_6_flash_high.py`: Core prediction pipeline for `gemini-3.6-flash-high`.
- `verify_submissions.py`: Local submission verification script for `gemini-3.5-flash-high` and `gemini-3.6-flash-high`.
- `verify_submissions_gemini_3_6.py`: Specific verification script for `gemini-3.6-flash-high`.

## The Prediction Pipelines
To regenerate predictions after new setlists are posted, run:
```bash
# For Gemini 3.5 Flash:
.\.venv\Scripts\python.exe agents/antigravity/predict_gemini_3_5_flash_high.py

# For Gemini 3.6 Flash:
.\.venv\Scripts\python.exe agents/antigravity/predict_gemini_3_6_flash_high.py
```
This script automates the following steps:
1. **Chronological Joint-Consistency & Tour-Rotation Discounts:** Checks the `run_context` of each target show. Since future shows are unplayed, it tracks simulated setlists chronologically. Any song called in a simulated setlist on a prior night of the same run is set to `0.0` probability and excluded. Additionally, any song played/called on the immediately preceding tour stop is discounted to `0.02` probability (tour rotation limit).
2. **Venue-Specific Boosts:** Compares canonical venue historical play rates to era-wide rates using `venue_history`. If a song is played more frequently at this venue than average, its base rate is boosted.
3. **Probability Calibration:** Calibrates the 30-song shortlist so that the total probability sum equals ~7.50 expected hits (recall × average distinct songs in setlist). This keeps log loss and Brier score penalty-free.
4. **Structured Setlist Builder:** Maximize sharpshooter and marquee points using `slot_propensities`:
   - Encore: High-propensity encore songs (`say-it-to-me-santos`, `tweezer-reprise`, `first-tube`, `loving-cup`, `rock-and-roll`, `possum`).
   - Set openers/closers: Selected based on `set1-open`, `set1-close`, `set2-open`, `set2-close` propensities (e.g. `chalk-dust-torture`, `carini`, `down-with-disease`, `character-zero`, `slave-to-the-traffic-light`, `harry-hood`).
   - Set mid songs: Slot-aligned setlist flows with zero duplicates.
5. **Show Rationales:** Constructs unique, 2-5 sentence show-specific rationales detailing venue history, run position, joint consistency exclusions, and rotation discounts.

## Verifying & Publishing
After running `predict_gemini_3_6_flash_high.py`, verify that all files are valid:
```bash
.\.venv\Scripts\python.exe agents/antigravity/verify_submissions_gemini_3_6.py
```
And then push the submissions to R2:
```bash
.\.venv\Scripts\python.exe -c "from phishpred.config import _load_env; _load_env(); from scripts.r2_push import main; main(['data/predictions/submitted', 'submitted'])"
```

