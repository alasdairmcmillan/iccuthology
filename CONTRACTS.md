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
                   "era_rate", "is_original", "plays_last_150"]

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
- `plays_last_150` = plays within the last 150 shows (~5 years of touring; window
  exported as `RECENT_RATE_WINDOW` for the heuristic's long-window rate floor).
- `era_rate` = plays within era(T) before T / max(1, shows in era(T) before T).
- `song_age_shows` = show_index(T) − show_index(first play observed in our data).
- `is_original` from songs table (NULL → 0.5).

## models/heuristic.py (Sonnet — phase 4a)

```python
def heuristic_scores(df: pd.DataFrame) -> pd.DataFrame
    """Input: feature frame (FEATURE_COLUMNS present). Returns copy with added
    columns: `score` plus `recent_rate`, `w_recent`, and multiplier columns
    `m_prev_show`, `m_in_run`, `m_venue`, `m_due` (for driver explanations).
    score = base * m_prev_show * m_in_run * m_venue * m_due
      recent_rate = plays_last_150 / RECENT_RATE_WINDOW
      w_recent    = clip((4 - gap_ratio) / 3, 0, 1)   # 1 while gap_ratio<=1, 0 at >=4
      base        = max(decayed_rate, w_recent * recent_rate)
      m_prev_show = 0.02 if played_prev_show else 1.0
      m_in_run    = 0.05 if played_in_run (and not prev show) else 1.0
      m_venue     = 0.3 if venue_gap <= 2 else 1.0
      m_due       = 1 + 0.3 * clip(gap_ratio - 1, 0, 2)
    The long-window floor keeps steady-but-rare rotation songs alive mid-cycle;
    the w_recent gate removes it beyond 4x median gap so decayed_rate + capped
    m_due stay the only bust-out mechanism. base >= decayed_rate elementwise."""

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

---

# Prediction modes & LLM path (modes plan — build order 1–5)

See `phish-predictor-modes-plan.md`. User answers baked in: horizon default =
rest-of-calendar-year (option to pick a named tour); run no-repeat = hard mask
default, soft as flag; LLM path model-agnostic (compare Claude/Gemini/GPT/open);
per-show one-call batching; setlist = deterministic sampler first, LLM behind
`--llm`.

## features.py — reusable helpers added for the simulator (Step 1)

Existing public API unchanged (`build_features`, `features_for_future_show`,
`mean_setlist_size`, `ID_COLUMNS`, `FEATURE_COLUMNS`, `VENUE_GAP_SENTINEL`). New:

```python
class _State:
    def copy(self) -> _State            # deep-enough; folding a show into a copy never mutates source

def build_state_to_now(conn, half_life=50) -> tuple[_State | None, float, int]
    """Sweep all indexed shows (apply-only). Returns (state, D, max_index); state None if no
    indexed shows. D is the emit denominator at max_index; extrapolate D_t = r**(t-max_index)*(D+1)."""
def emit_candidate_frame(state, *, index, showid, showdate, venueid, tourid, era,
                         run_start_index, D) -> pd.DataFrame   # y=NaN candidate frame
def show_meta(conn, showids: list[int]) -> dict[int, sqlite3.Row]   # alias-resolved venueid/tourid/showdate
def future_show_ids(conn) -> list[int]     # ordered not-yet-indexed showids; eff_index = max_index+1+rank
def future_run_start(conn, target_showid, target_venueid)   # (was _future_run_start; logic unchanged)
```

## simulate.py — forward Monte-Carlo simulator (Step 1)

```python
@dataclass
class SimConfig:
    n_sims: int = 2000; seed: int = 0
    strict_no_repeat: bool = True        # hard mask: P->0 for songs already played earlier in the SAME run
    model: str = "heuristic"             # "heuristic" | "lr" | "gbm"
    half_life: int = 50
    length_control: bool = False         # reserved (Plackett-Luce/top-K); NOT implemented — Bernoulli only

@dataclass
class SimResult:
    horizon_showids: list[int]; horizon_dates: list[str]; horizon_venueids: list[int]
    songs_meta: dict[int, tuple[str, str]]        # songid -> (slug, name)
    samples: list[list[set[int]]]                 # samples[m][t] = set of songids, sim m, horizon pos t
    config: SimConfig

def simulate_horizon(conn, horizon_showids: list[int], config: SimConfig | None = None) -> SimResult
```
Deterministic given `config.seed` (numpy `default_rng(seed).spawn(n_sims)`). lr/gbm
trained once per call, reused across sims. `strict_no_repeat` zeroes prob where the
candidate frame's own `played_in_run == 1`. Acceptance: single-show inclusion rate
over M sims ≈ `heuristic_predict` calibrated prob; hard mask ⇒ zero within-run repeats.

## slots.py — slot / set-structure model (Step 4)

```python
SLOTS = ["set1-open","set1-mid","set1-close","set2-open","set2-mid","set2-close",
         "set3-open","set3-mid","set3-close","encore"]
def classify_slot(set_label, rank_in_set, set_len) -> str   # rank==1 open; ==len close; else mid; e/e2/e3 -> encore; single-song -> open; set '4' -> set3-*
def slot_counts(conn) -> dict[int, dict[str, int]]          # raw observed counts per song per slot
def slot_propensities(conn, *, era_weighted=True, half_life_years=None) -> dict[int, dict[str, float]]
    # songid -> {slot: P(slot|song)}, ~sums to 1. era_weighted default: per-era weight 2**era_index
    # (1.0=1..4.0=16); half_life_years set -> continuous 0.5**(years_ago/half_life_years); False -> raw.
def set_structure_stats(conn, era=None) -> dict
    # {"n_shows", "num_sets_dist": Counter, "num_encores_dist": Counter,
    #  "set_lengths": {"1"|"2"|"encore": {"mean","std","hist"}}}
def sample_set_structure(conn, era: str, rng: np.random.Generator) -> dict[str, int]   # e.g. {'1':9,'2':7,'e':2}
```

## models/llm.py — model-agnostic LLM-as-model + bake-off (Step 3)

```python
class LLMError(RuntimeError): ...
class LLMClient(Protocol):
    model: str
    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 2048) -> dict
def get_client(provider, model, *, api_key=None, base_url=None, **kw) -> LLMClient
    # provider in {"anthropic","openai","google","openai-compat"}; httpx REST, no vendor SDKs.
    # defaults: anthropic=claude-sonnet-5, openai=gpt-4.1, google=gemini-2.5-flash. Keys from env
    # (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY; OPENAI_BASE_URL for openai-compat).

PREDICTIONS_SCHEMA   # {"predictions":[{"slug":str,"prob":number}, ...]}
class PredictionCache:                              # JSON-file cache under data/raw/llm_cache/
    def get(showid, model_name, prompt_version) -> dict | None
    def set(showid, model_name, prompt_version, payload) -> None
    def clear() -> None
class LLMSongModel:                                 # satisfies CalibratedSongModel
    name: str                                       # f"llm:{provider}:{model}"
    def __init__(client, *, prompt_version="v1", cache=None, context_fn=None,
                 k_hint_fn=None, floor_prob=0.01, provider=None)
    def predict_scores(df) -> np.ndarray            # ONE call per showid (batched); omitted songs -> floor
def llm_backtest(conn, model, *, half_life=50, holdout_tours=2, k_for_year=None) -> dict
    # {"metrics", "calibration", "holdout"} — reuses backtest.select_holdout/compute_metrics/etc + ml_predict
def render_llm_backtest(result, model_name) -> str
```
Bump `prompt_version` whenever prompt/schema changes (part of cache key). Tests inject a
fake `LLMClient`; never require a live key at import.

## modes.py — tour / run / chaser (Step 2)

Thin reductions over `simulate.SimResult`. Each report has `.render(json_out=False) -> str`
mirroring `predict.render_prediction` (ASCII rich table; `--json` via `asdict`, floats rounded).

```python
def resolve_tour_horizon(conn, *, tour: str | None = None, year: int | None = None) -> list[int]
    # default: rest-of-current-year future shows; tour=<substr> filters by tour_name. Order matches future_show_ids.
def resolve_run(conn, *, venue=None, nights=None, dates: list[str] | None = None) -> list[int]
def resolve_song(conn, query: str) -> tuple[int, str, str]   # slug exact, else name/slug substring; ValueError if none/ambiguous
def tour_mode(conn, horizon_showids, config: SimConfig | None = None) -> TourReport
def run_mode(conn, run_showids, config: SimConfig | None = None) -> RunReport      # config default strict_no_repeat=True
def chaser_mode(conn, song_query: str, horizon_showids, config: SimConfig | None = None) -> ChaserReport
```
- `TourReport.rows: list[TourSongRow(song, slug, expected_plays, p_at_least_one, dist{0/1/2/3/4+}, bucket, gap_ratio, analytic_p)]`,
  sorted by expected_plays. Buckets: **lock** P(≥1)≥0.9; **likely** ≥0.5; **bustout-watch** P(≥1)<0.5 AND gap_ratio≥2.0; else **longshot**. `analytic_p` = Σ per-show heuristic marginals (labeled over-counting approximation; MC is headline).
- `RunReport.rows: list[RunSongRow(song, slug, p_at_least_one, per_night_probs, most_likely_night_index, most_likely_night_date)]` — `p_at_least_one` is the true joint union across nights (not a per-night sum).
- `ChaserReport(song, slug, ..., p_not_within_horizon, modal_show_date, median_show_date, expected_shows_until_next_play, historical_play_count, low_signal_caveat, distribution: list[ChaserShowProb(showid, showdate, probability)])`. "expected_shows_until_next_play" = mean 1-indexed first-hit position over sims that hit (misses excluded, reported separately as `p_not_within_horizon`); `None` if no sim hits. `low_signal_caveat=True` when historical plays < 20.

## setlist.py — mode 5 (Step 5)

```python
def mine_segue_bigrams(conn, *, min_support=5) -> dict[int, list[tuple[int, float]]]   # prev -> [(next, conf), ...] over ' > '/' -> '
def hard_pairings(conn, *, dominance=0.9, min_support=5) -> dict[int, int]              # follower -> predecessor (near-deterministic segue)
@dataclass class SetlistSong: song_name; slug; songid; slot; prob; segue_mark=""       # mark AFTER this song ('', ' > ', ' -> ')
@dataclass class SetlistPrediction: showdate; venue_name; era; model; skeleton: dict[str,int]; sets: dict[str,list[SetlistSong]]
    def render(self, json_out: bool = False) -> str
def sample_setlist(conn, showdate, *, half_life=50, seed=0, skeleton=None,
                   strict_no_repeat=True, exclude_songids=None, discourage_songids=None) -> SetlistPrediction  # deterministic given seed (§6c-i)
def assemble_setlist_llm(conn, showdate, client, *, half_life=50, n_candidates=40, skeleton=None,
                         strict_no_repeat=True, exclude_songids=None, discourage_songids=None) -> SetlistPrediction  # §6c-ii; inject llm.LLMClient
def actual_setlist(conn, showid) -> list[int]                                          # ordered songids by position
def score_setlist(predicted, actual_ordered_songids) -> dict                           # hit_at_k, jaccard, kendall_tau, lcs_len/ratio, slot_accuracy (§6d)
def evaluate_sampler(conn, showids, *, seed=0, half_life=50) -> dict                    # LEAKAGE-FREE: build_features once + each show's real skeleton
SETLIST_SCHEMA   # {"sets": {<label>: [{"slug","segue_mark"}, ...]}}
```
- Sampler fills each skeleton slot without replacement weighted by `P(song) × P(slot|song)` (from `slots`), honoring mined `hard_pairings` (follower force-placed immediately after its predecessor, never without it) and preferring mined segue bigrams for marks.
- Run-scope no-repeat (mirrors `simulate.SimConfig.strict_no_repeat`): `strict_no_repeat=True` hard-masks candidates whose `played_in_run` feature fires (actual mid-run history); `exclude_songids` hard-masks too (publish passes songids placed in earlier PREDICTED nights of the same run); `discourage_songids` weights by `PREV_NIGHT_DISCOURAGE=0.02` (previous predicted night, different venue). Hard-masked songs are also barred from hard-pair force-placement. Publish derives a per-show seed `zlib.crc32(f"{seed}:{showdate}")` and records it in the setlist doc.
- LLM run-scope no-repeat (same params, same `PREV_NIGHT_DISCOURAGE` constant): with `strict_no_repeat` (default) `exclude_songids` are masked out of the candidate shortlist before the top-N cut, listed in the user prompt as "already played earlier in this run — do NOT select", and dropped from the response if the LLM selects one anyway; with `strict_no_repeat=False` they are only down-weighted, same as `discourage_songids` (soft-penalized regardless of the flag).
- `evaluate_sampler` uses leakage-free contemporaneous features from `features.build_features` (sliced per show) and each show's **real** set skeleton — an upper bound on assembly quality given correct structure; the very first show (no prior plays) is correctly unscoreable. NOTE: `sample_setlist(showdate)` on a *genuine future* show is leakage-free via `features_for_future_show`; on an already-indexed *past* date it inherits `features_for_future_show`'s "as-of-latest" behavior (documented follow-up), which is why `evaluate_sampler` bypasses it.
- CLI: `phishpred setlist <date> [--llm --provider P --model M]`; `--llm` builds `llm.get_client(provider, model)` and calls `assemble_setlist_llm`.

## Testing conventions

- pytest under `tests/`. No network in tests: API tests use JSON fixtures under
  `tests/fixtures/`; DB tests build tiny in-memory DBs via `db.init_db`.
- Feature unit test: hand-built mini-history, assert exact `gap`, `played_prev_show`,
  `decayed_rate`, `venue_gap`, `played_in_run` values.
- Run with: `python -m uv run pytest -q` (uv is NOT on PATH; use `python -m uv`).
