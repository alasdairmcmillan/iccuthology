"""Feature engineering — chronological sweep. See CONTRACTS.md.

Constants are final; function bodies implemented here.

The whole product depends on there being NO LEAKAGE: every feature for show T is
computed from state accumulated over shows with a strictly smaller show_index. We
do this with a single chronological sweep that, at each show, emits candidate rows
using the running state *before* applying that show's setlist, and only then folds
the show into the state.
"""
from __future__ import annotations

import sqlite3
import statistics
from bisect import bisect_left
from collections import defaultdict

import pandas as pd

from .config import era_for_year

ID_COLUMNS = [
    "showid", "showdate", "show_index", "venueid", "songid", "slug", "song_name", "y",
]
FEATURE_COLUMNS = [
    "decayed_rate", "gap", "gap_ratio", "played_prev_show", "played_in_run",
    "venue_gap", "plays_this_tour", "plays_last_10", "plays_last_50",
    "song_age_shows", "era_rate", "is_original",
]

VENUE_GAP_SENTINEL = 999

_ALL_COLUMNS = ID_COLUMNS + FEATURE_COLUMNS

# Windows for plays_last_N.
_WINDOW_10 = 10
_WINDOW_50 = 50
# Candidate-set thresholds.
_RECENT_WINDOW = 300
_BUSTOUT_PLAYS = 20


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_shows(conn: sqlite3.Connection, *, indexed_only: bool):
    """Shows with venue resolved to its canonical id (alias-aware).

    Renamed venues are distinct venueids linked by ``venues.alias`` (0 = self,
    else the canonical venueid). We resolve to the canonical id so that venue_gap
    and run detection treat all aliases of a venue as one place. If the venues
    row is missing we fall back to the show's own venueid.
    """
    where = "s.exclude = 0"
    if indexed_only:
        where += " AND s.show_index IS NOT NULL"
    sql = (
        "SELECT s.showid AS showid, s.showdate AS showdate, s.show_index AS show_index, "
        "s.tourid AS tourid, "
        "COALESCE(NULLIF(v.alias, 0), s.venueid) AS venueid "
        "FROM shows s LEFT JOIN venues v ON v.venueid = s.venueid "
        f"WHERE {where} "
        "ORDER BY s.show_index, s.showdate, s.showid"
    )
    return conn.execute(sql).fetchall()


def _load_setlists(conn: sqlite3.Connection) -> dict[int, set[int]]:
    setlists: dict[int, set[int]] = defaultdict(set)
    for row in conn.execute("SELECT showid, songid FROM performances"):
        setlists[row["showid"]].add(row["songid"])
    return setlists


def _load_songs(conn: sqlite3.Connection) -> dict[int, tuple[str, str, float]]:
    meta: dict[int, tuple[str, str, float]] = {}
    for row in conn.execute("SELECT songid, slug, name, is_original FROM songs"):
        iso = 0.5 if row["is_original"] is None else float(row["is_original"])
        meta[row["songid"]] = (row["slug"], row["name"], iso)
    return meta


# ---------------------------------------------------------------------------
# Sweep state
# ---------------------------------------------------------------------------

class _State:
    """Running per-song / per-venue / per-era state built up over the sweep.

    ``r`` is the per-show decay factor 0.5 ** (1 / half_life). The decayed_rate
    numerator per song is kept lazily: ``num[s]`` is Sum over past plays i of
    r ** (last_played[s] - i), i.e. normalized to the song's last play index.
    Scaling to any later index t is a single ``num[s] * r ** (t - last_played[s])``.
    """

    def __init__(self, songs_meta: dict[int, tuple[str, str, float]], r: float):
        self.songs_meta = songs_meta
        self.r = r
        self.ever_played: set[int] = set()
        self.last_played: dict[int, int] = {}
        self.first_play: dict[int, int] = {}
        self.plays: dict[int, int] = defaultdict(int)
        self.play_indexes: dict[int, list[int]] = defaultdict(list)
        self.gaps: dict[int, list[int]] = defaultdict(list)
        self.median_gap: dict[int, float] = {}
        self.num: dict[int, float] = {}
        self.venue_show_count: dict[int, int] = defaultdict(int)
        self.venue_last_ordinal: dict[tuple[int, int], int] = {}
        self.tour_play_count: dict[tuple[int, int], int] = defaultdict(int)
        self.era_song_plays: dict[tuple[str, int], int] = defaultdict(int)
        self.era_show_count: dict[str, int] = defaultdict(int)

    def apply_show(self, t: int, venueid: int, tourid, era: str, setlist: set[int]) -> None:
        """Fold show T (index t) into the state. Call AFTER emitting T's rows."""
        r = self.r
        self.era_show_count[era] += 1
        self.venue_show_count[venueid] += 1
        ordv = self.venue_show_count[venueid]
        for s in setlist:
            lp = self.last_played.get(s)
            if lp is None:
                self.num[s] = 1.0
                self.first_play[s] = t
            else:
                self.num[s] = self.num[s] * (r ** (t - lp)) + 1.0
                g = t - lp
                self.gaps[s].append(g)
                self.median_gap[s] = statistics.median(self.gaps[s])
            self.plays[s] += 1
            self.play_indexes[s].append(t)
            self.last_played[s] = t
            self.ever_played.add(s)
            self.venue_last_ordinal[(venueid, s)] = ordv
            if tourid is not None:
                self.tour_play_count[(tourid, s)] += 1
            self.era_song_plays[(era, s)] += 1

    def emit(self, cols: dict[str, list], *, index: int, showid: int, showdate: str,
             venueid: int, tourid, era: str, run_start_index, D: float,
             y_setlist: set[int] | None) -> None:
        """Append one candidate row per eligible song for the show at ``index``.

        ``y_setlist`` = the show's distinct songids for a training show (y in {0,1}),
        or ``None`` for a future show (y = NaN).
        """
        r = self.r
        meta = self.songs_meta
        n_v = self.venue_show_count.get(venueid, 0)
        era_shows_prior = self.era_show_count.get(era, 0)
        era_denom = era_shows_prior if era_shows_prior > 0 else 1
        run_active = run_start_index is not None and index > run_start_index

        for s in sorted(self.ever_played):
            lp = self.last_played[s]
            gap = index - lp
            pcount = self.plays[s]
            if gap > _RECENT_WINDOW and pcount < _BUSTOUT_PLAYS:
                continue

            numv = self.num[s] * (r ** (index - lp))
            decayed = numv / D if D > 0 else 0.0

            if self.gaps[s]:
                gap_ratio = gap / self.median_gap[s]
            else:
                gap_ratio = 1.0

            played_prev = 1 if gap == 1 else 0
            played_run = 1 if (run_active and lp >= run_start_index) else 0

            k = self.venue_last_ordinal.get((venueid, s))
            venue_gap = (n_v - k) if k is not None else VENUE_GAP_SENTINEL

            plays_tour = self.tour_play_count.get((tourid, s), 0) if tourid is not None else 0

            pl = self.play_indexes[s]
            plays10 = len(pl) - bisect_left(pl, index - _WINDOW_10)
            plays50 = len(pl) - bisect_left(pl, index - _WINDOW_50)

            age = index - self.first_play[s]
            era_rate = self.era_song_plays.get((era, s), 0) / era_denom
            slug, name, iso = meta.get(s, (str(s), str(s), 0.5))
            y = float("nan") if y_setlist is None else (1 if s in y_setlist else 0)

            cols["showid"].append(showid)
            cols["showdate"].append(showdate)
            cols["show_index"].append(index)
            cols["venueid"].append(venueid)
            cols["songid"].append(s)
            cols["slug"].append(slug)
            cols["song_name"].append(name)
            cols["y"].append(y)
            cols["decayed_rate"].append(decayed)
            cols["gap"].append(gap)
            cols["gap_ratio"].append(gap_ratio)
            cols["played_prev_show"].append(played_prev)
            cols["played_in_run"].append(played_run)
            cols["venue_gap"].append(venue_gap)
            cols["plays_this_tour"].append(plays_tour)
            cols["plays_last_10"].append(plays10)
            cols["plays_last_50"].append(plays50)
            cols["song_age_shows"].append(age)
            cols["era_rate"].append(era_rate)
            cols["is_original"].append(iso)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: [] for c in _ALL_COLUMNS})[_ALL_COLUMNS]


def _new_cols() -> dict[str, list]:
    return {c: [] for c in _ALL_COLUMNS}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_features(conn: sqlite3.Connection, half_life: int = 50) -> pd.DataFrame:
    """One chronological sweep over non-excluded, indexed shows.

    One row per (candidate song, show); columns = ID_COLUMNS + FEATURE_COLUMNS.
    y = 1 iff the song was played at that show. Every feature is leakage-free:
    it depends only on shows with a smaller show_index.
    """
    r = 0.5 ** (1.0 / half_life)
    shows = _load_shows(conn, indexed_only=True)
    if not shows:
        return _empty_frame()
    setlists = _load_setlists(conn)
    songs_meta = _load_songs(conn)

    state = _State(songs_meta, r)
    cols = _new_cols()

    prev_t: int | None = None
    prev_venue: int | None = None
    D = 0.0
    run_start = None

    for show in shows:
        t = show["show_index"]
        venueid = show["venueid"]
        tourid = show["tourid"]
        showdate = show["showdate"]
        era = era_for_year(int(showdate[:4]))
        setlist = setlists.get(show["showid"], set())

        if prev_t is None:
            D = 0.0
            run_start = t
        else:
            D = (r ** (t - prev_t)) * (D + 1.0)
            if t == prev_t + 1 and venueid == prev_venue:
                pass  # same run continues
            else:
                run_start = t

        state.emit(cols, index=t, showid=show["showid"], showdate=showdate,
                   venueid=venueid, tourid=tourid, era=era, run_start_index=run_start,
                   D=D, y_setlist=setlist)
        state.apply_show(t, venueid, tourid, era, setlist)

        prev_t = t
        prev_venue = venueid

    df = pd.DataFrame(cols)[_ALL_COLUMNS]
    return df


def features_for_future_show(
    conn: sqlite3.Connection, showid: int, half_life: int = 50
) -> pd.DataFrame:
    """Candidate rows (y = NaN) for a future show already present in ``shows``.

    All ingested indexed shows form the history. The effective show_index is
    max_index + 1 + the target's rank among not-yet-indexed shows dated after the
    last indexed show (ordered by showdate, showid). Run context: the contiguous
    block of same-venue shows immediately preceding the target (by calendar order)
    is treated as one run, so played_in_run / played_prev_show fire from the
    already-played (indexed) nights of that run.
    """
    r = 0.5 ** (1.0 / half_life)
    target = conn.execute(
        "SELECT s.showid AS showid, s.showdate AS showdate, s.tourid AS tourid, "
        "COALESCE(NULLIF(v.alias, 0), s.venueid) AS venueid "
        "FROM shows s LEFT JOIN venues v ON v.venueid = s.venueid "
        "WHERE s.showid = ?",
        (showid,),
    ).fetchone()
    if target is None:
        return _empty_frame()

    shows = _load_shows(conn, indexed_only=True)
    if not shows:
        return _empty_frame()
    setlists = _load_setlists(conn)
    songs_meta = _load_songs(conn)

    # Sweep all indexed shows to build final state; track denominator/last index.
    state = _State(songs_meta, r)
    prev_t: int | None = None
    D = 0.0
    max_index = 0
    for show in shows:
        t = show["show_index"]
        era = era_for_year(int(show["showdate"][:4]))
        setlist = setlists.get(show["showid"], set())
        if prev_t is None:
            D = 0.0
        else:
            D = (r ** (t - prev_t)) * (D + 1.0)
        state.apply_show(t, show["venueid"], show["tourid"], era, setlist)
        prev_t = t
        max_index = t
    # D now holds the denominator evaluated at the last indexed show.

    max_indexed_date = conn.execute(
        "SELECT showdate FROM shows WHERE show_index IS NOT NULL AND exclude = 0 "
        "ORDER BY show_index DESC LIMIT 1"
    ).fetchone()["showdate"]

    future_ids = [
        row["showid"]
        for row in conn.execute(
            "SELECT showid FROM shows WHERE show_index IS NULL AND exclude = 0 "
            "AND showdate > ? ORDER BY showdate, showid",
            (max_indexed_date,),
        )
    ]
    rank = future_ids.index(showid) if showid in future_ids else 0
    eff_index = max_index + 1 + rank

    D_eff = (r ** (eff_index - max_index)) * (D + 1.0)

    run_start = _future_run_start(conn, showid, target["venueid"])

    era = era_for_year(int(target["showdate"][:4]))
    cols = _new_cols()
    state.emit(cols, index=eff_index, showid=showid, showdate=target["showdate"],
               venueid=target["venueid"], tourid=target["tourid"], era=era,
               run_start_index=run_start, D=D_eff, y_setlist=None)
    return pd.DataFrame(cols)[_ALL_COLUMNS]


def _future_run_start(conn: sqlite3.Connection, target_showid: int, target_venueid: int):
    """Show_index of the earliest indexed show in the target's run, else None.

    The run is the contiguous block of shows at the same (canonical) venue that
    immediately precedes the target in calendar order. Only indexed members of
    that block can supply played_in_run (unplayed future nights carry no setlist).
    """
    rows = conn.execute(
        "SELECT s.showid AS showid, s.show_index AS show_index, "
        "COALESCE(NULLIF(v.alias, 0), s.venueid) AS venueid "
        "FROM shows s LEFT JOIN venues v ON v.venueid = s.venueid "
        "WHERE s.exclude = 0 ORDER BY s.showdate, s.showid"
    ).fetchall()
    pos = next((i for i, rw in enumerate(rows) if rw["showid"] == target_showid), None)
    if pos is None:
        return None
    indexed_member_indexes: list[int] = []
    j = pos - 1
    while j >= 0 and rows[j]["venueid"] == target_venueid:
        if rows[j]["show_index"] is not None:
            indexed_member_indexes.append(rows[j]["show_index"])
        j -= 1
    return min(indexed_member_indexes) if indexed_member_indexes else None


def mean_setlist_size(conn: sqlite3.Connection, era: str | None = None) -> float:
    """Mean number of DISTINCT songids per non-excluded indexed show (= K).

    Optionally restricted to shows whose showdate year falls in ``era``.
    """
    rows = conn.execute(
        "SELECT s.showdate AS showdate, COUNT(DISTINCT p.songid) AS cnt "
        "FROM shows s JOIN performances p ON p.showid = s.showid "
        "WHERE s.exclude = 0 AND s.show_index IS NOT NULL "
        "GROUP BY s.showid"
    ).fetchall()
    sizes = [
        row["cnt"]
        for row in rows
        if era is None or era_for_year(int(row["showdate"][:4])) == era
    ]
    if not sizes:
        return 0.0
    return sum(sizes) / len(sizes)
