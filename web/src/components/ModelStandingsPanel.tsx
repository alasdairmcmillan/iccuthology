import { useEffect, useState } from "react";
import { fetchScoreboard } from "../api";
import type { Scoreboard, ScoreboardModel } from "../types";
import { pct1 } from "../lib/format";

type Metric = "setlist" | "hit20";

interface ModelStandingsPanelProps {
  onOpenScorecards: () => void;
}

// Setlist hit rate only exists for sources that submitted an ordered call
// (§8) — heuristic and any model that hasn't yet does not, and sorts last.
function metricValue(m: ScoreboardModel, metric: Metric): number | null {
  return metric === "setlist" ? (m.setlist ? m.setlist.hit_rate : null) : m.hit_rate_top20;
}

/** Compact model leaderboard for the Tours page header band — the fuller,
 *  sortable standings table (all metrics) lives on the Shows page's Past
 *  scorecards mode; this is a toggle-able summary that links there. */
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
        <div>
          <span className="card-title">Model standings</span>
          <div className="card-sub" style={{ marginBottom: 0, marginTop: 4 }}>
            {metric === "setlist"
              ? "Ordered setlist call hit rate — unweighted mean over each model's scored shows."
              : "Top-20 shortlist hit rate — unweighted mean over each model's scored shows."}
          </div>
        </div>
        <div className="mode-toggle" role="tablist" aria-label="Standings metric">
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
          {ranked.map(({ key, m, value }) => (
            <div className="standings-panel-row" key={key}>
              <span className="standings-model">{key}</span>
              {/* n_shows varies per model (not every model submits for every
                  show), so raw ranks aren't apples-to-apples at low sample
                  sizes — surface the count next to every value rather than
                  hide it. */}
              <span className="standings-dim">{m.n_shows} shows</span>
              <span className={value !== null ? "standings-val" : "standings-dim"}>
                {value !== null ? pct1(value) : "—"}
              </span>
            </div>
          ))}
        </div>
      )}

      <button className="pager-btn standings-panel-cta" onClick={onOpenScorecards}>
        Past scorecards →
      </button>
    </div>
  );
}
