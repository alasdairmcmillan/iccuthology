import { useState } from "react";
import type { Screen } from "../App";

export interface SearchNight {
  label: string; // short date, e.g. "Jul 10"
  pct: string;
}
export interface SearchResult {
  slug: string;
  song: string;
  nights: SearchNight[];
}

interface HeaderProps {
  screen: Screen;
  onSelectScreen: (s: Screen) => void;
  search: (query: string) => SearchResult[];
}

const TABS: { id: Screen; label: string }[] = [
  { id: "tours", label: "Tours" },
  { id: "shows", label: "Shows" },
  { id: "about", label: "About" },
];

export default function Header({ screen, onSelectScreen, search }: HeaderProps) {
  const [query, setQuery] = useState("");
  const hasSearch = query.trim().length > 0;
  const results = hasSearch ? search(query) : [];

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
      <div className="search-wrap">
        <input
          className="search-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search for the song you're chasing..."
        />
        {hasSearch && (
          <div className="search-results">
            {results.map((r) => (
              <div className="search-result" key={r.slug}>
                <div className="search-song">{r.song}</div>
                <div className="night-chips">
                  {r.nights.map((n, i) => (
                    <span className="night-chip mono" key={i}>
                      {n.label} <span className="pct">{n.pct}</span>
                    </span>
                  ))}
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
