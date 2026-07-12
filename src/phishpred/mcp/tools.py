"""Pure functions backing the phishpred-mcp tools (deploy plan §5a,
DEPLOY-CONTRACTS.md §5). No dependency on the `mcp` SDK lives here -- these
are plain functions over a ``sqlite3.Connection`` so they are unit-testable
without a live MCP session; ``server.py`` wraps them as MCP tools.

Leakage safety: every read tool either delegates to ``predict.py`` /
``features.py`` (which already enforce "only history with a smaller
show_index / as-of-now state") or reads straight historical rows for
already-played shows only. Nothing here re-derives leakage-sensitive
quantities from scratch; ``song_history``'s "current decayed rate" reuses
the exact extrapolation formula ``features.build_state_to_now`` documents
for callers (``D_t = r**(t - max_index) * (D + 1.0)``).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from .. import features, predict
from ..epoch import utc_now_iso
from ..modes import resolve_song

_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_-]+")
# Raw set labels a structured setlist may use: "1","2",...,"e","e2",... (§5).
_SET_KEY_RE = re.compile(r"^(\d+|e\d*)$")
# A structured setlist may name at most this many songs (§5).
_MAX_SETLIST_SONGS = 40
# An MCP prediction shortlist must name between this many songs (inclusive) — a
# too-short list under-commits, a too-long one is a dragnet (§5). Bounds apply
# ONLY to MCP submissions, not to heuristic_prediction / the publish pipeline.
_MIN_SHORTLIST = 20
_MAX_SHORTLIST = 40
# Keep at most this many prior takes when a submission is rewritten (§5).
_MAX_VERSIONS = 10


def _safe_label(model_label: str) -> str:
    """Sanitize ``model_label`` into a filesystem-safe directory name."""
    label = _SAFE_LABEL_RE.sub("-", model_label.strip()).strip("-")
    if not label:
        raise ValueError(f"model_label {model_label!r} has no safe filename characters")
    return label


def _current_epoch(conn: sqlite3.Connection) -> str | None:
    """Best-effort current epoch (deploy plan §6 / DEPLOY-CONTRACTS.md §1).

    Prefer the published pointer at ``DATA_DIR/predictions/latest.json`` — it is
    synced from R2's ``latest.json`` and IS the epoch the live snapshot was
    published at, so a submission stamps the epoch it was actually made against.
    Fall back to recomputing via ``epoch.compute_epoch(conn)`` when no pointer
    exists (a local checkout that has never published). Degrade to ``None``
    rather than raising -- a read/write tool should never fail just because the
    epoch stamp isn't available.
    """
    try:
        from ..config import DATA_DIR
        from ..epoch import read_latest
        pointed = read_latest(DATA_DIR / "predictions" / "latest.json")
        if pointed is not None:
            return pointed
    except Exception:
        pass
    try:
        from ..epoch import compute_epoch
    except ImportError:
        return None
    try:
        epoch, _components = compute_epoch(conn)
        return epoch
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def upcoming_shows(conn: sqlite3.Connection, limit: int = 50) -> dict[str, Any]:
    """Future, non-excluded shows (showdate/venue) plus the current epoch.

    Leakage-safe by construction: delegates to ``predict.upcoming_shows``,
    which only ever looks at ``showdate >= today``.
    """
    rows = predict.upcoming_shows(conn, limit=limit)
    shows = [
        {
            "showid": row["showid"],
            "showdate": row["showdate"],
            "venue_name": row["venue_name"],
            "city": row["city"],
            "state": row["state"],
            "tour_name": row["tour_name"],
        }
        for row in rows
    ]
    return {"shows": shows, "epoch": _current_epoch(conn)}


def candidate_features(
    conn: sqlite3.Connection, showdate: str, half_life: int = 50, top: int = 50
) -> dict[str, Any]:
    """The exact feature frame ``predict_show`` builds for ``showdate``,
    compacted to the columns useful for an agent's reasoning (drops
    ``showid``/``show_index``/``y`` plumbing). Sorted by ``decayed_rate``
    descending, capped at ``top`` rows.

    Ground rules: ``played_in_run`` (already played earlier this run) and
    ``played_prev_show`` (played the immediately preceding show) flag songs
    that are essentially never / rarely (~2%) repeated -- see docs/MCP.md
    "Ground rules".
    """
    show = predict._resolve_show(conn, showdate)
    df = features.features_for_future_show(conn, show["showid"], half_life)
    if df.empty:
        return {"showdate": str(show["showdate"]), "half_life": half_life, "rows": []}

    df = df.sort_values("decayed_rate", ascending=False).head(top)
    cols = ["slug", "song_name", "songid"] + features.FEATURE_COLUMNS
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        row: dict[str, Any] = {}
        for col in cols:
            val = r[col]
            if isinstance(val, float):
                val = None if pd.isna(val) else round(float(val), 4)
            elif hasattr(val, "item"):
                val = val.item()
            row[col] = val
        rows.append(row)
    return {"showdate": str(show["showdate"]), "half_life": half_life, "rows": rows}


def song_history(conn: sqlite3.Connection, slug: str, half_life: int = 50) -> dict[str, Any]:
    """Gaps, decayed play rate, per-era rates, and venue history for a song
    (leakage-safe: reflects only already-played/indexed shows).
    """
    songid, resolved_slug, name = resolve_song(conn, slug)

    venue_rows = conn.execute(
        "SELECT sh.venueid AS venueid, v.name AS venue_name, v.city AS city, "
        "COUNT(*) AS plays "
        "FROM performances p JOIN shows sh ON sh.showid = p.showid "
        "LEFT JOIN venues v ON v.venueid = sh.venueid "
        "WHERE p.songid = ? AND sh.exclude = 0 "
        "GROUP BY sh.venueid ORDER BY plays DESC",
        (songid,),
    ).fetchall()
    venue_history_rows = [
        {"venue_name": r["venue_name"], "city": r["city"], "plays": r["plays"]}
        for r in venue_rows
    ]

    state, D, max_index = features.build_state_to_now(conn, half_life)
    if state is None or songid not in state.ever_played:
        return {
            "slug": resolved_slug,
            "song_name": name,
            "songid": songid,
            "historical_play_count": 0,
            "current_gap": None,
            "median_historical_gap": None,
            "decayed_rate": 0.0,
            "era_rates": {},
            "venue_history": venue_history_rows,
            "never_played": True,
        }

    r = 0.5 ** (1.0 / half_life)
    now_index = max_index + 1
    lp = state.last_played[songid]
    gap = now_index - lp
    numv = state.num[songid] * (r ** (now_index - lp))
    d_now = (r**1) * (D + 1.0)
    decayed_rate = (numv / d_now) if d_now > 0 else 0.0

    era_rates: dict[str, float] = {}
    for era, shows_in_era in state.era_show_count.items():
        if shows_in_era <= 0:
            continue
        era_rates[era] = state.era_song_plays.get((era, songid), 0) / shows_in_era

    return {
        "slug": resolved_slug,
        "song_name": name,
        "songid": songid,
        "historical_play_count": state.plays.get(songid, 0),
        "current_gap": gap,
        "median_historical_gap": state.median_gap.get(songid),
        "decayed_rate": round(decayed_rate, 4),
        "era_rates": {k: round(v, 4) for k, v in sorted(era_rates.items())},
        "venue_history": venue_history_rows,
        "never_played": False,
    }


def venue_history(conn: sqlite3.Connection, venue: str, top: int = 30) -> dict[str, Any]:
    """Songs that tend to get played at a venue (name/city substring match,
    case-insensitive), aliased venues merged via ``venues.alias``.
    """
    like = f"%{venue.lower()}%"
    matched = conn.execute(
        "SELECT venueid, name, city, state, alias FROM venues "
        "WHERE LOWER(name) LIKE ? OR LOWER(city) LIKE ?",
        (like, like),
    ).fetchall()
    if not matched:
        raise ValueError(f"No venue found matching {venue!r}")

    canonical_ids = {(row["alias"] or row["venueid"]) for row in matched}
    all_venues = conn.execute(
        "SELECT venueid, COALESCE(NULLIF(alias, 0), venueid) AS canon FROM venues"
    ).fetchall()
    target_ids = [row["venueid"] for row in all_venues if row["canon"] in canonical_ids]
    if not target_ids:
        target_ids = [row["venueid"] for row in matched]

    placeholders = ",".join("?" for _ in target_ids)
    total_shows_row = conn.execute(
        f"SELECT COUNT(DISTINCT showid) AS c FROM shows "
        f"WHERE venueid IN ({placeholders}) AND exclude = 0 AND show_index IS NOT NULL",
        target_ids,
    ).fetchone()
    total_shows = int(total_shows_row["c"]) if total_shows_row else 0

    song_rows = conn.execute(
        f"SELECT sg.slug AS slug, sg.name AS song_name, "
        f"COUNT(DISTINCT p.showid) AS n_shows_played, COUNT(*) AS total_plays "
        f"FROM performances p JOIN shows sh ON sh.showid = p.showid "
        f"JOIN songs sg ON sg.songid = p.songid "
        f"WHERE sh.venueid IN ({placeholders}) AND sh.exclude = 0 "
        f"GROUP BY p.songid ORDER BY n_shows_played DESC LIMIT ?",
        [*target_ids, top],
    ).fetchall()
    songs = [
        {
            "slug": r["slug"],
            "song_name": r["song_name"],
            "n_shows_played": r["n_shows_played"],
            "total_plays": r["total_plays"],
            "play_rate": round(r["n_shows_played"] / total_shows, 4) if total_shows else 0.0,
        }
        for r in song_rows
    ]

    primary = matched[0]
    return {
        "venue_name": primary["name"],
        "city": primary["city"],
        "state": primary["state"],
        "total_shows": total_shows,
        "songs": songs,
    }


def recent_setlists(conn: sqlite3.Connection, n: int = 10) -> dict[str, Any]:
    """The last ``n`` played shows' setlists, oldest first (tour context)."""
    shows = conn.execute(
        "SELECT s.showid AS showid, s.showdate AS showdate, s.tour_name AS tour_name, "
        "v.name AS venue_name, v.city AS city, v.state AS state "
        "FROM shows s LEFT JOIN venues v ON v.venueid = s.venueid "
        "WHERE s.show_index IS NOT NULL AND s.exclude = 0 "
        "ORDER BY s.show_index DESC LIMIT ?",
        (n,),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for show in shows:
        perf = conn.execute(
            "SELECT p.set_label AS set_label, p.position AS position, "
            "p.trans_mark AS trans_mark, sg.slug AS slug, sg.name AS song_name "
            "FROM performances p JOIN songs sg ON sg.songid = p.songid "
            "WHERE p.showid = ? ORDER BY p.set_label, p.position",
            (show["showid"],),
        ).fetchall()
        out.append(
            {
                "showdate": show["showdate"],
                "venue_name": show["venue_name"],
                "city": show["city"],
                "state": show["state"],
                "tour_name": show["tour_name"],
                "setlist": [
                    {
                        "set": p["set_label"],
                        "slug": p["slug"],
                        "song_name": p["song_name"],
                        "trans_mark": p["trans_mark"],
                    }
                    for p in perf
                ],
            }
        )
    out.reverse()  # chronological order (oldest of the n first)
    return {"shows": out}


def slot_propensities(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, Any]:
    """Per-song set-position tendencies plus the current era's set-structure
    stats — the data behind a setlist call's placement (§8 scores opener/closer
    marquee calls, right-set placement, and exact slots; this is how an agent
    earns them on purpose rather than by luck).

    ``slugs`` is a batch (one call for a whole draft setlist). Per known slug:
    ``{"n_plays": int, "slots": {slot: P(slot | played)}}`` over the buckets
    ``set{1,2,3}-{open,mid,close}`` and ``encore``, era-weighted (recent eras
    dominate, so a song's 90s role doesn't drown out its current one — see
    ``slots.slot_propensities``). Unknown slugs are collected under
    ``unknown_slugs`` rather than raising, so one typo doesn't sink a batch.

    ``set_structure`` summarizes the CURRENT era's show skeleton (era of the
    latest played show): shows counted, sets-per-show / encores-per-show
    distributions, and mean±std length per set label — the live version of the
    "~9 songs set 1, ~7-8 set 2, 1-2 encore" playbook prose.
    """
    from phishpred import slots as slots_mod
    from phishpred.config import era_for_year

    known = {
        str(r["slug"]): int(r["songid"])
        for r in conn.execute("SELECT slug, songid FROM songs")
    }
    requested = [str(s) for s in slugs]
    unknown = [s for s in requested if s not in known]
    wanted_ids = {known[s]: s for s in requested if s in known}

    props = slots_mod.slot_propensities(conn) if wanted_ids else {}
    counts = slots_mod.slot_counts(conn) if wanted_ids else {}

    songs_out: dict[str, Any] = {}
    for songid, slug in wanted_ids.items():
        slot_probs = props.get(songid)
        if slot_probs is None:  # known song, zero recorded plays
            songs_out[slug] = {"n_plays": 0, "slots": {}}
            continue
        songs_out[slug] = {
            "n_plays": sum(counts.get(songid, {}).values()),
            "slots": {
                slot: round(p, 3)
                for slot, p in sorted(slot_probs.items(), key=lambda kv: -kv[1])
            },
        }

    latest = conn.execute(
        "SELECT MAX(showdate) AS d FROM shows "
        "WHERE exclude = 0 AND show_index IS NOT NULL"
    ).fetchone()
    structure_out: dict[str, Any] = {}
    if latest is not None and latest["d"] is not None:
        era = era_for_year(int(str(latest["d"])[:4]))
        st = slots_mod.set_structure_stats(conn, era=era)
        structure_out = {
            "era": era,
            "n_shows": st["n_shows"],
            "num_sets_dist": {str(k): v for k, v in sorted(st["num_sets_dist"].items())},
            "num_encores_dist": {str(k): v for k, v in sorted(st["num_encores_dist"].items())},
            "set_lengths": {
                lbl: {"mean": round(s["mean"], 2), "std": round(s["std"], 2)}
                for lbl, s in st["set_lengths"].items()
            },
        }

    return {"songs": songs_out, "unknown_slugs": unknown, "set_structure": structure_out}


def backtest_shortlist(
    conn: sqlite3.Connection, slugs: list[str], n_shows: int = 20
) -> dict[str, Any]:
    """Score a hypothetical shortlist against the last ``n_shows`` PLAYED shows
    — the "test my working hypothesis before submitting" loop. Leakage-free by
    construction: it only ever reads played history.

    ``slugs`` is the candidate shortlist (1-40 distinct known slugs; unknown or
    duplicate slugs raise ``ValueError`` — a typo silently scoring 0 would
    corrupt the experiment). Per show (newest first): distinct songs played,
    how many of the shortlist hit, ``hit_rate`` (hits / shortlist length) and
    ``recall`` (hits / songs played). ``per_slug`` counts each slug's hits
    across the window — which parts of the hypothesis carry it.

    Caveat for interpretation (also in the ground rules): rotation means a
    song's past-window frequency is NOT its next-show probability — a song that
    hit 5 of the last 10 shows may be exactly the one cooling down next.
    """
    if not slugs:
        raise ValueError("slugs must not be empty")
    if len(slugs) > _MAX_SHORTLIST:
        raise ValueError(f"slugs must have at most {_MAX_SHORTLIST} entries, got {len(slugs)}")
    if len(set(slugs)) != len(slugs):
        raise ValueError("duplicate slugs in shortlist")
    known = {str(r["slug"]) for r in conn.execute("SELECT slug FROM songs")}
    unknown = [s for s in slugs if s not in known]
    if unknown:
        raise ValueError(f"unknown slug(s): {', '.join(map(str, unknown))}")

    shows = conn.execute(
        "SELECT s.showid AS showid, s.showdate AS showdate, v.name AS venue_name "
        "FROM shows s LEFT JOIN venues v ON v.venueid = s.venueid "
        "WHERE s.exclude = 0 AND s.show_index IS NOT NULL "
        "ORDER BY s.show_index DESC LIMIT ?",
        (max(int(n_shows), 0),),
    ).fetchall()

    shortlist = set(slugs)
    per_slug = {s: 0 for s in slugs}
    rows: list[dict[str, Any]] = []
    for show in shows:
        played = {
            str(r["slug"])
            for r in conn.execute(
                "SELECT DISTINCT sg.slug AS slug FROM performances p "
                "JOIN songs sg ON sg.songid = p.songid WHERE p.showid = ?",
                (show["showid"],),
            )
        }
        hit_slugs = shortlist & played
        for s in hit_slugs:
            per_slug[s] += 1
        rows.append(
            {
                "showdate": show["showdate"],
                "venue_name": show["venue_name"],
                "n_played": len(played),
                "hits": len(hit_slugs),
                "hit_rate": round(len(hit_slugs) / len(slugs), 4),
                "recall": round(len(hit_slugs) / len(played), 4) if played else 0.0,
            }
        )

    n = len(rows)
    return {
        "n_slugs": len(slugs),
        "n_shows": n,
        "mean_hit_rate": round(sum(r["hit_rate"] for r in rows) / n, 4) if n else 0.0,
        "mean_recall": round(sum(r["recall"] for r in rows) / n, 4) if n else 0.0,
        "shows": rows,
        "per_slug": per_slug,
    }


def show_length_stats(conn: sqlite3.Connection, years: int = 10) -> dict[str, Any]:
    """Songs-per-show distribution over the last ``years`` calendar years —
    calibration context for sizing a shortlist and its total probability mass
    (§5 ground rules: probs should sum near the expected setlist size).

    Anchored on the latest PLAYED showdate in the DB (not the wall clock), so
    the window is reproducible and leakage-safe: the returned span covers the
    ``years`` calendar years up to and including the latest played show's year.
    ``by_year`` is ascending; ``avg_songs`` counts performances (repeats
    included, e.g. a reprise), ``avg_distinct_songs`` counts distinct songs —
    the number a shortlist is actually scored against.
    """
    latest = conn.execute(
        "SELECT MAX(showdate) AS d FROM shows "
        "WHERE exclude = 0 AND show_index IS NOT NULL"
    ).fetchone()
    if latest is None or latest["d"] is None:
        return {"since": None, "overall": {"shows": 0}, "by_year": []}
    since = f"{int(str(latest['d'])[:4]) - years + 1:04d}-01-01"

    per_show = conn.execute(
        "SELECT substr(s.showdate, 1, 4) AS yr, "
        "COUNT(p.songid) AS n_songs, COUNT(DISTINCT p.songid) AS n_distinct "
        "FROM shows s JOIN performances p ON p.showid = s.showid "
        "WHERE s.exclude = 0 AND s.show_index IS NOT NULL AND s.showdate >= ? "
        "GROUP BY s.showid",
        (since,),
    ).fetchall()

    by_year: dict[str, dict[str, Any]] = {}
    all_songs: list[int] = []
    all_distinct: list[int] = []
    for row in per_show:
        y = by_year.setdefault(
            row["yr"], {"year": row["yr"], "shows": 0, "_songs": [], "_distinct": []}
        )
        y["shows"] += 1
        y["_songs"].append(row["n_songs"])
        y["_distinct"].append(row["n_distinct"])
        all_songs.append(row["n_songs"])
        all_distinct.append(row["n_distinct"])

    def _mean(vals: list[int]) -> float:
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    years_out = []
    for y in sorted(by_year.values(), key=lambda v: v["year"]):
        years_out.append(
            {
                "year": y["year"],
                "shows": y["shows"],
                "avg_songs": _mean(y["_songs"]),
                "avg_distinct_songs": _mean(y["_distinct"]),
                "min_songs": min(y["_songs"]),
                "max_songs": max(y["_songs"]),
            }
        )

    return {
        "since": since,
        "overall": {
            "shows": len(per_show),
            "avg_songs": _mean(all_songs),
            "avg_distinct_songs": _mean(all_distinct),
        },
        "by_year": years_out,
    }


def run_context(conn: sqlite3.Connection, showdate: str) -> dict[str, Any]:
    """The multi-night run ``showdate`` belongs to (maximal chain of shows at
    the same canonical venue), including already-played nights' setlists.
    Future nights carry ``played: False`` and no ``setlist`` key.

    Use the already-played nights to rule out same-run repeats when
    predicting a later night -- see docs/MCP.md "Ground rules".
    """
    target = predict._resolve_show(conn, showdate)
    target_showid = target["showid"]

    ordered = conn.execute(
        "SELECT s.showid AS showid, s.showdate AS showdate, s.show_index AS show_index, "
        "COALESCE(NULLIF(v.alias, 0), s.venueid) AS venueid, "
        "v.name AS venue_name, v.city AS city, v.state AS state "
        "FROM shows s LEFT JOIN venues v ON v.venueid = s.venueid "
        "WHERE s.exclude = 0 ORDER BY s.showdate, s.showid"
    ).fetchall()

    pos = next((i for i, r in enumerate(ordered) if r["showid"] == target_showid), None)
    if pos is None:
        raise ValueError(f"Show {showdate!r} not found among non-excluded shows.")

    target_venueid = ordered[pos]["venueid"]
    start = pos
    while start > 0 and ordered[start - 1]["venueid"] == target_venueid:
        start -= 1
    end = pos
    while end < len(ordered) - 1 and ordered[end + 1]["venueid"] == target_venueid:
        end += 1

    nights: list[dict[str, Any]] = []
    for row in ordered[start : end + 1]:
        played = row["show_index"] is not None
        night: dict[str, Any] = {
            "showdate": row["showdate"],
            "is_target": row["showid"] == target_showid,
            "played": played,
        }
        if played:
            perf = conn.execute(
                "SELECT sg.slug AS slug, sg.name AS song_name, p.set_label AS set_label "
                "FROM performances p JOIN songs sg ON sg.songid = p.songid "
                "WHERE p.showid = ? ORDER BY p.set_label, p.position",
                (row["showid"],),
            ).fetchall()
            night["setlist"] = [
                {"slug": p["slug"], "song_name": p["song_name"], "set": p["set_label"]}
                for p in perf
            ]
        nights.append(night)

    first = ordered[start]
    return {
        "venue_name": first["venue_name"],
        "city": first["city"],
        "state": first["state"],
        "target_showdate": str(target["showdate"]),
        "nights": nights,
    }


def heuristic_prediction(
    conn: sqlite3.Connection, showdate: str, half_life: int = 50, top: int = 30
) -> dict[str, Any]:
    """The statistical heuristic baseline (``predict_show``, model
    "heuristic") as a plain dict, so an agent can compare against / argue
    with it.
    """
    pred = predict.predict_show(conn, showdate, model="heuristic", half_life=half_life, top=top)
    payload = asdict(pred)
    payload["k"] = round(payload["k"], 4)
    for row in payload["rows"]:
        row["prob"] = round(row["prob"], 4)
    return payload


def scoreboard(
    scorecards_dir: str | Path,
    model_label: str | None = None,
    recent: int = 5,
) -> dict[str, Any]:
    """Your own track record + the heuristic baseline, for pre-submission
    calibration (§8). Reads the published scorecards tier -- leakage-safe, since
    scorecards only ever exist for already-played shows.

    ``scorecards_dir`` holds ``scoreboard.json`` plus one ``{showdate}.json`` per
    scored show (the output of ``phishpred score``). Returns:

    - ``models``: the ``scoreboard.json`` ``models`` mapping -- per-model
      aggregate metrics incl. ``avg_n_rows`` and, for non-heuristic models,
      ``vs_heuristic`` (paired deltas against the baseline).
    - ``recent_shows``: for the most recent ``recent`` scored shows (showdate
      DESC), a COMPACT per-show summary -- ``showdate``, ``venue_name``,
      ``n_played``, ``missed_by_all``, and per source (the ``heuristic`` plus,
      when ``model_label`` is given, ``mcp:{model_label}``) that source's
      ``metrics``/``best_call``/``biggest_whiff``. Full row lists are omitted to
      keep the payload small.

    A missing ``scoreboard.json`` / empty dir yields empty ``models`` /
    ``recent_shows`` (tolerance philosophy mirrors ``score.py``), never raises.
    """
    board_dir = Path(scorecards_dir)

    models: dict[str, Any] = {}
    board_path = board_dir / "scoreboard.json"
    if board_path.exists():
        try:
            board = json.loads(board_path.read_text(encoding="utf-8"))
            models = board.get("models") or {}
        except (json.JSONDecodeError, OSError):
            models = {}

    # The heuristic baseline, plus the caller's own track when a label is given.
    wanted_keys = ["heuristic"]
    if model_label is not None:
        wanted_keys.append(f"mcp:{model_label}")

    # Most recent scored shows: every {showdate}.json except scoreboard.json,
    # showdate (filename stem) DESC, capped at `recent`.
    card_paths = sorted(
        (p for p in board_dir.glob("*.json") if p.name != "scoreboard.json"),
        key=lambda p: p.stem,
        reverse=True,
    )[: max(recent, 0)]

    recent_shows: list[dict[str, Any]] = []
    for p in card_paths:
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sources = card.get("sources") or {}
        summary_sources: dict[str, Any] = {}
        for key in wanted_keys:
            src = sources.get(key)
            if src is None:
                continue
            summary_sources[key] = {
                "metrics": src.get("metrics"),
                "best_call": src.get("best_call"),
                "biggest_whiff": src.get("biggest_whiff"),
            }
        recent_shows.append(
            {
                "showdate": card.get("showdate"),
                "venue_name": card.get("venue_name"),
                "n_played": card.get("n_played"),
                "sources": summary_sources,
                "missed_by_all": card.get("missed_by_all") or [],
            }
        )

    return {"models": models, "recent_shows": recent_shows}


# ---------------------------------------------------------------------------
# Write tool
# ---------------------------------------------------------------------------

def _validate_setlist(setlist: Any, known: dict[str, str]) -> dict[str, Any]:
    """Validate a structured ``setlist`` call (§5) and return its clean shape
    ``{"sets": {"1": [slug, ...], ...}}``. Raises ``ValueError`` (matching the
    prediction-validation style) on any violation: set labels must match
    ``^(\\d+|e\\d*)$``, each set a non-empty list of known slugs, no slug may
    repeat anywhere in the setlist, and the total is capped at 40 songs. The
    setlist is a separate benchmark from ``predictions`` — validated on its own.
    """
    if not isinstance(setlist, dict):
        raise ValueError("setlist must be a dict shaped {'sets': {label: [slug, ...]}}")
    sets = setlist.get("sets")
    if not isinstance(sets, dict) or not sets:
        raise ValueError("setlist.sets must be a non-empty mapping of set labels to slug lists")

    seen: set[str] = set()
    total = 0
    clean: dict[str, list[str]] = {}
    for key, songs in sets.items():
        label = str(key)
        if not _SET_KEY_RE.match(label):
            raise ValueError(f"invalid set label {key!r}; must match ^(\\d+|e\\d*)$")
        if not isinstance(songs, list) or not songs:
            raise ValueError(f"set {label!r} must be a non-empty list of slugs")
        clean_slugs: list[str] = []
        for slug in songs:
            if slug not in known:
                raise ValueError(f"unknown slug {slug!r} in setlist set {label!r}")
            if slug in seen:
                raise ValueError(f"duplicate slug {slug!r} in setlist")
            seen.add(slug)
            clean_slugs.append(slug)
            total += 1
        clean[label] = clean_slugs
    if total > _MAX_SETLIST_SONGS:
        raise ValueError(f"setlist has {total} songs; max {_MAX_SETLIST_SONGS}")
    return {"sets": clean}


def submit_prediction(
    showdate: str,
    model_label: str,
    predictions: list[dict[str, Any]],
    rationale: str | None = None,
    setlist: dict[str, Any] | None = None,
    *,
    conn: sqlite3.Connection,
    out_dir: str | Path,
    epoch: str | None = None,
    submitted_at: str | None = None,
) -> dict[str, Any]:
    """Validate an agent's per-song predictions and write
    ``out_dir/{model_label}/{showdate}.json`` per DEPLOY-CONTRACTS.md §5.

    ``predictions`` is a list of ``{"slug": str, "prob": float}`` with prob in
    (0, 1] and between 20 and 40 songs. Unknown slugs, empty submissions,
    out-of-range/non-numeric probs, duplicate slugs, and a shortlist shorter than
    20 or longer than 40 all raise ``ValueError`` with a clear message. Never
    touches core tables -- this only ever writes to the submissions inbox
    (deploy plan §9: treat submissions as untrusted input).

    Probs are stored AS SUBMITTED (validated + clamped to (0, 1]). At ``publish``
    fold time they are published as submitted (each clamped to <= 0.99) and
    scaled DOWN only if their sum exceeds the show's expected setlist size K, so
    a partial shortlist keeps its submitted probabilities instead of being
    inflated by renormalization.

    ``setlist`` is an OPTIONAL structured setlist call — a SECOND benchmark
    (§8), independent of ``predictions``: ``{"sets": {"1": [slug, ...], ...}}``
    with set labels ``^(\\d+|e\\d*)$``, non-empty lists of known slugs, no slug
    repeated anywhere, <= 40 songs total. An invalid setlist raises.

    Versioning (§5): a resubmission for the same ``{label}/{showdate}`` never
    loses history — the prior file's content (minus its own ``versions`` key) is
    appended to the new file's ``versions`` array (oldest first, at most the 10
    most recent priors kept). First submissions omit the key entirely.

    Ground rules: avoid high probabilities for songs flagged
    ``played_in_run``/``played_prev_show`` by ``candidate_features``, and keep
    multi-night submissions for one run jointly consistent -- see docs/MCP.md
    "Ground rules".
    """
    if not predictions:
        raise ValueError("predictions must not be empty")

    show = predict._resolve_show(conn, showdate)
    resolved_showdate = str(show["showdate"])

    known = {row["slug"]: row["name"] for row in conn.execute("SELECT slug, name FROM songs")}

    setlist_payload = _validate_setlist(setlist, known) if setlist is not None else None

    seen: set[str] = set()
    slugs: list[str] = []
    scores: list[float] = []
    for entry in predictions:
        try:
            slug = entry["slug"]
            prob = entry["prob"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"each prediction needs 'slug' and 'prob': {entry!r}") from exc
        if slug not in known:
            raise ValueError(f"unknown slug {slug!r}; not present in the songs table")
        try:
            prob = float(prob)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"prob for {slug!r} must be numeric, got {prob!r}") from exc
        if not (0.0 < prob <= 1.0):
            raise ValueError(f"prob for {slug!r} must be in (0, 1], got {prob!r}")
        if slug in seen:
            raise ValueError(f"duplicate slug {slug!r} in predictions")
        seen.add(slug)
        slugs.append(slug)
        scores.append(prob)

    # Shortlist length bounds (§5): a live model track commits to a 20–40 song
    # shortlist. Checked after the per-entry validation so bad rows surface first.
    if not (_MIN_SHORTLIST <= len(slugs) <= _MAX_SHORTLIST):
        raise ValueError(
            f"predictions must have between {_MIN_SHORTLIST} and {_MAX_SHORTLIST} "
            f"songs, got {len(slugs)}"
        )

    # Store probs as submitted (clamped). publish renormalizes to K at fold time.
    rows = sorted(
        ({"slug": slug, "prob": round(float(p), 4)} for slug, p in zip(slugs, scores)),
        key=lambda r: r["prob"],
        reverse=True,
    )

    if submitted_at is None:
        submitted_at = utc_now_iso()
    if epoch is None:
        epoch = _current_epoch(conn)

    payload = {
        "model_label": model_label,
        "showdate": resolved_showdate,
        "epoch": epoch,
        "submitted_at": submitted_at,
        "rationale": rationale,
        "predictions": rows,
    }
    if setlist_payload is not None:
        payload["setlist"] = setlist_payload

    safe_label = _safe_label(model_label)
    dest_dir = Path(out_dir) / safe_label
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{resolved_showdate}.json"

    # Versioning (§5): fold a prior submission for the same {label}/{showdate}
    # into the new file's "versions" so the improvement arc across takes is
    # preserved. Carry prior versions over first (oldest first), then the prior
    # take itself; keep only the 10 most recent. Omit the key for a first
    # submission so legacy-shaped output is unchanged.
    if dest_path.exists():
        try:
            prior = json.loads(dest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"submit_prediction: existing {dest_path} unreadable ({exc}); "
                  "treating as no history", file=sys.stderr)
            prior = None
        if isinstance(prior, dict):
            prior_versions = prior.pop("versions", [])
            if not isinstance(prior_versions, list):
                prior_versions = []
            versions = [*prior_versions, prior][-_MAX_VERSIONS:]
            if versions:
                payload["versions"] = versions

    dest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {"path": str(dest_path), "payload": payload}
