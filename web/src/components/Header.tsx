import { useEffect, useRef, useState } from "react";
import type { Screen } from "../App";

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
   *  has a meaningful tour-wide P(at least once) — see App.tsx `search`. */
  tour?: { pct: string };
}

interface HeaderProps {
  screen: Screen;
  onSelectScreen: (s: Screen) => void;
  search: (query: string) => SearchResult[];
  onGotoShow: (date: string) => void;
}

const TABS: { id: Screen; label: string }[] = [
  { id: "tours", label: "Tours" },
  { id: "shows", label: "Shows" },
  { id: "personal", label: "Personal" },
  { id: "about", label: "About" },
];

export default function Header({ screen, onSelectScreen, search, onGotoShow }: HeaderProps) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const hasSearch = open && query.trim().length > 0;
  const results = hasSearch ? search(query) : [];

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

  const jump = (date: string) => {
    setOpen(false);
    setQuery("");
    onGotoShow(date);
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
            {results.map((r) => (
              <div className="search-result" key={r.slug}>
                <div className="search-song">{r.song}</div>
                <div className="night-chips">
                  {r.nights.map((n) => (
                    <button
                      className="night-chip mono"
                      key={n.date}
                      title={`Open ${n.label} on the Shows screen`}
                      onClick={() => jump(n.date)}
                    >
                      {n.label} <span className="pct">{n.pct}</span>
                    </button>
                  ))}
                  {r.nights.length === 0 && r.tour && (
                    <button
                      className="tour-chip mono"
                      title="P(at least once) across all scheduled shows — open Tours"
                      onClick={() => {
                        setOpen(false);
                        setQuery("");
                        onSelectScreen("tours");
                      }}
                    >
                      {r.tour.pct} <span className="tour-chip-label">this tour</span>
                    </button>
                  )}
                </div>
              </div>
            ))}
            {results.length === 0 && (
              <div className="search-empty">no candidate songs match</div>
            )}
          </div>
        )}
      </div>
    </header>
  );
}
