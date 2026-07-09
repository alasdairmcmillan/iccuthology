/**
 * samples.bin decoder + shared Monte-Carlo sample reductions.
 *
 * Pure / framework-free TypeScript port of DEPLOY-CONTRACTS.md §3 (binary
 * format, written by the Python publisher) and §4 (reduction math, mirrored
 * from `src/phishpred/modes.py` `run_mode` / `chaser_mode`). No Workers
 * globals, no I/O — safe to unit test directly under Node/vitest, and safe
 * to run in-browser later if the frontend wants client-side reduction.
 *
 * MUST stay byte-identical in interpretation to the Python writer: treat
 * DEPLOY-CONTRACTS.md §3's reference vectors as the conformance suite.
 */

const MAGIC = "PSMP";
const VERSION = 1;
const HEADER_BYTES = 17;

// ---------------------------------------------------------------------------
// uvarint (unsigned LEB128, same as protobuf varints)
// ---------------------------------------------------------------------------

/**
 * Decode a single uvarint starting at `offset` in `buf`.
 * Returns the decoded value and the offset of the next unread byte.
 *
 * Uses addition + power-of-two scaling (not bit-shifting) so it stays
 * correct for values beyond 32 bits -- JS's `<<`/`|` operate on signed
 * 32-bit ints and would silently wrap for large vocab/count values.
 */
export function uvarintDecode(buf: Uint8Array, offset: number): { value: number; next: number } {
  let result = 0;
  let shift = 0;
  let pos = offset;
  for (;;) {
    if (pos >= buf.length) {
      throw new Error(`uvarint decode: truncated buffer at offset ${pos}`);
    }
    const byte = buf[pos];
    pos += 1;
    result += (byte & 0x7f) * 2 ** shift;
    if ((byte & 0x80) === 0) break;
    shift += 7;
    if (shift > 63) throw new Error("uvarint decode: value too large (>63 bits)");
  }
  return { value: result, next: pos };
}

/** Encode a uvarint (mirrors the Python writer; mainly useful for tests). */
export function uvarintEncode(value: number): Uint8Array {
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`uvarint encode: value must be a non-negative integer, got ${value}`);
  }
  const bytes: number[] = [];
  let v = value;
  for (;;) {
    const b = v & 0x7f;
    v = Math.floor(v / 128); // v >>> 7, but safe beyond 32 bits
    if (v !== 0) {
      bytes.push(b | 0x80);
    } else {
      bytes.push(b);
      break;
    }
  }
  return new Uint8Array(bytes);
}

// ---------------------------------------------------------------------------
// samples.bin decode
// ---------------------------------------------------------------------------

export interface DecodedSamples {
  nSims: number;
  nShows: number;
  nVocab: number;
  /** samples[m][t] = ascending-sorted vocab indices sampled in sim m, horizon position t. */
  samples: number[][][];
}

export function decodeSamples(buf: ArrayBuffer | Uint8Array): DecodedSamples {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  if (bytes.length < HEADER_BYTES) {
    throw new Error(`samples.bin: buffer too short for header (${bytes.length} bytes)`);
  }

  const magic = String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]);
  if (magic !== MAGIC) {
    throw new Error(`samples.bin: bad magic ${JSON.stringify(magic)}, expected ${JSON.stringify(MAGIC)}`);
  }
  const version = bytes[4];
  if (version !== VERSION) {
    throw new Error(`samples.bin: unsupported version ${version}, expected ${VERSION}`);
  }

  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const nSims = view.getUint32(5, true);
  const nShows = view.getUint32(9, true);
  const nVocab = view.getUint32(13, true);

  const samples: number[][][] = new Array(nSims);
  let pos = HEADER_BYTES;
  for (let m = 0; m < nSims; m++) {
    const row: number[][] = new Array(nShows);
    for (let t = 0; t < nShows; t++) {
      const countDec = uvarintDecode(bytes, pos);
      pos = countDec.next;
      const count = countDec.value;
      const idx: number[] = new Array(count);
      for (let k = 0; k < count; k++) {
        const dec = uvarintDecode(bytes, pos);
        idx[k] = dec.value;
        pos = dec.next;
      }
      row[t] = idx;
    }
    samples[m] = row;
  }

  return { nSims, nShows, nVocab, samples };
}

// ---------------------------------------------------------------------------
// §4 reductions
// ---------------------------------------------------------------------------

export interface VocabEntry {
  i: number;
  songid: number;
  slug: string;
  name: string;
}

export interface RunSongRow {
  song: string;
  slug: string;
  /** p_union_over_S: P(song appears on >=1 selected night), joint over the run. */
  p_at_least_one: number;
  /** parallel to `selectedShowIndices`, in the order passed in. */
  per_night_probs: number[];
  /** index into `selectedShowIndices` (i.e. 0-based position within the run), or null if empty selection. */
  most_likely_night_index: number | null;
  most_likely_night_date: string | null;
}

/**
 * §4 run reduction: for every vocab index that appears at least once in the
 * selected nights, compute the joint P(>=1) over the union of those nights,
 * the per-night marginal, and the most-likely night. `selectedShowIndices`
 * are horizon positions `t` (0-based, matching `samples[m][t]`), and should
 * be passed in the order the caller wants `per_night_probs` reported in
 * (normally ascending / chronological horizon order).
 *
 * Ties in most-likely-night resolve to the first occurrence (matches
 * Python's `max(range(n), key=...)`, which also returns the first max).
 */
export function runReduction(
  samples: number[][][],
  selectedShowIndices: number[],
  vocab: VocabEntry[],
  horizonDates: string[],
): RunSongRow[] {
  const n = samples.length;
  const nSel = selectedShowIndices.length;

  const nightHits = new Map<number, number[]>(); // vocabIndex -> counts per selected slot
  const unionHits = new Map<number, number>();

  for (const sim of samples) {
    const seen = new Set<number>();
    for (let s = 0; s < nSel; s++) {
      const t = selectedShowIndices[s];
      const stepSet = sim[t] ?? [];
      for (const vi of stepSet) {
        let arr = nightHits.get(vi);
        if (!arr) {
          arr = new Array(nSel).fill(0);
          nightHits.set(vi, arr);
        }
        arr[s] += 1;
        seen.add(vi);
      }
    }
    for (const vi of seen) {
      unionHits.set(vi, (unionHits.get(vi) ?? 0) + 1);
    }
  }

  const vocabByIndex = new Map(vocab.map((v) => [v.i, v] as const));
  const rows: RunSongRow[] = [];
  for (const [vi, hits] of nightHits) {
    const perNight = n ? hits.map((h) => h / n) : hits.map(() => 0);
    const pGe1 = n ? (unionHits.get(vi) ?? 0) / n : 0;

    let bestS: number | null = null;
    if (nSel > 0) {
      bestS = 0;
      for (let s = 1; s < nSel; s++) {
        if (perNight[s] > perNight[bestS]) bestS = s;
      }
    }

    const meta = vocabByIndex.get(vi);
    rows.push({
      song: meta?.name ?? String(vi),
      slug: meta?.slug ?? String(vi),
      p_at_least_one: pGe1,
      per_night_probs: perNight,
      most_likely_night_index: bestS,
      most_likely_night_date: bestS !== null ? (horizonDates[selectedShowIndices[bestS]] ?? null) : null,
    });
  }

  rows.sort((a, b) => b.p_at_least_one - a.p_at_least_one);
  return rows;
}

export interface ChaserDistributionEntry {
  /** vocab-index-independent horizon show id (samples_meta.json `horizon_showids[t]`),
   * or null when the caller doesn't have it (e.g. older snapshots without that field). */
  showid: number | null;
  showdate: string;
  probability: number;
}

export interface ChaserReductionResult {
  p_not_within_horizon: number;
  modal_show_date: string | null;
  median_show_date: string | null;
  expected_shows_until_next_play: number | null;
  distribution: ChaserDistributionEntry[];
}

/**
 * §4 chaser reduction: for one song (vocab index), the distribution of the
 * FIRST horizon show it's played in across simulations, reduced over the
 * *full* horizon (all of `samples[m]`, in order) -- matches
 * `modes.chaser_mode`, which is never restricted to a subset.
 *
 * Median uses the lower-median convention for ties (matches
 * `modes.chaser_mode`: `hits[(len(hits) - 1) // 2]` over ascending-sorted
 * hit positions).
 *
 * `horizonShowids`, when passed (from `samples_meta.json`'s `horizon_showids`,
 * same order as `horizonDates`), is threaded onto each `distribution` entry
 * as `showid` to mirror `modes.ChaserShowProb` -- null per-entry when omitted.
 */
export function chaserReduction(
  samples: number[][][],
  vocabIndexForSlug: number,
  horizonDates: string[],
  horizonShowids?: number[],
): ChaserReductionResult {
  const n = samples.length;
  const nHorizon = horizonDates.length;
  const hitCounts = new Array(nHorizon).fill(0);
  const hitPositions: number[] = [];

  for (const sim of samples) {
    let hitT: number | null = null;
    for (let t = 0; t < nHorizon; t++) {
      const stepSet = sim[t] ?? [];
      if (stepSet.includes(vocabIndexForSlug)) {
        hitT = t;
        break;
      }
    }
    if (hitT !== null) {
      hitCounts[hitT] += 1;
      hitPositions.push(hitT);
    }
  }

  const misses = n - hitPositions.length;
  const pMiss = n ? misses / n : 0;

  const distribution: ChaserDistributionEntry[] = horizonDates.map((d, t) => ({
    showid: horizonShowids?.[t] ?? null,
    showdate: d,
    probability: n ? hitCounts[t] / n : 0,
  }));

  let modalDate: string | null = null;
  let medianDate: string | null = null;
  let expectedShows: number | null = null;

  if (hitPositions.length > 0) {
    let modalT = 0;
    for (let t = 1; t < nHorizon; t++) {
      if (hitCounts[t] > hitCounts[modalT]) modalT = t;
    }
    modalDate = horizonDates[modalT];

    const sortedHits = [...hitPositions].sort((a, b) => a - b);
    const medianT = sortedHits[Math.floor((sortedHits.length - 1) / 2)];
    medianDate = horizonDates[medianT];

    expectedShows = sortedHits.reduce((acc, t) => acc + (t + 1), 0) / sortedHits.length;
  }

  return {
    p_not_within_horizon: pMiss,
    modal_show_date: modalDate,
    median_show_date: medianDate,
    expected_shows_until_next_play: expectedShows,
    distribution,
  };
}
