import { useEffect, useMemo, useState } from "react";
import { fetchCatalog, USE_FIXTURES } from "../api";
import { searchSongs } from "../search";
import { buildSongIndex, type SongIndex } from "../songStats";
import type { Catalog, CatalogSong, Meta, Schedule, ShowReport, TourReport } from "../types";
import SongCard from "./SongCard";
import SongDetailView from "./SongDetailView";

interface SongsScreenProps {
  meta: Meta;
  schedule: Schedule;
  showsByDate: Record<string, ShowReport>;
  tour: TourReport | null;
  /** Set when arriving here via the header's quick-search "open detail" jump
   *  (App.tsx `gotoSongDetail`) — read once on mount, same pattern as
   *  ShowsScreen's `initialMode` (this screen fully unmounts/remounts on
   *  every screen switch). */
  initialSlug: string | null;
  onGotoShow: (date: string) => void;
}

const DEFAULT_COUNT = 10;
const SEARCH_LIMIT = 24;

export default function SongsScreen({
  meta,
  schedule,
  showsByDate,
  tour,
  initialSlug,
  onGotoShow,
}: SongsScreenProps) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetchCatalog()
      .then((c) => {
        if (!cancelled) setCatalog(c);
      })
      .catch(() => {
        /* handled by the loading/empty states below */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const songIndex: SongIndex | null = useMemo(
    () => (catalog ? buildSongIndex(catalog) : null),
    [catalog],
  );
  const songBySlug = useMemo(() => {
    const m = new Map<string, CatalogSong>();
    for (const s of catalog?.songs ?? []) m.set(s.slug, s);
    return m;
  }, [catalog]);

  const [query, setQuery] = useState("");
  const [selectedSlug, setSelectedSlug] = useState<string | null>(initialSlug);
  // This screen doesn't unmount between header quick-search jumps while
  // already on the Songs tab, so `selectedSlug` can't just read `initialSlug`
  // once on mount — re-sync whenever a fresh jump changes it. A local "back
  // to search" click doesn't change `initialSlug`, so it isn't clobbered here.
  useEffect(() => {
    setSelectedSlug(initialSlug);
  }, [initialSlug]);

  if (USE_FIXTURES) {
    return (
      <div className="personal-layout">
        <div className="screen-title">Songs</div>
        <div className="center-msg">
          The Songs page needs the live API — the offline preview doesn't bundle the song
          catalog or chaser predictions.
        </div>
      </div>
    );
  }

  if (!catalog || !songIndex) {
    return <div className="loading">Loading song catalog…</div>;
  }

  const selectedSong = selectedSlug ? songBySlug.get(selectedSlug) ?? null : null;
  if (selectedSong) {
    return (
      <SongDetailView
        catalog={catalog}
        songIndex={songIndex}
        song={selectedSong}
        meta={meta}
        schedule={schedule}
        showsByDate={showsByDate}
        onBack={() => setSelectedSlug(null)}
        onGotoShow={onGotoShow}
      />
    );
  }

  const trimmed = query.trim();
  const results = trimmed
    ? searchSongs(trimmed, { showsByDate, meta, tour }, SEARCH_LIMIT)
    : catalog.songs
        .slice(0, DEFAULT_COUNT)
        .map((s) => ({ slug: s.slug, song: s.name, nights: [] }));

  return (
    <div className="personal-layout">
      <div className="screen-title">Songs</div>
      <input
        className="text-input songs-search-input"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search for a song…"
        autoFocus
      />
      <div className="songs-grid">
        {results.map((r) => {
          const song = songBySlug.get(r.slug);
          if (!song) return null;
          return (
            <SongCard
              key={r.slug}
              catalog={catalog}
              songIndex={songIndex}
              song={song}
              onSelect={setSelectedSlug}
            />
          );
        })}
        {trimmed && results.length === 0 && (
          <div className="search-empty">no candidate songs match</div>
        )}
      </div>
    </div>
  );
}
