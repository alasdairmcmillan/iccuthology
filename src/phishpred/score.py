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
# A prior take's `after_showdate` only reaches back this many days — runs never
# gap longer, so a played show older than this couldn't be "the latest we knew".
_AFTER_SHOWDATE_WINDOW = 10
# exact_calls >= this earns the "sharpshooter" badge (§8, rare by design).
_SHARPSHOOTER_MIN = 2


# ---------------------------------------------------------------------------
# Pure scoring core
# ---------------------------------------------------------------------------

def _annotate_rows(rows: list[dict[str, Any]], played_slugs: set[str]) -> list[dict[str, Any]]:
    """Frozen rows (prob desc) annotated with their hit flag, order preserved."""
    return [
        {
            "song": r.get("song"),
            "slug": r.get("slug"),
            "prob": float(r.get("prob", 0.0)),
            "hit": r.get("slug") in played_slugs,
        }
        for r in rows
    ]


def _metrics(scored_rows: list[dict[str, Any]], played_slugs: set[str]) -> dict[str, Any]:
    """The §8 metrics bucket over one shortlist's hit-annotated rows."""
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

    return {
        "hits_top10": hits_top10,
        "hit_rate_top10": hit_rate_top10,
        "recall": recall,
        "brier": brier,
        "log_loss": log_loss,
    }


def _pos_match(pred_sets: dict, played_by_set: dict, key: str, idx: int) -> bool:
    """Predicted vs actual slug at ``idx`` in set ``key`` — only when the set is
    present (non-empty) on BOTH sides (§8 marquee)."""
    pred = pred_sets.get(key)
    actual = played_by_set.get(key)
    if not pred or not actual:
        return False
    return pred[idx].get("slug") == actual[idx]


def _score_setlist(setlist: dict[str, Any], played_slugs: set[str],
                   played_by_set: dict[str, list[str]]) -> dict[str, Any]:
    """Score a frozen structured setlist against the played sets (§8).

    ``setlist`` is the folded ``{"sets": {"1": [{"slug","song"},...], ...}}``.
    ``played_by_set`` maps each raw set label to its distinct played slug list
    (position order). Returns the ``setlist_score`` shape: per-song hit/placed
    annotations, hit/placed rates, marquee flags, exact_calls, sharpshooter.
    """
    pred_sets = setlist.get("sets") or {}
    annotated: dict[str, list[dict[str, Any]]] = {}
    n_songs = hits = placed = 0
    for key, songs in pred_sets.items():
        in_set = set(played_by_set.get(key, []))
        rows: list[dict[str, Any]] = []
        for s in songs:
            slug = s.get("slug")
            hit = slug in played_slugs           # played anywhere in the show
            placed_here = slug in in_set          # played in the PREDICTED set
            rows.append({"slug": slug, "song": s.get("song"), "hit": hit, "placed": placed_here})
            n_songs += 1
            hits += 1 if hit else 0
            placed += 1 if placed_here else 0
        annotated[key] = rows

    hit_rate = hits / n_songs if n_songs else 0.0
    # placed_rate denominator is HITS (of the songs that played, how many landed
    # in the predicted set); 0/0 -> 0.
    placed_rate = placed / hits if hits else 0.0

    marquee = {
        "opener": _pos_match(pred_sets, played_by_set, "1", 0),
        "set1_closer": _pos_match(pred_sets, played_by_set, "1", -1),
        "set2_opener": _pos_match(pred_sets, played_by_set, "2", 0),
        "set2_closer": _pos_match(pred_sets, played_by_set, "2", -1),
        # encore: any overlap between predicted and actual "e" (1–2 songs).
        "encore": bool(
            {s.get("slug") for s in pred_sets.get("e", [])} & set(played_by_set.get("e", []))
        ),
    }
    marquee_calls = sum(1 for v in marquee.values() if v)

    # exact_calls: (set, position) pairs where predicted slug == actual slug, over
    # shared keys up to the min length. Subsumes opener/closer positions.
    exact_calls = 0
    for key, songs in pred_sets.items():
        actual = played_by_set.get(key)
        if not actual:
            continue
        for i in range(min(len(songs), len(actual))):
            if songs[i].get("slug") == actual[i]:
                exact_calls += 1

    return {
        "n_songs": n_songs,
        "sets": annotated,
        "hits": hits,
        "hit_rate": hit_rate,
        "placed": placed,
        "placed_rate": placed_rate,
        "marquee": marquee,
        "marquee_calls": marquee_calls,
        "exact_calls": exact_calls,
        "sharpshooter": exact_calls >= _SHARPSHOOTER_MIN,
    }


def _after_showdate(submitted_at: Any, showdate: str | None,
                    played_showdates: list[str]) -> str | None:
    """The latest played showdate a prior take could have known (§8 UI label).

    The latest played showdate ``S`` with ``S < min(UTC date of submitted_at,
    this showdate)`` and ``S`` within the 10 days before this show; ``None``
    otherwise (also ``None`` when ``submitted_at`` is missing/unparseable). A
    UI labeling heuristic, not a metric — ``None`` renders as "pre-run".
    """
    if not submitted_at or not showdate:
        return None
    try:
        show_d = date.fromisoformat(str(showdate))
    except ValueError:
        return None
    try:
        raw = str(submitted_at).replace("Z", "+00:00")
        sub_dt = datetime.fromisoformat(raw)
        if sub_dt.tzinfo is not None:
            sub_dt = sub_dt.astimezone(timezone.utc)
        sub_d = sub_dt.date()
    except ValueError:
        return None

    upper = min(sub_d, show_d)                       # S must be strictly before this
    lower = date.fromordinal(show_d.toordinal() - _AFTER_SHOWDATE_WINDOW)
    best: date | None = None
    for s in played_showdates:
        try:
            sd = date.fromisoformat(str(s))
        except ValueError:
            continue
        if lower <= sd < upper and (best is None or sd > best):
            best = sd
    return best.isoformat() if best is not None else None


def _score_source(src: dict[str, Any], played_slugs: set[str],
                  played_by_set: dict[str, list[str]], showdate: str | None,
                  played_showdates: list[str]) -> dict[str, Any]:
    """Score one frozen source's shortlist against the played set (§8).

    ``src`` is a frozen ``sources[*]`` entry: ``{model, kind, rows: [...], ...}``
    with rows already prob-descending. Returns the scorecard source entry
    (metrics + best_call/biggest_whiff + hit-annotated rows + setlist_score +
    scored prior versions). ``mcp`` sources keep their frozen
    ``rationale``/``submitted_at`` verbatim.
    """
    scored_rows = _annotate_rows(src.get("rows") or [], played_slugs)
    metrics = _metrics(scored_rows, played_slugs)

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
        "n_rows": len(scored_rows),
        "metrics": metrics,
        "best_call": _call(best_call),
        "biggest_whiff": _call(biggest_whiff),
        "rows": scored_rows,
    }
    # mcp sources carry their agent rationale + submission timestamp verbatim.
    if src.get("kind") == "mcp":
        entry["rationale"] = src.get("rationale")
        entry["submitted_at"] = src.get("submitted_at")

    # Setlist benchmark: null when the frozen source carries no setlist (it sits
    # out and is excluded from scoreboard setlist aggregates).
    setlist = src.get("setlist")
    entry["setlist_score"] = (
        _score_setlist(setlist, played_slugs, played_by_set) if setlist else None
    )

    # Version scoring: every PRIOR take scored with the same machinery, oldest
    # first. The top-level entry IS the final take. Omit the key when only one
    # take exists.
    versions = src.get("versions") or []
    scored_versions: list[dict[str, Any]] = []
    for v in versions:
        v_rows = _annotate_rows(v.get("rows") or [], played_slugs)
        v_setlist = v.get("setlist")
        scored_versions.append({
            "submitted_at": v.get("submitted_at"),
            "after_showdate": _after_showdate(v.get("submitted_at"), showdate, played_showdates),
            "metrics": _metrics(v_rows, played_slugs),
            "setlist_score": _score_setlist(v_setlist, played_slugs, played_by_set) if v_setlist else None,
            "rows": v_rows,
        })
    if scored_versions:
        entry["versions"] = scored_versions
    return entry


def score_show(
    frozen_payload: dict[str, Any],
    played: list[dict[str, Any]],
    played_sets: dict[str, list[dict[str, Any]]] | None = None,
    played_showdates: list[str] | None = None,
) -> dict[str, Any]:
    """Score a frozen show prediction against the played setlist (§8).

    ``frozen_payload`` is a ``frozen/show/{showdate}.json`` doc (§2 show shape,
    all sources). ``played`` is the show's DISTINCT performed songs in setlist
    order: ``[{"slug": str, "song": str}, ...]``. ``played_sets`` maps each raw
    set label to its distinct-within-set performed songs (position order) — the
    setlist-placement benchmark's ground truth. ``played_showdates`` is every
    played showdate in the DB (for prior takes' ``after_showdate`` label). Kept
    PURE: the caller (``score_all``) resolves both from the DB. Returns the
    ``scorecards/{showdate}.json`` dict; floats are rounded by the writer.
    """
    showdate = frozen_payload.get("showdate")
    played_slugs = {p["slug"] for p in played}
    played_sets = played_sets or {}
    played_showdates = played_showdates or []
    # Per-set slug lists (position order) for the placement/marquee benchmark.
    played_by_set = {k: [s["slug"] for s in v] for k, v in played_sets.items()}

    sources_out: dict[str, Any] = {}
    all_shortlist: set[str] = set()
    for key, src in (frozen_payload.get("sources") or {}).items():
        sources_out[key] = _score_source(src, played_slugs, played_by_set, showdate, played_showdates)
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
        "played_sets": {
            k: [{"slug": s["slug"], "song": s["song"]} for s in v]
            for k, v in played_sets.items()
        },
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
    # model_key -> {"kind": str, metric lists..., setlist lists..., refresh deltas...}
    agg: dict[str, dict[str, Any]] = {}

    def _bucket(key: str, kind: Any) -> dict[str, Any]:
        return agg.setdefault(
            key,
            {
                "kind": kind,
                "hit_rate_top10": [], "recall": [], "brier": [], "log_loss": [],
                # setlist aggregate (only over shows where setlist_score is non-null)
                "sl_hit_rate": [], "sl_placed_rate": [],
                "sl_marquee_calls": 0, "sl_exact_calls": 0, "sl_sharpshooters": 0,
                # refresh_gain (only over shows with >= 1 prior version)
                "hit_rate_top10_delta": [], "recall_delta": [],
            },
        )

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
            bucket = _bucket(key, src.get("kind"))
            for m in ("hit_rate_top10", "recall", "brier", "log_loss"):
                if m in metrics:
                    bucket[m].append(metrics[m])

            # Setlist aggregate: only shows where this source carried a setlist.
            sl = src.get("setlist_score")
            if sl:
                bucket["sl_hit_rate"].append(sl.get("hit_rate", 0.0))
                bucket["sl_placed_rate"].append(sl.get("placed_rate", 0.0))
                bucket["sl_marquee_calls"] += sl.get("marquee_calls", 0)
                bucket["sl_exact_calls"] += sl.get("exact_calls", 0)
                bucket["sl_sharpshooters"] += 1 if sl.get("sharpshooter") else 0

            # refresh_gain: final (top-level) vs FIRST take, only when priors exist.
            versions = src.get("versions") or []
            if versions:
                first = versions[0].get("metrics") or {}
                for out_key, m in (("hit_rate_top10_delta", "hit_rate_top10"), ("recall_delta", "recall")):
                    if m in metrics and m in first:
                        bucket[out_key].append(metrics[m] - first[m])

    shows.sort(key=lambda s: (s.get("showdate") or ""), reverse=True)

    def _mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    models: dict[str, Any] = {}
    for key, b in agg.items():
        entry: dict[str, Any] = {
            "kind": b["kind"],
            "n_shows": len(b["hit_rate_top10"]),
            "hit_rate_top10": _mean(b["hit_rate_top10"]),
            "recall": _mean(b["recall"]),
            "brier": _mean(b["brier"]),
            "log_loss": _mean(b["log_loss"]),
        }
        # setlist aggregate (rates unweighted means; calls/sharpshooters totals).
        # Omit the key when no setlist-scored shows.
        n_sl = len(b["sl_hit_rate"])
        if n_sl:
            entry["setlist"] = {
                "n_shows": n_sl,
                "hit_rate": _mean(b["sl_hit_rate"]),
                "placed_rate": _mean(b["sl_placed_rate"]),
                "marquee_calls": b["sl_marquee_calls"],
                "exact_calls": b["sl_exact_calls"],
                "sharpshooters": b["sl_sharpshooters"],
            }
        # refresh_gain: omit when no multi-take shows.
        n_rg = len(b["hit_rate_top10_delta"])
        if n_rg:
            entry["refresh_gain"] = {
                "n_shows": n_rg,
                "mean_hit_rate_top10_delta": _mean(b["hit_rate_top10_delta"]),
                "mean_recall_delta": _mean(b["recall_delta"]),
            }
        models[key] = entry

    return {"updated_at": utc_now_iso(), "shows": shows, "models": models}


# ---------------------------------------------------------------------------
# DB-backed driver
# ---------------------------------------------------------------------------

def _played_songs(
    conn: sqlite3.Connection, showdate: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]] | None:
    """Distinct performed songs for a played show (§8). Returns
    ``(played, played_sets)`` or ``None`` if no indexed (played) show exists for
    the date — an unplayed / not-yet-ingested show is unscoreable.

    ``played`` is the flat distinct list in setlist order of first occurrence.
    ``played_sets`` maps each raw set label (in position order of first
    appearance) to its distinct-within-set songs — the setlist-placement
    benchmark's ground truth. Query idiom mirrors ``mcp.tools.recent_setlists``.
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
        "SELECT sg.slug AS slug, sg.name AS song, p.set_label AS set_label "
        "FROM performances p JOIN songs sg ON sg.songid = p.songid "
        "WHERE p.showid = ? ORDER BY p.set_label, p.position",
        (row["showid"],),
    ).fetchall()

    played: list[dict[str, Any]] = []
    seen: set[str] = set()
    played_sets: dict[str, list[dict[str, Any]]] = {}
    seen_in_set: dict[str, set[str]] = {}
    for p in perf:
        slug, song, label = p["slug"], p["song"], str(p["set_label"])
        if slug not in seen:
            seen.add(slug)
            played.append({"slug": slug, "song": song})
        in_set = seen_in_set.setdefault(label, set())
        if slug not in in_set:
            in_set.add(slug)
            played_sets.setdefault(label, []).append({"slug": slug, "song": song})
    return played, played_sets


def _played_showdates(conn: sqlite3.Connection) -> list[str]:
    """Every played (indexed, non-excluded) showdate, ascending — the candidate
    set for a prior take's ``after_showdate`` label (§8)."""
    return [
        str(r["showdate"])
        for r in conn.execute(
            "SELECT showdate FROM shows "
            "WHERE exclude = 0 AND show_index IS NOT NULL ORDER BY showdate"
        )
    ]


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
    played_showdates = _played_showdates(conn)  # for prior takes' after_showdate

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
        result = _played_songs(conn, showdate)
        if result is None:
            continue
        played, played_sets = result

        dest = out_path / f"{showdate}.json"
        # Skip an already-written scorecard unless it's inside the rescore window.
        if dest.exists() and showdate_date < rescore_cutoff:
            continue

        scorecard = score_show(payload, played, played_sets, played_showdates)
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
