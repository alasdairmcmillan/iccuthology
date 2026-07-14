import { useEffect, useMemo, useState } from "react";
import {
  fetchLatest,
  fetchSchedule,
  fetchSetlist,
  fetchShow,
  fetchTour,
  usingFixtures,
} from "./api";
import type {
  Meta,
  Schedule,
  SetlistPrediction,
  ShowReport,
  TourReport,
} from "./types";
import Header, { type SearchResult } from "./components/Header";
import ToursScreen from "./components/ToursScreen";
import ShowsScreen from "./components/ShowsScreen";
import PersonalScreen from "./components/PersonalScreen";
import AboutScreen from "./components/AboutScreen";
import { dateLabelDay, pct } from "./lib/format";

export type Screen = "tours" | "shows" | "personal" | "about";

function todayIso(): string {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

/** Next upcoming show with cached data (today ≈ Jul 9 2026 → 2026-07-10). */
function defaultNight(schedule: Schedule): string | null {
  const today = todayIso();
  const dataShows = schedule.shows.filter((s) => s.has_data);
  const upcoming = dataShows.find((s) => s.showdate >= today);
  return (upcoming ?? dataShows[0])?.showdate ?? null;
}

export default function App() {
  const [screen, setScreen] = useState<Screen>("tours");
  const [meta, setMeta] = useState<Meta | null>(null);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [tour, setTour] = useState<TourReport | null>(null);
  const [showsByDate, setShowsByDate] = useState<Record<string, ShowReport>>({});
  const [setlistsByDate, setSetlistsByDate] = useState<
    Record<string, SetlistPrediction | null>
  >({});
  const [selectedShows, setSelectedShows] = useState<string[]>([]);
  const [offline, setOffline] = useState(false);
  // Which mode ShowsScreen should mount in — flipped to "past" by the Tours
  // page's standings panel, reset to "upcoming" by any specific-show jump so
  // a later scorecards visit doesn't leak into an unrelated show lookup.
  const [showsInitialMode, setShowsInitialMode] = useState<"upcoming" | "past">("upcoming");

  // Initial load: meta, schedule, tour, then every data-having show + its setlist.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [m, sch, tr] = await Promise.all([
        fetchLatest(),
        fetchSchedule(),
        fetchTour(),
      ]);
      if (cancelled) return;
      setMeta(m);
      setSchedule(sch);
      setTour(tr);
      setSelectedShows((cur) => (cur.length ? cur : [defaultNight(sch)].filter(Boolean) as string[]));

      const dataDates = sch.shows.filter((s) => s.has_data).map((s) => s.showdate);
      const [shows, setlists] = await Promise.all([
        Promise.all(dataDates.map((d) => fetchShow(d).catch(() => null))),
        Promise.all(dataDates.map((d) => fetchSetlist(d).catch(() => null))),
      ]);
      if (cancelled) return;
      const showMap: Record<string, ShowReport> = {};
      const slMap: Record<string, SetlistPrediction | null> = {};
      dataDates.forEach((d, i) => {
        if (shows[i]) showMap[d] = shows[i]!;
        slMap[d] = setlists[i];
      });
      setShowsByDate(showMap);
      setSetlistsByDate(slMap);
      setOffline(usingFixtures);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Live song search across all loaded (data-having) shows, reading each show's
  // headline source (falling back to whichever source key is present).
  const search = useMemo(() => {
    return (query: string): SearchResult[] => {
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
      // Fall back to tour-level P(≥1) for songs absent from every loaded
      // show's (truncated) per-show rows — e.g. low per-night-probability
      // songs that still have a meaningful chance across the whole tour.
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
      return Array.from(seen.values()).slice(0, 6);
    };
  }, [showsByDate, meta, tour]);

  const gotoShow = (date: string) => {
    setSelectedShows([date]);
    setShowsInitialMode("upcoming");
    setScreen("shows");
  };

  const gotoPastScorecards = () => {
    setShowsInitialMode("past");
    setScreen("shows");
  };

  const loaded = meta && schedule && tour;

  return (
    <>
      <Header screen={screen} onSelectScreen={setScreen} search={search} onGotoShow={gotoShow} />
      <main className="page">
        {!loaded && <div className="loading">Loading predictions…</div>}
        {loaded && screen === "tours" && (
          <ToursScreen
            meta={meta}
            schedule={schedule}
            tour={tour}
            onGotoShow={gotoShow}
            onOpenScorecards={gotoPastScorecards}
          />
        )}
        {loaded && screen === "shows" && (
          <ShowsScreen
            meta={meta}
            schedule={schedule}
            showsByDate={showsByDate}
            setlistsByDate={setlistsByDate}
            selectedShows={selectedShows}
            onChangeSelected={setSelectedShows}
            initialMode={showsInitialMode}
          />
        )}
        {loaded && screen === "personal" && <PersonalScreen schedule={schedule} />}
        {loaded && screen === "about" && <AboutScreen />}
        {loaded && offline && (
          <div className="fixtures-banner" style={{ marginTop: 32 }}>
            offline preview — showing bundled sample data (no API connected)
          </div>
        )}
      </main>
    </>
  );
}
