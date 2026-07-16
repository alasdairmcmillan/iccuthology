import { useEffect, useMemo, useRef, useState } from "react";
import type { Screen } from "../App";
import type { SearchResult } from "../search";
import { fetchCatalog } from "../api";
import { buildSongIndex, type SongIndex } from "../songStats";
import type { Catalog, CatalogSong } from "../types";
import SongCard from "./SongCard";

export type { SearchNight, SearchResult } from "../search";

interface HeaderProps {
  screen: Screen;
  onSelectScreen: (s: Screen) => void;
  search: (query: string) => SearchResult[];
  onGotoSong: (slug: string) => void;
}

const TABS: { id: Screen; label: string }[] = [
  { id: "tours", label: "Tours" },
  { id: "shows", label: "Shows" },
  { id: "songs", label: "Songs" },
  { id: "personal", label: "Personal" },
  { id: "about", label: "About" },
];

export default function Header({ screen, onSelectScreen, search, onGotoSong }: HeaderProps) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const hasSearch = open && query.trim().length > 0;
  const results = hasSearch ? search(query) : [];

  const [catalog, setCatalog] = useState<Catalog | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetchCatalog()
      .then((c) => {
        if (!cancelled) setCatalog(c);
      })
      .catch(() => {
        /* dropdown stats just stay hidden when the catalog can't load */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const songIndex: SongIndex | null = useMemo(
    () => (catalog ? buildSongIndex(catalog) : null),
    [catalog],
  );
  const songById = useMemo(() => {
    const m = new Map<string, CatalogSong>();
    for (const s of catalog?.songs ?? []) m.set(s.slug, s);
    return m;
  }, [catalog]);

  // Dismiss the results panel on outside click or Escape (input keeps its text).
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const selectSong = (slug: string) => {
    setOpen(false);
    setQuery("");
    onGotoSong(slug);
  };

  return (
    <header className="header">
      <span className="wordmark">THE ICCUTHOLOGIST</span>
      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={"tab" + (screen === t.id ? " active" : "")}
            onClick={() => onSelectScreen(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <div className="header-spacer" />
      <div className="search-wrap" ref={wrapRef}>
        <input
          className="search-input"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          placeholder="Search for the song you're chasing..."
        />
        {hasSearch && (
          <div className="search-results">
            {results.map((r) => {
              const catalogSong = songById.get(r.slug);
              return catalog && songIndex && catalogSong ? (
                <SongCard
                  key={r.slug}
                  catalog={catalog}
                  songIndex={songIndex}
                  song={catalogSong}
                  onSelect={selectSong}
                />
              ) : (
                <button
                  className="song-card"
                  key={r.slug}
                  onClick={() => selectSong(r.slug)}
                >
                  <div className="song-card-name">{r.song}</div>
                </button>
              );
            })}
            {results.length === 0 && (
              <div className="search-empty">no candidate songs match</div>
            )}
          </div>
        )}
      </div>
    </header>
  );
}
