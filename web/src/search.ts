// Shared song-search matcher used by both the header's quick-search dropdown
// and the Songs page's full-page search (App.tsx previously inlined this as
// a `useMemo` local to the header search box only).
import type { Meta, ShowReport, TourReport } from "./types";
import { dateLabelDay, pct } from "./lib/format";

export interface SearchNight {
  date: string; // "2026-07-10" — jump target
  label: string; // short date, e.g. "Fri · Jul 10"
  pct: string;
}
export interface SearchResult {
  slug: string;
  song: string;
  nights: SearchNight[];
  /** Set when the song wasn't found in any loaded show's per-show rows but
   *  has a meaningful tour-wide P(at least once) — see searchSongs(). */
  tour?: { pct: string };
}

export interface SearchSource {
  showsByDate: Record<string, ShowReport>;
  meta: Meta | null;
  tour: TourReport | null;
}

/** Substring match (case-insensitive) across every loaded show's headline
 * source, falling back to tour-wide P(≥1) for songs absent from every
 * loaded show's (truncated) per-show rows. Capped at `limit` results. */
export function searchSongs(
  query: string,
  { showsByDate, meta, tour }: SearchSource,
  limit = 6,
): SearchResult[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const seen = new Map<string, SearchResult>();
  const dates = Object.keys(showsByDate).sort();
  for (const date of dates) {
    const sources = showsByDate[date].sources;
    const headlineKey =
      meta && sources[meta.headline_model] ? meta.headline_model : Object.keys(sources)[0];
    for (const r of (headlineKey ? sources[headlineKey]?.rows : undefined) ?? []) {
      if (!r.song.toLowerCase().includes(q)) continue;
      let entry = seen.get(r.slug);
      if (!entry) {
        entry = { slug: r.slug, song: r.song, nights: [] };
        seen.set(r.slug, entry);
      }
      entry.nights.push({
        date,
        label: dateLabelDay(date),
        pct: pct(r.prob),
      });
    }
  }
  for (const r of tour?.rows ?? []) {
    if (seen.has(r.slug)) continue;
    if (!r.song.toLowerCase().includes(q)) continue;
    seen.set(r.slug, {
      slug: r.slug,
      song: r.song,
      nights: [],
      tour: { pct: pct(r.p_at_least_one) },
    });
  }
  return Array.from(seen.values()).slice(0, limit);
}
