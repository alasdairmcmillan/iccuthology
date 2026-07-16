import { useEffect, useState } from "react";
import { fetchScoreboard } from "../api";
import type { Scoreboard, ScoreboardModel } from "../types";
import { modelDisplayName, pct1 } from "../lib/format";
import { METRIC_TIPS, hitRateTip } from "../lib/metricTips";
import StatPopover from "./StatPopover";

type Metric = "setlist" | "hit20";

interface ModelStandingsPanelProps {
  onOpenScorecards: () => void;
}

// Setlist hit rate only exists for sources that submitted an ordered call
// (§8) — heuristic and any model that hasn't yet does not, and sorts last.
function metricValue(m: ScoreboardModel, metric: Metric): number | null {
  return metric === "setlist" ? (m.setlist ? m.setlist.hit_rate : null) : m.hit_rate_top20;
}

/** Compact model leaderboard for the Tours page sidebar (above the schedule
 *  card) — the full standings board (all metrics, expandable per model)
 *  lives on the Shows page's Past scorecards mode; this is a toggle-able
 *  summary that links there. */
export default function ModelStandingsPanel({ onOpenScorecards }: ModelStandingsPanelProps) {
  const [scoreboard, setScoreboard] = useState<Scoreboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>("setlist");

  // Fetch once on mount — the Tours page is the app's default screen, so
  // this is typically the session's first scoreboard fetch (no mode gate
  // needed here, unlike ShowsScreen's lazy past-mode fetch).
  useEffect(() => {
    let cancelled = false;
    fetchScoreboard()
      .then((sb) => {
        if (!cancelled) setScoreboard(sb);
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message ?? String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const ranked = scoreboard
    ? Object.entries(scoreboard.models)
        .map(([key, m]) => ({ key, m, value: metricValue(m, metric) }))
        .sort((a, b) => {
          if (a.value === null && b.value === null) return 0;
          if (a.value === null) return 1; // missing metric always last
          if (b.value === null) return -1;
          return b.value - a.value;
        })
    : [];

  return (
    <div className="card standings-panel">
      <div className="standings-panel-head">
        <span className="label-caps">Model standings</span>
        <div className="mode-toggle mode-toggle-sm" role="tablist" aria-label="Standings metric">
          <button
            className={"mode-option" + (metric === "setlist" ? " active" : "")}
            role="tab"
            aria-selected={metric === "setlist"}
            onClick={() => setMetric("setlist")}
          >
            Setlist
          </button>
          <button
            className={"mode-option" + (metric === "hit20" ? " active" : "")}
            role="tab"
            aria-selected={metric === "hit20"}
            onClick={() => setMetric("hit20")}
          >
            Hit·20
          </button>
        </div>
      </div>

      {error ? (
        <div className="note">Couldn't load standings: {error}</div>
      ) : !scoreboard ? (
        <div className="center-msg">Loading standings…</div>
      ) : ranked.length === 0 ? (
        <div className="center-msg">No scored shows yet — check back after the first night.</div>
      ) : (
        <div className="standings-panel-list">
          {ranked.map(({ key, m, value }, i) => (
            <div className="standings-panel-row" key={key}>
              <span className={"standings-rank" + (i === 0 && value !== null ? " lead" : "")}>
                {i + 1}
              </span>
              <span className="standings-panel-id">
                <span className="standings-model">{modelDisplayName(key)}</span>
                {/* n_shows varies per model (not every model submits for every
                    show), so raw ranks aren't apples-to-apples at low sample
                    sizes — surface the count next to every value rather than
                    hide it. */}
                <span className="standings-sub">
                  {m.n_shows} {m.n_shows === 1 ? "show" : "shows"}
                </span>
              </span>
              <span className={value !== null ? "standings-val" : "standings-dim"}>
                {value !== null ? pct1(value) : "—"}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="standings-panel-foot">
        <StatPopover
          trigger={
            <span className="tip-label standings-sub">
              {metric === "setlist" ? "setlist call hit rate" : "top-20 hit rate"}
            </span>
          }
        >
          <div className="stat-pop-line">
            {metric === "setlist" ? METRIC_TIPS.setlistHitRate : hitRateTip(20)}{" "}
            Unweighted mean over each model's scored shows.
          </div>
        </StatPopover>
        <button className="standings-link" onClick={onOpenScorecards}>
          scorecards →
        </button>
      </div>
    </div>
  );
}
