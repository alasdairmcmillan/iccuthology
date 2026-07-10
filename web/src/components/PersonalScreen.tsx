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

interface PersonalRow {
  songid: number;
  song: string;
  slug: string;
  plays: number;
  last: string | null;
  pSee: number;
  modalDate: string | null;
  modalProb: number;
}

interface PersonalReport {
  nDatesGiven: number;
  nMatched: number;
  nSeenSongs: number;
  horizonStart: string;
  horizonEnd: string;
  nHorizon: number;
  nSims: number;
  rows: PersonalRow[];
}

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
  const [username, setUsername] = useState("");
  const [pasted, setPasted] = useState("");
  const [pasteOpen, setPasteOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<PersonalReport | null>(null);
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
      const dates = pasteOpen && pasted.trim()
        ? parsePastedDates(pasted)
        : (await fetchSeedfile(username.trim())).dates;
      if (dates.length === 0) {
        throw new Error("no showdates found — check the username or pasted dates");
      }

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

      // Reduce the published simulation once, then join vocab index -> songid.
      const horizonDates = samples.meta.horizon_showdates;
      const oddsByVocab = personalReduction(samples.decoded.samples, horizonDates);
      const oddsBySongid = new Map<number, { pSee: number; modalDate: string | null; modalProb: number }>();
      for (const v of samples.meta.vocab) {
        const odds = oddsByVocab.get(v.i);
        if (odds) oddsBySongid.set(v.songid, odds);
      }

      const rows: PersonalRow[] = [];
      for (const s of catalog.songs) {
        if (seen.has(s.songid) || s.plays < MIN_PLAYS) continue;
        const odds = oddsBySongid.get(s.songid);
        rows.push({
          songid: s.songid,
          song: s.name,
          slug: s.slug,
          plays: s.plays,
          last: s.last,
          pSee: odds?.pSee ?? 0,
          modalDate: odds?.modalDate ?? null,
          modalProb: odds?.modalProb ?? 0,
        });
        if (rows.length >= TOP) break;
      }

      setPage(0);
      setReport({
        nDatesGiven: dates.length,
        nMatched: matched.length,
        nSeenSongs: seen.size,
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
          <div className="card-title">Your lookahead</div>
          <div className="card-sub mono" style={{ marginTop: 4 }}>
            {report.nMatched} of {report.nDatesGiven} shows matched the published history ·{" "}
            {report.nSeenSongs} distinct songs seen · horizon {report.horizonStart} …{" "}
            {report.horizonEnd} ({report.nHorizon} shows, {report.nSims} sims)
          </div>
          <div className="personal-grid personal-grid-head">
            <span>Song</span>
            <span style={{ textAlign: "right" }}>Last played</span>
            <span style={{ textAlign: "right" }}>P(finally see it)</span>
            <span style={{ textAlign: "right" }}>Most likely show</span>
          </div>
          {report.rows.slice(page * pageRows, (page + 1) * pageRows).map((r) => (
            <div className="personal-grid personal-grid-row" key={r.songid}>
              <span className="r-song">
                {r.song} <span className="personal-plays-inline">({r.plays})</span>
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
            totalRows={report.rows.length}
            pageSize={pageRows}
            onPage={setPage}
          />
          {report.rows.length === 0 && (
            <div className="center-msg">
              Nothing left to chase — you've seen every commonly played song. Go see a
              Fishman-on-vacuum encore for the rest of us.
            </div>
          )}
          <div className="note">
            P(finally see it) = odds the song is played at least once across the whole
            horizon; most likely show = the night most often FIRST to play it in the
            published simulation. Seen-songs are derived from your attended dates and the
            published show history — dates that don't match a Phish show are ignored.
          </div>
        </div>
      )}
    </div>
  );
}
