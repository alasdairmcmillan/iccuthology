"""Backfill (`full_ingest`) and incremental (`refresh`) ingest from phish.net
into the local SQLite schema (see schema.sql / CONTRACTS.md).

Design notes:
- Everything is an upsert (INSERT ... ON CONFLICT DO UPDATE) so re-running
  ingest/refresh is always safe.
- The `shows` table only ever contains rows we've decided are Phish (filtered
  at insert time), so downstream code never has to re-filter by artist.
- Venue rows sourced from /venues are authoritative and always overwrite;
  venue info gleaned incidentally from show/setlist rows only fills gaps
  (INSERT ... ON CONFLICT DO NOTHING) for venues /venues didn't mention.
- `exclude` and `show_index` are recomputed from scratch at the end of every
  full_ingest/refresh call rather than incrementally maintained, so they're
  always consistent with whatever is currently in `performances`.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from .api import PhishNetClient, PhishNetError

logger = logging.getLogger(__name__)


def first_key(d: dict, *names: str):
    """Return the first present, non-null/non-empty value among `names` in `d`."""
    for name in names:
        value = d.get(name)
        if value is not None and value != "":
            return value
    return None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "unknown"


def _today_str() -> str:
    return datetime.date.today().isoformat()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class IngestStats:
    years_processed: int = 0
    shows_seen: int = 0
    shows_kept: int = 0
    shows_excluded: int = 0
    non_phish_shows_skipped: int = 0
    non_phish_performances_skipped: int = 0
    excluded_performance_rows_skipped: int = 0
    performances_inserted: int = 0
    songs_upserted: int = 0
    venues_upserted: int = 0
    setlist_fallback_years: int = 0
    observed_artistid: int | None = None

    def __str__(self) -> str:
        return (
            "IngestStats("
            f"years_processed={self.years_processed}, "
            f"shows_seen={self.shows_seen}, shows_kept={self.shows_kept}, "
            f"shows_excluded={self.shows_excluded}, "
            f"non_phish_shows_skipped={self.non_phish_shows_skipped}, "
            f"non_phish_performances_skipped={self.non_phish_performances_skipped}, "
            f"excluded_performance_rows_skipped={self.excluded_performance_rows_skipped}, "
            f"performances_inserted={self.performances_inserted}, "
            f"songs_upserted={self.songs_upserted}, "
            f"venues_upserted={self.venues_upserted}, "
            f"setlist_fallback_years={self.setlist_fallback_years}, "
            f"phish_artistid={self.observed_artistid})"
        )


# -- shared helpers -----------------------------------------------------------


def _upsert_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _is_phish_row(row: dict) -> bool:
    """Same Phish-filter rule applied to show rows and setlist/performance rows."""
    artist_name = row.get("artist_name")
    artistid = row.get("artistid")
    if artist_name is not None:
        return str(artist_name).strip().lower() == "phish" or artistid == 1
    if artistid is not None:
        return artistid == 1
    # Neither field present: can't tell, so don't drop the row.
    return True


def _maybe_insert_fallback_venue(conn: sqlite3.Connection, row: dict) -> None:
    """Insert a venue row from show/setlist-carried fields, but only if that
    venueid isn't already known (venues() master data always wins)."""
    venueid = row.get("venueid")
    if venueid is None:
        return
    name = first_key(row, "venue", "venuename", "venue_name")
    if name is None:
        return
    city = row.get("city")
    state = row.get("state")
    country = row.get("country")
    conn.execute(
        "INSERT INTO venues (venueid, name, city, state, country) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(venueid) DO NOTHING",
        (venueid, name, city, state, country),
    )


def _upsert_show(conn: sqlite3.Connection, row: dict) -> None:
    showid = row["showid"]
    showdate = row.get("showdate")
    venueid = row.get("venueid")
    tourid = first_key(row, "tourid", "tour_id")
    tour_name = first_key(row, "tour_name", "tourname")
    artistid = row.get("artistid")
    conn.execute(
        """
        INSERT INTO shows (showid, showdate, venueid, tourid, tour_name, artistid, exclude, show_index)
        VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
        ON CONFLICT(showid) DO UPDATE SET
            showdate = excluded.showdate,
            venueid = excluded.venueid,
            tourid = excluded.tourid,
            tour_name = excluded.tour_name,
            artistid = excluded.artistid
        """,
        (showid, showdate, venueid, tourid, tour_name, artistid),
    )


def _ingest_venues_master(
    conn: sqlite3.Connection, client: PhishNetClient, stats: IngestStats, force: bool
) -> None:
    try:
        rows = client.venues(force=force)
    except PhishNetError as exc:
        logger.warning("venues() failed, skipping venue master sync: %s", exc)
        return
    for row in rows:
        venueid = row.get("venueid")
        if venueid is None:
            logger.warning("venue row missing venueid, skipping: %r", row)
            continue
        name = first_key(row, "venuename", "venue", "name") or f"Venue {venueid}"
        city = row.get("city")
        state = row.get("state")
        country = row.get("country")
        alias = row.get("alias") or 0
        conn.execute(
            """
            INSERT INTO venues (venueid, name, city, state, country, alias)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(venueid) DO UPDATE SET
                name = excluded.name, city = excluded.city,
                state = excluded.state, country = excluded.country,
                alias = excluded.alias
            """,
            (venueid, name, city, state, country, alias),
        )
        stats.venues_upserted += 1
    conn.commit()


def _ingest_songs_master(
    conn: sqlite3.Connection, client: PhishNetClient, stats: IngestStats, force: bool
) -> None:
    """Upsert the song catalog. NOTE: the /songs endpoint carries an `artist`
    field (original artist string, e.g. "Prince") but no boolean/int
    is_original flag — per live API discovery, `songs.is_original` must
    instead be aggregated from the `is_original` field on setlist rows (see
    `_process_performance_row`), so this function deliberately never writes
    that column (omitted from both INSERT and UPDATE clauses)."""
    try:
        rows = client.songs(force=force)
    except PhishNetError as exc:
        logger.warning("songs() failed, skipping song master sync: %s", exc)
        return
    for row in rows:
        songid = row.get("songid")
        if songid is None:
            logger.warning("song row missing songid, skipping: %r", row)
            continue
        name = first_key(row, "song", "name") or f"Song {songid}"
        slug = row.get("slug") or _slugify(name)
        debut = first_key(row, "debut", "debut_date")
        times_played = row.get("times_played")
        conn.execute(
            """
            INSERT INTO songs (songid, slug, name, debut_date, times_played)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(songid) DO UPDATE SET
                slug = excluded.slug, name = excluded.name,
                debut_date = excluded.debut_date,
                times_played = excluded.times_played
            """,
            (songid, slug, name, debut, times_played),
        )
        stats.songs_upserted += 1
    conn.commit()


def _process_performance_row(
    conn: sqlite3.Connection,
    row: dict,
    stats: IngestStats,
    showdate_to_id: dict[str, int],
    position_counters: dict[int, int],
) -> None:
    if not _is_phish_row(row):
        stats.non_phish_performances_skipped += 1
        return

    showid = row.get("showid")
    if showid is None:
        showid = showdate_to_id.get(row.get("showdate"))
    if showid is None:
        logger.warning("setlist row has no resolvable showid, skipping: %r", row)
        return

    songid = row.get("songid")
    if songid is None:
        # Contract: songid is assumed present on every setlist row; if it's
        # ever missing that's a live-data assumption violated, so fail loudly
        # rather than silently dropping a performance.
        logger.error("setlist row missing songid for showid=%s: %r", showid, row)
        raise ValueError(f"setlist row missing songid (showid={showid}): {row!r}")

    name = row.get("song")
    slug = row.get("slug") or (_slugify(name) if name else None)
    if slug:
        # Defensive stub in case /songs doesn't (yet) know this song; the
        # master sync from _ingest_songs_master always wins on conflict.
        conn.execute(
            "INSERT INTO songs (songid, slug, name) VALUES (?, ?, ?) "
            "ON CONFLICT(songid) DO NOTHING",
            (songid, slug, name or slug),
        )
    else:
        logger.warning("setlist row missing both slug and song name for songid=%s", songid)

    # is_original lives on setlist rows (0/1), NOT on the /songs endpoint
    # (confirmed via live API discovery). Aggregate as max-per-songid so a
    # single 1 anywhere in the song's history marks it original.
    row_is_original = row.get("is_original")
    if row_is_original is not None:
        conn.execute(
            "UPDATE songs SET is_original = MAX(COALESCE(is_original, -1), ?) WHERE songid = ?",
            (int(row_is_original), songid),
        )

    _maybe_insert_fallback_venue(conn, row)

    # Setlist rows carry their own `exclude` flag (e.g. soundcheck snippets,
    # notes rows); such rows are not real performances and must not be
    # inserted, nor count toward "this show has a performance".
    if row.get("exclude"):
        stats.excluded_performance_rows_skipped += 1
        return

    set_label = row.get("set")
    position = row.get("position")
    if position is None:
        position = position_counters.get(showid, 0)
        logger.info(
            "setlist row missing position for showid=%s songid=%s, using counter %d",
            showid, songid, position,
        )
    position_counters[showid] = max(position_counters.get(showid, 0), position) + 1

    gap = row.get("gap")
    trans_mark = row.get("trans_mark")

    conn.execute(
        """
        INSERT INTO performances (showid, songid, set_label, position, gap, trans_mark)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(showid, songid, position) DO UPDATE SET
            set_label = excluded.set_label,
            gap = excluded.gap,
            trans_mark = excluded.trans_mark
        """,
        (showid, songid, set_label, position, gap, trans_mark),
    )
    stats.performances_inserted += 1


def _ingest_year(
    conn: sqlite3.Connection,
    client: PhishNetClient,
    year: int,
    stats: IngestStats,
    force: bool,
    showdate_to_id: dict[str, int],
    forced_exclude_showids: set[int],
) -> None:
    stats.years_processed += 1
    try:
        shows = client.shows_by_year(year, force=force)
    except PhishNetError as exc:
        logger.warning("shows/showyear/%s failed: %s", year, exc)
        return

    phish_shows_this_year: list[dict] = []
    for row in shows:
        stats.shows_seen += 1
        artist_name = str(row.get("artist_name") or "").strip().lower()
        artistid = row.get("artistid")

        if artist_name == "phish" and artistid is not None:
            if stats.observed_artistid is None:
                stats.observed_artistid = artistid
            elif stats.observed_artistid != artistid:
                logger.warning(
                    "conflicting phish artistid observed: had %s, saw %s",
                    stats.observed_artistid, artistid,
                )

        if not (artist_name == "phish" or artistid == 1):
            stats.non_phish_shows_skipped += 1
            continue

        showid = row.get("showid")
        if showid is None:
            logger.warning("show row missing showid, skipping: %r", row)
            continue

        # Venue must exist before the show row (FK), so insert the fallback
        # venue first — a no-op if venues() master data already covers it.
        _maybe_insert_fallback_venue(conn, row)
        _upsert_show(conn, row)
        # exclude_from_stats (confirmed via live API discovery, maps to our
        # `exclude` column) is an authoritative "don't use this show" signal
        # independent of whether it ends up with performances — e.g. an
        # officially-cancelled or asterisked show. Tracked separately so it
        # survives the performance-based exclude recompute at the end.
        if row.get("exclude_from_stats"):
            forced_exclude_showids.add(showid)
        if row.get("showdate"):
            showdate_to_id[row["showdate"]] = showid
        phish_shows_this_year.append(row)
        stats.shows_kept += 1

    try:
        setlist_rows = client.setlists_by_year(year, force=force)
    except PhishNetError as exc:
        logger.info(
            "setlists/showyear/%s failed (%s); falling back to per-showdate calls",
            year, exc,
        )
        stats.setlist_fallback_years += 1
        setlist_rows = []
        for show in phish_shows_this_year:
            showdate = show.get("showdate")
            if not showdate:
                continue
            try:
                setlist_rows.extend(client.setlists_by_showdate(showdate, force=force))
            except PhishNetError as exc2:
                logger.warning("setlists/showdate/%s failed: %s", showdate, exc2)

    position_counters: dict[int, int] = {}
    for prow in setlist_rows:
        _process_performance_row(conn, prow, stats, showdate_to_id, position_counters)

    conn.commit()


def _recompute_exclusions(conn: sqlite3.Connection, forced_exclude_showids: set[int]) -> None:
    """Rule 1: a past show with zero recorded performances is excluded,
    everything else defaults to included. Rule 2: showid's carrying an
    API-observed `exclude_from_stats` flag are force-excluded regardless of
    rule 1, and that fact is persisted in `meta` (as JSON) so it survives
    incremental refreshes that don't re-touch that show's year."""
    today = _today_str()

    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'forced_exclude_showids'"
    ).fetchone()
    persisted: set[int] = set()
    if row is not None and row["value"]:
        try:
            persisted = set(json.loads(row["value"]))
        except (ValueError, TypeError):
            persisted = set()
    merged = persisted | forced_exclude_showids
    _upsert_meta(conn, "forced_exclude_showids", json.dumps(sorted(merged)))

    conn.execute(
        """
        UPDATE shows
        SET exclude = CASE
            WHEN showdate < ?
                 AND NOT EXISTS (SELECT 1 FROM performances p WHERE p.showid = shows.showid)
            THEN 1
            ELSE 0
        END
        """,
        (today,),
    )

    if merged:
        placeholders = ",".join("?" for _ in merged)
        conn.execute(
            f"UPDATE shows SET exclude = 1 WHERE showid IN ({placeholders})",
            tuple(merged),
        )
    conn.commit()


def _finalize(
    conn: sqlite3.Connection, stats: IngestStats, forced_exclude_showids: set[int]
) -> IngestStats:
    _recompute_exclusions(conn, forced_exclude_showids)
    compute_show_indexes(conn)

    if stats.observed_artistid is not None:
        _upsert_meta(conn, "phish_artistid", str(stats.observed_artistid))
    _upsert_meta(conn, "last_refresh", _now_iso())
    conn.commit()

    stats.shows_excluded = conn.execute(
        "SELECT COUNT(*) FROM shows WHERE exclude = 1"
    ).fetchone()[0]
    conn.commit()
    return stats


# -- public entry points -------------------------------------------------


def full_ingest(
    conn: sqlite3.Connection,
    client: PhishNetClient,
    start_year: int = 1983,
    end_year: int | None = None,
    force: bool = False,
) -> IngestStats:
    stats = IngestStats()
    if end_year is None:
        end_year = datetime.date.today().year

    _ingest_venues_master(conn, client, stats, force=force)
    _ingest_songs_master(conn, client, stats, force=force)

    showdate_to_id: dict[str, int] = {}
    forced_exclude_showids: set[int] = set()
    for year in range(start_year, end_year + 1):
        _ingest_year(conn, client, year, stats, force, showdate_to_id, forced_exclude_showids)

    return _finalize(conn, stats, forced_exclude_showids)


def refresh(conn: sqlite3.Connection, client: PhishNetClient) -> IngestStats:
    stats = IngestStats()
    today = datetime.date.today()
    years_to_pull = {today.year}

    row = conn.execute("SELECT value FROM meta WHERE key = 'last_refresh'").fetchone()
    indexed_shows = conn.execute(
        "SELECT COUNT(*) FROM shows WHERE show_index IS NOT NULL"
    ).fetchone()[0]
    if row is None or not row["value"] or indexed_shows == 0:
        logger.info(
            "refresh: fresh or empty database detected (last_refresh=%s, "
            "indexed shows=%d) -- falling back to full ingest 1983..%d",
            row["value"] if row is not None else None, indexed_shows, today.year,
        )
        return full_ingest(conn, client)

    if row is not None and row["value"]:
        last_refresh_date: datetime.date | None
        try:
            last_refresh_date = datetime.datetime.fromisoformat(row["value"]).date()
        except ValueError:
            last_refresh_date = None
        if last_refresh_date is not None:
            cur = conn.execute(
                "SELECT DISTINCT showdate FROM shows WHERE showdate > ?",
                (last_refresh_date.isoformat(),),
            )
            for r in cur.fetchall():
                showdate = r["showdate"]
                if showdate:
                    try:
                        years_to_pull.add(int(showdate[:4]))
                    except ValueError:
                        pass

    showdate_to_id: dict[str, int] = {}
    forced_exclude_showids: set[int] = set()
    for year in sorted(years_to_pull):
        _ingest_year(
            conn, client, year, stats, force=True,
            showdate_to_id=showdate_to_id,
            forced_exclude_showids=forced_exclude_showids,
        )

    return _finalize(conn, stats, forced_exclude_showids)


def compute_show_indexes(conn: sqlite3.Connection) -> None:
    """Dense 0..N chronological ordinal over non-excluded, past, Phish shows
    that have at least one performance. Everything else gets show_index NULL."""
    today = _today_str()
    cur = conn.execute(
        """
        SELECT s.showid
        FROM shows s
        WHERE s.exclude = 0
          AND s.showdate <= ?
          AND EXISTS (SELECT 1 FROM performances p WHERE p.showid = s.showid)
        ORDER BY s.showdate, s.showid
        """,
        (today,),
    )
    ordered_showids = [r["showid"] for r in cur.fetchall()]

    conn.execute("UPDATE shows SET show_index = NULL")
    conn.executemany(
        "UPDATE shows SET show_index = ? WHERE showid = ?",
        [(idx, showid) for idx, showid in enumerate(ordered_showids)],
    )
    conn.commit()
