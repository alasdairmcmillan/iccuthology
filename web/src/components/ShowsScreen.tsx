import { useEffect, useMemo, useRef, useState } from "react";
import { fetchScorecard, fetchScoreboard, postRun } from "../api";
import type {
  Meta,
  RunReport,
  Schedule,
  ScorecardCall,
  ScorecardMetrics,
  ScorecardRow,
  ScorecardSetlistMarquee,
  ScorecardSetlistScore,
  Scoreboard,
  ScoreboardModel,
  Scorecard,
  ScorecardSource,
  SetlistPrediction,
  ShowReport,
} from "../types";
import { dateLabel, dateLabelDay, dateLabelShort, pct, pct1 } from "../lib/format";
import { songPageSize } from "../lib/paging";
import Pager from "./Pager";
import StatPopover from "./StatPopover";

interface ShowsScreenProps {
  meta: Meta;
  schedule: Schedule;
  showsByDate: Record<string, ShowReport>;
  setlistsByDate: Record<string, SetlistPrediction | null>;
  selectedShows: string[];
  onChangeSelected: (next: string[]) => void;
  /** Which mode to mount in — e.g. the Tours page's standings panel links
   *  straight to "past" scorecards. Only read once, on mount (this screen
   *  fully unmounts/remounts on every screen switch, so a plain initial
   *  value is enough — no need to react to prop changes after that). */
  initialMode?: "upcoming" | "past";
}

interface ModelOption {
  id: string;
  label: string;
}

// Model picker options are derived from the source keys actually present on the
// loaded shows — never hardcoded. Order: meta.headline_model first, then
// meta.models in their declared order, then any remaining keys (e.g. mcp:*/llm:*
// entries not yet listed in meta.models) alphabetically. Models in meta.models
// render the published structured setlist; any other source key (mcp:*/llm:*
// folded into show/{date}.json) renders its ranked per-song shortlist plus
// rationale. Only a source absent from the night entirely shows the
// "no predictions yet" note.
function deriveModelOptions(
  meta: Meta,
  showsByDate: Record<string, ShowReport>,
): ModelOption[] {
  const kindByKey = new Map<string, string>();
  for (const show of Object.values(showsByDate)) {
    for (const [key, source] of Object.entries(show.sources)) {
      if (!kindByKey.has(key)) kindByKey.set(key, source.kind);
    }
  }
  const ordered: string[] = [];
  const add = (id: string) => {
    if (kindByKey.has(id) && !ordered.includes(id)) ordered.push(id);
  };
  add(meta.headline_model);
  meta.models.forEach(add);
  const extras = [...kindByKey.keys()].filter((id) => !ordered.includes(id)).sort();
  ordered.push(...extras);
  return ordered.map((id) => {
    const kind = kindByKey.get(id);
    return { id, label: kind && kind !== "statistical" ? `${id} (${kind})` : id };
  });
}

function setKeyLabel(key: string): string {
  if (key === "e") return "Encore";
  if (key === "e2") return "Encore 2";
  if (/^\d+$/.test(key)) return `Set ${key}`;
  return key;
}

function orderSetKeys(keys: string[]): string[] {
  return [...keys].sort((a, b) => {
    const an = /^\d+$/.test(a);
    const bn = /^\d+$/.test(b);
    if (an && bn) return Number(a) - Number(b);
    if (an) return -1;
    if (bn) return 1;
    return a.localeCompare(b); // "e" before "e2"
  });
}

// A prior take's version-chip label (§8: "after_showdate" is a UI labeling
// heuristic, not a metric — null means the take predates any run context).
function afterShowdateLabel(after: string | null): string {
  if (!after) return "pre-run";
  return `after ${dateLabelShort(after).split(",")[0]}`;
}

// Prior versions don't carry a precomputed best_call/biggest_whiff (§8 only
// gives {submitted_at, after_showdate, metrics, setlist_score, rows} for
// them) — derive the same "gutsiest hit / biggest whiff" callouts client-side
// so switching version chips keeps the callouts populated.
function deriveCall(rows: ScorecardRow[], wantHit: boolean): ScorecardCall | null {
  const candidates = rows.filter((r) => r.hit === wantHit);
  if (candidates.length === 0) return null;
  const pick = candidates.reduce((best, r) =>
    (wantHit ? r.prob < best.prob : r.prob > best.prob) ? r : best,
  );
  return { song: pick.song, slug: pick.slug, prob: pick.prob };
}

const MARQUEE_LABELS: Record<string, string> = {
  opener: "Called the opener",
  set1_closer: "Called the set 1 closer",
  set2_opener: "Called the set 2 opener",
  set2_closer: "Called the set 2 closer",
  encore: "Called the encore",
};

function marqueeBadgeLabels(marquee: ScorecardSetlistMarquee): string[] {
  return Object.entries(marquee)
    .filter(([, v]) => v === true)
    .map(([key]) => MARQUEE_LABELS[key] ?? `Called the ${key.replace(/_/g, " ")}`);
}

/** 0.15 -> "+15%", -0.05 -> "-5%" — signed whole-percent for the refresh-gain delta. */
function formatSignedPct(x: number): string {
  return (x >= 0 ? "+" : "") + pct(x);
}

// Centralized metric-definition copy, reused by the scores-band metric labels
// and the standings column headers (§8). "top N" renders from metrics.top_n
// via hitRateTip() where a card knows its window; the scoreboard carries no
// per-model top_n so its Hit column tip uses the current 20.
const METRIC_TIPS = {
  recall:
    "Of the songs actually played, the share that appeared anywhere in the model's full shortlist. Longer shortlists (20–40 allowed) make this easier — read it next to the list length.",
  brier:
    "Mean squared error between each shortlist probability and the outcome (1 = played, 0 = not). Rewards calibration: a confident miss costs far more than a hedged one. 0 is perfect; always guessing 50% scores 0.25.",
  vsHeuristic:
    "This model's top-20 hit rate minus the statistical baseline's on the same shows — positive means it beat the baseline.",
  list: "Average shortlist length submitted (20–40 allowed). Context for recall: longer lists cover more.",
  setlistWeighted:
    "Each called song: 1 point for playing at all, +1 for the right set, +1 for the exact slot. 100% = every called song in its exact slot.",
  setlistHitRate: "Share of songs in the called setlist that played anywhere in the show.",
  placedRate: "Of the called songs that played, the share the model put in the correct set.",
  exactCalls: "Right song, right set, right slot.",
  refreshGain:
    "Mean change in top-20 hit rate from a model's first take to its final take, over shows with multiple takes.",
  shows: "Scored shows this model has a frozen, scored take for.",
  sharp: "Shows where the model landed 2+ exact calls (right song, right set, right slot).",
} as const;

function hitRateTip(topN: number): string {
  return `Of the model's ${topN} highest-probability songs, the share that actually played — anywhere in the show, any set. With ~18 songs in a typical show, a perfect 20-song list tops out near 90%.`;
}

// A metric/column label that reveals its definition on hover/tap (StatPopover).
function TipLabel({
  text,
  tip,
  className,
}: {
  text: string;
  tip: string;
  className?: string;
}) {
  return (
    <StatPopover
      triggerClassName={className}
      trigger={<span className="tip-label">{text}</span>}
    >
      <div className="stat-pop-line">{tip}</div>
    </StatPopover>
  );
}

// Sortable standings columns (numeric only — Model/Kind are not sortable).
type SortCol =
  | "n_shows"
  | "hit_rate_top20"
  | "recall"
  | "brier"
  | "avg_n_rows"
  | "vs_heuristic"
  | "setlist_hit_rate"
  | "placed_rate"
  | "sharpshooters"
  | "refresh_gain";

// Numeric sort key for a model row; null (missing metric) always sorts last.
function standingsSortValue(m: ScoreboardModel, col: SortCol): number | null {
  switch (col) {
    case "n_shows":
      return m.n_shows;
    case "hit_rate_top20":
      return m.hit_rate_top20;
    case "recall":
      return m.recall;
    case "brier":
      return m.brier;
    case "avg_n_rows":
      return m.avg_n_rows;
    case "vs_heuristic":
      return m.vs_heuristic ? m.vs_heuristic.hit_rate_top20_delta : null;
    case "setlist_hit_rate":
      return m.setlist ? m.setlist.hit_rate : null;
    case "placed_rate":
      return m.setlist ? m.setlist.placed_rate : null;
    case "sharpshooters":
      return m.setlist ? m.setlist.sharpshooters : null;
    case "refresh_gain":
      return m.refresh_gain ? m.refresh_gain.mean_hit_rate_top20_delta : null;
  }
}

// One "take" of a scorecard source's own metrics/rows/setlist call — either
// the final (top-level) take or a prior version, normalized to the same
// shape so the rest of the render doesn't care which is selected (§8).
interface ScoredTake {
  metrics: ScorecardMetrics;
  rows: ScorecardRow[];
  setlistScore: ScorecardSetlistScore | null;
  bestCall: ScorecardCall | null;
  biggestWhiff: ScorecardCall | null;
  nRows: number;
}

export default function ShowsScreen({
  meta,
  schedule,
  showsByDate,
  setlistsByDate,
  selectedShows,
  onChangeSelected,
  initialMode,
}: ShowsScreenProps) {
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [setlistNight, setSetlistNight] = useState<string | null>(null);
  const [model, setModel] = useState(meta.headline_model);
  const [run, setRun] = useState<RunReport | null>(null);
  const [runPage, setRunPage] = useState(0);
  const [runPageRows] = useState(songPageSize);
  const multiselectRef = useRef<HTMLDivElement | null>(null);

  // Past-scorecard mode (DEPLOY-CONTRACTS §8). Default view stays FUTURE
  // predictions; the scoreboard is lazy-fetched on the first toggle and cached
  // in this component's state so switching back and forth is instant.
  const [mode, setMode] = useState<"upcoming" | "past">(initialMode ?? "upcoming");
  const [scoreboard, setScoreboard] = useState<Scoreboard | null>(null);
  const [scoreboardError, setScoreboardError] = useState<string | null>(null);
  const [pastDate, setPastDate] = useState<string | null>(null);
  const [pastModel, setPastModel] = useState<string | null>(null);
  const [scorecards, setScorecards] = useState<Record<string, Scorecard>>({});
  const [scorecardErrors, setScorecardErrors] = useState<Record<string, string>>({});
  // Which take of the active source is shown — "final" (the top-level,
  // official-benchmark entry) or a prior version's index (§8 versioning).
  const [versionSel, setVersionSel] = useState<number | "final">("final");
  // Standings sort — numeric columns only; default setlist hit rate desc.
  const [sortCol, setSortCol] = useState<SortCol>("setlist_hit_rate");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  // Pager for the (30–40 row) frozen shortlist in past mode — fixed at 20
  // rather than the shared dynamic songPageSize(): this list is a probability
  // ranking, and 20 lines up with the standard top-20 hit-rate window.
  const [shortlistPage, setShortlistPage] = useState(0);
  const [shortlistPageRows] = useState(20);

  // Dismiss the night picker on outside click or Escape.
  useEffect(() => {
    if (!dropdownOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!multiselectRef.current?.contains(e.target as Node)) setDropdownOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDropdownOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [dropdownOpen]);

  const modelOptions = useMemo(
    () => deriveModelOptions(meta, showsByDate),
    [meta, showsByDate],
  );

  const sortedSelected = useMemo(() => [...selectedShows].sort(), [selectedShows]);
  const dataShows = useMemo(
    () => sortedSelected.filter((d) => showsByDate[d]),
    [sortedSelected, showsByDate],
  );

  // Fetch the exact joint run whenever the selection changes.
  useEffect(() => {
    if (sortedSelected.length === 0) {
      setRun(null);
      return;
    }
    let cancelled = false;
    postRun(sortedSelected, meta.headline_model).then((r) => {
      if (!cancelled) {
        setRun(r);
        setRunPage(0);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [sortedSelected.join(","), meta.headline_model]);

  // Lazy-fetch the scoreboard the first time past mode is opened. NOTE: the
  // effect must not depend on state it sets itself — a self-triggered re-run
  // fires the cleanup, flips `cancelled`, and the response is thrown away.
  useEffect(() => {
    if (mode !== "past" || scoreboard || scoreboardError) return;
    let cancelled = false;
    fetchScoreboard()
      .then((sb) => {
        if (!cancelled) setScoreboard(sb);
      })
      .catch((err) => {
        if (!cancelled) setScoreboardError(err?.message ?? String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [mode, scoreboard, scoreboardError]);

  // Selected past show — default to the most recent scored show (shows desc).
  const activePastDate = useMemo(() => {
    if (!scoreboard || scoreboard.shows.length === 0) return null;
    if (pastDate && scoreboard.shows.some((s) => s.showdate === pastDate)) return pastDate;
    return scoreboard.shows[0].showdate;
  }, [scoreboard, pastDate]);

  // Fetch (and cache) the selected show's scorecard.
  useEffect(() => {
    if (mode !== "past" || !activePastDate) return;
    if (scorecards[activePastDate] || scorecardErrors[activePastDate]) return;
    let cancelled = false;
    fetchScorecard(activePastDate)
      .then((sc) => {
        if (!cancelled) setScorecards((m) => ({ ...m, [activePastDate]: sc }));
      })
      .catch((err) => {
        if (!cancelled)
          setScorecardErrors((m) => ({ ...m, [activePastDate]: err?.message ?? String(err) }));
      });
    return () => {
      cancelled = true;
    };
  }, [mode, activePastDate, scorecards, scorecardErrors]);

  const activeScorecard = activePastDate ? scorecards[activePastDate] ?? null : null;
  const pastSourceKeys = activeScorecard ? Object.keys(activeScorecard.sources) : [];
  // Model picker over the scorecard's own source keys; default heuristic.
  const activePastModel = useMemo(() => {
    if (pastSourceKeys.length === 0) return null;
    if (pastModel && pastSourceKeys.includes(pastModel)) return pastModel;
    return pastSourceKeys.includes("heuristic") ? "heuristic" : pastSourceKeys[0];
  }, [pastSourceKeys.join(","), pastModel]);
  const activeSource: ScorecardSource | null =
    activePastModel && activeScorecard ? activeScorecard.sources[activePastModel] : null;
  const activeVersions = activeSource?.versions ?? [];

  // Reset the version-chip selection whenever the show or model changes —
  // a version index from the previous source is meaningless for this one.
  useEffect(() => {
    setVersionSel("final");
  }, [activePastDate, activePastModel]);

  // Reset the shortlist pager whenever the show, model, or take changes so a
  // 30–40 row list never opens mid-way through.
  useEffect(() => {
    setShortlistPage(0);
  }, [activePastDate, activePastModel, versionSel]);

  // Normalize the currently-selected take (final or a prior version, §8) to
  // one shape so metrics/callouts/rows/setlist-card below render identically
  // either way.
  const shownTake: ScoredTake | null = useMemo(() => {
    if (!activeSource) return null;
    const idx =
      typeof versionSel === "number" && versionSel < activeVersions.length ? versionSel : "final";
    if (idx === "final") {
      return {
        metrics: activeSource.metrics,
        rows: activeSource.rows,
        setlistScore: activeSource.setlist_score ?? null,
        bestCall: activeSource.best_call,
        biggestWhiff: activeSource.biggest_whiff,
        nRows: activeSource.n_rows,
      };
    }
    const v = activeVersions[idx];
    return {
      metrics: v.metrics,
      rows: v.rows,
      setlistScore: v.setlist_score,
      bestCall: deriveCall(v.rows, true),
      biggestWhiff: deriveCall(v.rows, false),
      nRows: v.rows.length,
    };
  }, [activeSource, activeVersions, versionSel]);

  const marqueeBadges = shownTake?.setlistScore
    ? marqueeBadgeLabels(shownTake.setlistScore.marquee)
    : [];

  // Per-show "vs heuristic": this take's top-20 hit rate minus the heuristic
  // source's FINAL hit rate on the same scorecard (§8, computed client-side).
  // Undefined when viewing the heuristic itself or the show has no heuristic
  // source — the metrics strip then falls back to the log-loss slot.
  const heuristicSource = activeScorecard?.sources.heuristic ?? null;
  const vsHeuristicDelta =
    shownTake && heuristicSource && activePastModel !== "heuristic"
      ? shownTake.metrics.hit_rate_top20 - heuristicSource.metrics.hit_rate_top20
      : null;

  // Slugs no source shortlisted — marked inline on the actual-setlist chips
  // (dashed + ×) instead of a separate "predicted by nobody" section.
  const missedByAllSlugs = useMemo(
    () => new Set((activeScorecard?.missed_by_all ?? []).map((s) => s.slug)),
    [activeScorecard],
  );
  // Played songs the SHOWN take's shortlist called — highlighted on the
  // actual-setlist chips so the model's coverage reads at a glance.
  const modelHitSlugs = useMemo(
    () => new Set((shownTake?.rows ?? []).filter((r) => r.hit).map((r) => r.slug)),
    [shownTake],
  );
  const playedChip = (s: { slug: string; song: string }) =>
    missedByAllSlugs.has(s.slug) ? (
      <span className="played-chip miss" key={s.slug} title="predicted by nobody">
        <span className="chip-x" aria-hidden="true">
          ×
        </span>
        {s.song}
      </span>
    ) : modelHitSlugs.has(s.slug) ? (
      <span className="played-chip hit" key={s.slug} title="in this model's shortlist">
        <span className="chip-x" aria-hidden="true">
          ✓
        </span>
        {s.song}
      </span>
    ) : (
      <span className="played-chip" key={s.slug}>
        {s.song}
      </span>
    );

  // Standings sorted by the selected column; nulls last, direction toggled.
  const sortedModels = useMemo(() => {
    if (!scoreboard) return [];
    const entries = Object.entries(scoreboard.models);
    const sign = sortDir === "asc" ? 1 : -1;
    return entries.sort(([, a], [, b]) => {
      const va = standingsSortValue(a, sortCol);
      const vb = standingsSortValue(b, sortCol);
      if (va === null && vb === null) return 0;
      if (va === null) return 1; // missing always last
      if (vb === null) return -1;
      return (va - vb) * sign;
    });
  }, [scoreboard, sortCol, sortDir]);

  const onSort = (col: SortCol) => {
    if (col === sortCol) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(col);
      setSortDir("desc");
    }
  };
  const ariaSort = (col: SortCol): "ascending" | "descending" | "none" =>
    col === sortCol ? (sortDir === "asc" ? "ascending" : "descending") : "none";
  const sortCaret = (col: SortCol): string =>
    col === sortCol ? (sortDir === "asc" ? " ▲" : " ▼") : "";

  // A right-aligned, click-to-sort standings header whose label reveals its
  // definition on hover/tap (StatPopover). The button is the popover trigger,
  // so a click both sorts and (harmlessly) toggles the tip.
  const sortHeader = (col: SortCol, label: string, tip: string) => (
    <StatPopover
      triggerClassName="th-sort"
      trigger={
        <button
          type="button"
          className="standings-th"
          aria-sort={ariaSort(col)}
          onClick={() => onSort(col)}
        >
          {label}
          {sortCaret(col)}
        </button>
      }
    >
      <div className="stat-pop-line">{tip}</div>
    </StatPopover>
  );

  // Keep the setlist night valid (first data-having selected night).
  const activeNight =
    setlistNight && dataShows.includes(setlistNight) ? setlistNight : dataShows[0] ?? null;

  const toggleShow = (date: string) => {
    const has = selectedShows.includes(date);
    if (has) {
      if (selectedShows.length === 1) return; // keep at least one
      onChangeSelected(selectedShows.filter((d) => d !== date));
    } else {
      onChangeSelected([...selectedShows, date].sort());
    }
  };

  const summary =
    sortedSelected.length === 1
      ? dateLabelShort(sortedSelected[0])
      : `${sortedSelected.length} nights selected`;

  // Models in meta.models have a published structured setlist; other sources
  // (mcp:*/llm:*) carry a flat ranked shortlist inside the night's show JSON.
  const modelHasSetlist = meta.models.includes(model);
  const modelLabel = modelOptions.find((m) => m.id === model)?.label ?? model;
  const setlist = activeNight ? setlistsByDate[activeNight] : null;
  const sourceForNight = activeNight
    ? showsByDate[activeNight]?.sources[model] ?? null
    : null;

  return (
    <>
      {/* Toolbar band: the "Build your run" block (upcoming mode) shares a row
          with the mode toggle so the toggle doesn't strand a mostly-empty band
          above the content. Mobile stacks them, toggle first — see
          .shows-toolbar in styles.css. */}
      <div className="shows-toolbar">
        {mode === "upcoming" && (
          <div>
            <div className="label-caps" style={{ marginBottom: 8 }}>
              Build your run:
            </div>
            <div className="multiselect" ref={multiselectRef}>
              <button className="multiselect-toggle" onClick={() => setDropdownOpen((o) => !o)}>
                <span className="multiselect-summary">{summary}</span>
                <span style={{ color: "var(--text-label)", fontSize: 11 }}>
                  {dropdownOpen ? "▴" : "▾"}
                </span>
              </button>
              {dropdownOpen && (
                <div className="multiselect-panel">
                  {schedule.shows.map((s) => {
                    const checked = selectedShows.includes(s.showdate);
                    return (
                      <button
                        key={s.showdate}
                        className={"ms-option" + (checked ? " checked" : "")}
                        onClick={() => toggleShow(s.showdate)}
                      >
                        <span className="ms-check" />
                        <span style={{ flex: 1 }}>
                          <span className="ms-date" style={{ display: "block" }}>
                            {dateLabel(s.showdate)}
                          </span>
                          <span className="ms-venue">
                            {s.venue_name} — {s.city}, {s.state}
                          </span>
                        </span>
                        {!s.has_data && <span className="ms-nodata">no data</span>}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}
        <div className="mode-toggle" role="tablist" aria-label="Prediction mode">
          <button
            className={"mode-option" + (mode === "upcoming" ? " active" : "")}
            role="tab"
            aria-selected={mode === "upcoming"}
            onClick={() => setMode("upcoming")}
          >
            Upcoming
          </button>
          <button
            className={"mode-option" + (mode === "past" ? " active" : "")}
            role="tab"
            aria-selected={mode === "past"}
            onClick={() => setMode("past")}
          >
            Past scorecards
          </button>
        </div>
      </div>

      {mode === "upcoming" && (
      <>

      <div className="shows-row">
        {/* CARD A: RUN VIEW */}
        <div className="card shows-card">
          <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
            <span className="card-title">Run view</span>
          </div>
          <div className="card-sub" style={{ marginTop: 4 }}>
            Probability of hearing each song at least once across the selected run,
            according to a heuristic model.
          </div>
          <div className="run-grid-head">
            <span>Song</span>
            <span style={{ textAlign: "right" }}>P(≥1 in run)</span>
            <span style={{ textAlign: "right" }}>Most likely night</span>
          </div>
          {(run?.rows ?? [])
            .slice(runPage * runPageRows, (runPage + 1) * runPageRows)
            .map((r) => (
              <div className="run-grid-row" key={r.slug}>
                <span className="r-song">{r.song}</span>
                <span className="run-p">{pct1(r.p_at_least_one)}</span>
                <span className="run-night">{dateLabelDay(r.most_likely_night_date)}</span>
              </div>
            ))}
          <Pager
            page={runPage}
            totalRows={run?.rows.length ?? 0}
            pageSize={runPageRows}
            onPage={setRunPage}
          />
          {run && run.missing.length > 0 && (
            <div className="note">
              No cached predictions yet for: {run.missing.join(", ")}
            </div>
          )}
          {run?.approximate && (
            <div className="note">
              offline estimate — independent-events union 1 − Π(1−p). Live runs use the
              exact joint sample reduction (POST /api/run).
            </div>
          )}
        </div>

        {/* CARD B: SETLISTS */}
        <div className="card shows-card">
          <div className="setlist-head">
            <span className="card-title">Setlist calls</span>
            <div className="setlist-controls">
              <div className="control">
                <span className="control-label">Run night:</span>
                <select
                  className="select"
                  value={activeNight ?? ""}
                  onChange={(e) => setSetlistNight(e.target.value)}
                >
                  {dataShows.map((d) => (
                    <option key={d} value={d}>
                      {dateLabel(d)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="control">
                <span className="control-label">Model:</span>
                <select
                  className="select"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                >
                  {modelOptions.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </div>
              {sourceForNight?.versions && sourceForNight.versions.length > 0 && (
                <span className="rev-badge">
                  rev {sourceForNight.versions.length + 1}
                  {sourceForNight.submitted_at
                    ? ` · updated ${dateLabelShort(sourceForNight.submitted_at.slice(0, 10))}`
                    : " · updated after last night"}
                </span>
              )}
            </div>
          </div>
          <div className="card-sub" style={{ marginTop: 4, marginBottom: 14 }}>
            Hypothetical setlists from competing heuristic and AI models; click{" "}
            <em>Past scorecards</em> above for standings
          </div>

          {!modelHasSetlist && !sourceForNight ? (
            <div className="center-msg">
              No published predictions yet for {modelLabel} on this night.
            </div>
          ) : modelHasSetlist && !setlist ? (
            <div className="center-msg">
              No cached predictions for the selected night(s) yet.
            </div>
          ) : (
            <div>
              <div style={{ color: "var(--text-primary)", fontSize: 14, fontWeight: 700 }}>
                {activeNight && dateLabel(activeNight)}
              </div>
              <div
                className="mono"
                style={{ color: "var(--text-muted)", fontSize: 11, margin: "2px 0 14px" }}
              >
                {modelHasSetlist ? setlist!.venue_name : showsByDate[activeNight!].venue_name}
                {showsByDate[activeNight!] &&
                  ` · ${showsByDate[activeNight!].city}, ${showsByDate[activeNight!].state}`}
              </div>
              {modelHasSetlist ? (
                orderSetKeys(Object.keys(setlist!.sets)).map((key) => (
                  <div className="set-section" key={key}>
                    <div className="set-label">{setKeyLabel(key)}</div>
                    {setlist!.sets[key].map((sg, i) => (
                      <div className="set-song" key={sg.slug + i}>
                        <span className="set-idx">{i + 1}</span>
                        <span className="set-name">{sg.song_name}</span>
                        {sg.segue_mark.trim() && (
                          <span className="set-segue">{sg.segue_mark.trim()}</span>
                        )}
                        <span className="set-pct">{pct1(sg.prob)}</span>
                      </div>
                    ))}
                  </div>
                ))
              ) : (
                <>
                  {/* A structured setlist call (§2/§5) is a stronger prediction than
                      the flat ranked shortlist — prefer it when present, and keep the
                      shortlist below for the full picture. */}
                  {sourceForNight!.setlist &&
                    orderSetKeys(Object.keys(sourceForNight!.setlist.sets)).map((key) => (
                      <div className="set-section" key={key}>
                        <div className="set-label">{setKeyLabel(key)} · called</div>
                        {sourceForNight!.setlist!.sets[key].map((sg, i) => (
                          <div className="set-song" key={sg.slug + i}>
                            <span className="set-idx">{i + 1}</span>
                            <span className="set-name">{sg.song}</span>
                          </div>
                        ))}
                      </div>
                    ))}
                  {/* The model's own narrative sits between the setlist call and the
                      shortlist — it frames both without leading the card. */}
                  {sourceForNight!.rationale && (
                    <div className="rationale">
                      <span className="rationale-kicker">Rationale</span>
                      <p className="rationale-body">{sourceForNight!.rationale}</p>
                    </div>
                  )}
                  <div className="set-section">
                    <div className="set-label">Predicted songs · P(played)</div>
                    {sourceForNight!.rows.map((r, i) => (
                      <div className="set-song" key={r.slug}>
                        <span className="set-idx">{i + 1}</span>
                        <span className="set-name">{r.song}</span>
                        <span className="set-pct">{pct1(r.prob)}</span>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>
      </>
      )}

      {mode === "past" &&
        (scoreboardError ? (
          <div className="note">Couldn't load the scoreboard: {scoreboardError}</div>
        ) : !scoreboard ? (
          <div className="center-msg">Loading scorecards…</div>
        ) : scoreboard.shows.length === 0 ? (
          <div className="center-msg">
            No scored shows yet — check back after the first night.
          </div>
        ) : (
          <>
            {Object.keys(scoreboard.models).length > 0 && (
              <div className="card standings-card">
                <span className="card-title">Model standings</span>
                <div className="card-sub" style={{ marginTop: 4 }}>
                  Unweighted means over scored shows (final takes only).
                </div>
                <div className="standings-scroll">
                  <div className="standings-grid standings-head">
                    <span>Model</span>
                    <span>Kind</span>
                    {sortHeader("setlist_hit_rate", "Setlist", METRIC_TIPS.setlistHitRate)}
                    {sortHeader("placed_rate", "Placed", METRIC_TIPS.placedRate)}
                    {sortHeader("n_shows", "Shows", METRIC_TIPS.shows)}
                    {sortHeader("hit_rate_top20", "Hit·20", hitRateTip(20))}
                    {sortHeader("recall", "Recall", METRIC_TIPS.recall)}
                    {sortHeader("brier", "Brier", METRIC_TIPS.brier)}
                    {sortHeader("avg_n_rows", "List", METRIC_TIPS.list)}
                    {sortHeader("vs_heuristic", "vs heur", METRIC_TIPS.vsHeuristic)}
                    {sortHeader("sharpshooters", "Sharp", METRIC_TIPS.sharp)}
                    {sortHeader("refresh_gain", "Refresh gain", METRIC_TIPS.refreshGain)}
                  </div>
                  {sortedModels.map(([key, m]) => (
                    <div className="standings-grid standings-row" key={key}>
                      <span className="standings-model">{key}</span>
                      <span className="standings-dim">{m.kind}</span>
                      <span
                        className={m.setlist ? "standings-val" : "standings-dim"}
                        style={{ textAlign: "right" }}
                      >
                        {m.setlist ? pct1(m.setlist.hit_rate) : "—"}
                      </span>
                      <span
                        className={m.setlist ? "standings-val" : "standings-dim"}
                        style={{ textAlign: "right" }}
                      >
                        {m.setlist ? pct1(m.setlist.placed_rate) : "—"}
                      </span>
                      <span style={{ textAlign: "right" }}>{m.n_shows}</span>
                      <span className="standings-val" style={{ textAlign: "right" }}>
                        {pct1(m.hit_rate_top20)}
                      </span>
                      <span className="standings-val" style={{ textAlign: "right" }}>
                        {pct1(m.recall)}
                      </span>
                      <span style={{ textAlign: "right" }}>{m.brier.toFixed(3)}</span>
                      <span style={{ textAlign: "right" }}>{m.avg_n_rows.toFixed(1)}</span>
                      {m.vs_heuristic ? (
                        <span
                          className={
                            m.vs_heuristic.hit_rate_top20_delta >= 0
                              ? "standings-delta pos"
                              : "standings-delta neg"
                          }
                        >
                          {formatSignedPct(m.vs_heuristic.hit_rate_top20_delta)}
                        </span>
                      ) : (
                        <span className="standings-dim" style={{ textAlign: "right" }}>
                          —
                        </span>
                      )}
                      <span className="standings-dim" style={{ textAlign: "right" }}>
                        {m.setlist ? m.setlist.sharpshooters : "—"}
                      </span>
                      <span
                        className={m.refresh_gain ? "standings-val" : "standings-dim"}
                        style={{ textAlign: "right" }}
                      >
                        {m.refresh_gain
                          ? `${formatSignedPct(m.refresh_gain.mean_hit_rate_top20_delta)} after refresh`
                          : "—"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="setlist-controls" style={{ marginBottom: 24 }}>
              <div className="control">
                <span className="control-label">Show:</span>
                <select
                  className="select"
                  value={activePastDate ?? ""}
                  onChange={(e) => setPastDate(e.target.value)}
                >
                  {scoreboard.shows.map((s) => (
                    <option key={s.showdate} value={s.showdate}>
                      {dateLabelShort(s.showdate)} · {s.venue_name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="control">
                <span className="control-label">Model:</span>
                <select
                  className="select"
                  value={activePastModel ?? ""}
                  onChange={(e) => setPastModel(e.target.value)}
                  disabled={pastSourceKeys.length === 0}
                >
                  {pastSourceKeys.map((key) => {
                    const src = activeScorecard!.sources[key];
                    const label =
                      src.kind !== "statistical" ? `${key} (${src.kind})` : key;
                    return (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    );
                  })}
                </select>
              </div>
            </div>

            {activePastDate && scorecardErrors[activePastDate] ? (
              <div className="note">
                Couldn't load the scorecard: {scorecardErrors[activePastDate]}
              </div>
            ) : !activeScorecard || !activeSource || !shownTake ? (
              <div className="center-msg">Loading scorecard…</div>
            ) : (
              <>
                {/* SCORES BAND (full width) — parallels the Upcoming tab: model
                    header, improvement arc + version chips, then the shortlist
                    metrics and (when a call was scored) the setlist-call metrics,
                    grouped with small labels, plus the callouts. */}
                <div className="card scores-band">
                  <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <span className="card-title">{activePastModel}</span>
                    <span className="label-caps">{activeSource.kind}</span>
                  </div>
                  <div className="card-sub" style={{ marginTop: 4 }}>
                    Frozen {shownTake.nRows}-song shortlist scored against what actually played.
                  </div>

                  {activeVersions.length > 0 && (
                    <>
                      <div className="improvement-line">
                        hit rate{" "}
                        <span className="imp-value">
                          {pct(activeVersions[0].metrics.hit_rate_top20)}
                        </span>{" "}
                        →{" "}
                        <span
                          className={
                            "imp-value" +
                            (activeSource.metrics.hit_rate_top20 >
                            activeVersions[0].metrics.hit_rate_top20
                              ? " imp-up"
                              : activeSource.metrics.hit_rate_top20 <
                                  activeVersions[0].metrics.hit_rate_top20
                                ? " imp-down"
                                : "")
                          }
                        >
                          {pct(activeSource.metrics.hit_rate_top20)}
                        </span>{" "}
                        across {activeVersions.length + 1} takes
                      </div>
                      <div className="version-chips">
                        {activeVersions.map((v, i) => (
                          <button
                            key={v.submitted_at + i}
                            type="button"
                            className={"version-chip" + (versionSel === i ? " active" : "")}
                            onClick={() => setVersionSel(i)}
                          >
                            {afterShowdateLabel(v.after_showdate)} ·{" "}
                            {pct(v.metrics.hit_rate_top20)}
                          </button>
                        ))}
                        <button
                          type="button"
                          className={"version-chip" + (versionSel === "final" ? " active" : "")}
                          onClick={() => setVersionSel("final")}
                        >
                          final · {pct(activeSource.metrics.hit_rate_top20)}
                        </button>
                      </div>
                    </>
                  )}

                  {/* The model's frozen narrative leads the band — the most human
                      part of the take, read before the numbers. Versions carry no
                      rationale (§8): the frozen rationale belongs to the FINAL
                      take only, so hide it when a prior take is selected. */}
                  {versionSel === "final" && activeSource.rationale && (
                    <div className="rationale">
                      <span className="rationale-kicker">Rationale</span>
                      <p className="rationale-body">{activeSource.rationale}</p>
                    </div>
                  )}

                  {/* Shortlist metric group */}
                  <div className="metric-group">
                    <div className="metric-group-label">Shortlist</div>
                    <div className="metrics-strip">
                      <div className="metric">
                        <TipLabel
                          text={`Hit rate · top ${shownTake.metrics.top_n}`}
                          tip={hitRateTip(shownTake.metrics.top_n)}
                        />
                        <span className="metric-value">
                          {pct1(shownTake.metrics.hit_rate_top20)}
                        </span>
                        <span className="metric-sub">
                          {shownTake.metrics.hits_top20}/
                          {Math.min(shownTake.metrics.top_n, shownTake.nRows)} hit
                        </span>
                      </div>
                      <div className="metric">
                        <TipLabel text="Recall" tip={METRIC_TIPS.recall} />
                        <span className="metric-value">{pct1(shownTake.metrics.recall)}</span>
                        <span className="metric-sub">of {activeScorecard.n_played} played</span>
                      </div>
                      <div className="metric">
                        <TipLabel text="Brier" tip={METRIC_TIPS.brier} />
                        <span className="metric-value">{shownTake.metrics.brier.toFixed(3)}</span>
                        <span className="metric-sub">lower = better</span>
                      </div>
                      {vsHeuristicDelta !== null ? (
                        <div className="metric">
                          <TipLabel text="vs heuristic" tip={METRIC_TIPS.vsHeuristic} />
                          <span
                            className={
                              "metric-value" + (vsHeuristicDelta >= 0 ? " pos" : " neg")
                            }
                          >
                            {formatSignedPct(vsHeuristicDelta)}
                          </span>
                          <span className="metric-sub">vs baseline</span>
                        </div>
                      ) : (
                        <div className="metric">
                          <span className="metric-label">Log loss</span>
                          <span className="metric-value">
                            {shownTake.metrics.log_loss.toFixed(3)}
                          </span>
                          <span className="metric-sub">lower = better</span>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Setlist-call metric group — only when a call was scored */}
                  {shownTake.setlistScore && (
                    <div className="metric-group">
                      <div className="metric-group-head">
                        <div className="metric-group-label">Setlist call</div>
                        {shownTake.setlistScore.sharpshooter && (
                          <span className="sharpshooter-badge">★ Sharpshooter</span>
                        )}
                      </div>
                      <div className="metrics-strip">
                        <div className="metric">
                          <TipLabel text="Weighted score" tip={METRIC_TIPS.setlistWeighted} />
                          <span className="metric-value">
                            {pct1(shownTake.setlistScore.weighted_score)}
                          </span>
                          <span className="metric-sub">hit + set + slot</span>
                        </div>
                        <div className="metric">
                          <TipLabel text="Hit rate" tip={METRIC_TIPS.setlistHitRate} />
                          <span className="metric-value">
                            {pct1(shownTake.setlistScore.hit_rate)}
                          </span>
                          <span className="metric-sub">
                            {shownTake.setlistScore.hits}/{shownTake.setlistScore.n_songs} songs
                          </span>
                        </div>
                        <div className="metric">
                          <TipLabel text="Placed rate" tip={METRIC_TIPS.placedRate} />
                          <span className="metric-value">
                            {pct1(shownTake.setlistScore.placed_rate)}
                          </span>
                          <span className="metric-sub">
                            {shownTake.setlistScore.placed}/{shownTake.setlistScore.hits} in the
                            right set
                          </span>
                        </div>
                        <div className="metric">
                          <TipLabel text="Exact calls" tip={METRIC_TIPS.exactCalls} />
                          <span className="metric-value">
                            {shownTake.setlistScore.exact_calls}
                          </span>
                          <span className="metric-sub">position matches</span>
                        </div>
                      </div>
                      {marqueeBadges.length > 0 && (
                        <div className="marquee-badges">
                          {marqueeBadges.map((label) => (
                            <span className="marquee-badge" key={label}>
                              {label}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {(shownTake.bestCall || shownTake.biggestWhiff) && (
                    <div className="callouts">
                      {shownTake.bestCall && (
                        <div className="callout hit">
                          <span className="callout-kicker">
                            {shownTake.bestCall.prob >= 0.3 ? "Best call" : "Gutsiest hit"}
                          </span>
                          <span className="callout-body">
                            {shownTake.bestCall.song}
                            <span className="callout-prob">
                              {" "}
                              · {pct1(shownTake.bestCall.prob)}
                            </span>
                          </span>
                        </div>
                      )}
                      {shownTake.biggestWhiff && (
                        <div className="callout miss">
                          <span className="callout-kicker">Biggest whiff</span>
                          <span className="callout-body">
                            {shownTake.biggestWhiff.song}
                            <span className="callout-prob">
                              {" "}
                              · {pct1(shownTake.biggestWhiff.prob)}
                            </span>
                          </span>
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* TWO-COLUMN ROW: what played + scored setlist call (left) |
                    frozen shortlist rows (right). Setlist-call content leads
                    (§ ask: setlist above shortlist) — on mobile the row
                    stacks in this same source order, so it reads setlist
                    card on top, shortlist card below. */}
                <div className="shows-row">
                  {/* LEFT: what played + the scored setlist call, one panel —
                      actual setlist chips on top (missed-by-all marked inline),
                      the model's ordered call scored against it below. */}
                  <div className="card shows-card">
                    <span className="card-title">What played</span>
                    <div
                      className="mono"
                      style={{ color: "var(--text-muted)", fontSize: 11, margin: "4px 0 16px" }}
                    >
                      {dateLabel(activeScorecard.showdate)} · {activeScorecard.venue_name} ·{" "}
                      {activeScorecard.city}, {activeScorecard.state}
                    </div>

                    <div className="set-section">
                      <div className="set-label">Actual setlist · {activeScorecard.n_played} songs</div>
                      {activeScorecard.played_sets ? (
                        orderSetKeys(Object.keys(activeScorecard.played_sets)).map((key) => (
                          <div key={key} style={{ marginBottom: 10 }}>
                            <div className="label-caps" style={{ marginBottom: 4 }}>
                              {setKeyLabel(key)}
                            </div>
                            <div className="played-list">
                              {activeScorecard.played_sets![key].map(playedChip)}
                            </div>
                          </div>
                        ))
                      ) : (
                        <div className="played-list">
                          {activeScorecard.played.map(playedChip)}
                        </div>
                      )}
                      {(modelHitSlugs.size > 0 ||
                        activeScorecard.missed_by_all.length > 0) && (
                        <div className="note" style={{ marginTop: 6 }}>
                          {modelHitSlugs.size > 0 && "✓ = in this model's shortlist"}
                          {modelHitSlugs.size > 0 &&
                            activeScorecard.missed_by_all.length > 0 &&
                            " · "}
                          {activeScorecard.missed_by_all.length > 0 &&
                            "× = in nobody's shortlist"}
                        </div>
                      )}
                    </div>

                    <div className="set-section">
                      <div className="set-label">Setlist call · predicted order</div>
                      {shownTake.setlistScore ? (
                        orderSetKeys(Object.keys(shownTake.setlistScore.sets)).map((key) => (
                          <div key={key} style={{ marginBottom: 10 }}>
                            <div className="label-caps" style={{ marginBottom: 4 }}>
                              {setKeyLabel(key)} · called
                            </div>
                            {shownTake.setlistScore!.sets[key].map((sg, i) => (
                              <div
                                className={"setlist-song" + (sg.hit ? "" : " miss")}
                                key={sg.slug + i}
                              >
                                <span
                                  className={
                                    "setlist-mark" +
                                    (sg.exact
                                      ? " exact"
                                      : sg.placed
                                        ? " placed"
                                        : sg.hit
                                          ? " hit-only"
                                          : "")
                                  }
                                >
                                  {sg.hit ? "✓" : "×"}
                                </span>
                                <span className="setlist-name">{sg.song}</span>
                                {sg.exact ? (
                                  <span className="setlist-exact-tag">★ exact slot</span>
                                ) : sg.placed ? (
                                  <span className="setlist-placed-tag">right set</span>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        ))
                      ) : (
                        <div className="note">no setlist call for this show</div>
                      )}
                    </div>

                    <a
                      className="btn-link"
                      href={activeScorecard.phishnet_url}
                      target="_blank"
                      rel="noreferrer"
                      style={{ display: "inline-block", marginTop: 4 }}
                    >
                      full setlist on phish.net →
                    </a>
                  </div>

                  {/* RIGHT: shortlist rows */}
                  <div className="card shows-card">
                    <div className="set-section">
                      <div className="set-label">Shortlist · frozen P(played) · hit / miss</div>
                      {shownTake.rows
                        .slice(
                          shortlistPage * shortlistPageRows,
                          (shortlistPage + 1) * shortlistPageRows,
                        )
                        .map((r) => (
                          <div
                            className={"score-row" + (r.hit ? " hit" : " miss")}
                            key={r.slug}
                          >
                            <span className="score-mark">{r.hit ? "✓" : "×"}</span>
                            <span className="score-name">{r.song}</span>
                            <span className="score-prob">{pct1(r.prob)}</span>
                          </div>
                        ))}
                      <Pager
                        page={shortlistPage}
                        totalRows={shownTake.rows.length}
                        pageSize={shortlistPageRows}
                        onPage={setShortlistPage}
                      />
                    </div>
                  </div>
                </div>
              </>
            )}
          </>
        ))}
    </>
  );
}
