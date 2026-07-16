import { useEffect, useMemo, useState } from "react";
import { fetchChaser } from "../api";
import { dateLabelShort, pct } from "../lib/format";
import { computeSongStats, type SongIndex } from "../songStats";
import type { Catalog, CatalogSong, ChaserReport } from "../types";

interface SongCardProps {
  catalog: Catalog;
  songIndex: SongIndex;
  song: CatalogSong;
  onSelect: (slug: string) => void;
}

/** Compact per-song result card: name + three headline stats. Used both in
 * the header's quick-search dropdown and the Songs page's browse list, so
 * the two surfaces stay visually and behaviorally consistent. */
export default function SongCard({ catalog, songIndex, song, onSelect }: SongCardProps) {
  const stats = useMemo(
    () => computeSongStats(catalog, songIndex, song.songid, song.debut_date),
    [catalog, songIndex, song.songid, song.debut_date],
  );

  // Lazy per-card fetch (not prefetched for the whole catalog) — result sets
  // showing this card are always small (header dropdown caps at 6; the Songs
  // page's browse list stays short since search is the primary interaction).
  const [chaser, setChaser] = useState<ChaserReport | "loading" | "error">("loading");
  useEffect(() => {
    let cancelled = false;
    setChaser("loading");
    fetchChaser(song.slug)
      .then((c) => {
        if (!cancelled) setChaser(c);
      })
      .catch(() => {
        if (!cancelled) setChaser("error");
      });
    return () => {
      cancelled = true;
    };
  }, [song.slug]);

  const nextShowLabel =
    chaser === "loading"
      ? "…"
      : chaser === "error"
        ? "—"
        : chaser.modal_show_date
          ? dateLabelShort(chaser.modal_show_date)
          : "not in horizon";

  return (
    <button className="song-card" onClick={() => onSelect(song.slug)}>
      <div className="song-card-name">{song.name}</div>
      <div className="song-card-stats">
        <div className="song-stat">
          <span className="song-stat-value">
            {stats.pctShowsPlayed === null ? "—" : pct(stats.pctShowsPlayed)}
          </span>
          <span className="song-stat-label">of shows</span>
        </div>
        <div className="song-stat">
          <span className="song-stat-value">{stats.playsThisYear}</span>
          <span className="song-stat-label">plays this yr</span>
        </div>
        <div className="song-stat">
          <span className="song-stat-value mono">{nextShowLabel}</span>
          <span className="song-stat-label">next likely</span>
        </div>
      </div>
    </button>
  );
}
