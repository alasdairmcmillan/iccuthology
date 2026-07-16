import { useEffect, useState } from "react";
import type { Meta, Schedule, TourReport } from "../types";
import { fetchTour, fetchTourById } from "../api";
import { ACCENT, bucketColor } from "../theme";
import { monthLabel, pct1 } from "../lib/format";
import { songPageSize } from "../lib/paging";
import Pager from "./Pager";
import StatPopover from "./StatPopover";
import ModelStandingsPanel from "./ModelStandingsPanel";

interface ToursScreenProps {
  meta: Meta;
  schedule: Schedule;
  tour: TourReport;
  onGotoShow: (date: string) => void;
  onOpenScorecards: () => void;
}

const SHORT_MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
function shortDate(showdate: string): string {
  const [, m, d] = showdate.split("-").map(Number);
  return `${SHORT_MONTHS[m - 1]} ${d}`;
}

// Canonical display order for dist buckets. Current epochs publish
// 0/1/2/3/4+; epochs published before the 4+ split have 0/1/2/3+, so we
// render whichever keys the artifact actually carries.
const DIST_BUCKET_ORDER = ["0", "1", "2", "3", "3+", "4+"];
function distBuckets(dist: Record<string, number>): string[] {
  return DIST_BUCKET_ORDER.filter((k) => k in dist);
}

// A song is played a whole number of times; the fractional "expected_plays" is a
// mean. Report the most likely integer count and our confidence in it, straight
// from the play-count distribution the simulator already produces.
function mostLikelyPlays(dist: Record<string, number>): { label: string; prob: number } {
  const entries = distBuckets(dist).map((k) => ({ label: k, prob: dist[k] }));
  return entries.reduce((best, e) => (e.prob > best.prob ? e : best), entries[0]);
}

// "0" -> "0 plays", "1" -> "1 play", "3+"/"4+" -> "3+ plays"/"4+ plays".
function distLabel(bucket: string): string {
  if (bucket === "1") return "1 play";
  return `${bucket} plays`;
}

// DESIGN-DECISION: placeholder sub-labels for tours with no cached data. The API
// (meta.tours) only carries id/tour_name/has_data, so these hints are UI copy.
const PLACEHOLDER_SUBLABEL: Record<string, string> = {
  "fall-2026": "dates not yet announced",
  "new-years-2026": "rumored · late Dec",
};

const PAGE_SIZE = 50;

export default function ToursScreen({
  meta,
  schedule,
  tour,
  onGotoShow,
  onOpenScorecards,
}: ToursScreenProps) {
  const [tourId, setTourId] = useState<string>(
    meta.tours.find((t) => t.has_data)?.id ?? "all",
  );
  const [page, setPage] = useState(0);
  const [songPage, setSongPage] = useState(0);
  const [songPageRows] = useState(songPageSize);

  // Per-tour table: each tour/{id}.json is a reduction of the same published
  // simulation over just that tour's nights (the "all" pill uses tour.json).
  // Initialized with the prop so the first paint has data; refetched on select.
  const [tourData, setTourData] = useState<TourReport>(tour);
  useEffect(() => {
    let cancelled = false;
    const has = tourId === "all" || meta.tours.find((t) => t.id === tourId)?.has_data;
    if (!has) return; // no-data tours render the placeholder card, no fetch
    const p = tourId === "all" ? fetchTour() : fetchTourById(tourId);
    p.then((t) => {
      if (!cancelled) setTourData(t);
    });
    return () => {
      cancelled = true;
    };
  }, [tourId, meta.tours]);

  // Build pill options: every meta tour, plus an "all future scheduled dates" pseudo-tour.
  const allCount = schedule.shows.length;
  const options = [
    ...meta.tours.map((t) => {
      const tourShows = schedule.shows.filter((s) => s.tour_id === t.id);
      // `schedule.shows` is future-only, so once a tour is underway it
      // undercounts the tour's total dates — prefer the tracker's
      // n_shows_total (played + remaining) for the currently-loaded tour.
      const totalShows =
        t.id === tourId && tourData.tracker ? tourData.tracker.n_shows_total : tourShows.length;
      const subLabel = t.has_data
        ? `${totalShows} shows · ${shortDate(tourShows[0].showdate)} – ${shortDate(
            tourShows[tourShows.length - 1].showdate,
          )}`
        : PLACEHOLDER_SUBLABEL[t.id] ?? "dates not yet announced";
      return { id: t.id, label: t.tour_name, subLabel, hasData: t.has_data };
    }),
    {
      id: "all",
      label: "All future scheduled dates",
      subLabel: `${allCount} shows on the books`,
      hasData: allCount > 0,
    },
  ];

  const selected = options.find((o) => o.id === tourId) ?? options[0];

  // Per-tour tables now come from /api/tour/{id}; the schedule sidebar filters
  // client-side to the selected tour ("all" shows every future show).
  const scheduleForTour =
    tourId === "all"
      ? schedule.shows
      : schedule.shows.filter((s) => s.tour_id === tourId);

  const totalPages = Math.max(1, Math.ceil(scheduleForTour.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const pageSlice = scheduleForTour.slice(
    safePage * PAGE_SIZE,
    safePage * PAGE_SIZE + PAGE_SIZE,
  );

  // Group the current page's shows by YYYY-MM.
  const months: { key: string; rows: typeof pageSlice }[] = [];
  for (const s of pageSlice) {
    const key = s.showdate.slice(0, 7);
    let g = months.find((m) => m.key === key);
    if (!g) {
      g = { key, rows: [] };
      months.push(g);
    }
    g.rows.push(s);
  }

  return (
    <>
      {/* No model label here (yet): tour tables are heuristic-only until
          per-model tour predictions are wired up — see
          docs/llm-auto-predictor-plan.md "Per-model tour predictions". */}
      <div className="tour-pills">
        {options.map((o) => (
          <button
            key={o.id}
            className={"tour-pill" + (o.id === tourId ? " active" : "")}
            onClick={() => {
              setTourId(o.id);
              setPage(0);
              setSongPage(0);
            }}
          >
            <div className="pill-label">{o.label}</div>
            <div className="pill-sub">{o.subLabel}</div>
          </button>
        ))}
      </div>

      {/* The standings panel lives in the sidebar even for no-data tours, so
          the leaderboard never disappears when browsing unannounced tours —
          only the table/schedule react to the selected pill. */}
      <div className="tours-row">
        {!selected.hasData ? (
          <div className="no-data-card tour-table-card">
            No cached predictions for this tour yet — dates aren't finalized. Check back
            once they're announced.
          </div>
        ) : (
          <div className="card tour-table-card">
            <div className="tour-scroll">
              <div className="tour-grid-head">
                <span>Song</span>
                <span style={{ textAlign: "center" }}>
                  <span className="when-wide">Plays · Conf %</span>
                  <span className="when-narrow">Plays</span>
                </span>
                <span style={{ textAlign: "center" }}>P(≥1)</span>
                <span style={{ textAlign: "center" }}>Bucket</span>
                <span style={{ textAlign: "center" }}>
                  Dist{" "}
                  {tourData.rows.length
                    ? distBuckets(tourData.rows[0].dist).join("/")
                    : "0/1/2/3/4+"}
                </span>
              </div>
              {tourData.rows
                .slice(songPage * songPageRows, (songPage + 1) * songPageRows)
                .map((r) => {
                const bc = bucketColor(r.bucket, ACCENT);
                const dist =
                  distBuckets(r.dist)
                    .map((k) => Math.round(r.dist[k] * 100))
                    .join("/") + "%";
                const ml = mostLikelyPlays(r.dist);
                // Actual plays of this song on the tour to date, from the frozen
                // doc's tracker (per-tour docs only; the all-future "all" pill
                // has none) — surfaced in the bucket-chip popover, hidden if 0.
                const soFar = tourData.tracker?.played_counts[r.slug] ?? 0;
                const soFarDates = tourData.tracker?.played_dates?.[r.slug] ?? [];
                return (
                  <div className="tour-grid-row" key={r.slug}>
                    <span className="r-song">{r.song}</span>
                    <span className="r-num">
                      {ml.label}
                      <span className="when-wide"> · {Math.round(ml.prob * 100)}%</span>
                    </span>
                    <span className="r-p">{pct1(r.p_at_least_one)}</span>
                    <StatPopover
                      trigger={
                        <span
                          className="badge"
                          style={{ background: bc.bg, color: bc.fg, border: `1px solid ${bc.border}` }}
                        >
                          {r.bucket}
                        </span>
                      }
                    >
                      <div className="stat-pop-line">
                        <strong>{pct1(r.p_at_least_one)}</strong> chance of 1+ plays
                      </div>
                      <div className="stat-pop-line">
                        mean <strong>{r.expected_plays.toFixed(2)}</strong> plays · analytic{" "}
                        <strong>{r.analytic_p.toFixed(2)}</strong>
                      </div>
                      <div className="stat-pop-label">Probability distribution</div>
                      <ul className="stat-pop-dist">
                        {distBuckets(r.dist).map((k) => (
                          <li key={k}>
                            <span>{distLabel(k)}</span>
                            <span>{pct1(r.dist[k])}</span>
                          </li>
                        ))}
                      </ul>
                      {soFar > 0 && (
                        <div className="stat-pop-line" style={{ marginTop: 8 }}>
                          Plays so far this tour: <strong>{soFar}</strong> (
                          {soFarDates.map(shortDate).join(", ")})
                        </div>
                      )}
                    </StatPopover>
                    <span className="r-dist">{dist}</span>
                  </div>
                );
                })}
            </div>
            <Pager
              page={songPage}
              totalRows={tourData.rows.length}
              pageSize={songPageRows}
              onPage={setSongPage}
            />
            <div className="cli-caption">
              {tourData.tracker ? (
                <>
                  {tourData.backcast ? "Frozen pre-tour " : "Frozen "}
                  {tourData.model} predictions from {tourData.n_sims.toLocaleString()}{" "}
                  Monte-Carlo simulations · actual plays tracked as of{" "}
                  {tourData.tracker.as_of.slice(0, 10)} ({tourData.tracker.n_shows_played}/
                  {tourData.tracker.n_shows_total} shows played)
                </>
              ) : (
                <>
                  Estimated from {tourData.n_sims.toLocaleString()} Monte-Carlo simulations of{" "}
                  {tourId === "all" ? "all future shows" : selected.label} · {tourData.model} model
                </>
              )}
            </div>
          </div>
        )}

        <div className="tour-side">
          <ModelStandingsPanel onOpenScorecards={onOpenScorecards} />
          {scheduleForTour.length > 0 && (
            <div className="card" style={{ padding: "18px 20px" }}>
              <div style={{ color: "var(--text-primary)", fontSize: 13, fontWeight: 700 }}>
                {selected.label}
              </div>
              <div
                className="label-caps"
                style={{ color: "var(--text-muted)", fontSize: 10, margin: "2px 0 14px" }}
              >
                full schedule — click a highlighted date to view its Shows page
              </div>
              {months.map((m) => (
                <div className="sched-month" key={m.key}>
                  <div className="sched-month-label">{monthLabel(m.key)}</div>
                  {m.rows.map((s) => (
                    <button
                      key={s.showdate}
                      className={"sched-row " + (s.has_data ? "has-data" : "no-data")}
                      onClick={s.has_data ? () => onGotoShow(s.showdate) : undefined}
                      disabled={!s.has_data}
                    >
                      <span className="dash">—</span>
                      <span className="sched-name">
                        {s.showdate} {s.venue_name} — {s.city}, {s.state}
                      </span>
                    </button>
                  ))}
                </div>
              ))}
              <div className="sched-pager">
                <span className="mono" style={{ color: "var(--text-muted)", fontSize: 11 }}>
                  Page {safePage + 1} of {totalPages}
                </span>
                <div style={{ display: "flex", gap: 6 }}>
                  <button
                    className="pager-btn"
                    disabled={safePage === 0}
                    onClick={() => setPage(Math.max(0, safePage - 1))}
                  >
                    ‹ Prev
                  </button>
                  <button
                    className="pager-btn"
                    disabled={safePage === totalPages - 1}
                    onClick={() => setPage(Math.min(totalPages - 1, safePage + 1))}
                  >
                    Next ›
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
