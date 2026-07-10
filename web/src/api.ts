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
export function fetchScoreboard(): Promise<Scoreboard> {
  return getJson("/api/scoreboard", () => genScoreboard);
}

export function fetchScorecard(showdate: string): Promise<Scorecard> {
  return getJson(`/api/scorecard/${showdate}`, () => {
    const s = genScorecards[showdate];
    if (!s) throw new Error(`no fixture scorecard for ${showdate}`);
    return s;
  });
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
