# Module contracts — phish-predictor

Single source of truth for cross-module interfaces. If an implementation needs to
deviate, it must keep these signatures working (add, don't break). Written by the
orchestrator; referenced by all implementation agents.

## Already written (do not modify)

- `src/phishpred/schema.sql` — final DB schema. Field names verified against the
  official phish.net v5 sample project: setlist rows carry `gap`, `set` (values
  `1,2,3,4,e,e2,e3`), `position`, `slug`, `song`, `trans_mark`, `venueid`,
  `artist_name`, `showdate`, `showyear`, `city`, `state`, `setlistnotes`,
  `isjamchart`. Shows are queryable via `/v5/shows/artist/phish.json` and
  `/v5/shows/showyear/{Y}.json`; setlists via `/v5/setlists/showdate/{d}.json` and
  (verify) `/v5/setlists/showyear/{Y}.json`.
- `src/phishpred/config.py` — `PROJECT_ROOT, DATA_DIR, RAW_DIR, DB_PATH, BASE_URL`,
  `get_api_key()`, `ERAS`, `era_for_year(year) -> str` ("1.0"…"4.0").
- `src/phishpred/db.py` — `get_connection(db_path=DB_PATH)` (Row factory, FK on),
  `init_db(conn)`.
- `src/phishpred/probs.py` — `renormalize_to_k(scores, k, cap=0.99) -> np.ndarray`.
- `src/phishpred/cli.py` — typer app; thin wrappers that import the functions below
  lazily. Owned by the orchestrator — do not edit.

## api.py (Sonnet — phase 1)

```python
class PhishNetClient:
    def __init__(self, api_key: str | None = None, cache_dir: Path = RAW_DIR,
                 throttle_seconds: float = 1.0): ...
    def get(self, method_path: str, force: bool = False, **params) -> list[dict]:
        """GET {BASE_URL}/{method_path}.json. Returns response['data'].
        Caches raw JSON body to {cache_dir}/{method_path with / -> _}.json before
        parsing; cache hit skips the network unless force=True.
        Raises PhishNetError on error_message in payload.
        >=1s between real requests; exponential backoff w/ retries on 429/5xx."""
    # convenience wrappers, all -> list[dict]:
    def shows_by_year(self, year: int, force=False)      # shows/showyear/{Y}
    def setlists_by_year(self, year: int, force=False)   # setlists/showyear/{Y}
    def setlists_by_showdate(self, date: str, force=False)
    def songs(self, force=False)                         # songs
    def venues(self, force=False)                        # venues
```

## ingest.py (Sonnet — phase 2)

```python
def full_ingest(conn, client, start_year=1983, end_year=None, force=False) -> IngestStats
def refresh(conn, client) -> IngestStats   # re-pull current year + anything since meta.last_refresh; always force network for those
def compute_show_indexes(conn) -> None     # chronological ordinal over non-excluded, past (showdate <= today), Phish shows; future shows keep show_index NULL
```

Rules:
- Filter to Phish only: keep rows whose `artist_name` lower == "phish" (or artistid
  == 1 once confirmed from payloads — check both, log what you see).
- Future shows (no setlist yet): keep in `shows`, no performances, show_index NULL.
- Songs canonicalized by `slug`. Upsert everywhere (INSERT ... ON CONFLICT DO UPDATE).
- A show with a date in the past but zero performances after setlist ingest:
  exclude=1.
- `meta` keys: `last_refresh` (ISO timestamp), `phish_artistid` (observed).
- Set labels stored lowercase as-is from API ('1','2','3','e','e2',...).

## features.py (Opus — phase 3)

```python
ID_COLUMNS = ["showid", "showdate", "show_index", "venueid", "songid", "slug", "song_name", "y"]
FEATURE_COLUMNS = ["decayed_rate", "gap", "gap_ratio", "played_prev_show",
                   "played_in_run", "venue_gap", "plays_this_tour",
                   "plays_last_10", "plays_last_50", "song_age_shows",
                   "era_rate", "is_original"]

def build_features(conn, half_life: int = 50) -> pd.DataFrame
    """One chronological sweep over all non-excluded shows with show_index NOT NULL.
    One row per (candidate song, show). Columns = ID_COLUMNS + FEATURE_COLUMNS.
    y = 1 if song played at that show. NO LEAKAGE: every feature uses only shows
    with smaller show_index. Recompute gap from own chronology (API gap is as-of-now)."""

def features_for_future_show(conn, showid: int, half_life: int = 50) -> pd.DataFrame
    """Same columns (y = NaN) for a future show already present in `shows`.
    Uses ALL ingested past shows as history. Effective show_index = max+1 + rank of
    the target among future shows ordered by date. Run context: if preceding
    calendar-consecutive show(s) at the same venue already have performances,
    played_in_run/played_prev_show fire from them."""

def mean_setlist_size(conn, era: str | None = None) -> float
    """Mean distinct songs per non-excluded show (optionally within an era). This is K."""
```

Definitions (binding):
- Candidate set at show T: songs played ≥1 time in the previous 300 shows, plus
  songs with ≥20 cumulative plays before T. (Candidates always have ≥1 prior play.)
- `gap` = show_index(T) − show_index(last play). Consecutive-show repeat ⇒ gap=1.
- `played_prev_show` = 1 iff gap == 1.
- Run = maximal chain of consecutive show_index values at the same venueid.
  `played_in_run` = 1 iff played at an earlier show of T's run.
- `venue_gap` = number of shows at T's venue since the song was last played there
  (count of intervening shows at that venue); sentinel 999 if never played there.
- `gap_ratio` = gap / median historical gap of the song (median over its own past
  play-to-play gaps; if <2 past plays, gap_ratio = 1.0).
- `decayed_rate` = Σ_plays 0.5^((T−i)/H) / Σ_{all past shows i} 0.5^((T−i)/H).
- `plays_this_tour`: same tourid as T, before T (0 if tourid NULL).
- `era_rate` = plays within era(T) before T / max(1, shows in era(T) before T).
- `song_age_shows` = show_index(T) − show_index(first play observed in our data).
- `is_original` from songs table (NULL → 0.5).

## models/heuristic.py (Sonnet — phase 4a)

```python
def heuristic_scores(df: pd.DataFrame) -> pd.DataFrame
    """Input: feature frame (FEATURE_COLUMNS present). Returns copy with added
    columns: `score` plus multiplier columns `m_prev_show`, `m_in_run`, `m_venue`,
    `m_due` (the fired multipliers, for driver explanations).
    score = decayed_rate * m_prev_show * m_in_run * m_venue * m_due
      m_prev_show = 0.02 if played_prev_show else 1.0
      m_in_run    = 0.05 if played_in_run (and not prev show) else 1.0
      m_venue     = 0.3 if venue_gap <= 2 else 1.0
      m_due       = 1 + 0.3 * clip(gap_ratio - 1, 0, 2)"""

def heuristic_predict(df: pd.DataFrame, k: float) -> pd.DataFrame
    """heuristic_scores + `prob` column via probs.renormalize_to_k(score, k).
    Must be grouped per show (groupby showid) when df spans multiple shows."""
```

## models/ml.py (Opus — phase 4b)

```python
FEATURE_COLUMNS  # import from features.py

class CalibratedSongModel(Protocol):
    name: str
    def predict_scores(self, df: pd.DataFrame) -> np.ndarray  # calibrated per-row probs, pre-renormalization

def train_lr(train_df, valid_df, seed=42) -> CalibratedSongModel   # scaled LogisticRegression + isotonic on valid slice
def train_gbm(train_df, valid_df, seed=42) -> CalibratedSongModel  # LightGBM + isotonic on valid slice
def ml_predict(model, df, k: float) -> pd.DataFrame  # adds `prob` via renormalize_to_k, per show
```

Training data: rows with show year >= 2009 (era 3.0+). Fixed seeds everywhere.

## backtest.py (Opus — phase 5)

```python
def run_backtest(conn, half_lives=(25, 50, 100), holdout_tours=2, seed=42) -> BacktestReport
    """Holdout = shows of the `holdout_tours` most recent distinct tours that have
    setlists. Walk-forward: features already leakage-free per show, so train on
    show_index < holdout start, validate/calibrate on the tail of train (e.g. last
    15%), score each holdout show. Models: heuristic, lr, gbm. Metrics per model:
    Brier, log loss, Hit@20, Hit@25, calibration table (10 buckets: predicted vs
    empirical vs n). H sweep applies to all models (features rebuilt per H).
    BacktestReport must render to a plain-text table (__str__ or .render())."""
```

## predict.py (Sonnet — phase 6)

```python
def upcoming_shows(conn, venue_query: str | None = None, limit: int = 10) -> list[sqlite3.Row]
    """Future shows (showdate >= today), optionally venue name/city ILIKE filter."""

def predict_show(conn, showdate: str, model: str = "heuristic", half_life: int = 50,
                 top: int = 30) -> ShowPrediction
    """Resolve show by date; features via features_for_future_show (works for past
    shows too, for eyeballing); heuristic directly, or train lr/gbm on all history
    (cache trained model per process). ShowPrediction: showdate, venue, city/state,
    rows = [(song, prob, gap, drivers: list[str])], k."""

def render_prediction(pred, json_out: bool = False) -> str
    """rich table (song, prob %, gap, drivers) or JSON string."""
```

Drivers: for heuristic, names of multipliers ≠ 1 (e.g. "due×1.4", "played-prev-show×0.02")
plus "decayed_rate=0.31". For ML, top |coef·x| terms for LR; skip SHAP for MVP.

## Testing conventions

- pytest under `tests/`. No network in tests: API tests use JSON fixtures under
  `tests/fixtures/`; DB tests build tiny in-memory DBs via `db.init_db`.
- Feature unit test: hand-built mini-history, assert exact `gap`, `played_prev_show`,
  `decayed_rate`, `venue_gap`, `played_in_run` values.
- Run with: `python -m uv run pytest -q` (uv is NOT on PATH; use `python -m uv`).
