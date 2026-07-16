import { useMemo, useState } from "react";
import { fetchCatalog, fetchSamples, fetchSeedfile, USE_FIXTURES } from "../api";
import { personalReduction } from "../lib/samples";
import type { Schedule } from "../types";
import { dateLabelDay } from "../lib/format";
import { songPageSize } from "../lib/paging";
import Pager from "./Pager";

// Mirrors the `phishpred personal` CLI defaults: drop obscure songs, rank the
// remainder by career play count (the "surprise" axis, per catalog.json order).
const MIN_PLAYS = 20;
const TOP = 100;

// Remember the last phish.net username that produced a report, so returning
// visitors don't retype it. localStorage can throw (private browsing, blocked
// storage) — treat it as best-effort.
const USERNAME_KEY = "phishnet-username";
function loadSavedUsername(): string {
  try {
    return window.localStorage.getItem(USERNAME_KEY) ?? "";
  } catch {
    return "";
  }
}
function saveUsername(name: string) {
  try {
    window.localStorage.setItem(USERNAME_KEY, name);
  } catch {
    /* best-effort */
  }
}

interface PersonalRow {
  songid: number;
  song: string;
  slug: string;
  plays: number;
  /** Plays on or after the user's first attended show (from catalog.by_show)
   *  — the IHOZ-style "you had your chances" axis. */
  playsSince: number;
  last: string | null;
  pSee: number;
  modalDate: string | null;
  modalProb: number;
}

interface PersonalReport {
  nDatesGiven: number;
  nMatched: number;
  nSeenSongs: number;
  firstShow: string;
  horizonStart: string;
  horizonEnd: string;
  nHorizon: number;
  nSims: number;
  /** All unseen candidates (career plays >= MIN_PLAYS), unranked — the
   *  active toggle sorts and slices to TOP at render. */
  rows: PersonalRow[];
}

/** Rank unseen songs by all-time plays, or by plays since the user's first
 *  show — how many times they've "missed" it while a fan. */
type PlaysMode = "since" | "alltime";

/** Dates from pasted text: phish.net seedfile M/D/YY(YY) lines or ISO dates. */
function parsePastedDates(text: string): string[] {
  const out = new Set<string>();
  for (const m of text.matchAll(/\b(\d{1,2})\/(\d{1,2})\/(\d{2,4})\b/g)) {
    const month = Number(m[1]);
    const day = Number(m[2]);
    let year = Number(m[3]);
    if (year <= 99) year = year < 70 ? 2000 + year : 1900 + year;
    out.add(
      `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`,
    );
  }
  for (const m of text.matchAll(/\b\d{4}-\d{2}-\d{2}\b/g)) out.add(m[0]);
  return [...out].sort();
}

interface PersonalScreenProps {
  schedule: Schedule;
}

export default function PersonalScreen({ schedule }: PersonalScreenProps) {
  const [username, setUsername] = useState(loadSavedUsername);
  const [pasted, setPasted] = useState("");
  const [pasteOpen, setPasteOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<PersonalReport | null>(null);
  const [playsMode, setPlaysMode] = useState<PlaysMode>("since");
  const [page, setPage] = useState(0);
  const [pageRows] = useState(songPageSize);

  const venueByDate = useMemo(() => {
    const map: Record<string, string> = {};
    for (const s of schedule.shows) {
      map[s.showdate] = `${s.venue_name} · ${s.city}, ${s.state}`;
    }
    return map;
  }, [schedule]);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const usingUsername = !(pasteOpen && pasted.trim());
      const dates = usingUsername
        ? (await fetchSeedfile(username.trim())).dates
        : parsePastedDates(pasted);
      if (dates.length === 0) {
        throw new Error("no showdates found — check the username or pasted dates");
      }
      // Only persist a username that actually resolved to showdates.
      if (usingUsername) saveUsername(username.trim());

      const [catalog, samples] = await Promise.all([fetchCatalog(), fetchSamples()]);

      // Seen-set from catalog.by_show; dates with no published history
      // (typos, future shows, non-Phish nights) simply don't match.
      const matched = dates.filter((d) => catalog.by_show[d] !== undefined);
      if (matched.length === 0) {
        throw new Error("none of those dates match a Phish show in the published history");
      }
      const seen = new Set<number>();
      for (const d of matched) {
        for (const sid of catalog.by_show[d]) seen.add(sid);
      }
      const firstShow = matched.reduce((a, b) => (b < a ? b : a));

      // Per-song plays since the first attended show, tallied from the
      // published show history (by_show covers every past show).
      const sinceCounts = new Map<number, number>();
      for (const [d, sids] of Object.entries(catalog.by_show)) {
        if (d < firstShow) continue;
        for (const sid of sids) sinceCounts.set(sid, (sinceCounts.get(sid) ?? 0) + 1);
      }

      // Reduce the published simulation once, then join vocab index -> songid.
      const horizonDates = samples.meta.horizon_showdates;
      const oddsByVocab = personalReduction(samples.decoded.samples, horizonDates);
      const oddsBySongid = new Map<number, { pSee: number; modalDate: string | null; modalProb: number }>();
      for (const v of samples.meta.vocab) {
        const odds = oddsByVocab.get(v.i);
        if (odds) oddsBySongid.set(v.songid, odds);
      }

      // Collect EVERY candidate (not just the first TOP): the two ranking
      // modes order them differently, so the slice happens at render.
      const rows: PersonalRow[] = [];
      for (const s of catalog.songs) {
        if (seen.has(s.songid) || s.plays < MIN_PLAYS) continue;
        const odds = oddsBySongid.get(s.songid);
        rows.push({
          songid: s.songid,
          song: s.name,
          slug: s.slug,
          plays: s.plays,
          playsSince: sinceCounts.get(s.songid) ?? 0,
          last: s.last,
          pSee: odds?.pSee ?? 0,
          modalDate: odds?.modalDate ?? null,
          modalProb: odds?.modalProb ?? 0,
        });
      }

      setPage(0);
      setReport({
        nDatesGiven: dates.length,
        nMatched: matched.length,
        nSeenSongs: seen.size,
        firstShow,
        horizonStart: horizonDates[0] ?? "?",
        horizonEnd: horizonDates[horizonDates.length - 1] ?? "?",
        nHorizon: horizonDates.length,
        nSims: samples.meta.n_sims,
        rows,
      });
    } catch (err) {
      setReport(null);
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const canLoad = !loading && ((pasteOpen && pasted.trim().length > 0) || username.trim().length > 0);

  // Candidates ranked per the active toggle. catalog.songs order IS the
  // all-time ranking (plays desc), so only "since" needs a re-sort; career
  // plays break ties so stable-but-rare songs don't shuffle randomly.
  const rankedRows = useMemo(() => {
    if (!report) return [];
    const rows =
      playsMode === "since"
        ? [...report.rows].sort((a, b) => b.playsSince - a.playsSince || b.plays - a.plays)
        : report.rows;
    return rows.slice(0, TOP);
  }, [report, playsMode]);

  return (
    <div className="personal-layout">
      <div className="card">
        <div className="card-title">Due to see</div>
        <div className="card-sub" style={{ marginTop: 4 }}>
          The most common songs you've never caught live, and your odds of finally hearing
          each one across the upcoming shows.
        </div>

        {USE_FIXTURES ? (
          <div className="center-msg">
            The personal view needs the live API — the offline preview doesn't bundle the
            song catalog or simulation samples.
          </div>
        ) : (
          <>
            <div className="personal-controls">
              <span className="control-label">phish.net username:</span>
              <input
                className="text-input"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && canLoad) void load();
                }}
                placeholder="your phish.net username"
                disabled={pasteOpen}
              />
              <button className="btn" onClick={() => void load()} disabled={!canLoad}>
                {loading ? "Loading…" : "Look ahead"}
              </button>
              <button className="btn-link" onClick={() => setPasteOpen((v) => !v)}>
                {pasteOpen ? "use a username instead" : "no account? paste your dates"}
              </button>
            </div>
            {pasteOpen && (
              <textarea
                className="text-input personal-paste"
                value={pasted}
                onChange={(e) => setPasted(e.target.value)}
                placeholder={"One show per line — seedfile format (7/10/26) or 2026-07-10"}
                rows={5}
              />
            )}
            {error && <div className="note">⚠ {error}</div>}
          </>
        )}
      </div>

      {report && (
        <div className="card">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 10,
              flexWrap: "wrap",
            }}
          >
            <div className="card-title">Your lookahead</div>
            {/* IHOZ-style ranking axis: how often it's played all-time, or how
                often it's played SINCE your first show — i.e. how many chances
                you've already blown. Defaults to since-first-show. */}
            <div className="mode-toggle mode-toggle-sm" role="tablist" aria-label="Plays ranking">
              <button
                className={"mode-option" + (playsMode === "since" ? " active" : "")}
                role="tab"
                aria-selected={playsMode === "since"}
                onClick={() => {
                  setPlaysMode("since");
                  setPage(0);
                }}
              >
                Since first show
              </button>
              <button
                className={"mode-option" + (playsMode === "alltime" ? " active" : "")}
                role="tab"
                aria-selected={playsMode === "alltime"}
                onClick={() => {
                  setPlaysMode("alltime");
                  setPage(0);
                }}
              >
                All-time
              </button>
            </div>
          </div>
          <div className="card-sub mono" style={{ marginTop: 4 }}>
            {report.nMatched} of {report.nDatesGiven} shows matched the published history ·{" "}
            first show {report.firstShow} · {report.nSeenSongs} distinct songs seen · horizon{" "}
            {report.horizonStart} … {report.horizonEnd} ({report.nHorizon} shows,{" "}
            {report.nSims} sims)
          </div>
          <div className="personal-grid personal-grid-head">
            <span>Song</span>
            <span style={{ textAlign: "right" }}>Last played</span>
            <span style={{ textAlign: "right" }}>P(finally see it)</span>
            <span style={{ textAlign: "right" }}>Most likely show</span>
          </div>
          {rankedRows.slice(page * pageRows, (page + 1) * pageRows).map((r) => (
            <div className="personal-grid personal-grid-row" key={r.songid}>
              <span className="r-song">
                {r.song}{" "}
                <span
                  className="personal-plays-inline"
                  title={`${r.plays} plays all-time · ${r.playsSince} since your first show`}
                >
                  ({playsMode === "since" ? r.playsSince : r.plays})
                </span>
              </span>
              <span className="mono personal-dim">{r.last ?? "-"}</span>
              <span className="run-p">{(r.pSee * 100).toFixed(1)}%</span>
              <span
                className="run-night"
                title={r.modalDate ? venueByDate[r.modalDate] : undefined}
              >
                {r.modalDate
                  ? `${dateLabelDay(r.modalDate)} (${Math.round(r.modalProb * 100)}%)`
                  : "-"}
              </span>
            </div>
          ))}
          <Pager
            page={page}
            totalRows={rankedRows.length}
            pageSize={pageRows}
            onPage={setPage}
          />
          {rankedRows.length === 0 && (
            <div className="center-msg">
              Nothing left to chase — you've seen every commonly played song. Go see a
              Fishman-on-vacuum encore for the rest of us.
            </div>
          )}
          <div className="note">
            P(finally see it) = odds the song is played at least once across the whole
            horizon; most likely show = the night most often FIRST to play it in the
            published simulation. Seen-songs are derived from your attended dates and the
            published show history — dates that don't match a Phish show are ignored. The
            count after each song is its play total on the selected axis: shows since
            your first show, or all-time.
          </div>
        </div>
      )}
    </div>
  );
}
