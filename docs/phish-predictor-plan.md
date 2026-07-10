# Phish Setlist Predictor — Implementation Plan

Handoff document for Claude Code. Build a local tool that ingests the full Phish.net dataset and generates calibrated per-song probabilities for upcoming shows, with a backtest harness to validate. Designed to deploy online later (Neon Postgres + Azure Container Apps) and eventually grow into a play-money prediction market.

---

## 0. Stack & project setup

- **Language:** Python 3.12 (modeling loop is fastest in pandas/scikit-learn). Use `uv` for env management.
- **Storage:** SQLite (`data/phish.db`) locally. Write all SQL portably (no SQLite-only tricks) so migration to Postgres/Neon is trivial.
- **CLI:** `typer`. Commands: `ingest`, `refresh`, `build-features`, `backtest`, `predict`.
- **Deps:** `httpx`, `typer`, `pandas`, `scikit-learn`, `lightgbm` (phase 4b), `python-dotenv`.
- **Repo layout:**

```
phish-predictor/
  pyproject.toml
  .env                    # PHISHNET_API_KEY=...
  data/
    raw/                  # cached raw JSON API responses (gitignored)
    phish.db
  src/phishpred/
    api.py                # phish.net client
    ingest.py             # backfill + incremental refresh
    schema.sql
    features.py           # feature engineering
    models/
      heuristic.py        # phase 4a baseline scorer
      ml.py               # phase 4b logistic regression / GBM
    backtest.py
    predict.py
    cli.py
  tests/
```

API key: user must request one at https://phish.net (docs: https://docs.phish.net). Read from `.env`, never hardcode.

---

## 1. Phish.net API v5 — client (`api.py`)

Base URL: `https://api.phish.net/v5`. Pattern: `/v5/{method}.json`, `/v5/{method}/{id}.json`, or `/v5/{method}/{column}/{value}.json`, with `apikey` as a query param. Supported extras: `order_by`, `direction`, `limit`.

Relevant methods: `shows`, `setlists`, `songs`, `venues`, `songdata`, `jamcharts`.

**⚠️ DISCOVERY STEP (do this first, before finalizing the schema):** Field names below are best-effort from memory of the v5 API. Before writing `schema.sql`, make 3–4 real calls and inspect actual payloads:

1. `/v5/shows/showyear/2025.json`
2. `/v5/setlists/showdate/2025-07-25.json` (or any recent show)
3. `/v5/songs.json?limit=20`
4. `/v5/venues.json?limit=20`

Confirm the exact keys — in particular verify that setlist rows include a **`gap`** field (shows since the song was last played — phish.net computes this and it's our most valuable feature), plus `set`, `position`, `songid`, `slug`, `showid`, `artistid`, `trans_mark`, `venueid`. Adjust schema to match reality.

**Client requirements:**
- Every raw response cached to `data/raw/{method}/{key}.json` before parsing. Re-runs read from cache unless `--force`. Phish.net tracks usage and disables abusive apps — be a good citizen.
- Throttle: ≥1s between requests, exponential backoff on 429/5xx.
- **Filter to Phish only.** The DB includes other artists (side projects, guest data). Confirm Phish's `artistid` (historically `1`) from the shows payload and filter everywhere.

---

## 2. Schema (`schema.sql`)

```sql
CREATE TABLE venues (
  venueid    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  city       TEXT, state TEXT, country TEXT
);

CREATE TABLE shows (
  showid     INTEGER PRIMARY KEY,
  showdate   TEXT NOT NULL,          -- ISO yyyy-mm-dd
  venueid    INTEGER REFERENCES venues(venueid),
  tourid     INTEGER,
  tour_name  TEXT,
  exclude    INTEGER DEFAULT 0,      -- 1 for cancelled/no-setlist/soundcheck-only
  show_index INTEGER                 -- 0..N chronological ordinal, computed post-ingest
);
CREATE INDEX idx_shows_date ON shows(showdate);

CREATE TABLE songs (
  songid     INTEGER PRIMARY KEY,
  slug       TEXT UNIQUE NOT NULL,   -- canonical identity; names/aliases vary
  name       TEXT NOT NULL,
  is_original INTEGER,               -- original vs cover, from songs/songdata
  debut_date TEXT,
  times_played INTEGER               -- phish.net lifetime count, for sanity checks
);

CREATE TABLE performances (
  showid     INTEGER REFERENCES shows(showid),
  songid     INTEGER REFERENCES songs(songid),
  set_label  TEXT,                   -- '1','2','3','E','E2', etc.
  position   INTEGER,                -- ordinal within show
  gap        INTEGER,                -- shows since last played (from API)
  trans_mark TEXT,                   -- '>' / '->' segue markers
  PRIMARY KEY (showid, songid, position)
);
CREATE INDEX idx_perf_song ON performances(songid);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- last_refresh, etc.
```

**Post-ingest pass:** compute `show_index` (chronological ordinal over non-excluded Phish shows). All recency math uses show_index, **not calendar dates** — Phish plays in tour bursts, so "shows ago" is the correct clock, not "days ago".

**Ingest gotchas:**
- Future shows exist in `/shows` with no setlist — keep them (they're the prediction targets) but exclude from training.
- Canonicalize songs by `slug`, not name (aliases/typos exist).
- A song can appear twice in one show (e.g. Tweezer + Tweezer Reprise are *different* songs, but a true repeat within a show is rare and legitimate — hence `position` in the PK).
- Soundchecks and one-off cancelled shows: mark `exclude=1` if they show up with anomalous data.

`ingest` = full backfill (iterate `/v5/shows/showyear/{Y}.json` for 1983..current, then `/v5/setlists/showyear/{Y}.json` per year — verify this per-year setlist route works in discovery; fall back to per-showdate calls if not). `refresh` = re-pull current year + any show since `meta.last_refresh`.

**Acceptance:** ~2,000+ shows, ~1,000 songs, ~35,000+ performance rows; row counts for a few known songs match phish.net's `times_played`; spot-check a famous show (e.g. 1997-11-17) reproduces its setlist in order.

---

## 3. Feature engineering (`features.py`)

For a target show T (with venue V, and known preceding shows), produce one row per **candidate song** — every song played at least once in the last 300 shows, plus anything with lifetime plays ≥ 20 (bustout candidates). For each (song, T):

| Feature | Definition |
|---|---|
| `decayed_rate` | Σ over past plays of `0.5^(shows_ago / H)`, normalized by Σ of the same weights over all past shows. H = half-life in shows, default **50**, tune in backtest. This is the recency-biased base rate. |
| `gap` | show_index(T) − show_index(last play). |
| `gap_ratio` | `gap / median_historical_gap(song)`. >1.5–2 ⇒ "due"/bustout signal. Median gap computed from the song's own play history. |
| `played_prev_show` | 1 if played at show T−1. (Near-fatal: Phish virtually never repeats night-to-night.) |
| `played_in_run` | 1 if played earlier in the same multi-night run (consecutive dates, same venue). Stronger than prev-show alone — they avoid repeats across a whole MSG 4-night run. |
| `venue_gap` | shows-at-this-venue since song last played *at venue V* (∞/large if never). Captures reduced repeat likelihood at recurring venues. |
| `plays_this_tour` | count of plays in current tour (same `tourid`). New songs debuted this tour get heavy rotation. |
| `plays_last_10` / `plays_last_50` | raw counts, complements decayed_rate. |
| `song_age_shows` | show_index(T) − show_index(debut). |
| `era_rate` | play rate within current era only (eras: 1.0 ≤1996, 2.0 1997–2000, hiatus, 3.0 2009–2020, 4.0 2021+). Handles retired songs cleanly. |
| `is_original` | covers rotate differently. |

Label: `y = 1` if song appeared at show T.

**Critical constraint: no leakage.** Every feature for show T must be computable using only shows with `show_index < T`. Build features via a single chronological sweep maintaining running state (last-played index per song, per-venue last-played, rolling counts) — this makes both backtesting and live prediction use identical code. Don't trust the API's `gap` field for training features (it's as-of-now); recompute gap from your own chronology. Use the API gap only as a cross-check.

**Acceptance:** feature builder runs over full history in < a few minutes; unit test: for a hand-picked show, verify `played_prev_show`, `gap`, and `decayed_rate` against manually computed values.

---

## 4. Models

### 4a. Heuristic baseline (`models/heuristic.py`) — build this first

```
score = decayed_rate
      × (0.02 if played_prev_show else 1.0)
      × (0.05 if played_in_run else 1.0)
      × venue_penalty(venue_gap)        # e.g. 0.3 if played within last 2 shows at venue
      × due_boost(gap_ratio)            # e.g. 1 + 0.3·clamp(gap_ratio − 1, 0, 2)
```

Convert scores to probabilities: an average show has **K ≈ 20** songs (compute the true mean per era from data). Scale so Σp over candidates = K, cap at 0.99. This baseline is the benchmark every ML model must beat.

### 4b. ML model (`models/ml.py`)

Logistic regression first (interpretable coefficients = sanity check: `played_prev_show` should be hugely negative), then LightGBM. Train on all shows from 2009→(holdout start). **Calibrate** with isotonic regression on a validation slice — calibration is the whole product ("confidence levels"), not an afterthought. Same Σp≈K renormalization at the end.

---

## 5. Backtest harness (`backtest.py`)

Walk-forward over a holdout (default: two most recent tours). For each holdout show: build features from prior shows only → predict → score against the actual setlist.

Metrics per model:
- **Brier score** and **log loss** over all (song, show) pairs.
- **Hit@20 / Hit@25**: of the top-K predicted songs, how many were actually played (intuitive; a good model should hit low-teens out of ~20).
- **Calibration table**: bucket predictions (0–10%, 10–20%, …) and compare predicted vs empirical frequency. Print it.

`backtest` CLI compares heuristic vs LR vs GBM side by side and sweeps H ∈ {25, 50, 100}. **Acceptance:** ML beats heuristic on Brier; calibration buckets within ~5pts; results reproducible (fixed seed).

---

## 6. Predict CLI (`predict.py`)

`phishpred predict 2026-07-25` → resolves the show from the shows table (venue, run context — if it's night 2+, prior nights' setlists feed `played_in_run`), builds features, outputs top 30 as a table: song, probability, gap, key drivers (for the heuristic: which multipliers fired; for GBM: SHAP top-3 optional). `--json` flag for the future API. If predicting mid-run, run `refresh` first so last night's setlist is in.

---

## 7. Later phases (design for, don't build yet)

- **API + frontend:** FastAPI serving `/shows/upcoming`, `/predictions/{showdate}`; React frontend with probability bars. Keep prediction logic import-clean from the CLI so this is a thin wrapper.
- **Deploy:** Neon Postgres (swap SQLite via SQLAlchemy URL), Azure Container Apps (backfills/model jobs won't fit serverless timeouts), nightly cron: `refresh` + regenerate predictions.
- **Prediction market (M2):** play-money YES/NO contracts per (song, show); model provides initial market-maker pricing (LMSR is the standard choice); auto-resolution when the setlist posts to the API; leaderboard scored by Brier. The backtest harness doubles as the resolution/scoring engine.

---

## 8. Suggested build order

1. Project scaffold + API client + discovery calls (verify field names, adjust this doc's schema).
2. Full backfill + schema + acceptance checks.
3. Chronological feature sweep + unit tests.
4. Heuristic baseline + backtest harness + calibration report.
5. LR/GBM + isotonic calibration; beat the baseline.
6. `predict` CLI polish.
