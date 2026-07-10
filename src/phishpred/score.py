"""`phishpred score` — post-show accuracy scorecards (deploy plan §8,
DEPLOY-CONTRACTS.md §8).

Once a show we published predictions for is played, we score the FROZEN
pre-show prediction against the actual (distinct) setlist. Two epoch-independent
R2 prefixes back this tier: ``frozen/show/{showdate}.json`` (the frozen §2 show
doc, all sources) and ``scorecards/{showdate}.json`` + ``scoreboard.json``.

The core (`score_show` / `build_scoreboard`) is pure and unit-testable over
plain dicts; the driver (`score_all`) implements the §8 scan/skip/rescore-window
semantics over a DB connection and a directory of frozen files. Metrics are
computed over each source's OWN shortlist rows only (shortlists differ in length
across sources — ``n_rows`` is published so the UI can caveat). A scorecard may
only ever be computed from a frozen file, never a current-epoch artifact
(DEPLOY-CONTRACTS §8 "Freeze rule").

Tolerance philosophy mirrors publish (§5): a malformed/unreadable frozen file is
skipped with a stderr warning, never crashes the batch.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .epoch import utc_now_iso
from .modes import _round_floats

# Metrics look at the first N rows for the "top-N" family (§8). Rows in a frozen
# source are already prob-descending; we preserve that order.
_TOP_N = 10
# log_loss probability clamp so an all-in 0/1 prediction can't blow up to inf.
_LOG_LOSS_CLAMP = (0.001, 0.999)


# ---------------------------------------------------------------------------
# Pure scoring core
# ---------------------------------------------------------------------------

def _score_source(src: dict[str, Any], played_slugs: set[str]) -> dict[str, Any]:
    """Score one frozen source's shortlist against the played set (§8).

    ``src`` is a frozen ``sources[*]`` entry: ``{model, kind, rows: [...], ...}``
    with rows already prob-descending. Returns the scorecard source entry
    (metrics + best_call/biggest_whiff + hit-annotated rows). ``mcp`` sources
    keep their frozen ``rationale``/``submitted_at`` verbatim.
    """
    frozen_rows = src.get("rows") or []
    # Preserve frozen (prob desc) order; annotate each row with its hit.
    scored_rows: list[dict[str, Any]] = []
    for r in frozen_rows:
        slug = r.get("slug")
        prob = float(r.get("prob", 0.0))
        hit = slug in played_slugs
        scored_rows.append({"song": r.get("song"), "slug": slug, "prob": prob, "hit": hit})

    n_rows = len(scored_rows)
    top_n = min(_TOP_N, n_rows)
    hits_top10 = sum(1 for r in scored_rows[:top_n] if r["hit"])
    hit_rate_top10 = hits_top10 / top_n if top_n else 0.0

    shortlist = {r["slug"] for r in scored_rows}
    n_played = len(played_slugs)
    recall = len(shortlist & played_slugs) / n_played if n_played else 0.0

    if n_rows:
        brier = sum((r["prob"] - (1.0 if r["hit"] else 0.0)) ** 2 for r in scored_rows) / n_rows
        log_loss = 0.0
        lo, hi = _LOG_LOSS_CLAMP
        for r in scored_rows:
            p = min(max(r["prob"], lo), hi)
            y = 1.0 if r["hit"] else 0.0
            log_loss += -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))
        log_loss /= n_rows
    else:
        brier = 0.0
        log_loss = 0.0

    # best_call = hit with the LOWEST prob; biggest_whiff = miss with the
    # HIGHEST prob. Null when there are no hits / no misses respectively.
    hits = [r for r in scored_rows if r["hit"]]
    misses = [r for r in scored_rows if not r["hit"]]
    best_call = min(hits, key=lambda r: r["prob"]) if hits else None
    biggest_whiff = max(misses, key=lambda r: r["prob"]) if misses else None

    def _call(r: dict[str, Any] | None) -> dict[str, Any] | None:
        if r is None:
            return None
        return {"song": r["song"], "slug": r["slug"], "prob": r["prob"]}

    entry: dict[str, Any] = {
        "model": src.get("model"),
        "kind": src.get("kind"),
        "n_rows": n_rows,
        "metrics": {
            "hits_top10": hits_top10,
            "hit_rate_top10": hit_rate_top10,
            "recall": recall,
            "brier": brier,
            "log_loss": log_loss,
        },
        "best_call": _call(best_call),
        "biggest_whiff": _call(biggest_whiff),
        "rows": scored_rows,
    }
    # mcp sources carry their agent rationale + submission timestamp verbatim.
    if src.get("kind") == "mcp":
        entry["rationale"] = src.get("rationale")
        entry["submitted_at"] = src.get("submitted_at")
    return entry


def score_show(frozen_payload: dict[str, Any], played: list[dict[str, Any]]) -> dict[str, Any]:
    """Score a frozen show prediction against the played setlist (§8).

    ``frozen_payload`` is a ``frozen/show/{showdate}.json`` doc (§2 show shape,
    all sources). ``played`` is the show's DISTINCT performed songs in setlist
    order: ``[{"slug": str, "song": str}, ...]``. Returns the
    ``scorecards/{showdate}.json`` dict. Floats are rounded to 4 decimals by the
    writer (`_round_floats`), consistent with the rest of the deploy tier.
    """
    showdate = frozen_payload.get("showdate")
    played_slugs = {p["slug"] for p in played}

    sources_out: dict[str, Any] = {}
    all_shortlist: set[str] = set()
    for key, src in (frozen_payload.get("sources") or {}).items():
        sources_out[key] = _score_source(src, played_slugs)
        all_shortlist |= {r.get("slug") for r in (src.get("rows") or [])}

    # Played songs that appeared in NO source's shortlist (preserve setlist order).
    missed_by_all = [
        {"slug": p["slug"], "song": p["song"]}
        for p in played
        if p["slug"] not in all_shortlist
    ]

    return {
        "showdate": showdate,
        "venue_name": frozen_payload.get("venue_name"),
        "city": frozen_payload.get("city"),
        "state": frozen_payload.get("state"),
        "frozen_epoch": frozen_payload.get("epoch"),
        "scored_at": utc_now_iso(),
        "phishnet_url": f"https://phish.net/setlists/?d={showdate}",
        "n_played": len(played),
        "played": [{"slug": p["slug"], "song": p["song"]} for p in played],
        "sources": sources_out,
        "missed_by_all": missed_by_all,
    }


def build_scoreboard(scorecards: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll every scorecard into ``scorecards/scoreboard.json`` (§8).

    ``shows`` lists every scored show (showdate DESC); ``models`` holds the
    unweighted mean of each source's per-show metrics over the shows it appears
    in. Empty ``shows``/``models`` is valid (no scored shows yet).
    """
    shows = []
    # model_key -> {"kind": str, metric lists...}
    agg: dict[str, dict[str, Any]] = {}
    for sc in scorecards:
        source_keys = list((sc.get("sources") or {}).keys())
        shows.append(
            {
                "showdate": sc.get("showdate"),
                "venue_name": sc.get("venue_name"),
                "city": sc.get("city"),
                "state": sc.get("state"),
                "n_played": sc.get("n_played"),
                "source_keys": source_keys,
            }
        )
        for key, src in (sc.get("sources") or {}).items():
            metrics = src.get("metrics") or {}
            bucket = agg.setdefault(
                key,
                {"kind": src.get("kind"), "hit_rate_top10": [], "recall": [], "brier": [], "log_loss": []},
            )
            for m in ("hit_rate_top10", "recall", "brier", "log_loss"):
                if m in metrics:
                    bucket[m].append(metrics[m])

    shows.sort(key=lambda s: (s.get("showdate") or ""), reverse=True)

    def _mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    models = {
        key: {
            "kind": b["kind"],
            "n_shows": len(b["hit_rate_top10"]),
            "hit_rate_top10": _mean(b["hit_rate_top10"]),
            "recall": _mean(b["recall"]),
            "brier": _mean(b["brier"]),
            "log_loss": _mean(b["log_loss"]),
        }
        for key, b in agg.items()
    }

    return {"updated_at": utc_now_iso(), "shows": shows, "models": models}


# ---------------------------------------------------------------------------
# DB-backed driver
# ---------------------------------------------------------------------------

def _played_songs(conn: sqlite3.Connection, showdate: str) -> list[dict[str, Any]] | None:
    """Distinct performed songs for a played show, in setlist order of first
    occurrence (§8). Returns ``None`` if no indexed (played) show exists for the
    date — an unplayed / not-yet-ingested show is unscoreable.

    Query idiom mirrors ``mcp.tools.recent_setlists`` (performances joined to
    songs, ordered by set_label then position).
    """
    row = conn.execute(
        "SELECT showid FROM shows "
        "WHERE showdate = ? AND exclude = 0 AND show_index IS NOT NULL "
        "ORDER BY showid LIMIT 1",
        (showdate,),
    ).fetchone()
    if row is None:
        return None

    perf = conn.execute(
        "SELECT sg.slug AS slug, sg.name AS song "
        "FROM performances p JOIN songs sg ON sg.songid = p.songid "
        "WHERE p.showid = ? ORDER BY p.set_label, p.position",
        (row["showid"],),
    ).fetchall()

    played: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in perf:
        if p["slug"] in seen:
            continue
        seen.add(p["slug"])
        played.append({"slug": p["slug"], "song": p["song"]})
    return played


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_round_floats(obj), ensure_ascii=False), encoding="utf-8")


def score_all(
    conn: sqlite3.Connection,
    frozen_dir: str | Path,
    out_dir: str | Path,
    rescore_days: int = 7,
    today: date | None = None,
) -> list[str]:
    """Scan frozen show docs, score the played ones, rebuild the scoreboard (§8).

    For each ``{frozen_dir}/{showdate}.json`` whose show is indexed in the DB
    (``show_index IS NOT NULL``) and ``showdate < UTC today``: compute the
    scorecard and write ``{out_dir}/{showdate}.json``. If a scorecard already
    exists it is SKIPPED, UNLESS ``showdate >= UTC today - rescore_days`` — inside
    that window scoring is an idempotent rewrite so late setlist corrections and
    partially-ingested west-coast shows self-heal on the next run.

    Afterwards ALWAYS rebuilds ``{out_dir}/scoreboard.json`` from every scorecard
    present (an empty scoreboard is valid). ``out_dir`` is created if needed.
    ``today`` is injectable for tests (defaults to UTC today).

    Returns the list of showdates whose scorecards were (re)written this run.
    Malformed/unreadable frozen files are skipped with a stderr warning.
    """
    frozen_path = Path(frozen_dir)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if today is None:
        today = datetime.now(timezone.utc).date()
    rescore_cutoff = date.fromordinal(today.toordinal() - rescore_days)

    written: list[str] = []
    for f in sorted(frozen_path.glob("*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
            showdate = str(payload["showdate"])
            showdate_date = date.fromisoformat(showdate)
        except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as exc:
            print(f"score: skipping malformed frozen file {f}: {exc}", file=sys.stderr)
            continue

        # Only score shows strictly in the past (played) that are indexed in the DB.
        if showdate_date >= today:
            continue
        played = _played_songs(conn, showdate)
        if played is None:
            continue

        dest = out_path / f"{showdate}.json"
        # Skip an already-written scorecard unless it's inside the rescore window.
        if dest.exists() and showdate_date < rescore_cutoff:
            continue

        scorecard = score_show(payload, played)
        _write_json(dest, scorecard)
        written.append(showdate)

    # Always rebuild the scoreboard from every scorecard present.
    scorecards: list[dict[str, Any]] = []
    for f in sorted(out_path.glob("*.json")):
        if f.name == "scoreboard.json":
            continue
        try:
            scorecards.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"score: skipping unreadable scorecard {f}: {exc}", file=sys.stderr)
            continue
    _write_json(out_path / "scoreboard.json", build_scoreboard(scorecards))

    return written
