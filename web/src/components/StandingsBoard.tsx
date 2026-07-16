import { useMemo, useState } from "react";
import type { ScoreboardModel } from "../types";
import { modelDisplayName, pct1 } from "../lib/format";
import { METRIC_TIPS, formatSignedPct, hitRateTip } from "../lib/metricTips";
import StatPopover from "./StatPopover";

// One scoreboard metric: how to read it off a model, format it, and whether
// it participates in the sort-chip row and the ◆ best-in-field marks.
// null (model never submitted the underlying artifact) renders "—" and
// always sorts last, matching the old table's behavior.
interface MetricDef {
  id: string;
  label: string;
  tip: string;
  value: (m: ScoreboardModel) => number | null;
  format: (v: number) => string;
  /** Lower is better (Brier): chip default-sorts ascending, ◆ marks the min. */
  lowerBetter?: boolean;
  /** Eligible for the ◆ mark. Off for context columns (Shows, List) where
   *  "best" is meaningless. */
  best?: boolean;
  /** Signed delta — color pos/neg instead of accent. */
  signed?: boolean;
  /** In the sort-chip row (detail-only metrics like Exact calls are not). */
  sortable?: boolean;
}

const count = (v: number) => String(v);

// Chip order mirrors the old table's columns; detail-only extras follow.
const METRICS: MetricDef[] = [
  { id: "setlist", label: "Setlist", tip: METRIC_TIPS.setlistHitRate, value: (m) => m.setlist?.hit_rate ?? null, format: pct1, best: true, sortable: true },
  { id: "placed", label: "Placed", tip: METRIC_TIPS.placedRate, value: (m) => m.setlist?.placed_rate ?? null, format: pct1, best: true, sortable: true },
  { id: "shows", label: "Shows", tip: METRIC_TIPS.shows, value: (m) => m.n_shows, format: count, sortable: true },
  { id: "hit20", label: "Hit·20", tip: hitRateTip(20), value: (m) => m.hit_rate_top20, format: pct1, best: true, sortable: true },
  { id: "recall", label: "Recall", tip: METRIC_TIPS.recall, value: (m) => m.recall, format: pct1, best: true, sortable: true },
  { id: "brier", label: "Brier", tip: METRIC_TIPS.brier, value: (m) => m.brier, format: (v) => v.toFixed(3), lowerBetter: true, best: true, sortable: true },
  { id: "list", label: "List", tip: METRIC_TIPS.list, value: (m) => m.avg_n_rows, format: (v) => v.toFixed(1), sortable: true },
  { id: "vs_heur", label: "Δ base", tip: METRIC_TIPS.vsHeuristic, value: (m) => m.vs_heuristic?.hit_rate_top20_delta ?? null, format: formatSignedPct, signed: true, best: true, sortable: true },
  { id: "sharp", label: "Sharp", tip: METRIC_TIPS.sharp, value: (m) => m.setlist?.sharpshooters ?? null, format: count, best: true, sortable: true },
  { id: "refresh", label: "Refresh", tip: METRIC_TIPS.refreshGain, value: (m) => m.refresh_gain?.mean_hit_rate_top20_delta ?? null, format: formatSignedPct, signed: true, best: true, sortable: true },
  { id: "weighted", label: "Weighted", tip: METRIC_TIPS.setlistWeighted, value: (m) => m.setlist?.weighted_score ?? null, format: pct1, best: true },
  { id: "exact", label: "Exact calls", tip: METRIC_TIPS.exactCalls, value: (m) => m.setlist?.exact_calls ?? null, format: count },
  { id: "marquee", label: "Marquee", tip: METRIC_TIPS.marqueeCalls, value: (m) => m.setlist?.marquee_calls ?? null, format: count },
];

const SORTABLE = METRICS.filter((d) => d.sortable);

// Detail grid order: setlist-call metrics first, then shortlist metrics,
// then context (List/Shows last — they're denominators, not scores).
const DETAIL_ORDER = [
  "setlist", "placed", "weighted", "exact", "marquee", "sharp",
  "hit20", "recall", "brier", "vs_heur", "refresh", "list", "shows",
];
const DETAIL = DETAIL_ORDER.map((id) => METRICS.find((d) => d.id === id)!);

// A metric label whose definition pops on hover/tap — same affordance as the
// scores band's TipLabel, kept visible in the redesign per user ask.
function MetricTip({ def }: { def: MetricDef }) {
  return (
    <StatPopover trigger={<span className="tip-label">{def.label}</span>}>
      <div className="stat-pop-line">{def.tip}</div>
    </StatPopover>
  );
}

interface StandingsBoardProps {
  models: Record<string, ScoreboardModel>;
}

/** Past-scorecards standings: a sort-chip row plus one expandable card per
 *  model (rank / name / headline metric, tap for the full breakdown) —
 *  replaces the old 12-column table so standings read without horizontal
 *  scrolling on any viewport. */
export default function StandingsBoard({ models }: StandingsBoardProps) {
  const [sortId, setSortId] = useState("setlist");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const activeDef = METRICS.find((d) => d.id === sortId)!;

  const onSort = (def: MetricDef) => {
    if (def.id === sortId) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortId(def.id);
      setSortDir(def.lowerBetter ? "asc" : "desc");
    }
  };

  const toggle = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const ranked = useMemo(() => {
    const sign = sortDir === "asc" ? 1 : -1;
    return Object.entries(models).sort(([, a], [, b]) => {
      const va = activeDef.value(a);
      const vb = activeDef.value(b);
      if (va === null && vb === null) return 0;
      if (va === null) return 1; // missing metric always last
      if (vb === null) return -1;
      return (va - vb) * sign;
    });
  }, [models, activeDef, sortDir]);

  // ◆ per metric: the leading value across models, only when 2+ models have
  // the metric (a field of one has no "best"). Ties all get the mark.
  const bestByMetric = useMemo(() => {
    const best = new Map<string, number>();
    for (const def of METRICS) {
      if (!def.best) continue;
      const vals = Object.values(models)
        .map((m) => def.value(m))
        .filter((v): v is number => v !== null);
      if (vals.length < 2) continue;
      best.set(def.id, def.lowerBetter ? Math.min(...vals) : Math.max(...vals));
    }
    return best;
  }, [models]);

  const valClass = (def: MetricDef, v: number | null): string => {
    if (v === null) return "standings-dim";
    if (def.signed) return v >= 0 ? "standings-delta pos" : "standings-delta neg";
    return "standings-val";
  };

  return (
    <div className="card standings-card">
      <span className="card-title">Model standings</span>
      <div className="card-sub" style={{ marginTop: 4 }}>
        Every model's report card across scored shows (final takes only) — pick a
        metric to rank by, tap a model for the full breakdown.
      </div>

      <div className="standings-chips">
        {SORTABLE.map((def) => {
          const active = def.id === sortId;
          return (
            <StatPopover
              key={def.id}
              trigger={
                <button
                  type="button"
                  className={"standings-chip" + (active ? " active" : "")}
                  aria-pressed={active}
                  onClick={() => onSort(def)}
                >
                  {def.label}
                  {active ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                </button>
              }
            >
              <div className="stat-pop-line">{def.tip}</div>
            </StatPopover>
          );
        })}
      </div>

      <div className="standings-cards">
        {ranked.map(([key, m], i) => {
          const open = expanded.has(key);
          const hv = activeDef.value(m);
          return (
            <div className={"mcard" + (open ? " open" : "")} key={key}>
              <button
                type="button"
                className="mcard-head"
                aria-expanded={open}
                onClick={() => toggle(key)}
              >
                <span className={"mcard-rank" + (i === 0 && hv !== null ? " lead" : "")}>
                  {i + 1}
                </span>
                <span className="mcard-id">
                  <span className="mcard-name">{modelDisplayName(key)}</span>
                  <span className="mcard-kind">
                    {m.kind} · {m.n_shows} {m.n_shows === 1 ? "show" : "shows"}
                  </span>
                </span>
                <span className="mcard-headline">
                  <span className={"mcard-headline-val " + valClass(activeDef, hv)}>
                    {hv === null ? "—" : activeDef.format(hv)}
                  </span>
                  <span className="mcard-headline-label">{activeDef.label}</span>
                </span>
                <span className="mcard-chev" aria-hidden="true">
                  {open ? "▴" : "▾"}
                </span>
              </button>
              {open && (
                <div className="mcard-detail">
                  {DETAIL.map((def) => {
                    const v = def.value(m);
                    const isBest =
                      def.best && v !== null && bestByMetric.get(def.id) === v;
                    return (
                      <div className="mcard-metric" key={def.id}>
                        <MetricTip def={def} />
                        <span className={"mcard-metric-val " + valClass(def, v)}>
                          {v === null ? "—" : def.format(v)}
                          {isBest && (
                            <span className="best-mark" title="best in field">
                              {" "}◆
                            </span>
                          )}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="standings-legend">
        ◆ best in field · Δ base = top-20 hit rate vs the heuristic baseline on
        shared shows
      </div>
    </div>
  );
}
