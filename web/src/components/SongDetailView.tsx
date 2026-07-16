import { useEffect, useMemo, useState } from "react";
import { dateLabel, dateLabelShort, pct } from "../lib/format";
import { computeSongStats, type SongIndex } from "../songStats";
import type {
  Catalog,
  CatalogSong,
  Meta,
  Schedule,
  ScheduleShow,
  ShowReport,
} from "../types";
import { deriveModelOptions, setKeyLabel } from "./ShowsScreen";

interface SongDetailViewProps {
  catalog: Catalog;
  songIndex: SongIndex;
  song: CatalogSong;
  meta: Meta;
  schedule: Schedule;
  showsByDate: Record<string, ShowReport>;
  onBack: () => void;
  onGotoShow: (date: string) => void;
}

function venueLoc(s: ScheduleShow): string {
  return [s.city, s.state].filter(Boolean).join(", ");
}

interface ModelDate {
  date: string;
  prob: number;
}

export default function SongDetailView({
  catalog,
  songIndex,
  song,
  meta,
  schedule,
  showsByDate,
  onBack,
  onGotoShow,
}: SongDetailViewProps) {
  const stats = useMemo(
    () => computeSongStats(catalog, songIndex, song.songid, song.debut_date),
    [catalog, songIndex, song.songid, song.debut_date],
  );

  const venueByDate = useMemo(() => {
    const m = new Map<string, ScheduleShow>();
    for (const s of schedule.shows) m.set(s.showdate, s);
    return m;
  }, [schedule]);

  const modelOptions = useMemo(
    () => deriveModelOptions(meta, showsByDate),
    [meta, showsByDate],
  );
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  useEffect(() => {
    if (modelOptions.length === 0) {
      setSelectedModel(null);
      return;
    }
    setSelectedModel((cur) =>
      cur && modelOptions.some((m) => m.id === cur) ? cur : modelOptions[0].id,
    );
  }, [modelOptions]);

  // "Predicted next show" is model-specific: the horizon show where the
  // selected model assigns this song the highest P(play), not tied to the
  // heuristic-only Monte-Carlo chaser (mcp/llm sources only ever publish a
  // per-show `rows` shortlist, never samples, so this is the one method
  // that generalizes across every model).
  const modelDates: ModelDate[] = useMemo(() => {
    if (!selectedModel) return [];
    const rows: ModelDate[] = [];
    for (const [date, show] of Object.entries(showsByDate)) {
      const row = show.sources[selectedModel]?.rows.find((r) => r.slug === song.slug);
      if (row) rows.push({ date, prob: row.prob });
    }
    return rows.sort((a, b) => b.prob - a.prob);
  }, [showsByDate, selectedModel, song.slug]);
  const topDate = modelDates[0] ?? null;
  const otherDates = modelDates.slice(1, 9);
  const topVenue = topDate ? venueByDate.get(topDate.date) ?? null : null;

  const topSlot = useMemo(() => {
    if (!topDate || !selectedModel) return null;
    const setlist = showsByDate[topDate.date]?.sources[selectedModel]?.setlist;
    if (!setlist) return null;
    for (const [setKey, slots] of Object.entries(setlist.sets)) {
      const idx = slots.findIndex((s) => s.slug === song.slug);
      if (idx !== -1) return { set: setKey, position: idx + 1 };
    }
    return null;
  }, [showsByDate, topDate, selectedModel, song.slug]);

  // Dates where the selected model made a concrete structured-setlist call
  // for this song — a stronger, more specific claim than just ranking high
  // by probability, so it's surfaced separately.
  const setlistCallDates = useMemo(() => {
    if (!selectedModel) return [];
    const dates: string[] = [];
    for (const [date, show] of Object.entries(showsByDate)) {
      const setlist = show.sources[selectedModel]?.setlist;
      if (!setlist) continue;
      const called = Object.values(setlist.sets).some((slots) =>
        slots.some((s) => s.slug === song.slug),
      );
      if (called) dates.push(date);
    }
    return dates.sort();
  }, [showsByDate, selectedModel, song.slug]);

  return (
    <div className="personal-layout">
      <div className="song-detail-header">
        <button className="btn-link" onClick={onBack}>
          ← Back<span className="when-wide"> to search</span>
        </button>
        <div className="screen-title song-detail-title">{song.name}</div>
        <span aria-hidden="true" />
      </div>

      <div className="song-detail-layout">
        <div className="card song-detail-stats-panel">
          <div className="card-title">Song stats</div>
          <div className="metrics-strip song-detail-metrics">
            <div className="metric">
              <span className="metric-label">of shows played</span>
              <span className="metric-value">
                {stats.pctShowsPlayed === null ? "—" : pct(stats.pctShowsPlayed)}
              </span>
              <span className="metric-sub">{stats.eligibleShows} eligible shows</span>
            </div>
            <div className="metric">
              <span className="metric-label">plays this year</span>
              <span className="metric-value">{stats.playsThisYear}</span>
            </div>
            <div className="metric">
              <span className="metric-label">lifetime plays</span>
              <span className="metric-value">{song.plays}</span>
            </div>
            <div className="metric">
              <span className="metric-label">last played</span>
              <span className="metric-value">
                {song.last ? dateLabelShort(song.last) : "—"}
              </span>
            </div>
            <div className="metric">
              <span className="metric-label">plays/yr, all-time</span>
              <span className="metric-value">
                {stats.playsPerYearAllTime === null ? "—" : stats.playsPerYearAllTime.toFixed(1)}
              </span>
            </div>
            <div className="metric">
              <span className="metric-label">plays/yr, last 150 shows</span>
              <span className="metric-value">
                {stats.playsPerYearRecent === null ? "—" : stats.playsPerYearRecent.toFixed(1)}
              </span>
            </div>
            <div className="metric">
              <span className="metric-label">recent tours played</span>
              <span className="metric-value">
                {stats.tourFrequency
                  ? `${stats.tourFrequency.playedIn} / ${stats.tourFrequency.ofLastTours}`
                  : "—"}
              </span>
            </div>
          </div>
          <a
            className="song-detail-phishnet"
            href={`https://phish.net/song/${song.slug}`}
            target="_blank"
            rel="noreferrer"
          >
            Full song history on phish.net →
          </a>
        </div>

        <div className="card song-detail-next-panel">
          <div className="card-title">Predicted next show</div>

          {modelOptions.length > 0 && (
            <div className="control song-detail-model-control">
              <span className="control-label">Model:</span>
              <select
                className="select"
                value={selectedModel ?? ""}
                onChange={(e) => setSelectedModel(e.target.value)}
              >
                {modelOptions.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
          )}

          {topDate ? (
            <>
              <button className="song-detail-next-show" onClick={() => onGotoShow(topDate.date)}>
                {dateLabel(topDate.date)}
              </button>
              {topVenue && (
                <div className="song-detail-venue">
                  {topVenue.venue_name} — {venueLoc(topVenue)}
                </div>
              )}
              <div className="song-detail-model-row">
                <span>P(play): {pct(topDate.prob)}</span>
                <span>
                  Set placement:{" "}
                  {topSlot ? `${setKeyLabel(topSlot.set)} #${topSlot.position}` : "not called"}
                </span>
              </div>
            </>
          ) : (
            <div className="song-stat-label">
              Not shortlisted for any show in the current horizon by this model.
            </div>
          )}

          {setlistCallDates.length > 0 && (
            <div className="song-detail-setlist-calls">
              <span className="song-stat-label">Setlist call:</span>
              {setlistCallDates.map((d) => (
                <button
                  key={d}
                  className="song-detail-setlist-chip mono"
                  onClick={() => onGotoShow(d)}
                >
                  {dateLabelShort(d)}
                </button>
              ))}
            </div>
          )}

          {otherDates.length > 0 && (
            <>
              <div className="song-stat-label song-detail-dist-label">Other likely dates</div>
              <ul className="song-detail-dist-list">
                {otherDates.map((d) => {
                  const venue = venueByDate.get(d.date);
                  return (
                    <li key={d.date}>
                      <button
                        className="song-detail-dist-row"
                        onClick={() => onGotoShow(d.date)}
                      >
                        <span className="song-detail-dist-date">{dateLabelShort(d.date)}</span>
                        {venue && (
                          <span className="song-detail-dist-venue">
                            <span className="song-detail-dist-venue-name">
                              {venue.venue_name}
                            </span>
                            <span className="song-detail-dist-venue-loc">{venueLoc(venue)}</span>
                          </span>
                        )}
                        <span className="song-detail-dist-pct">{pct(d.prob)}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
