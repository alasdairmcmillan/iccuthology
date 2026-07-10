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
export interface TourReport {
  epoch: string;
  horizon_showdates: string[];
  model: string;
  n_sims: number;
  half_life: number;
  rows: TourRow[];
}

// GET /api/show/{showdate} -> show/{showdate}.json  (multi-source)
export interface ShowSourceRow {
  song: string;
  slug: string;
  prob: number;
  gap?: number;
  drivers?: string[];
}
export interface ShowSource {
  model: string;
  kind: "statistical" | "llm" | "mcp";
  rationale?: string;
  submitted_at?: string;
  rows: ShowSourceRow[];
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
export interface ScoreboardModel {
  kind: "statistical" | "llm" | "mcp";
  n_shows: number;
  hit_rate_top10: number;
  recall: number;
  brier: number;
  log_loss: number;
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
  hits_top10: number;
  hit_rate_top10: number;
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
