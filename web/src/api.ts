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
  Meta,
  RunReport,
  Schedule,
  SetlistPrediction,
  ShowReport,
  TourReport,
} from "./types";
import {
  genMeta,
  genSchedule,
  genSetlists,
  genShows,
  genTour,
} from "./fixtures/generated";
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
