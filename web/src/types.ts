// JSON shapes returned by the Worker /api/* endpoints.
// Source of truth: DEPLOY-CONTRACTS.md §2 (publish artifacts) and §6 (Worker API).

export type Bucket = "lock" | "likely" | "bustout-watch" | "longshot";

// GET /api/latest  -> meta.json
export interface TourMeta {
  id: string;
  tour_name: string;
  has_data: boolean;
}
export interface Meta {
  epoch: string;
  created_at: string;
  as_of_showdate: string;
  as_of_show_index: number;
  code_version: string;
  models: string[];
  headline_model: string;
  n_sims: number;
  seed: number;
  half_life: number;
  horizon_showdates: string[];
  tours: TourMeta[];
}

// GET /api/schedule -> schedule.json
export interface ScheduleShow {
  showdate: string;
  venue_name: string;
  city: string;
  state: string;
  tour_id: string;
  tour_name: string;
  has_data: boolean;
}
export interface Schedule {
  shows: ScheduleShow[];
}

// GET /api/tour -> tour.json  (mirrors TourReport)
export interface TourRow {
  song: string;
  slug: string;
  expected_plays: number;
  p_at_least_one: number;
  // Play-count buckets -> P(exactly n). Current epochs publish
  // {"0","1","2","3","4+"}; epochs published before the 4+ split have
  // {"0","1","2","3+"}, so consumers must key off what's present.
  dist: Record<string, number>;
  bucket: Bucket;
  gap_ratio?: number;
  analytic_p: number;
}
// Per-tour "plays-so-far" tracker (DEPLOY-CONTRACTS §3). Present on per-tour
// docs (/api/tour/{id}); absent on the all-future tour.json. Time-varying —
// refreshed every publish, never part of the frozen prediction.
export interface TourTracker {
  /** indexed (played) shows in this tour so far */
  n_shows_played: number;
  /** all non-excluded shows in the tour */
  n_shows_total: number;
  /** slug -> # played tour shows featuring it (distinct per show) */
  played_counts: Record<string, number>;
  /** = meta.created_at */
  as_of: string;
}
export interface TourReport {
  epoch: string;
  horizon_showdates: string[];
  model: string;
  n_sims: number;
  half_life: number;
  rows: TourRow[];
  /** §3 plays-so-far tracker (per-tour docs only). */
  tracker?: TourTracker;
  /** true when `rows` are frozen pre-tour backcast predictions. */
  backcast?: boolean;
  /** last pre-tour played showdate the backcast knew (backcast docs only). */
  as_of_showdate?: string;
}

// GET /api/show/{showdate} -> show/{showdate}.json  (multi-source)
export interface ShowSourceRow {
  song: string;
  slug: string;
  prob: number;
  gap?: number;
  drivers?: string[];
}
// A submitted structured setlist call (DEPLOY-CONTRACTS §2/§5) — the 2nd
// benchmark, independent of the ranked `rows` shortlist above.
export interface ShowSetlistSlot {
  slug: string;
  song: string;
}
export interface ShowSetlist {
  sets: Record<string, ShowSetlistSlot[]>;
}
// A PRIOR submission for the same {label, showdate} (§5 versioning), oldest
// first. The top-level ShowSource is always the FINAL/current take.
export interface ShowSourceVersion {
  submitted_at: string;
  rationale?: string;
  rows: ShowSourceRow[];
  setlist?: ShowSetlist;
}
export interface ShowSource {
  model: string;
  kind: "statistical" | "llm" | "mcp";
  rationale?: string;
  submitted_at?: string;
  rows: ShowSourceRow[];
  /** OPTIONAL structured setlist call (§2/§5); absent if not submitted. */
  setlist?: ShowSetlist;
  /** OPTIONAL prior takes, oldest first; absent if none. */
  versions?: ShowSourceVersion[];
}
export interface ShowReport {
  showdate: string;
  venue_name: string;
  city: string;
  state: string;
  epoch: string;
  k: number;
  sources: Record<string, ShowSource>;
}

// GET /api/setlist/{showdate} -> setlist/{showdate}.json  (mirrors SetlistPrediction)
export interface SetlistSong {
  song_name: string;
  slug: string;
  songid: number;
  slot: string;
  prob: number;
  segue_mark: string; // "", " > ", " -> "
}
export interface SetlistPrediction {
  showdate: string;
  venue_name: string;
  era: string;
  model: string;
  seed: number;
  skeleton: Record<string, number>;
  sets: Record<string, SetlistSong[]>;
}

// GET /api/catalog -> catalog.json  (DEPLOY-CONTRACTS.md §2a, --with-catalog)
export interface CatalogSong {
  songid: number;
  slug: string;
  name: string;
  plays: number;
  last: string | null;
}
export interface Catalog {
  epoch: string;
  /** sorted by plays desc — the "due to see" ranking axis */
  songs: CatalogSong[];
  /** each PAST showdate -> songids played */
  by_show: Record<string, number[]>;
}

// GET /api/samples-meta -> samples_meta.json  (DEPLOY-CONTRACTS.md §2)
export interface SamplesVocabEntry {
  i: number;
  songid: number;
  slug: string;
  name: string;
}
export interface SamplesMeta {
  epoch: string;
  n_sims: number;
  seed: number;
  horizon_showdates: string[];
  horizon_showids?: number[];
  vocab: SamplesVocabEntry[];
}

// GET /api/seedfile/{user} -> proxied + parsed phish.net seedfile
export interface Seedfile {
  user: string;
  dates: string[];
}

// GET /api/scoreboard -> scorecards/scoreboard.json  (DEPLOY-CONTRACTS.md §8)
// Epoch-INDEPENDENT: past-prediction accuracy, not scoped to the current epoch.
export interface ScoreboardShow {
  showdate: string;
  venue_name: string;
  city: string;
  state: string;
  n_played: number;
  source_keys: string[];
}
// §8 setlist-benchmark aggregate over scored shows (absent when no
// setlist-scored shows for this model). marquee_calls/exact_calls/
// sharpshooters are TOTALS; hit_rate/placed_rate are unweighted means.
export interface ScoreboardModelSetlist {
  n_shows: number;
  hit_rate: number;
  placed_rate: number;
  /** OPTIONAL: unweighted mean of per-show weighted_score. Absent on legacy
   *  artifacts written before the weighted benchmark (can't be reconstructed
   *  from the aggregate) — the UI renders "—" for it. */
  weighted_score?: number;
  marquee_calls: number;
  exact_calls: number;
  sharpshooters: number;
}
// §8 "Monty Hall dividend" — final take vs. each show's first take (absent
// when no multi-take shows for this model).
export interface ScoreboardModelRefreshGain {
  n_shows: number;
  mean_hit_rate_top20_delta: number;
  mean_recall_delta: number;
}
// §8 head-to-head against the statistical baseline over the SAME scored shows
// (absent for the heuristic entry itself — it is the baseline).
export interface ScoreboardModelVsHeuristic {
  n_shows: number;
  hit_rate_top20_delta: number;
  recall_delta: number;
}
export interface ScoreboardModel {
  kind: "statistical" | "llm" | "mcp";
  n_shows: number;
  hit_rate_top20: number;
  recall: number;
  brier: number;
  log_loss: number;
  /** mean shortlist length submitted over scored shows (20–40 allowed). */
  avg_n_rows: number;
  setlist?: ScoreboardModelSetlist;
  refresh_gain?: ScoreboardModelRefreshGain;
  vs_heuristic?: ScoreboardModelVsHeuristic;
}
export interface Scoreboard {
  updated_at: string;
  /** every scored show, showdate DESC */
  shows: ScoreboardShow[];
  /** unweighted means over scored shows, keyed by source */
  models: Record<string, ScoreboardModel>;
}

// GET /api/scorecard/{showdate} -> scorecards/{showdate}.json  (DEPLOY-CONTRACTS.md §8)
export interface ScoredSong {
  slug: string;
  song: string;
}
export interface ScorecardMetrics {
  /** Size of the hit-rate window: how many top-ranked rows it covers (20
   *  today). Legacy artifacts predate the field and are normalized to 10 on
   *  ingest (see api.ts), so labels render "top 10" for them. */
  top_n: number;
  hits_top20: number;
  hit_rate_top20: number;
  recall: number;
  brier: number;
  log_loss: number;
}
/** best_call = gutsiest hit (lowest-prob hit); biggest_whiff = highest-prob miss. */
export interface ScorecardCall {
  song: string;
  slug: string;
  prob: number;
}
export interface ScorecardRow {
  song: string;
  slug: string;
  prob: number;
  hit: boolean;
}
// Setlist benchmark (§8) — non-null only when the frozen source carried a
// structured setlist call; sources without one (incl. legacy v0) sit out and
// their setlist_score is null.
export interface ScorecardSetlistSong {
  slug: string;
  song: string;
  hit: boolean;
  placed: boolean;
  /** predicted (set, position) == actual (set, position); exact ⊆ placed ⊆ hit. */
  exact: boolean;
}
export interface ScorecardSetlistMarquee {
  opener?: boolean;
  set1_closer?: boolean;
  set2_opener?: boolean;
  set2_closer?: boolean;
  encore?: boolean;
  // Marquee flags only compare set keys present on BOTH sides, so extra keys
  // (e.g. a called "3" the band never played) simply won't appear.
  [slot: string]: boolean | undefined;
}
export interface ScorecardSetlistScore {
  n_songs: number;
  /** predicted setlist by set, annotated hit/placed */
  sets: Record<string, ScorecardSetlistSong[]>;
  hits: number;
  hit_rate: number;
  placed: number;
  placed_rate: number;
  /** (hits + placed + exact_calls) / (3 * n_songs); tiers hit/placed/exact. */
  weighted_score: number;
  marquee: ScorecardSetlistMarquee;
  marquee_calls: number;
  exact_calls: number;
  /** exact_calls >= 2 */
  sharpshooter: boolean;
}
// A PRIOR frozen take, scored with the same machinery, oldest first (§8
// versioning). The top-level ScorecardSource is always the FINAL take.
export interface ScorecardVersion {
  submitted_at: string;
  /** UI labeling heuristic; null -> "pre-run" */
  after_showdate: string | null;
  metrics: ScorecardMetrics;
  setlist_score: ScorecardSetlistScore | null;
  rows: ScorecardRow[];
}
export interface ScorecardSource {
  model: string;
  kind: "statistical" | "llm" | "mcp";
  n_rows: number;
  metrics: ScorecardMetrics;
  best_call: ScorecardCall | null;
  biggest_whiff: ScorecardCall | null;
  /** frozen rows, prob desc */
  rows: ScorecardRow[];
  // mcp:* sources keep their frozen submission fields verbatim.
  rationale?: string;
  submitted_at?: string;
  /** null when this source has no setlist call (sits out the benchmark).
   *  OPTIONAL for backward compat with scorecards written before §8's
   *  setlist benchmark shipped. */
  setlist_score?: ScorecardSetlistScore | null;
  /** OPTIONAL prior scored takes, oldest first; absent/empty when only one
   *  take exists. */
  versions?: ScorecardVersion[];
}
export interface Scorecard {
  showdate: string;
  venue_name: string;
  city: string;
  state: string;
  frozen_epoch: string;
  scored_at: string;
  phishnet_url: string;
  n_played: number;
  /** distinct performed slugs, setlist order */
  played: ScoredSong[];
  /** OPTIONAL per raw set label, position order, distinct within each set. */
  played_sets?: Record<string, ScoredSong[]>;
  sources: Record<string, ScorecardSource>;
  /** played songs in NO source's shortlist */
  missed_by_all: ScoredSong[];
}

// POST /api/run -> run reduction
export interface RunRow {
  song: string;
  slug: string;
  p_at_least_one: number;
  per_night_probs: number[];
  most_likely_night_date: string;
}
export interface RunReport {
  showdates: string[];
  rows: RunRow[];
  missing: string[];
  /** true when produced by the offline union approximation (no samples.bin). */
  approximate?: boolean;
}
