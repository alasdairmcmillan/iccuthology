/**
 * Browser-side samples.bin decoder + personal "due to see" reduction.
 *
 * The decoder is a copy of worker/src/samples.ts (the canonical TS port of
 * DEPLOY-CONTRACTS.md §3 — Python writer, JS reader; keep byte-identical in
 * interpretation). The reduction mirrors src/phishpred/personal.py
 * `_horizon_reductions`: per song, P(>=1 across the horizon) plus the modal
 * FIRST show to play it. Everything here is keyed by vocab index; callers
 * map to songids via samples_meta.json's vocab.
 */

const MAGIC = "PSMP";
const VERSION = 1;
const HEADER_BYTES = 17;

/** Decode a single uvarint (unsigned LEB128). Addition + power-of-two scaling
 * (not bit-shifting) so values beyond 32 bits stay correct. */
function uvarintDecode(buf: Uint8Array, offset: number): { value: number; next: number } {
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

export interface PersonalSongOdds {
  /** P(the song is played at least once across the whole horizon). */
  pSee: number;
  /** Horizon showdate most likely to be the FIRST to play it. */
  modalDate: string | null;
  /** P(that show is the first to play it). */
  modalProb: number;
}

/**
 * Per-vocab-index horizon reduction for the personal view. One pass over the
 * sims; songs never sampled anywhere simply have no entry (P = 0).
 * Ties in the modal first-hit show resolve to the earliest show, matching
 * personal.py's `max(range(ndates), key=...)`.
 */
export function personalReduction(
  samples: number[][][],
  horizonDates: string[],
): Map<number, PersonalSongOdds> {
  const n = samples.length;
  const nDates = horizonDates.length;
  const union = new Map<number, number>();
  const firstHit = new Map<number, number[]>();

  for (const sim of samples) {
    const hitAt = new Map<number, number>();
    for (let t = 0; t < nDates; t++) {
      for (const vi of sim[t] ?? []) {
        if (!hitAt.has(vi)) hitAt.set(vi, t);
      }
    }
    for (const [vi, t] of hitAt) {
      union.set(vi, (union.get(vi) ?? 0) + 1);
      let counts = firstHit.get(vi);
      if (!counts) {
        counts = new Array(nDates).fill(0);
        firstHit.set(vi, counts);
      }
      counts[t] += 1;
    }
  }

  const out = new Map<number, PersonalSongOdds>();
  for (const [vi, counts] of firstHit) {
    let best = 0;
    for (let t = 1; t < nDates; t++) {
      if (counts[t] > counts[best]) best = t;
    }
    out.set(vi, {
      pSee: n ? (union.get(vi) ?? 0) / n : 0,
      modalDate: horizonDates[best] ?? null,
      modalProb: n ? counts[best] / n : 0,
    });
  }
  return out;
}
