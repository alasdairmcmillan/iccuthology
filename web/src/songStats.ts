// Pure client-side derivations over catalog.json (DEPLOY-CONTRACTS.md §2a) for
// the Songs page. No network calls here -- everything is computed from data
// the app already has once `fetchCatalog()` resolves.
import type { Catalog } from "./types";

// Matches the heuristic model's own decay/gap-ratio window (half-life 50
// shows, plays_last_150 signal -- see src/phishpred/models/heuristic.py and
// features.py). "Recent" plays/year uses the same horizon the model itself
// scores songs against, rather than an arbitrary calendar window.
const RECENT_SHOWS_WINDOW = 150;
const RECENT_TOURS_WINDOW = 10;
const MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000;

export interface SongIndex {
  /** every past showdate in catalog.by_show, ascending */
  allDates: string[];
  /** songid -> showdates that song was played on, ascending */
  playedDates: Map<number, string[]>;
}

export function buildSongIndex(catalog: Catalog): SongIndex {
  const allDates = Object.keys(catalog.by_show).sort();
  const playedDates = new Map<number, string[]>();
  for (const date of allDates) {
    for (const songid of catalog.by_show[date]) {
      const arr = playedDates.get(songid);
      if (arr) arr.push(date);
      else playedDates.set(songid, [date]);
    }
  }
  return { allDates, playedDates };
}

// Clamped away from 0 so a same-day or reversed range never divides by zero.
function yearsBetween(startIso: string, endIso: string): number {
  const span = Date.parse(endIso) - Date.parse(startIso);
  return Math.max(span / MS_PER_YEAR, 1 / 365.25);
}

export interface TourFrequency {
  playedIn: number;
  ofLastTours: number;
}

export interface SongStats {
  eligibleShows: number;
  pctShowsPlayed: number | null;
  playsThisYear: number;
  playsPerYearAllTime: number | null;
  playsPerYearRecent: number | null;
  tourFrequency: TourFrequency | null;
}

export function computeSongStats(
  catalog: Catalog,
  index: SongIndex,
  songid: number,
  debutDate: string | null | undefined,
  now: Date = new Date(),
): SongStats {
  const { allDates } = index;
  const played = index.playedDates.get(songid) ?? [];

  // Denominator is shows since debut (not all-time show count) so a
  // late-debuting song isn't penalized for shows it couldn't have played.
  const eligibleDates = debutDate ? allDates.filter((d) => d >= debutDate) : allDates;
  const pctShowsPlayed = eligibleDates.length ? played.length / eligibleDates.length : null;

  const currentYear = String(now.getUTCFullYear());
  const playsThisYear = played.filter((d) => d.slice(0, 4) === currentYear).length;

  const firstDate = debutDate ?? played[0] ?? allDates[0];
  const lastAllDate = allDates[allDates.length - 1];
  const playsPerYearAllTime =
    firstDate && lastAllDate ? played.length / yearsBetween(firstDate, lastAllDate) : null;

  const recentWindow = allDates.slice(-RECENT_SHOWS_WINDOW);
  let playsPerYearRecent: number | null = null;
  if (recentWindow.length > 1) {
    const recentSet = new Set(recentWindow);
    const playsInWindow = played.filter((d) => recentSet.has(d)).length;
    playsPerYearRecent =
      playsInWindow / yearsBetween(recentWindow[0], recentWindow[recentWindow.length - 1]);
  }

  // catalog.tours is already ordered by first chronological appearance
  // (publish.py builds it while iterating shows ascending by showdate), so
  // the tail of the array is the M most recent tours. `tours`/`show_tours`
  // are absent on snapshots published before this field existed — tolerate
  // that rather than crash (DEPLOY-CONTRACTS.md "add, don't break" rule).
  const recentTourIds = (catalog.tours ?? []).slice(-RECENT_TOURS_WINDOW).map((t) => t.id);
  let tourFrequency: TourFrequency | null = null;
  if (recentTourIds.length) {
    const showTours = catalog.show_tours ?? {};
    const playedTourIds = new Set(
      played.map((d) => showTours[d]).filter((id): id is string => Boolean(id)),
    );
    tourFrequency = {
      playedIn: recentTourIds.filter((id) => playedTourIds.has(id)).length,
      ofLastTours: recentTourIds.length,
    };
  }

  return {
    eligibleShows: eligibleDates.length,
    pctShowsPlayed,
    playsThisYear,
    playsPerYearAllTime,
    playsPerYearRecent,
    tourFrequency,
  };
}
