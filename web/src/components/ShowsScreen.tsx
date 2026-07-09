import { useEffect, useMemo, useRef, useState } from "react";
import { postRun } from "../api";
import type {
  Meta,
  RunReport,
  Schedule,
  SetlistPrediction,
  ShowReport,
} from "../types";
import { dateLabel, dateLabelDay, dateLabelShort, pct1 } from "../lib/format";

interface ShowsScreenProps {
  meta: Meta;
  schedule: Schedule;
  showsByDate: Record<string, ShowReport>;
  setlistsByDate: Record<string, SetlistPrediction | null>;
  selectedShows: string[];
  onChangeSelected: (next: string[]) => void;
}

interface ModelOption {
  id: string;
  label: string;
}

// Model picker options are derived from the source keys actually present on the
// loaded shows — never hardcoded. Order: meta.headline_model first, then
// meta.models in their declared order, then any remaining keys (e.g. mcp:*/llm:*
// entries not yet listed in meta.models) alphabetically. hasData is derived from
// meta.models, so only sources actually published render a setlist; others show
// a "no predictions yet" note.
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

export default function ShowsScreen({
  meta,
  schedule,
  showsByDate,
  setlistsByDate,
  selectedShows,
  onChangeSelected,
}: ShowsScreenProps) {
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [setlistNight, setSetlistNight] = useState<string | null>(null);
  const [model, setModel] = useState(meta.headline_model);
  const [run, setRun] = useState<RunReport | null>(null);
  const multiselectRef = useRef<HTMLDivElement | null>(null);

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
      if (!cancelled) setRun(r);
    });
    return () => {
      cancelled = true;
    };
  }, [sortedSelected.join(","), meta.headline_model]);

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

  const modelHasData = meta.models.includes(model);
  const modelLabel = modelOptions.find((m) => m.id === model)?.label ?? model;
  const setlist = activeNight ? setlistsByDate[activeNight] : null;

  return (
    <>
      <div style={{ marginBottom: 26 }}>
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

      <div className="shows-row">
        {/* CARD A: RUN VIEW */}
        <div className="card shows-card">
          <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
            <span className="card-title">Run view</span>
          </div>
          <div className="card-sub" style={{ marginTop: 4 }}>
            Probability of hearing each song at least once across the selected run.
          </div>
          <div className="run-grid-head">
            <span>Song</span>
            <span style={{ textAlign: "right" }}>P(≥1 in run)</span>
            <span style={{ textAlign: "right" }}>Most likely night</span>
          </div>
          {(run?.rows ?? []).map((r) => (
            <div className="run-grid-row" key={r.slug}>
              <span className="r-song">{r.song}</span>
              <span className="run-p">{pct1(r.p_at_least_one)}</span>
              <span className="run-night">{dateLabelDay(r.most_likely_night_date)}</span>
            </div>
          ))}
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
            <span className="card-title">Proposed setlist</span>
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
            </div>
          </div>

          {!modelHasData ? (
            <div className="center-msg">
              No published predictions yet for {modelLabel} in this preview.
            </div>
          ) : !setlist ? (
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
                {setlist.venue_name}
                {showsByDate[activeNight!] &&
                  ` · ${showsByDate[activeNight!].city}, ${showsByDate[activeNight!].state}`}
              </div>
              {orderSetKeys(Object.keys(setlist.sets)).map((key) => (
                <div className="set-section" key={key}>
                  <div className="set-label">{setKeyLabel(key)}</div>
                  {setlist.sets[key].map((sg, i) => (
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
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
