"""Prediction modes 1 (tour), 2 (run), 4 (chaser) — thin reductions over the
forward Monte-Carlo simulator in ``simulate.py``. See
phish-predictor-modes-plan.md sections 2, 3, 5 and CONTRACTS.md.

Mode 3 (single show) already lives in ``predict.py``; mode 5 (ordered setlist)
is future work (plan §6). Every function here either resolves a horizon/run/
song into showids/songid (the ``resolve_*`` helpers) or reduces a
``simulate.simulate_horizon`` result into a report dataclass with a
``.render(json_out=False)`` method that mirrors ``predict.render_prediction``:
a header line, then a ``rich`` ASCII table (so redirected output survives
Windows cp1252), or a JSON string via ``dataclasses.asdict``.
"""
from __future__ import annotations

import io
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date

from rich import box
from rich.console import Console
from rich.table import Table

from . import features
from .config import era_for_year
from .models.heuristic import heuristic_predict
from .simulate import SimConfig, SimResult, simulate_horizon

# ---------------------------------------------------------------------------
# Tunable thresholds (documented here for CONTRACTS.md / CLI help text).
# ---------------------------------------------------------------------------

LOCK_THRESHOLD = 0.9          # P(>=1 play) >= this -> "lock"
LIKELY_THRESHOLD = 0.5        # P(>=1 play) >= this (and < LOCK) -> "likely"
BUSTOUT_GAP_RATIO_THRESHOLD = 2.0  # below LIKELY, gap_ratio >= this -> "bustout-watch"
# else -> "longshot"

LOW_SIGNAL_PLAY_COUNT = 20    # historical plays below this -> chaser confidence caveat
# (matches features._BUSTOUT_PLAYS: the same "few training signals" bar the
# candidate-set logic already uses to keep rare songs in play.)


# ---------------------------------------------------------------------------
# JSON rendering helper
# ---------------------------------------------------------------------------

def _round_floats(obj, ndigits: int = 4):
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _new_console() -> Console:
    # Render into a buffer only -- callers decide where the text goes. Same
    # convention as predict.render_prediction.
    return Console(record=True, width=120, file=io.StringIO())


# ---------------------------------------------------------------------------
# Shared resolution helpers
# ---------------------------------------------------------------------------

def resolve_tour_horizon(
    conn: sqlite3.Connection, *, tour: str | None = None, year: int | None = None
) -> list[int]:
    """Ordered future showids for tour/chaser horizons (plan §9: default =
    rest-of-calendar-year, with an option to select a named tour).

    Starts from ``features.future_show_ids`` (show_index NULL, exclude=0,
    dated after the last indexed show; ordered by showdate, showid -- the
    exact ordering ``simulate.simulate_horizon`` expects and that
    ``features_for_future_show`` uses for effective-index ranking).

    - ``tour=None``: keep future shows whose showdate year equals ``year``
      (default: today's calendar year).
    - ``tour=<substring>``: keep future shows whose ``shows.tour_name`` matches
      case-insensitively (substring), ignoring ``year``.
    """
    future_ids = features.future_show_ids(conn)
    if not future_ids:
        return []

    placeholders = ",".join("?" for _ in future_ids)
    rows = conn.execute(
        f"SELECT showid, showdate, tour_name FROM shows WHERE showid IN ({placeholders})",
        future_ids,
    ).fetchall()
    meta = {row["showid"]: row for row in rows}

    target_year = year if year is not None else date.today().year
    needle = tour.lower() if tour else None

    out: list[int] = []
    for showid in future_ids:  # already ordered by (showdate, showid)
        row = meta.get(showid)
        if row is None:
            continue
        if needle is not None:
            tour_name = (row["tour_name"] or "").lower()
            if needle not in tour_name:
                continue
        else:
            year_of = int(str(row["showdate"])[:4])
            if year_of != target_year:
                continue
        out.append(showid)
    return out


def resolve_run(
    conn: sqlite3.Connection,
    *,
    venue: str | None = None,
    nights: int | None = None,
    dates: list[str] | None = None,
) -> list[int]:
    """Ordered future showids for a multi-night run (plan §3).

    Either pass explicit ``dates`` (yyyy-mm-dd strings), or ``venue`` (+
    optional ``nights``, default 3) to take the next N future shows whose
    venue name or city matches (case-insensitive substring), in calendar
    order -- normally contiguous by date since that's what a "run" is, but
    this does not hard-validate contiguity (off-day gaps in real tour
    schedules are common and still form one logical run booking-wise).

    Restricted to the same future-show universe as ``resolve_tour_horizon``
    so results are always valid ``simulate_horizon`` horizons.
    """
    future_ids = features.future_show_ids(conn)
    if not future_ids:
        return []

    placeholders = ",".join("?" for _ in future_ids)
    rows = conn.execute(
        "SELECT s.showid AS showid, s.showdate AS showdate, "
        "v.name AS venue_name, v.city AS city "
        "FROM shows s LEFT JOIN venues v ON v.venueid = s.venueid "
        f"WHERE s.showid IN ({placeholders})",
        future_ids,
    ).fetchall()
    meta = {row["showid"]: row for row in rows}
    ordered = [meta[sid] for sid in future_ids if sid in meta]

    if dates:
        wanted = set(dates)
        return [row["showid"] for row in ordered if row["showdate"] in wanted]

    if not venue:
        raise ValueError("resolve_run requires either `dates` or `venue` (+ optional `nights`)")

    n = nights if nights is not None else 3
    needle = venue.lower()
    matched = [
        row for row in ordered
        if needle in (row["venue_name"] or "").lower() or needle in (row["city"] or "").lower()
    ]
    return [row["showid"] for row in matched[:n]]


def resolve_song(conn: sqlite3.Connection, query: str) -> tuple[int, str, str]:
    """Resolve a song query to (songid, slug, name).

    Exact (case-insensitive) slug match wins outright. Otherwise a
    case-insensitive substring match against name OR slug; exactly one hit
    resolves, zero or multiple raise ``ValueError`` (multiple lists up to 15
    candidate names).
    """
    q = query.strip()
    if not q:
        raise ValueError("song query must not be empty")

    row = conn.execute(
        "SELECT songid, slug, name FROM songs WHERE LOWER(slug) = LOWER(?)", (q,)
    ).fetchone()
    if row is not None:
        return int(row["songid"]), row["slug"], row["name"]

    like = f"%{q.lower()}%"
    rows = conn.execute(
        "SELECT songid, slug, name FROM songs WHERE LOWER(name) LIKE ? OR LOWER(slug) LIKE ? "
        "ORDER BY name",
        (like, like),
    ).fetchall()
    if len(rows) == 1:
        r = rows[0]
        return int(r["songid"]), r["slug"], r["name"]
    if not rows:
        raise ValueError(f"No song found matching {query!r}")
    candidates = ", ".join(r["name"] for r in rows[:15])
    raise ValueError(f"Ambiguous song {query!r}; candidates: {candidates}")


# ---------------------------------------------------------------------------
# Shared MC reduction helpers
# ---------------------------------------------------------------------------

def _per_song_counts(samples: list[list[set[int]]]) -> dict[int, list[int]]:
    """songid -> per-sim play counts across the whole horizon (len == n_sims)."""
    n = len(samples)
    counts: dict[int, list[int]] = defaultdict(lambda: [0] * n)
    for m, sim in enumerate(samples):
        for step_set in sim:
            for songid in step_set:
                counts[songid][m] += 1
    return counts


# ---------------------------------------------------------------------------
# Mode 1 -- Tour
# ---------------------------------------------------------------------------

@dataclass
class TourSongRow:
    song: str
    slug: str
    expected_plays: float
    p_at_least_one: float
    dist: dict[str, float]          # "0","1","2","3+" -> P(exactly n) over the horizon
    bucket: str                     # lock | likely | longshot | bustout-watch
    gap_ratio: float | None         # from the first horizon show's candidate frame
    analytic_p: float               # Sigma over horizon shows of heuristic marginal P(song, show)


@dataclass
class TourReport:
    horizon_showids: list[int]
    horizon_dates: list[str]
    model: str
    n_sims: int
    half_life: int
    rows: list[TourSongRow] = field(default_factory=list)

    def render(self, json_out: bool = False) -> str:
        if json_out:
            return json.dumps(_round_floats(asdict(self)))

        console = _new_console()
        start = self.horizon_dates[0] if self.horizon_dates else "?"
        end = self.horizon_dates[-1] if self.horizon_dates else "?"
        header = (
            f"TOUR {start}..{end} ({len(self.horizon_showids)} shows) | "
            f"model={self.model}  n_sims={self.n_sims}  half_life={self.half_life}"
        )
        console.print(header)
        console.print(
            "Analytic column = Sigma of per-show heuristic marginals (approximation; "
            "over-counts frequent songs -- MC columns are the headline)."
        )

        table = Table(box=box.ASCII)
        table.add_column("Song")
        table.add_column("Expected", justify="right")
        table.add_column("P(>=1)", justify="right")
        table.add_column("Bucket")
        table.add_column("Dist 0/1/2/3+")
        table.add_column("Analytic", justify="right")
        for row in self.rows:
            dist_str = (
                f"{row.dist['0'] * 100:.0f}/{row.dist['1'] * 100:.0f}/"
                f"{row.dist['2'] * 100:.0f}/{row.dist['3+'] * 100:.0f}%"
            )
            table.add_row(
                row.song,
                f"{row.expected_plays:.2f}",
                f"{row.p_at_least_one * 100:.1f}%",
                row.bucket,
                dist_str,
                f"{row.analytic_p:.2f}",
            )
        console.print(table)
        return console.export_text()


def _bucket_for(p_at_least_one: float, gap_ratio: float | None) -> str:
    if p_at_least_one >= LOCK_THRESHOLD:
        return "lock"
    if p_at_least_one >= LIKELY_THRESHOLD:
        return "likely"
    if gap_ratio is not None and gap_ratio >= BUSTOUT_GAP_RATIO_THRESHOLD:
        return "bustout-watch"
    return "longshot"


def tour_mode(
    conn: sqlite3.Connection,
    horizon_showids: list[int],
    config: SimConfig | None = None,
    *,
    result: SimResult | None = None,
) -> TourReport:
    """Mode 1 (plan §2): per-song expected plays, P(>=1 play), play-count
    distribution, and a lock/likely/longshot/bustout-watch bucket, reduced
    over ``simulate_horizon``'s Monte-Carlo samples. Also computes the plan's
    fast analytic sanity check (Sigma of per-show heuristic marginals -- an
    approximation that over-counts frequent songs since it ignores rotation
    cooldown; MC is the headline number).

    Pass ``result`` (a precomputed ``SimResult`` over the same horizon) to reuse
    an existing simulation instead of running a fresh one -- lets ``publish``
    derive the tour table and the raw ``samples.bin`` from a SINGLE simulate run
    per epoch (deploy plan §3 "compute the sims once, reduce many ways"). When
    ``result`` is supplied its own ``config`` is authoritative, so the report's
    model/n_sims/half_life metadata can never disagree with the samples.
    """
    if result is None:
        config = config or SimConfig()
        result = simulate_horizon(conn, horizon_showids, config)
    else:
        config = result.config
    n = len(result.samples)
    counts = _per_song_counts(result.samples)

    analytic: dict[int, float] = defaultdict(float)
    for showid in result.horizon_showids:
        feat_df = features.features_for_future_show(conn, showid, config.half_life)
        if feat_df.empty:
            continue
        year = int(str(feat_df["showdate"].iloc[0])[:4])
        k = features.mean_setlist_size(conn, era_for_year(year))
        pred_df = heuristic_predict(feat_df, k)
        for songid, p in zip(pred_df["songid"], pred_df["prob"]):
            analytic[int(songid)] += float(p)

    gap_ratios: dict[int, float] = {}
    if result.horizon_showids:
        first_feat = features.features_for_future_show(
            conn, result.horizon_showids[0], config.half_life
        )
        for songid, gr in zip(first_feat["songid"], first_feat["gap_ratio"]):
            gap_ratios[int(songid)] = float(gr)

    rows: list[TourSongRow] = []
    for songid in set(counts) | set(analytic):
        c = counts.get(songid, [0] * n)
        expected = (sum(c) / n) if n else 0.0
        p_ge1 = (sum(1 for x in c if x >= 1) / n) if n else 0.0
        dist = {
            "0": (sum(1 for x in c if x == 0) / n) if n else 0.0,
            "1": (sum(1 for x in c if x == 1) / n) if n else 0.0,
            "2": (sum(1 for x in c if x == 2) / n) if n else 0.0,
            "3+": (sum(1 for x in c if x >= 3) / n) if n else 0.0,
        }
        gr = gap_ratios.get(songid)
        slug, name = result.songs_meta.get(songid, (str(songid), str(songid)))
        rows.append(
            TourSongRow(
                song=name, slug=slug, expected_plays=expected, p_at_least_one=p_ge1,
                dist=dist, bucket=_bucket_for(p_ge1, gr), gap_ratio=gr,
                analytic_p=analytic.get(songid, 0.0),
            )
        )

    rows.sort(key=lambda r: r.expected_plays, reverse=True)
    return TourReport(
        horizon_showids=result.horizon_showids, horizon_dates=result.horizon_dates,
        model=config.model, n_sims=config.n_sims, half_life=config.half_life, rows=rows,
    )


# ---------------------------------------------------------------------------
# Mode 2 -- Run
# ---------------------------------------------------------------------------

@dataclass
class RunSongRow:
    song: str
    slug: str
    p_at_least_one: float           # P(song appears on >=1 night of the run)
    per_night_probs: list[float]    # parallel to run_dates
    most_likely_night_index: int | None
    most_likely_night_date: str | None


@dataclass
class RunReport:
    run_showids: list[int]
    run_dates: list[str]
    model: str
    n_sims: int
    strict_no_repeat: bool
    rows: list[RunSongRow] = field(default_factory=list)

    def render(self, json_out: bool = False) -> str:
        if json_out:
            return json.dumps(_round_floats(asdict(self)))

        console = _new_console()
        start = self.run_dates[0] if self.run_dates else "?"
        end = self.run_dates[-1] if self.run_dates else "?"
        header = (
            f"RUN {start}..{end} ({len(self.run_showids)} nights) | "
            f"model={self.model}  n_sims={self.n_sims}  strict_no_repeat={self.strict_no_repeat}"
        )
        console.print(header)

        table = Table(box=box.ASCII)
        table.add_column("Song")
        table.add_column("P(>=1 in run)", justify="right")
        table.add_column("Most likely night")
        table.add_column("Per-night %")
        for row in self.rows:
            per_night_str = " ".join(
                f"N{i + 1}:{p * 100:.0f}%" for i, p in enumerate(row.per_night_probs)
            )
            table.add_row(
                row.song,
                f"{row.p_at_least_one * 100:.1f}%",
                row.most_likely_night_date or "-",
                per_night_str,
            )
        console.print(table)
        return console.export_text()


def run_mode(
    conn: sqlite3.Connection, run_showids: list[int], config: SimConfig | None = None
) -> RunReport:
    """Mode 2 (plan §3): per-song P(hear >=1 in the run), most-likely night,
    and per-night probabilities, reduced over a joint simulation of the run's
    shows. ``SimConfig.strict_no_repeat`` defaults to ``True`` (plan §9: hard
    mask default, soft as a flag) so a song sampled one night is masked out of
    later nights within the same run.

    This is deliberately a *joint* reduction (P(>=1) computed per-simulation
    as a union across nights), not a sum of the per-night marginals, which
    would triple-count a song like Harry Hood across a 3-night run.
    """
    config = config or SimConfig()
    result: SimResult = simulate_horizon(conn, run_showids, config)
    n = len(result.samples)
    n_nights = len(result.horizon_showids)

    night_hits: dict[int, list[int]] = defaultdict(lambda: [0] * n_nights)
    union_hits: dict[int, int] = defaultdict(int)
    for sim in result.samples:
        seen: set[int] = set()
        for t, step_set in enumerate(sim):
            for songid in step_set:
                night_hits[songid][t] += 1
                seen.add(songid)
        for songid in seen:
            union_hits[songid] += 1

    rows: list[RunSongRow] = []
    for songid, hits in night_hits.items():
        per_night = [h / n for h in hits] if n else [0.0] * n_nights
        p_ge1 = (union_hits.get(songid, 0) / n) if n else 0.0
        best_t = max(range(n_nights), key=lambda t: per_night[t]) if n_nights else None
        slug, name = result.songs_meta.get(songid, (str(songid), str(songid)))
        rows.append(
            RunSongRow(
                song=name, slug=slug, p_at_least_one=p_ge1, per_night_probs=per_night,
                most_likely_night_index=best_t,
                most_likely_night_date=(
                    result.horizon_dates[best_t] if best_t is not None else None
                ),
            )
        )

    rows.sort(key=lambda r: r.p_at_least_one, reverse=True)
    return RunReport(
        run_showids=result.horizon_showids, run_dates=result.horizon_dates,
        model=config.model, n_sims=config.n_sims, strict_no_repeat=config.strict_no_repeat,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Mode 4 -- Chaser
# ---------------------------------------------------------------------------

@dataclass
class ChaserShowProb:
    showid: int
    showdate: str
    probability: float   # P(this show is the FIRST horizon show with the song)


@dataclass
class ChaserReport:
    song: str
    slug: str
    horizon_showids: list[int]
    horizon_dates: list[str]
    model: str
    n_sims: int
    p_not_within_horizon: float
    modal_show_date: str | None
    median_show_date: str | None
    expected_shows_until_next_play: float | None
    historical_play_count: int
    low_signal_caveat: bool
    distribution: list[ChaserShowProb] = field(default_factory=list)

    def render(self, json_out: bool = False) -> str:
        if json_out:
            return json.dumps(_round_floats(asdict(self)))

        console = _new_console()
        start = self.horizon_dates[0] if self.horizon_dates else "?"
        end = self.horizon_dates[-1] if self.horizon_dates else "?"
        header = (
            f"CHASER: {self.song} | horizon {start}..{end} "
            f"({len(self.horizon_showids)} shows) | model={self.model}  n_sims={self.n_sims}"
        )
        console.print(header)
        console.print(
            f"Modal next show: {self.modal_show_date or 'n/a'}  |  "
            f"Median next show: {self.median_show_date or 'n/a'}  |  "
            f"P(not within horizon): {self.p_not_within_horizon * 100:.1f}%  |  "
            f"Expected shows until next play "
            f"(mean over sims that hit; misses reported separately above): "
            f"{'n/a' if self.expected_shows_until_next_play is None else f'{self.expected_shows_until_next_play:.2f}'}"
        )
        if self.low_signal_caveat:
            console.print(
                f"CAVEAT: only {self.historical_play_count} historical plays -- rare-song "
                "forecasts like this have limited training signal, treat probabilities loosely."
            )

        table = Table(box=box.ASCII)
        table.add_column("Show date")
        table.add_column("P(next play)", justify="right")
        for entry in self.distribution:
            table.add_row(entry.showdate, f"{entry.probability * 100:.1f}%")
        console.print(table)
        return console.export_text()


def chaser_mode(
    conn: sqlite3.Connection,
    song_query: str,
    horizon_showids: list[int],
    config: SimConfig | None = None,
) -> ChaserReport:
    """Mode 4 (plan §5): for the resolved song, the distribution of the FIRST
    horizon show it's played in, across simulations.

    Miss handling: ``expected_shows_until_next_play`` is the mean 1-indexed
    position (1 = the very next horizon show) among simulations that hit
    *within* the horizon; simulations that never play the song inside the
    horizon are excluded from that mean and instead counted in
    ``p_not_within_horizon``, which is reported alongside it (never silently
    folded in). If the song never hits in any simulation,
    ``expected_shows_until_next_play`` is ``None``.

    ``low_signal_caveat`` fires when the song has fewer than
    ``LOW_SIGNAL_PLAY_COUNT`` historical plays (rare bustouts have few
    training signals per the plan's note).
    """
    config = config or SimConfig()
    songid, slug, name = resolve_song(conn, song_query)
    result: SimResult = simulate_horizon(conn, horizon_showids, config)
    n = len(result.samples)
    n_horizon = len(result.horizon_showids)

    first_hit: list[int | None] = []
    for sim in result.samples:
        hit = None
        for t, step_set in enumerate(sim):
            if songid in step_set:
                hit = t
                break
        first_hit.append(hit)

    hit_counts = [0] * n_horizon
    for idx in first_hit:
        if idx is not None:
            hit_counts[idx] += 1
    misses = sum(1 for idx in first_hit if idx is None)
    p_miss = (misses / n) if n else 0.0

    distribution = [
        ChaserShowProb(
            showid=result.horizon_showids[t], showdate=result.horizon_dates[t],
            probability=(hit_counts[t] / n) if n else 0.0,
        )
        for t in range(n_horizon)
    ]

    hits = sorted(idx for idx in first_hit if idx is not None)
    if hits:
        modal_idx = max(range(n_horizon), key=lambda t: hit_counts[t])
        modal_date = result.horizon_dates[modal_idx]
        # Lower-median convention for even counts (documented choice).
        median_idx = hits[(len(hits) - 1) // 2]
        median_date = result.horizon_dates[median_idx]
        expected_shows = sum(idx + 1 for idx in hits) / len(hits)
    else:
        modal_date = None
        median_date = None
        expected_shows = None

    play_count_row = conn.execute(
        "SELECT COUNT(*) AS c FROM performances WHERE songid = ?", (songid,)
    ).fetchone()
    historical_play_count = int(play_count_row["c"]) if play_count_row is not None else 0
    low_signal_caveat = historical_play_count < LOW_SIGNAL_PLAY_COUNT

    return ChaserReport(
        song=name, slug=slug, horizon_showids=result.horizon_showids,
        horizon_dates=result.horizon_dates, model=config.model, n_sims=config.n_sims,
        p_not_within_horizon=p_miss, modal_show_date=modal_date, median_show_date=median_date,
        expected_shows_until_next_play=expected_shows,
        historical_play_count=historical_play_count, low_signal_caveat=low_signal_caveat,
        distribution=distribution,
    )
