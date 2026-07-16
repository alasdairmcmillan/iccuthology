// Typed API client for the Worker /api/* endpoints (DEPLOY-CONTRACTS §6).
//
// Data source resolution:
//   VITE_API_BASE     base URL for the API ("" = same origin, the prod default).
//   VITE_USE_FIXTURES "true"  -> always use bundled fixtures
//                     "false" -> always hit the network
//                     unset   -> fixtures in dev, real API in prod
// On any network/parse error we fall back to fixtures so the app still renders
// (offline dev, API downtime). Fixture-backed responses match the JSON shapes.
import type {
  Catalog,
  ChaserReport,
  Meta,
  RunReport,
  SamplesMeta,
  Schedule,
  Scoreboard,
  Scorecard,
  Seedfile,
  SetlistPrediction,
  ShowReport,
  TourReport,
} from "./types";
import { decodeSamples, type DecodedSamples } from "./lib/samples";
import {
  genMeta,
  genSchedule,
  genSetlists,
  genShows,
  genTour,
} from "./fixtures/generated";
import { genScoreboard, genScorecards } from "./fixtures/scorecards";
import { computeRunFromShows } from "./lib/run";

const API_BASE: string = import.meta.env.VITE_API_BASE ?? "";
const RAW_FLAG = import.meta.env.VITE_USE_FIXTURES;
export const USE_FIXTURES: boolean =
  RAW_FLAG === "true" ? true : RAW_FLAG === "false" ? false : import.meta.env.DEV;

/** True when the last data shown came from fixtures rather than the live API. */
export let usingFixtures = USE_FIXTURES;

async function getJson<T>(path: string, fixture: () => T): Promise<T> {
  if (USE_FIXTURES) {
    usingFixtures = true;
    return fixture();
  }
  try {
    const res = await fetch(API_BASE + path);
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    usingFixtures = false;
    return (await res.json()) as T;
  } catch (err) {
    console.warn(`[api] ${path} failed, using fixtures:`, err);
    usingFixtures = true;
    return fixture();
  }
}

export function fetchLatest(): Promise<Meta> {
  return getJson("/api/latest", () => genMeta);
}

export function fetchSchedule(): Promise<Schedule> {
  return getJson("/api/schedule", () => genSchedule);
}

export function fetchTour(): Promise<TourReport> {
  return getJson("/api/tour", () => genTour);
}

/**
 * GET /api/tour/{id} — the tour table for one tour, a reduction of the same
 * published simulation over just that tour's nights (DEPLOY-CONTRACTS §2/§6).
 * Fixtures only carry the one summer tour, so every id maps to the same table.
 */
export function fetchTourById(tourId: string): Promise<TourReport> {
  return getJson(`/api/tour/${tourId}`, () => genTour);
}

export function fetchShow(showdate: string): Promise<ShowReport> {
  return getJson(`/api/show/${showdate}`, () => {
    const s = genShows[showdate];
    if (!s) throw new Error(`no fixture show for ${showdate}`);
    return s;
  });
}

export function fetchSetlist(showdate: string): Promise<SetlistPrediction | null> {
  return getJson(`/api/setlist/${showdate}`, () => genSetlists[showdate] ?? null);
}

// Accuracy scorecards (DEPLOY-CONTRACTS §8) — past-prediction scoring, NOT
// epoch-scoped. Same getJson(path, fixture) pattern as the epoch artifacts so
// past mode is fully developable offline.

// ---------------------------------------------------------------------------
// Legacy-name tolerance. Deployed R2 may still hold scorecards/scoreboard
// written under the pre-"top20" field names until a forced rescore runs. Map
// the old names onto the current ones in place (a no-op once the artifact
// already uses the new shape) and default top_n to the legacy window of 10, so
// old artifacts render correctly — the dynamic top_n then labels them "top 10".
// Kept intentionally tiny; delete once R2 is guaranteed rescored.
// ---------------------------------------------------------------------------
type Loose = Record<string, unknown>;
function normalizeMetrics(m: Loose | undefined): void {
  if (!m) return;
  if (m.top_n === undefined) m.top_n = 10;
  if (m.hits_top20 === undefined && m.hits_top10 !== undefined) m.hits_top20 = m.hits_top10;
  if (m.hit_rate_top20 === undefined && m.hit_rate_top10 !== undefined)
    m.hit_rate_top20 = m.hit_rate_top10;
}
// A pre-weighted-benchmark setlist_score is missing per-song `exact` flags and
// the `weighted_score` field. Default each row's `exact` to false, then rebuild
// `weighted_score` from the still-present counts — the reconstruction is EXACT
// (all three counts ship in the legacy shape), no-op once the field is present.
function normalizeSetlistScore(ss: Loose | null | undefined): void {
  if (!ss) return;
  const sets = ss.sets as Record<string, Loose[]> | undefined;
  if (sets) {
    for (const rows of Object.values(sets)) {
      for (const r of rows) if (r.exact === undefined) r.exact = false;
    }
  }
  if (ss.weighted_score === undefined) {
    const n = (ss.n_songs as number) ?? 0;
    const hits = (ss.hits as number) ?? 0;
    const placed = (ss.placed as number) ?? 0;
    const exact = (ss.exact_calls as number) ?? 0;
    ss.weighted_score = n ? (hits + placed + exact) / (3 * n) : 0;
  }
}
function normalizeScorecard(sc: Scorecard): Scorecard {
  for (const src of Object.values(sc.sources) as unknown as Loose[]) {
    normalizeMetrics(src.metrics as Loose);
    normalizeSetlistScore(src.setlist_score as Loose | null);
    for (const v of (src.versions as Loose[] | undefined) ?? []) {
      normalizeMetrics(v.metrics as Loose);
      normalizeSetlistScore(v.setlist_score as Loose | null);
    }
  }
  return sc;
}
function normalizeScoreboard(sb: Scoreboard): Scoreboard {
  for (const m of Object.values(sb.models) as unknown as Loose[]) {
    if (m.hit_rate_top20 === undefined && m.hit_rate_top10 !== undefined)
      m.hit_rate_top20 = m.hit_rate_top10;
    if (m.avg_n_rows === undefined) m.avg_n_rows = 0;
    const rg = m.refresh_gain as Loose | undefined;
    if (rg && rg.mean_hit_rate_top20_delta === undefined && rg.mean_hit_rate_top10_delta !== undefined)
      rg.mean_hit_rate_top20_delta = rg.mean_hit_rate_top10_delta;
  }
  return sb;
}

export function fetchScoreboard(): Promise<Scoreboard> {
  return getJson("/api/scoreboard", () => genScoreboard).then(normalizeScoreboard);
}

export function fetchScorecard(showdate: string): Promise<Scorecard> {
  return getJson(`/api/scorecard/${showdate}`, () => {
    const s = genScorecards[showdate];
    if (!s) throw new Error(`no fixture scorecard for ${showdate}`);
    return s;
  }).then(normalizeScorecard);
}

// ---------------------------------------------------------------------------
// Personal "due to see" data (DEPLOY-CONTRACTS §2a). No fixture fallback —
// catalog.json (~300 KB) and samples.bin (~1.3 MB) are far too heavy to
// bundle, so the Personal screen requires the live API and shows a note in
// the offline preview. Both are epoch-pinned and immutable, so cache the
// in-flight promise module-wide (same pattern as the Worker's samples cache);
// failures evict so the next visit retries.
// ---------------------------------------------------------------------------

async function liveJson<T>(path: string): Promise<T> {
  if (USE_FIXTURES) throw new Error(`${path} is not available in the offline preview`);
  const res = await fetch(API_BASE + path);
  if (!res.ok) {
    let message = `${path} -> ${res.status}`;
    try {
      const body = (await res.json()) as { message?: string };
      if (body.message) message = body.message;
    } catch {
      /* keep the status message */
    }
    throw new Error(message);
  }
  return (await res.json()) as T;
}

let catalogCache: Promise<Catalog> | null = null;
export function fetchCatalog(): Promise<Catalog> {
  if (!catalogCache) {
    const p = liveJson<Catalog>("/api/catalog");
    catalogCache = p;
    p.catch(() => {
      if (catalogCache === p) catalogCache = null;
    });
  }
  return catalogCache;
}

// GET /api/chaser/{slug} (DEPLOY-CONTRACTS §6). No fixture, same reasoning as
// fetchCatalog/fetchSamples — cached per-slug so the Songs page can call this
// once per visible song card without refetching on re-render.
const chaserCache = new Map<string, Promise<ChaserReport>>();
export function fetchChaser(slug: string): Promise<ChaserReport> {
  let p = chaserCache.get(slug);
  if (!p) {
    p = liveJson<ChaserReport>(`/api/chaser/${encodeURIComponent(slug)}`);
    chaserCache.set(slug, p);
    p.catch(() => {
      if (chaserCache.get(slug) === p) chaserCache.delete(slug);
    });
  }
  return p;
}

export interface SamplesBundle {
  meta: SamplesMeta;
  decoded: DecodedSamples;
}

let samplesCache: Promise<SamplesBundle> | null = null;
export function fetchSamples(): Promise<SamplesBundle> {
  if (!samplesCache) {
    const p = (async (): Promise<SamplesBundle> => {
      if (USE_FIXTURES) throw new Error("samples.bin is not available in the offline preview");
      const [meta, binRes] = await Promise.all([
        liveJson<SamplesMeta>("/api/samples-meta"),
        fetch(API_BASE + "/api/samples"),
      ]);
      if (!binRes.ok) throw new Error(`/api/samples -> ${binRes.status}`);
      return { meta, decoded: decodeSamples(await binRes.arrayBuffer()) };
    })();
    samplesCache = p;
    p.catch(() => {
      if (samplesCache === p) samplesCache = null;
    });
  }
  return samplesCache;
}

/** GET /api/seedfile/{user} — Worker-proxied phish.net seedfile (no CORS upstream). */
export function fetchSeedfile(user: string): Promise<Seedfile> {
  return liveJson<Seedfile>(`/api/seedfile/${encodeURIComponent(user)}`);
}

/**
 * POST /api/run — exact joint probability across the selected nights (union
 * over Monte-Carlo samples, DEPLOY-CONTRACTS §4). Offline, we fall back to the
 * labeled independent-events union approximation over the per-show probs
 * (RunReport.approximate = true); the live Worker does the exact joint reduction.
 * `headlineModel` should be the caller's meta.headline_model — the offline
 * reduction has no other way to know which source to aggregate.
 */
export async function postRun(
  showdates: string[],
  headlineModel: string,
): Promise<RunReport> {
  const runFixture = (): RunReport =>
    computeRunFromShows([...showdates].sort(), genShows, headlineModel);

  if (USE_FIXTURES) {
    usingFixtures = true;
    return runFixture();
  }
  try {
    const res = await fetch(API_BASE + "/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ showdates }),
    });
    if (!res.ok) throw new Error(`/api/run -> ${res.status}`);
    usingFixtures = false;
    return (await res.json()) as RunReport;
  } catch (err) {
    console.warn("[api] /api/run failed, using offline reduction:", err);
    usingFixtures = true;
    return runFixture();
  }
}
