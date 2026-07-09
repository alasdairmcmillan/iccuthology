import { describe, expect, it } from "vitest";
import {
  chaserReduction,
  decodeSamples,
  runReduction,
  uvarintDecode,
  uvarintEncode,
  type VocabEntry,
} from "../src/samples";
import worker, { type Env } from "../src/index";

// ---------------------------------------------------------------------------
// Helpers: build a samples.bin buffer per DEPLOY-CONTRACTS.md §3.
// ---------------------------------------------------------------------------

function buildHeader(nSims: number, nShows: number, nVocab: number): number[] {
  const bytes: number[] = [];
  bytes.push(0x50, 0x53, 0x4d, 0x50); // "PSMP"
  bytes.push(0x01); // version
  const u32 = (v: number) => [v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff];
  bytes.push(...u32(nSims), ...u32(nShows), ...u32(nVocab));
  return bytes;
}

function buildBinFile(nSims: number, nShows: number, nVocab: number, body: number[]): ArrayBuffer {
  const bytes = [...buildHeader(nSims, nShows, nVocab), ...body];
  return new Uint8Array(bytes).buffer;
}

// ---------------------------------------------------------------------------
// §3 reference vectors: uvarint
// ---------------------------------------------------------------------------

describe("uvarintDecode -- §3 reference vectors", () => {
  it("uvarint(0) = [0x00]", () => {
    const { value, next } = uvarintDecode(new Uint8Array([0x00]), 0);
    expect(value).toBe(0);
    expect(next).toBe(1);
  });

  it("uvarint(1) = [0x01]", () => {
    const { value, next } = uvarintDecode(new Uint8Array([0x01]), 0);
    expect(value).toBe(1);
    expect(next).toBe(1);
  });

  it("uvarint(127) = [0x7F]", () => {
    const { value, next } = uvarintDecode(new Uint8Array([0x7f]), 0);
    expect(value).toBe(127);
    expect(next).toBe(1);
  });

  it("uvarint(128) = [0x80, 0x01]", () => {
    const { value, next } = uvarintDecode(new Uint8Array([0x80, 0x01]), 0);
    expect(value).toBe(128);
    expect(next).toBe(2);
  });

  it("uvarint(300) = [0xAC, 0x02]", () => {
    const { value, next } = uvarintDecode(new Uint8Array([0xac, 0x02]), 0);
    expect(value).toBe(300);
    expect(next).toBe(2);
  });

  it("decodes a uvarint embedded mid-buffer (respects offset)", () => {
    const buf = new Uint8Array([0xff, 0xff, 0x80, 0x01, 0x00]);
    const { value, next } = uvarintDecode(buf, 2);
    expect(value).toBe(128);
    expect(next).toBe(4);
  });

  it("round-trips uvarintEncode <-> uvarintDecode for the reference values", () => {
    for (const v of [0, 1, 127, 128, 300, 12345, 2 ** 20]) {
      const encoded = uvarintEncode(v);
      const { value, next } = uvarintDecode(encoded, 0);
      expect(value).toBe(v);
      expect(next).toBe(encoded.length);
    }
  });

  it("uvarintEncode matches the exact reference byte sequences", () => {
    expect([...uvarintEncode(0)]).toEqual([0x00]);
    expect([...uvarintEncode(1)]).toEqual([0x01]);
    expect([...uvarintEncode(127)]).toEqual([0x7f]);
    expect([...uvarintEncode(128)]).toEqual([0x80, 0x01]);
    expect([...uvarintEncode(300)]).toEqual([0xac, 0x02]);
  });
});

// ---------------------------------------------------------------------------
// §3 reference vector: the worked file example
// ---------------------------------------------------------------------------

describe("decodeSamples -- §3 worked file example", () => {
  it("n_sims=1, n_shows=1, vocab=[0,1,2], sample {0,2} decodes to header + [0x02,0x00,0x02]", () => {
    // count=2, idx 0, idx 2 (ascending sorted)
    const buf = buildBinFile(1, 1, 3, [0x02, 0x00, 0x02]);
    const decoded = decodeSamples(buf);

    expect(decoded.nSims).toBe(1);
    expect(decoded.nShows).toBe(1);
    expect(decoded.nVocab).toBe(3);
    expect(decoded.samples).toHaveLength(1);
    expect(decoded.samples[0]).toHaveLength(1);
    expect(decoded.samples[0][0]).toEqual([0, 2]);
  });

  it("decodes an empty set (count=0) for a (sim, show)", () => {
    const buf = buildBinFile(1, 1, 3, [0x00]);
    const decoded = decodeSamples(buf);
    expect(decoded.samples[0][0]).toEqual([]);
  });

  it("round-trips a multi-sim, multi-show file built with uvarintEncode", () => {
    // sim0: show0={0,300}, show1={}
    // sim1: show0={1}, show1={0,1,2}
    const body: number[] = [];
    const pushSet = (indices: number[]) => {
      body.push(...uvarintEncode(indices.length));
      for (const i of indices) body.push(...uvarintEncode(i));
    };
    pushSet([0, 300]);
    pushSet([]);
    pushSet([1]);
    pushSet([0, 1, 2]);

    const buf = buildBinFile(2, 2, 301, body);
    const decoded = decodeSamples(buf);

    expect(decoded.nSims).toBe(2);
    expect(decoded.nShows).toBe(2);
    expect(decoded.samples[0][0]).toEqual([0, 300]);
    expect(decoded.samples[0][1]).toEqual([]);
    expect(decoded.samples[1][0]).toEqual([1]);
    expect(decoded.samples[1][1]).toEqual([0, 1, 2]);
  });

  it("rejects bad magic", () => {
    const bytes = new Uint8Array(buildBinFile(0, 0, 0, []));
    bytes[0] = 0x00;
    expect(() => decodeSamples(bytes)).toThrow(/magic/i);
  });

  it("rejects unsupported version", () => {
    const bytes = new Uint8Array(buildBinFile(0, 0, 0, []));
    bytes[4] = 0x02;
    expect(() => decodeSamples(bytes)).toThrow(/version/i);
  });
});

// ---------------------------------------------------------------------------
// §4 reductions: hand-built sample sets
// ---------------------------------------------------------------------------

const VOCAB: VocabEntry[] = [{ i: 0, songid: 101, slug: "test-song", name: "Test Song" }];
const HORIZON_DATES = ["2026-07-10", "2026-07-12"];

describe("runReduction -- §4 formulas, 2-night union", () => {
  // 3 sims x 2 nights (horizon positions t=0,1). Song (vocab index 0):
  //   sim0: night0={0}, night1={}      -> hits union, night0 only
  //   sim1: night0={},  night1={0}     -> hits union, night1 only
  //   sim2: night0={},  night1={}      -> never hits
  const samples: number[][][] = [
    [[0], []],
    [[], [0]],
    [[], []],
  ];

  it("computes p_at_least_one as the joint union over both nights, not sum/product of marginals", () => {
    const rows = runReduction(samples, [0, 1], VOCAB, HORIZON_DATES);
    expect(rows).toHaveLength(1);
    const row = rows[0];
    expect(row.song).toBe("Test Song");
    expect(row.slug).toBe("test-song");
    // union hits: sim0, sim1 -> 2/3 (NOT 1/3 + 1/3 = 2/3 coincidentally equal here since
    // no sim hits on both nights; the point is this is mean([i in union]), verified below
    // against a manual union computation, not the additive shortcut).
    expect(row.p_at_least_one).toBeCloseTo(2 / 3, 10);
  });

  it("computes correct per-night marginals independent of the union", () => {
    const rows = runReduction(samples, [0, 1], VOCAB, HORIZON_DATES);
    const row = rows[0];
    expect(row.per_night_probs).toEqual([1 / 3, 1 / 3]);
  });

  it("picks the first tied night as most-likely (matches Python max() first-occurrence)", () => {
    const rows = runReduction(samples, [0, 1], VOCAB, HORIZON_DATES);
    const row = rows[0];
    expect(row.most_likely_night_index).toBe(0);
    expect(row.most_likely_night_date).toBe("2026-07-10");
  });

  it("union differs from a hypothetical (wrong) 1-Π(1-p_i) computation when nights are correlated", () => {
    // A song that always plays both nights together: union prob must equal the
    // per-night marginal exactly (not double count), unlike 1-(1-p)^2.
    const correlated: number[][][] = [
      [[0], [0]],
      [[], []],
    ];
    const rows = runReduction(correlated, [0, 1], VOCAB, HORIZON_DATES);
    const row = rows[0];
    expect(row.per_night_probs).toEqual([0.5, 0.5]);
    // Correct joint union: 1/2 (only sim0 has it, on both nights).
    expect(row.p_at_least_one).toBeCloseTo(0.5, 10);
    // The wrong additive/independence formula 1-(1-0.5)*(1-0.5) = 0.75 must NOT match.
    expect(row.p_at_least_one).not.toBeCloseTo(0.75, 10);
  });

  it("restricting selectedShowIndices to a single night reduces to that night's marginal", () => {
    const rows = runReduction(samples, [1], VOCAB, HORIZON_DATES);
    const row = rows[0];
    expect(row.per_night_probs).toEqual([1 / 3]);
    expect(row.p_at_least_one).toBeCloseTo(1 / 3, 10);
    expect(row.most_likely_night_date).toBe("2026-07-12");
  });
});

describe("chaserReduction -- §4 first-hit-index formulas", () => {
  // Same 3-sim setup as above, reduced over the full 2-show horizon:
  //   sim0 hits at t=0, sim1 hits at t=1, sim2 misses entirely.
  const samples: number[][][] = [
    [[0], []],
    [[], [0]],
    [[], []],
  ];

  it("computes P(next play at t) per horizon show", () => {
    const result = chaserReduction(samples, 0, HORIZON_DATES);
    expect(result.distribution).toEqual([
      { showid: null, showdate: "2026-07-10", probability: 1 / 3 },
      { showid: null, showdate: "2026-07-12", probability: 1 / 3 },
    ]);
  });

  it("threads horizonShowids onto each distribution entry as `showid` (mirrors modes.ChaserShowProb)", () => {
    const result = chaserReduction(samples, 0, HORIZON_DATES, [5001, 5002]);
    expect(result.distribution).toEqual([
      { showid: 5001, showdate: "2026-07-10", probability: 1 / 3 },
      { showid: 5002, showdate: "2026-07-12", probability: 1 / 3 },
    ]);
  });

  it("computes p_not_within as the miss fraction", () => {
    const result = chaserReduction(samples, 0, HORIZON_DATES);
    expect(result.p_not_within_horizon).toBeCloseTo(1 / 3, 10);
  });

  it("computes modal/median show dates with the lower-median tie convention", () => {
    const result = chaserReduction(samples, 0, HORIZON_DATES);
    // hitCounts = [1, 1] tie -> first index (t=0)
    expect(result.modal_show_date).toBe("2026-07-10");
    // sorted hit positions = [0, 1]; lower-median index = floor((2-1)/2) = 0 -> position 0
    expect(result.median_show_date).toBe("2026-07-10");
  });

  it("computes expected_shows_until_next_play as the mean 1-indexed position among hits only", () => {
    const result = chaserReduction(samples, 0, HORIZON_DATES);
    // hits at t=0 and t=1 -> 1-indexed positions 1 and 2 -> mean 1.5 (miss excluded)
    expect(result.expected_shows_until_next_play).toBeCloseTo(1.5, 10);
  });

  it("returns null modal/median/expected and p_not_within=1 when the song never hits", () => {
    const allMiss: number[][][] = [
      [[], []],
      [[], []],
    ];
    const result = chaserReduction(allMiss, 0, HORIZON_DATES);
    expect(result.p_not_within_horizon).toBe(1);
    expect(result.modal_show_date).toBeNull();
    expect(result.median_show_date).toBeNull();
    expect(result.expected_shows_until_next_play).toBeNull();
    expect(result.distribution.every((d) => d.probability === 0)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// index.ts integration: /api/chaser response contract (DEPLOY-CONTRACTS.md
// §6, mirrors modes.ChaserReport/ChaserShowProb) + the module-scope samples
// cache. Kept in this file (rather than a new test file) per the file-scope
// constraint on this change -- see PR description.
// ---------------------------------------------------------------------------

interface FakeR2Object {
  json(): Promise<unknown>;
  arrayBuffer(): Promise<ArrayBuffer>;
  httpEtag: string;
}

/** Minimal in-memory stand-in for the `SNAPSHOTS` R2 binding, with a per-key
 * call counter so tests can assert on cache hits/misses. */
function makeFakeR2(initial: Record<string, unknown | ArrayBuffer> = {}) {
  const files = new Map<string, unknown | ArrayBuffer>(Object.entries(initial));
  const calls = new Map<string, number>();
  const bucket = {
    async get(key: string): Promise<FakeR2Object | null> {
      calls.set(key, (calls.get(key) ?? 0) + 1);
      if (!files.has(key)) return null;
      const value = files.get(key);
      const isBytes = value instanceof ArrayBuffer;
      return {
        json: async () => {
          if (isBytes) throw new Error(`${key} is not JSON`);
          return value;
        },
        arrayBuffer: async () => {
          if (!isBytes) throw new Error(`${key} is not bytes`);
          return value;
        },
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

const CHASER_VOCAB: VocabEntry[] = [{ i: 0, songid: 101, slug: "test-song", name: "Test Song" }];
const CHASER_HORIZON_DATES = ["2026-07-10", "2026-07-12"];
const CHASER_HORIZON_SHOWIDS = [5001, 5002];
// sim0 hits t=0, sim1 hits t=1, sim2 misses -- same fixture as the
// chaserReduction unit tests above.
const CHASER_SAMPLES: number[][][] = [
  [[0], []],
  [[], [0]],
  [[], []],
];

function chaserMeta(withShowids: boolean, epoch: string) {
  return {
    epoch,
    n_sims: 3,
    seed: 0,
    horizon_showdates: CHASER_HORIZON_DATES,
    ...(withShowids ? { horizon_showids: CHASER_HORIZON_SHOWIDS } : {}),
    horizon_venueids: [1, 1],
    vocab: CHASER_VOCAB,
  };
}

function chaserSamplesBin(): ArrayBuffer {
  const body: number[] = [];
  for (const sim of CHASER_SAMPLES) {
    for (const stepSet of sim) {
      body.push(...uvarintEncode(stepSet.length));
      for (const i of stepSet) body.push(...uvarintEncode(i));
    }
  }
  return buildBinFile(3, 2, 1, body);
}

async function fetchChaser(env: Env, slug = "test-song"): Promise<Response> {
  return worker.fetch(new Request(`https://worker.test/api/chaser/${slug}`), env, {} as unknown as ExecutionContext);
}

describe("apiChaser response contract (DEPLOY-CONTRACTS.md §6, modes.ChaserReport)", () => {
  it("matches ChaserReport/ChaserShowProb field names for the derivable subset, no fabricated DB fields", async () => {
    const epoch = "epoch-contract";
    const { bucket } = makeFakeR2({
      "latest.json": { epoch },
      [`snapshots/${epoch}/samples_meta.json`]: chaserMeta(true, epoch),
      [`snapshots/${epoch}/samples.bin`]: chaserSamplesBin(),
    });

    const res = await fetchChaser(makeEnv(bucket));
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;

    expect(Object.keys(body).sort()).toEqual(
      [
        "song",
        "slug",
        "songid",
        "epoch",
        "n_sims",
        "horizon_showids",
        "horizon_dates",
        "p_not_within_horizon",
        "modal_show_date",
        "median_show_date",
        "expected_shows_until_next_play",
        "distribution",
      ].sort(),
    );
    expect(body.song).toBe("Test Song");
    expect(body.slug).toBe("test-song");
    expect(body.songid).toBe(101);
    expect(body.epoch).toBe(epoch);
    expect(body.n_sims).toBe(3);
    expect(body.horizon_showids).toEqual(CHASER_HORIZON_SHOWIDS);
    expect(body.horizon_dates).toEqual(CHASER_HORIZON_DATES);
    expect(body.modal_show_date).toBe("2026-07-10");
    expect(body.median_show_date).toBe("2026-07-10");
    expect(body.expected_shows_until_next_play).toBeCloseTo(1.5, 3);
    expect(body.distribution).toEqual([
      { showid: 5001, showdate: "2026-07-10", probability: 0.3333 },
      { showid: 5002, showdate: "2026-07-12", probability: 0.3333 },
    ]);
    // Not derivable from samples + meta alone (needs DB) -- must not be fabricated.
    expect(body).not.toHaveProperty("model");
    expect(body).not.toHaveProperty("historical_play_count");
    expect(body).not.toHaveProperty("low_signal_caveat");
  });

  it("omits horizon_showids and nulls per-entry showid for snapshots without meta.horizon_showids", async () => {
    const epoch = "epoch-no-showids";
    const { bucket } = makeFakeR2({
      "latest.json": { epoch },
      [`snapshots/${epoch}/samples_meta.json`]: chaserMeta(false, epoch),
      [`snapshots/${epoch}/samples.bin`]: chaserSamplesBin(),
    });

    const res = await fetchChaser(makeEnv(bucket));
    const body = (await res.json()) as Record<string, unknown>;

    expect(body).not.toHaveProperty("horizon_showids");
    expect((body.distribution as Array<{ showid: unknown }>).map((d) => d.showid)).toEqual([null, null]);
  });
});

describe("loadSamplesAndMeta module-scope cache (index.ts)", () => {
  it("reuses the decoded samples across two sequential requests for the same epoch (single R2 fetch)", async () => {
    const epoch = "epoch-cache-sequential";
    const { bucket, callsFor } = makeFakeR2({
      "latest.json": { epoch },
      [`snapshots/${epoch}/samples_meta.json`]: chaserMeta(true, epoch),
      [`snapshots/${epoch}/samples.bin`]: chaserSamplesBin(),
    });
    const env = makeEnv(bucket);

    const res1 = await fetchChaser(env);
    const res2 = await fetchChaser(env);

    expect(res1.status).toBe(200);
    expect(res2.status).toBe(200);
    expect(callsFor(`snapshots/${epoch}/samples.bin`)).toBe(1);
    expect(callsFor(`snapshots/${epoch}/samples_meta.json`)).toBe(1);
  });

  it("shares one in-flight load across concurrent requests for the same epoch", async () => {
    const epoch = "epoch-cache-concurrent";
    const { bucket, callsFor } = makeFakeR2({
      "latest.json": { epoch },
      [`snapshots/${epoch}/samples_meta.json`]: chaserMeta(true, epoch),
      [`snapshots/${epoch}/samples.bin`]: chaserSamplesBin(),
    });
    const env = makeEnv(bucket);

    const [res1, res2] = await Promise.all([fetchChaser(env), fetchChaser(env)]);

    expect(res1.status).toBe(200);
    expect(res2.status).toBe(200);
    expect(callsFor(`snapshots/${epoch}/samples.bin`)).toBe(1);
  });

  it("does not cache a failed load -- a later request for the same epoch retries R2", async () => {
    const epoch = "epoch-cache-retry";
    const { bucket, files } = makeFakeR2({
      "latest.json": { epoch },
      // samples_meta.json intentionally missing for the first request.
      [`snapshots/${epoch}/samples.bin`]: chaserSamplesBin(),
    });
    const env = makeEnv(bucket);

    const res1 = await fetchChaser(env);
    expect(res1.status).toBe(404);

    files.set(`snapshots/${epoch}/samples_meta.json`, chaserMeta(true, epoch));
    const res2 = await fetchChaser(env);
    expect(res2.status).toBe(200);
  });
});
