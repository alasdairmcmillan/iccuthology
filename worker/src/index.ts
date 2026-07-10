/**
 * Serve tier: single Cloudflare Worker.
 *
 * Serves the static React app (`ASSETS` binding, built from `../web/dist`)
 * for everything under `/*`, and a read-only JSON API under `/api/*` backed
 * by the `SNAPSHOTS` R2 binding. `/api/run` and `/api/chaser/{slug}` decode
 * `samples.bin` and reduce it on the fly (§3, §4) -- no Python, no simulator
 * in the request path. See DEPLOY-CONTRACTS.md §6 for the endpoint contract.
 *
 * Routing: `wrangler.toml` sets `run_worker_first = ["/api/*"]` so this
 * fetch handler always runs for `/api/*` (rather than falling through to the
 * assets binding's SPA fallback). Non-`/api/*` requests are handled by the
 * assets binding directly by Cloudflare and normally never reach `fetch()`
 * at all; the `env.ASSETS.fetch(request)` fallback below exists for
 * completeness (e.g. older Wrangler behavior, `wrangler dev` quirks).
 */

import { chaserReduction, decodeSamples, runReduction, type VocabEntry } from "./samples";
import { parseSeedfile, SEEDFILE_USER_RE, seedfileUrl } from "./seedfile";
import { getBytes, getJson, resolveEpoch, snapshotKey, type Env } from "./r2";

export type { Env };

// ---------------------------------------------------------------------------
// Response helpers
// ---------------------------------------------------------------------------

function corsHeaders(): Record<string, string> {
  return {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type",
  };
}

interface JsonResponseOpts {
  status?: number;
  /** true (default) => `public, max-age=300`; false => `no-store` (DEPLOY-CONTRACTS.md §6: /api/run). */
  cache?: boolean;
}

function jsonResponse(data: unknown, opts: JsonResponseOpts = {}): Response {
  const headers = new Headers({
    "content-type": "application/json; charset=utf-8",
    ...corsHeaders(),
  });
  headers.set("cache-control", opts.cache === false ? "no-store" : "public, max-age=300");
  return new Response(JSON.stringify(data), { status: opts.status ?? 200, headers });
}

function notFound(message: string, extra: Record<string, unknown> = {}): Response {
  return jsonResponse({ error: "not_found", message, ...extra }, { status: 404, cache: false });
}

/** DEPLOY-CONTRACTS.md preamble: "floats rounded to 4 decimals unless noted." */
function round4(n: number): number {
  return Math.round(n * 10000) / 10000;
}

function roundFloatsDeep(value: unknown): unknown {
  if (typeof value === "number") {
    return Number.isInteger(value) ? value : round4(value);
  }
  if (Array.isArray(value)) return value.map(roundFloatsDeep);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = roundFloatsDeep(v);
    }
    return out;
  }
  return value;
}

// ---------------------------------------------------------------------------
// samples_meta.json shape (DEPLOY-CONTRACTS.md §2)
// ---------------------------------------------------------------------------

interface SamplesMeta {
  epoch: string;
  n_sims: number;
  seed: number;
  horizon_showdates: string[];
  horizon_showids?: number[];
  horizon_venueids: Array<number | string>;
  vocab: VocabEntry[];
}

// ---------------------------------------------------------------------------
// Epoch + snapshot object loading
// ---------------------------------------------------------------------------

async function requireEpoch(env: Env): Promise<string | Response> {
  const epoch = await resolveEpoch(env);
  if (epoch === null) {
    return notFound("no published epoch (latest.json missing or empty)");
  }
  return epoch;
}

interface SamplesData {
  meta: SamplesMeta;
  decoded: ReturnType<typeof decodeSamples>;
}

/** Thrown by `fetchSamplesAndMeta` on any load failure; carries enough to
 * rebuild the right error `Response` per request without re-fetching. */
class SamplesLoadError extends Error {
  constructor(
    public readonly kind: "meta_missing" | "bin_missing" | "bad_samples_bin",
    public readonly epoch: string,
    message: string,
  ) {
    super(message);
    this.name = "SamplesLoadError";
  }
}

async function fetchSamplesAndMeta(env: Env, epoch: string): Promise<SamplesData> {
  const [meta, bin] = await Promise.all([
    getJson<SamplesMeta>(env, snapshotKey(epoch, "samples_meta.json")),
    getBytes(env, snapshotKey(epoch, "samples.bin")),
  ]);
  if (meta === null) {
    throw new SamplesLoadError("meta_missing", epoch, "samples_meta.json not found for current epoch");
  }
  if (bin === null) {
    throw new SamplesLoadError("bin_missing", epoch, "samples.bin not found for current epoch");
  }

  let decoded: ReturnType<typeof decodeSamples>;
  try {
    decoded = decodeSamples(bin.body);
  } catch (err) {
    throw new SamplesLoadError("bad_samples_bin", epoch, (err as Error).message);
  }
  return { meta, decoded };
}

// Module-scope cache of the decoded {meta, decoded samples} pair, keyed by
// epoch. Decoding samples.bin is the expensive part of every /api/run and
// /api/chaser request; within an epoch the bytes never change, so decode
// once per isolate and reuse. Cache the PROMISE (not the resolved value) so
// concurrent requests for the same epoch share one in-flight R2 fetch +
// decode. Failures are never cached: the entry is evicted on rejection so
// the next request retries, and the Response for a failure is rebuilt fresh
// per request from the rejection (never memoized as a Response).
let samplesCache: { epoch: string; value: Promise<SamplesData> } | null = null;

async function loadSamplesAndMeta(env: Env, epoch: string): Promise<SamplesData | Response> {
  if (samplesCache === null || samplesCache.epoch !== epoch) {
    const value = fetchSamplesAndMeta(env, epoch);
    samplesCache = { epoch, value };
    void value.catch(() => {
      if (samplesCache?.value === value) samplesCache = null;
    });
  }

  try {
    return await samplesCache.value;
  } catch (err) {
    if (!(err instanceof SamplesLoadError)) throw err;
    if (err.kind === "bad_samples_bin") {
      return jsonResponse(
        { error: "bad_samples_bin", message: err.message, epoch: err.epoch },
        { status: 502, cache: false },
      );
    }
    const message =
      err.kind === "meta_missing"
        ? "samples_meta.json not found for current epoch"
        : "samples.bin not found for current epoch";
    return notFound(message, { epoch: err.epoch });
  }
}

// ---------------------------------------------------------------------------
// Endpoint handlers
// ---------------------------------------------------------------------------

async function apiLatest(env: Env): Promise<Response> {
  const epoch = await requireEpoch(env);
  if (epoch instanceof Response) return epoch;
  const meta = await getJson<unknown>(env, snapshotKey(epoch, "meta.json"));
  if (meta === null) return notFound("meta.json not found for current epoch", { epoch });
  return jsonResponse(meta);
}

/** `{showdate}` path segments must look like YYYY-MM-DD; matches apiSeedfile's 400 style. */
const SHOWDATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * GET /api/scoreboard -> scorecards/scoreboard.json (DEPLOY-CONTRACTS.md §8).
 * Epoch-INDEPENDENT: `scorecards/` lives outside `snapshots/{epoch}/`, so this
 * does not consult `latest.json`/resolveEpoch at all.
 */
async function apiScoreboard(env: Env): Promise<Response> {
  const data = await getJson<unknown>(env, "scorecards/scoreboard.json");
  if (data === null) return notFound("no scoreboard published yet");
  return jsonResponse(data);
}

/**
 * GET /api/scorecard/{showdate} -> scorecards/{showdate}.json (DEPLOY-CONTRACTS.md
 * §8). Epoch-INDEPENDENT, same as apiScoreboard.
 */
async function apiScorecard(env: Env, showdate: string): Promise<Response> {
  if (!SHOWDATE_RE.test(showdate)) {
    return jsonResponse(
      { error: "bad_request", message: `invalid showdate ${JSON.stringify(showdate)}` },
      { status: 400, cache: false },
    );
  }
  const data = await getJson<unknown>(env, `scorecards/${showdate}.json`);
  if (data === null) return notFound(`no scorecard for ${showdate}`);
  return jsonResponse(data);
}

/** GET /api/schedule, /api/tour, /api/show/{showdate}, /api/setlist/{showdate}, /api/samples-meta */
async function apiSnapshotJson(env: Env, relPath: string): Promise<Response> {
  const epoch = await requireEpoch(env);
  if (epoch instanceof Response) return epoch;
  const data = await getJson<unknown>(env, snapshotKey(epoch, relPath));
  if (data === null) return notFound(`${relPath} not found for current epoch`, { epoch });
  return jsonResponse(data);
}

async function apiSamplesBin(env: Env): Promise<Response> {
  const epoch = await requireEpoch(env);
  if (epoch instanceof Response) return epoch;
  const bin = await getBytes(env, snapshotKey(epoch, "samples.bin"));
  if (bin === null) return notFound("samples.bin not found for current epoch", { epoch });

  const headers = new Headers({
    "content-type": "application/octet-stream",
    "content-length": String(bin.body.byteLength),
    "x-samples-meta-url": "/api/samples-meta",
    "cache-control": "public, max-age=300",
    etag: bin.etag,
    ...corsHeaders(),
  });
  return new Response(bin.body, { status: 200, headers });
}

interface RunRequestBody {
  showdates?: unknown;
}

async function apiRun(request: Request, env: Env): Promise<Response> {
  let body: RunRequestBody;
  try {
    body = (await request.json()) as RunRequestBody;
  } catch {
    return jsonResponse({ error: "bad_request", message: "body must be valid JSON" }, { status: 400, cache: false });
  }
  if (!Array.isArray(body.showdates) || !body.showdates.every((d) => typeof d === "string")) {
    return jsonResponse(
      { error: "bad_request", message: '"showdates" must be an array of strings' },
      { status: 400, cache: false },
    );
  }
  const requested: string[] = body.showdates;

  const epoch = await requireEpoch(env);
  if (epoch instanceof Response) return epoch;

  const loaded = await loadSamplesAndMeta(env, epoch);
  if (loaded instanceof Response) return loaded;
  const { meta, decoded } = loaded;

  // Map requested showdates -> horizon indices, preserving horizon (chronological)
  // order regardless of request order; unmatched dates are reported in `missing`.
  const indexByDate = new Map(meta.horizon_showdates.map((d, t) => [d, t] as const));
  const requestedSet = new Set(requested);
  const missing = requested.filter((d) => !indexByDate.has(d));

  const selectedShowIndices: number[] = [];
  const matchedDates: string[] = [];
  for (let t = 0; t < meta.horizon_showdates.length; t++) {
    const d = meta.horizon_showdates[t];
    if (requestedSet.has(d)) {
      selectedShowIndices.push(t);
      matchedDates.push(d);
    }
  }

  const rows = runReduction(decoded.samples, selectedShowIndices, meta.vocab, meta.horizon_showdates);

  return jsonResponse(
    roundFloatsDeep({ showdates: matchedDates, rows, missing }),
    { status: 200, cache: false }, // /api/run: no-store (DEPLOY-CONTRACTS.md §6)
  );
}

async function apiChaser(env: Env, slug: string): Promise<Response> {
  const epoch = await requireEpoch(env);
  if (epoch instanceof Response) return epoch;

  const loaded = await loadSamplesAndMeta(env, epoch);
  if (loaded instanceof Response) return loaded;
  const { meta, decoded } = loaded;

  const needle = slug.toLowerCase();
  const vocabEntry =
    meta.vocab.find((v) => v.slug === slug) ?? meta.vocab.find((v) => v.slug.toLowerCase() === needle);
  if (!vocabEntry) {
    return notFound(`unknown song slug ${JSON.stringify(slug)}`, { epoch });
  }

  const reduced = chaserReduction(decoded.samples, vocabEntry.i, meta.horizon_showdates, meta.horizon_showids);

  // Match src/phishpred/modes.py ChaserReport / ChaserShowProb by key name
  // (as far as derivable from samples + meta -- no DB, so `model`,
  // `historical_play_count`, `low_signal_caveat` are omitted rather than
  // fabricated; see DEPLOY-CONTRACTS.md §6).
  return jsonResponse(
    roundFloatsDeep({
      song: vocabEntry.name,
      slug: vocabEntry.slug,
      songid: vocabEntry.songid,
      epoch: meta.epoch,
      n_sims: meta.n_sims,
      ...(meta.horizon_showids ? { horizon_showids: meta.horizon_showids } : {}),
      horizon_dates: meta.horizon_showdates,
      p_not_within_horizon: reduced.p_not_within_horizon,
      modal_show_date: reduced.modal_show_date,
      median_show_date: reduced.median_show_date,
      expected_shows_until_next_play: reduced.expected_shows_until_next_play,
      distribution: reduced.distribution,
    }),
  );
}

/**
 * GET /api/seedfile/{user} — proxy + parse a phish.net seedfile (attended
 * showdates). phish.net sends no CORS headers, so the browser can't fetch
 * seedfiles itself; the Personal screen calls this instead. Response:
 * `{"user": ..., "dates": ["yyyy-mm-dd", ...]}` (sorted, deduped).
 */
async function apiSeedfile(user: string): Promise<Response> {
  if (!SEEDFILE_USER_RE.test(user)) {
    return jsonResponse(
      { error: "bad_request", message: "invalid phish.net username" },
      { status: 400, cache: false },
    );
  }

  let upstream: Response;
  try {
    upstream = await fetch(seedfileUrl(user), {
      headers: { "user-agent": "phishpred-worker/0.1" },
    });
  } catch (err) {
    return jsonResponse(
      { error: "upstream_error", message: `phish.net fetch failed: ${(err as Error).message}` },
      { status: 502, cache: false },
    );
  }
  if (!upstream.ok) {
    return notFound(`no seedfile for user ${JSON.stringify(user)} (phish.net ${upstream.status})`);
  }

  const dates = parseSeedfile(await upstream.text());
  if (dates.length === 0) {
    return notFound(`no attended showdates found in seedfile for ${JSON.stringify(user)}`);
  }
  return jsonResponse({ user, dates });
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

async function handleApi(request: Request, env: Env, url: URL): Promise<Response> {
  const { pathname } = url;
  const method = request.method;

  if (method === "GET" && pathname === "/api/latest") return apiLatest(env);
  if (method === "GET" && pathname === "/api/scoreboard") return apiScoreboard(env);
  if (method === "GET" && pathname === "/api/schedule") return apiSnapshotJson(env, "schedule.json");
  if (method === "GET" && pathname === "/api/tour") return apiSnapshotJson(env, "tour.json");
  if (method === "GET" && pathname === "/api/samples") return apiSamplesBin(env);
  if (method === "GET" && pathname === "/api/samples-meta") return apiSnapshotJson(env, "samples_meta.json");
  if (method === "GET" && pathname === "/api/catalog") return apiSnapshotJson(env, "catalog.json");
  if (method === "POST" && pathname === "/api/run") return apiRun(request, env);

  let m: RegExpMatchArray | null;
  if (method === "GET" && (m = pathname.match(/^\/api\/show\/([^/]+)$/))) {
    return apiSnapshotJson(env, `show/${decodeURIComponent(m[1])}.json`);
  }
  if (method === "GET" && (m = pathname.match(/^\/api\/setlist\/([^/]+)$/))) {
    return apiSnapshotJson(env, `setlist/${decodeURIComponent(m[1])}.json`);
  }
  if (method === "GET" && (m = pathname.match(/^\/api\/tour\/([^/]+)$/))) {
    return apiSnapshotJson(env, `tour/${decodeURIComponent(m[1])}.json`);
  }
  if (method === "GET" && (m = pathname.match(/^\/api\/chaser\/([^/]+)$/))) {
    return apiChaser(env, decodeURIComponent(m[1]));
  }
  if (method === "GET" && (m = pathname.match(/^\/api\/seedfile\/([^/]+)$/))) {
    return apiSeedfile(decodeURIComponent(m[1]));
  }
  if (method === "GET" && (m = pathname.match(/^\/api\/scorecard\/([^/]+)$/))) {
    return apiScorecard(env, decodeURIComponent(m[1]));
  }

  return jsonResponse(
    { error: "not_found", message: `no route for ${method} ${pathname}` },
    { status: 404, cache: false },
  );
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/api/")) {
      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders() });
      }
      try {
        return await handleApi(request, env, url);
      } catch (err) {
        return jsonResponse(
          { error: "internal_error", message: (err as Error).message },
          { status: 500, cache: false },
        );
      }
    }

    return env.ASSETS.fetch(request);
  },
};
