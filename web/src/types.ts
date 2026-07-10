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
