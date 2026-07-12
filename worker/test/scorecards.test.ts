import { describe, expect, it } from "vitest";
import worker, { type Env } from "../src/index";

// ---------------------------------------------------------------------------
// GET /api/scoreboard, /api/scorecard/{showdate} (DEPLOY-CONTRACTS.md §6, §8).
// Both are epoch-INDEPENDENT: `scorecards/` lives outside `snapshots/{epoch}/`
// and readers must not consult `latest.json`. Fake R2 mirrors the harness in
// samples.test.ts (kept local here since that one isn't exported).
// ---------------------------------------------------------------------------

interface FakeR2Object {
  json(): Promise<unknown>;
  arrayBuffer(): Promise<ArrayBuffer>;
  httpEtag: string;
}

function makeFakeR2(initial: Record<string, unknown> = {}) {
  const files = new Map<string, unknown>(Object.entries(initial));
  const calls = new Map<string, number>();
  const bucket = {
    async get(key: string): Promise<FakeR2Object | null> {
      calls.set(key, (calls.get(key) ?? 0) + 1);
      if (!files.has(key)) return null;
      const value = files.get(key);
      return {
        json: async () => value,
        arrayBuffer: async () => value as ArrayBuffer,
        httpEtag: `"${key}"`,
      };
    },
  };
  return { bucket, files, callsFor: (key: string) => calls.get(key) ?? 0 };
}

function makeEnv(bucket: unknown): Env {
  return {
    SNAPSHOTS: bucket,
    ASSETS: { fetch: async () => new Response(null, { status: 404 }) },
  } as unknown as Env;
}

async function fetchPath(env: Env, path: string): Promise<Response> {
  return worker.fetch(new Request(`https://worker.test${path}`), env, {} as unknown as ExecutionContext);
}

const SCOREBOARD = {
  updated_at: "2026-07-11T06:10:00Z",
  shows: [
    {
      showdate: "2026-07-10",
      venue_name: "Ruoff Music Center",
      city: "Noblesville",
      state: "IN",
      n_played: 21,
      source_keys: ["heuristic"],
    },
  ],
  models: {
    heuristic: {
      kind: "statistical",
      n_shows: 3,
      hit_rate_top20: 0.55,
      recall: 0.41,
      brier: 0.09,
      log_loss: 0.29,
      avg_n_rows: 37.5,
    },
  },
};

const SCORECARD = {
  showdate: "2026-07-10",
  venue_name: "Ruoff Music Center",
  city: "Noblesville",
  state: "IN",
  frozen_epoch: "228c7eb3a0e9",
  scored_at: "2026-07-11T06:10:00Z",
  phishnet_url: "https://phish.net/setlists/?d=2026-07-10",
  n_played: 21,
  played: [{ slug: "harry-hood", song: "Harry Hood" }],
  sources: {
    heuristic: {
      model: "heuristic",
      kind: "statistical",
      n_rows: 40,
      metrics: { top_n: 20, hits_top20: 6, hit_rate_top20: 0.6, recall: 0.4286, brier: 0.081, log_loss: 0.31 },
      best_call: { song: "Harry Hood", slug: "harry-hood", prob: 0.12 },
      biggest_whiff: null,
      rows: [{ song: "Harry Hood", slug: "harry-hood", prob: 0.61, hit: true }],
    },
  },
  missed_by_all: [],
};

describe("GET /api/scoreboard", () => {
  it("serves scorecards/scoreboard.json verbatim (200)", async () => {
    const { bucket } = makeFakeR2({ "scorecards/scoreboard.json": SCOREBOARD });
    const res = await fetchPath(makeEnv(bucket), "/api/scoreboard");

    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("application/json");
    expect(res.headers.get("cache-control")).toBe("public, max-age=300");
    expect(res.headers.get("access-control-allow-origin")).toBe("*");
    expect(await res.json()).toEqual(SCOREBOARD);
  });

  it("returns 404 with a clear message when no scoreboard has been published", async () => {
    const { bucket } = makeFakeR2({});
    const res = await fetchPath(makeEnv(bucket), "/api/scoreboard");

    expect(res.status).toBe(404);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body.error).toBe("not_found");
    expect(body.message).toMatch(/no scoreboard published yet/i);
  });

  it("does not consult latest.json -- epoch-independent even when no epoch is published", async () => {
    const { bucket, callsFor } = makeFakeR2({ "scorecards/scoreboard.json": SCOREBOARD });
    const res = await fetchPath(makeEnv(bucket), "/api/scoreboard");

    expect(res.status).toBe(200);
    expect(callsFor("latest.json")).toBe(0);
  });
});

describe("GET /api/scorecard/{showdate}", () => {
  it("serves scorecards/{showdate}.json verbatim (200)", async () => {
    const { bucket } = makeFakeR2({ "scorecards/2026-07-10.json": SCORECARD });
    const res = await fetchPath(makeEnv(bucket), "/api/scorecard/2026-07-10");

    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("public, max-age=300");
    expect(await res.json()).toEqual(SCORECARD);
  });

  it("returns 404 with a clear message when no scorecard exists for the showdate", async () => {
    const { bucket } = makeFakeR2({});
    const res = await fetchPath(makeEnv(bucket), "/api/scorecard/2026-07-10");

    expect(res.status).toBe(404);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body.error).toBe("not_found");
    expect(body.message).toMatch(/no scorecard for 2026-07-10/i);
  });

  it("returns 400 bad_request for a malformed showdate path segment", async () => {
    const { bucket } = makeFakeR2({});
    const res = await fetchPath(makeEnv(bucket), "/api/scorecard/not-a-date");

    expect(res.status).toBe(400);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body.error).toBe("bad_request");
  });

  it("does not consult latest.json -- epoch-independent even when no epoch is published", async () => {
    const { bucket, callsFor } = makeFakeR2({ "scorecards/2026-07-10.json": SCORECARD });
    const res = await fetchPath(makeEnv(bucket), "/api/scorecard/2026-07-10");

    expect(res.status).toBe(200);
    expect(callsFor("latest.json")).toBe(0);
  });

  it("decodes URI-encoded showdate segments like siblings (apiShow, apiSetlist)", async () => {
    // %2D is a literal "-"; decodeURIComponent should yield the same key.
    const { bucket } = makeFakeR2({ "scorecards/2026-07-10.json": SCORECARD });
    const res = await fetchPath(makeEnv(bucket), "/api/scorecard/2026%2D07%2D10");

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(SCORECARD);
  });
});
