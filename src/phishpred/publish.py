"""`phishpred publish` — compute every publishable artifact for the current
epoch and write JSON (+ optional raw samples) to a directory (deploy plan §3,
DEPLOY-CONTRACTS.md §2).

Predictions are a batch artifact, not a live computation (deploy plan §0). A
SINGLE ``simulate_horizon`` run over the full future horizon feeds the tour
table and the raw ``samples.bin`` (which the Worker/browser reduce for any
run/chaser/subset query); per-show marginals and setlists come from the existing
``predict_show`` / ``sample_setlist`` paths. Agent submissions (§5) are folded in
as extra ``mcp:<label>`` sources. Everything is a thin wrapper over the library,
deterministic given ``seed``.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import zlib
from pathlib import Path

import numpy as np

from . import features
from .config import era_for_year
from .epoch import compute_epoch, utc_now_iso
from .mcp.tools import _MAX_SETLIST_SONGS, _SET_KEY_RE, _safe_label
from .models.llm import LLMError
from .modes import _round_floats, tour_mode
from .predict import predict_show
from .probs import renormalize_to_k
from .samples_codec import encode_samples
from .setlist import sample_setlist
from .simulate import SimConfig, SimResult, simulate_horizon

# Publish the full candidate list per show, not the CLI's display top-N.
_SHOW_TOP = 1000
# Cap an agent's free-text rationale at fold time (untrusted input, §5/§9).
_MAX_RATIONALE = 4000


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_round_floats(obj), ensure_ascii=False), encoding="utf-8")


def tour_id_for(tour_name: str | None) -> str:
    """Stable short id for a tour name, kept distinct across years by appending
    the year token when present: "2026 Summer Tour" -> "summer-2026",
    "New Year's Run 2026" -> "new-years-2026", a year-less name -> plain slug,
    None -> "unknown". Drops a 4-digit year token and the generic "tour"/"run"
    words; lowercases; strips apostrophes.

    NB: we strip "'" (so "New Year's" -> "new-years"), which differs
    intentionally from ingest._slugify (it maps "'" -> "-" -> "new-year-s").
    """
    if not tour_name:
        return "unknown"
    year: str | None = None
    tokens: list[str] = []
    for t in tour_name.split():
        if re.fullmatch(r"\d{4}", t):
            year = t
        elif t.lower() not in {"tour", "run"}:
            tokens.append(t)
    slug = "-".join(tokens).lower().replace("'", "")
    slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
    if not slug:
        return year or "unknown"
    return f"{slug}-{year}" if year else slug


def _tour_doc(report, epoch: str) -> dict:
    return {
        "epoch": epoch, "horizon_showdates": report.horizon_dates,
        "model": report.model, "n_sims": report.n_sims, "half_life": report.half_life,
        "rows": [
            {
                "song": r.song, "slug": r.slug, "expected_plays": r.expected_plays,
                "p_at_least_one": r.p_at_least_one, "dist": r.dist, "bucket": r.bucket,
                "gap_ratio": r.gap_ratio, "analytic_p": r.analytic_p,
            }
            for r in report.rows
        ],
    }


def _slice_result(result: SimResult, positions: list[int], showids: list[int]) -> SimResult:
    """A SimResult restricted to a subset of horizon positions (deploy plan §3:
    reduce the one simulation many ways). Forward-state context from earlier
    positions is preserved in the samples — the tour-context semantics of §4a."""
    return SimResult(
        horizon_showids=showids,
        horizon_dates=[result.horizon_dates[p] for p in positions],
        horizon_venueids=[result.horizon_venueids[p] for p in positions],
        songs_meta=result.songs_meta,
        samples=[[sim[p] for p in positions] for sim in result.samples],
        config=result.config,
    )


def _tour_tracker(conn: sqlite3.Connection, tour_id: str, as_of: str | None) -> dict:
    """Per-tour "plays-so-far" tracker (DEPLOY-CONTRACTS §3): how many of the
    tour's shows have been played (indexed) and, per song, how many of those
    played shows featured it (plus which dates).

    A tour's shows are every non-excluded show whose ``tour_name`` maps to
    ``tour_id`` (``tour_id_for``); its PLAYED shows are the complement of
    ``_future_schedule``'s ``show_index IS NULL`` filter (indexed = already
    ingested). Counts are DISTINCT-show per song, matching the tour-rotation
    feature (``_State.tour_play_count`` increments once per song per show over a
    set of distinct songids). ``as_of`` is the injectable ``created_at`` — never
    a bare now() (keeps ``compute_epoch``/reproducibility intact)."""
    played_showids: list[int] = []
    n_total = 0
    for r in conn.execute(
        "SELECT showid, tour_name, show_index FROM shows WHERE exclude = 0"
    ):
        if tour_id_for(r["tour_name"]) != tour_id:
            continue
        n_total += 1
        if r["show_index"] is not None:
            played_showids.append(int(r["showid"]))

    dates: dict[str, list[str]] = {}
    if played_showids:
        placeholders = ",".join("?" for _ in played_showids)
        for row in conn.execute(
            "SELECT so.slug AS slug, s.showdate AS showdate "
            "FROM performances p "
            "JOIN songs so ON so.songid = p.songid "
            "JOIN shows s ON s.showid = p.showid "
            f"WHERE p.showid IN ({placeholders}) "
            "GROUP BY so.slug, s.showdate ORDER BY so.slug, s.showdate",
            played_showids,
        ):
            dates.setdefault(row["slug"], []).append(row["showdate"])
    counts = {slug: len(d) for slug, d in dates.items()}

    return {
        "n_shows_played": len(played_showids),
        "n_shows_total": n_total,
        "played_counts": counts,
        "played_dates": dates,
        "as_of": as_of,
    }


def _load_frozen_tour(frozen_dir: Path | str | None, tour_id: str) -> dict | None:
    """The frozen per-tour prediction doc at ``{frozen_dir}/tour/{tour_id}.json``
    (the local mirror of R2's ``frozen/tour`` prefix), or ``None`` when there is
    no frozen doc yet. When present, its rows/metadata are authoritative — the
    served doc never re-simulates over them (DEPLOY-CONTRACTS §3 freeze-once)."""
    if frozen_dir is None:
        return None
    path = Path(frozen_dir) / "tour" / f"{tour_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"publish: ignoring unreadable frozen tour doc {path}: {exc}", file=sys.stderr)
        return None


def _scrub_for_backcast(conn: sqlite3.Connection, cutoff_showdate: str) -> None:
    """Blind the feature pipeline to a tour: make every show on/after
    ``cutoff_showdate`` look un-played on a WRITABLE COPY of the DB.

    The tour simulation path reads only ``shows``/``performances``/``songs``
    (via ``features.build_state_to_now`` / ``mean_setlist_size`` /
    ``future_show_ids`` / ``future_run_start`` — all gated on
    ``show_index IS NOT NULL`` for history), with no derived/cached tables at
    request time. So NULLing ``show_index`` and deleting the performance rows for
    the cutoff-and-later shows leaves the pipeline with exactly the pre-tour
    state: those shows become future (un-indexed), carry no setlist, and are
    invisible to gap/last-played/tour-rotation/era-rate/K features. MUTATES the
    connection — the caller must pass a disposable copy (see ``backcast_tour``)."""
    showids = [int(r["showid"]) for r in conn.execute(
        "SELECT showid FROM shows WHERE showdate >= ?", (cutoff_showdate,)
    )]
    conn.executemany("DELETE FROM performances WHERE showid = ?", [(s,) for s in showids])
    conn.execute("UPDATE shows SET show_index = NULL WHERE showdate >= ?", (cutoff_showdate,))
    conn.commit()


def backcast_tour(
    conn: sqlite3.Connection,
    tour_id: str,
    *,
    n_sims: int = 2000,
    model: str = "heuristic",
    seed: int = 0,
    half_life: int = 50,
) -> dict:
    """Back-compute the frozen pre-tour prediction for ``tour_id`` (DEPLOY-
    CONTRACTS §3): what today's heuristic would have predicted the day before the
    tour opener, with zero information about the tour itself.

    Runs on the passed connection, which MUST be a writable, disposable COPY of
    the DB (this scrubs it). Scrubs every show on/after the tour's first date to
    look un-played, then runs the normal tour simulation over the whole tour
    horizon and returns a ``_tour_doc``-shaped dict tagged ``{"backcast": true,
    "as_of_showdate": <last pre-tour played show>}``. Deterministic given
    ``seed``. Raises ``ValueError`` if the tour has no shows."""
    tour_rows = [
        r for r in conn.execute(
            "SELECT showid, showdate, tour_name FROM shows "
            "WHERE exclude = 0 ORDER BY showdate, showid"
        )
        if tour_id_for(r["tour_name"]) == tour_id
    ]
    if not tour_rows:
        raise ValueError(f"no shows for tour {tour_id!r}")
    cutoff = str(tour_rows[0]["showdate"])

    # Last pre-tour played show — the honest "as of" date the backcast knew
    # (captured BEFORE the scrub un-indexes everything).
    pre = conn.execute(
        "SELECT showdate FROM shows WHERE show_index IS NOT NULL AND exclude = 0 "
        "AND showdate < ? ORDER BY show_index DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    as_of_showdate = str(pre["showdate"]) if pre is not None else None

    _scrub_for_backcast(conn, cutoff)

    # After the scrub the tour's shows are future (show_index NULL); simulate the
    # whole tour horizon in showdate order.
    tour_showids = [int(r["showid"]) for r in tour_rows]
    cfg = SimConfig(n_sims=n_sims, seed=seed, model=model, half_life=half_life)
    result: SimResult = simulate_horizon(conn, tour_showids, cfg)
    epoch, _components = compute_epoch(
        conn, model=model, n_sims=n_sims, seed=seed, half_life=half_life,
    )
    doc = _tour_doc(tour_mode(conn, tour_showids, cfg, result=result), epoch)
    doc["backcast"] = True
    doc["as_of_showdate"] = as_of_showdate
    return doc


def _as_of(conn: sqlite3.Connection) -> tuple[str | None, int | None]:
    row = conn.execute(
        "SELECT showdate, show_index FROM shows "
        "WHERE show_index IS NOT NULL ORDER BY show_index DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None, None
    return str(row["showdate"]), int(row["show_index"])


def _catalog(conn: sqlite3.Connection) -> dict:
    """Compact history catalog for the client-side personalized 'due to see'
    view: global per-song play stats (for ranking) + each past show's songids
    (so the browser can compute a user's seen-songs from their seedfile, then
    reduce samples.bin locally), plus a historical show->tour map (so the
    browser can derive "played in N of the last M tours" for the Songs page
    without a second fetch). See DEPLOY-CONTRACTS §2a."""
    songs = features.song_play_catalog(conn)  # shared with personal.unlikely_unseen
    by_show: dict[str, set] = {}
    for r in conn.execute(
        "SELECT sh.showdate AS showdate, p.songid AS songid "
        "FROM performances p JOIN shows sh ON sh.showid = p.showid "
        "WHERE sh.exclude = 0 AND sh.show_index IS NOT NULL"
    ):
        by_show.setdefault(str(r["showdate"]), set()).add(int(r["songid"]))

    show_tours: dict[str, str] = {}
    tour_names: dict[str, str] = {}
    for r in conn.execute(
        "SELECT showdate, tour_name FROM shows "
        "WHERE exclude = 0 AND show_index IS NOT NULL ORDER BY showdate"
    ):
        tid = tour_id_for(r["tour_name"])
        show_tours[str(r["showdate"])] = tid
        tour_names.setdefault(tid, r["tour_name"] or tid)

    return {
        "songs": [
            {"songid": int(r["songid"]), "slug": r["slug"], "name": r["name"],
             "plays": int(r["plays"]), "last": r["last_played"],
             "debut_date": r["debut_date"]}
            for r in songs
        ],
        "by_show": {d: sorted(ids) for d, ids in sorted(by_show.items())},
        "show_tours": dict(sorted(show_tours.items())),
        "tours": [{"id": tid, "tour_name": name} for tid, name in tour_names.items()],
    }


def _future_schedule(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.showid AS showid, s.showdate AS showdate, s.venueid AS venueid, "
        "s.tour_name AS tour_name, v.name AS venue_name, v.city AS city, v.state AS state "
        "FROM shows s LEFT JOIN venues v ON v.venueid = s.venueid "
        "WHERE s.show_index IS NULL AND s.exclude = 0 ORDER BY s.showdate, s.showid"
    ).fetchall()


def _show_prediction_source(conn, showdate, model, half_life):
    pred = predict_show(conn, showdate, model=model, half_life=half_life, top=_SHOW_TOP)
    kind = "llm" if model.startswith("llm:") else "statistical"
    rows = [
        {"song": r.song, "slug": r.slug, "prob": r.prob, "gap": r.gap, "drivers": r.drivers}
        for r in pred.rows
    ]
    # pred.model is the resolved name (an "llm:<provider>" spec becomes
    # "llm:<provider>:<model-id>"); identical to `model` for heuristic/lr/gbm.
    return pred, {"model": pred.model, "kind": kind, "rows": rows}


def _truncate_rationale(rationale):
    """Cap an agent's free-text rationale at fold time (untrusted input §5/§9)."""
    if isinstance(rationale, str) and len(rationale) > _MAX_RATIONALE:
        return rationale[:_MAX_RATIONALE]
    return rationale


def _fold_rows(preds, songs, k) -> list[dict]:
    """Validate/clamp/renorm/cap ONE submission's predictions (§5), returning
    published rows sorted prob desc. Applied identically to the latest rows and
    to each prior version's rows. Raises ``ValueError`` on a duplicate slug
    (rejects the whole take); returns ``[]`` when no row is individually valid.

    Probs are published AS SUBMITTED, each clamped to <= 0.99; only if their sum
    exceeds the era's expected setlist size K are they scaled DOWN — a sparse
    shortlist keeps its submitted probabilities (never scaled up)."""
    valid: list[tuple[str, float]] = []
    seen: set[str] = set()
    for p in preds if isinstance(preds, list) else []:
        if not isinstance(p, dict) or p.get("slug") not in songs:
            continue
        slug = p["slug"]
        prob = p.get("prob")
        if isinstance(prob, bool) or not isinstance(prob, (int, float)):
            continue
        prob = float(prob)
        if not (0.0 < prob <= 1.0):
            continue
        if slug in seen:
            raise ValueError(f"duplicate slug {slug!r}")
        seen.add(slug)
        valid.append((slug, prob))
    if not valid:
        return []

    clamped = [min(prob, 0.99) for _s, prob in valid]
    if sum(clamped) > k:
        clamped = [float(x) for x in renormalize_to_k(np.array(clamped, dtype=float), k)]
    rows = [
        {"song": songs[slug][1], "slug": slug, "prob": prob}
        for (slug, _orig), prob in zip(valid, clamped)
    ]
    rows.sort(key=lambda r: r["prob"], reverse=True)
    return rows[:_SHOW_TOP]


def _fold_setlist(setlist, songs):
    """Fold a submitted structured setlist (§2/§5) into the resolved
    ``{"sets": {"1": [{"slug","song"},...], ...}}`` shape, or ``None`` when it
    should be dropped. Same shape rules as ``tools._validate_setlist`` but the
    fold's tolerance philosophy applies — a malformed setlist is skipped-with-
    warning (by the caller) rather than raised. If ANY slug is unknown the WHOLE
    setlist is dropped: a partially-resolved setlist corrupts the placement
    benchmark."""
    if not isinstance(setlist, dict):
        return None
    sets = setlist.get("sets")
    if not isinstance(sets, dict) or not sets:
        return None
    seen: set[str] = set()
    total = 0
    out: dict[str, list[dict]] = {}
    for key, slugs in sets.items():
        label = str(key)
        if not _SET_KEY_RE.match(label):
            return None
        if not isinstance(slugs, list) or not slugs:
            return None
        resolved: list[dict] = []
        for slug in slugs:
            if slug not in songs:  # any unknown slug -> drop the whole setlist
                return None
            if slug in seen:
                return None
            seen.add(slug)
            resolved.append({"slug": slug, "song": songs[slug][1]})
            total += 1
        out[label] = resolved
    if total > _MAX_SETLIST_SONGS:
        return None
    return {"sets": out}


def _fold_submissions(conn, submitted_dir, show_docs, half_life) -> None:
    """Fold `submitted/{label}/{showdate}.json` inbox entries into the matching
    show doc under `sources["mcp:"+label]` (DEPLOY-CONTRACTS §2/§5). Untrusted
    input: any malformed file (bad JSON, non-list predictions, non-dict entries,
    duplicate slugs) is skipped with a warning rather than crashing the batch;
    directory names that aren't already ``_safe_label``-clean are skipped too.

    Extras (§2 fold policy): an optional structured ``setlist`` is validated and
    slug-resolved (dropped whole with a warning if any slug is unknown), and
    prior ``versions`` are each validated/clamped/capped with the SAME
    ``_fold_rows`` machinery as the latest rows — a version that fails validation
    is dropped individually. Rationale is truncated and rows capped at
    ``_SHOW_TOP``."""
    root = Path(submitted_dir) if submitted_dir else None
    if root is None or not root.exists():
        return

    songs = {r["slug"]: (int(r["songid"]), r["name"]) for r in conn.execute("SELECT songid, slug, name FROM songs")}
    k_by_era: dict[str, float] = {}  # memoize mean_setlist_size per era over the fold

    for label_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        label = label_dir.name
        try:
            safe = _safe_label(label)
        except ValueError:
            safe = None
        if safe != label:
            print(f"publish: skipping submission dir {label_dir}: unsafe label", file=sys.stderr)
            continue
        for f in sorted(label_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                showdate = str(data["showdate"])
                if showdate not in show_docs:
                    print(f"publish: submission {f} showdate {showdate} not in horizon; skipping", file=sys.stderr)
                    continue

                era = era_for_year(int(showdate[:4]))
                if era not in k_by_era:
                    k_by_era[era] = features.mean_setlist_size(conn, era)
                k = k_by_era[era]

                rows = _fold_rows(data["predictions"], songs, k)
                if not rows:
                    print(f"publish: submission {f} has no valid predictions; skipping", file=sys.stderr)
                    continue

                source = {
                    "model": f"mcp:{label}",
                    "kind": "mcp",
                    "rationale": _truncate_rationale(data.get("rationale")),
                    "submitted_at": data.get("submitted_at"),
                    "rows": rows,
                }

                # Optional structured setlist on the latest take (§2 fold policy).
                if data.get("setlist") is not None:
                    folded = _fold_setlist(data["setlist"], songs)
                    if folded is not None:
                        source["setlist"] = folded
                    else:
                        print(f"publish: dropping invalid/unknown setlist for {f}", file=sys.stderr)

                # Prior versions: each validated with the same helper; a version
                # that fails validation is dropped individually.
                folded_versions: list[dict] = []
                for v in data.get("versions") or []:
                    if not isinstance(v, dict):
                        print(f"publish: dropping non-dict version in {f}", file=sys.stderr)
                        continue
                    try:
                        vrows = _fold_rows(v.get("predictions"), songs, k)
                    except ValueError as exc:
                        print(f"publish: dropping invalid version in {f}: {exc}", file=sys.stderr)
                        continue
                    if not vrows:
                        print(f"publish: dropping version with no valid predictions in {f}", file=sys.stderr)
                        continue
                    ventry = {
                        "submitted_at": v.get("submitted_at"),
                        "rationale": _truncate_rationale(v.get("rationale")),
                        "rows": vrows,
                    }
                    if v.get("setlist") is not None:
                        vfolded = _fold_setlist(v["setlist"], songs)
                        if vfolded is not None:
                            ventry["setlist"] = vfolded
                        else:
                            print(f"publish: dropping invalid/unknown setlist on a version in {f}", file=sys.stderr)
                    folded_versions.append(ventry)
                if folded_versions:
                    source["versions"] = folded_versions

                show_docs[showdate]["sources"][f"mcp:{label}"] = source
            except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as exc:
                print(f"publish: skipping malformed submission {f}: {exc}", file=sys.stderr)
                continue


def publish(
    conn: sqlite3.Connection,
    out_dir: Path | str,
    *,
    n_sims: int = 2000,
    model: str = "heuristic",
    seed: int = 0,
    half_life: int = 50,
    with_samples: bool = False,
    sample_sims: int | None = None,
    with_catalog: bool = False,
    compare_models: list[str] | None = None,
    submitted_dir: Path | str | None = None,
    frozen_dir: Path | str | None = None,
    created_at: str | None = None,
) -> dict:
    """Write the full snapshot tree under `out_dir` and return meta.json's dict.

    Deterministic given `seed`. `compare_models` are extra per-show source
    columns: statistical ("lr"/"gbm") or LLM ("llm:<provider>[:<model-id>]",
    e.g. "llm:anthropic"). An llm:* compare source that fails with `LLMError`
    (typically a missing provider API key in the scheduled workflow) is skipped
    with a stderr warning instead of crashing the batch. `sample_sims` (<= n_sims) ships a downsampled
    samples.bin for a smaller client download while the reduced tables keep the
    full n_sims accuracy (deploy plan §11). `frozen_dir` is the local mirror of
    R2's `frozen/` prefix (e.g. `data/frozen`): when a `{frozen_dir}/tour/
    {tour_id}.json` exists its rows/metadata are authoritative and are NOT
    re-simulated over — the served per-tour doc carries those frozen predictions
    plus a freshly-computed plays-so-far tracker (DEPLOY-CONTRACTS §3). When
    absent, the freshly simulated per-tour doc is both served and staged to
    `{out}/tour_frozen/{tour_id}.json` for the workflow to push to `frozen/tour`.
    `created_at` is injectable for reproducible tests (defaults to now, UTC)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    created_at = created_at or utc_now_iso()
    compare_models = compare_models or []

    epoch, _components = compute_epoch(
        conn, model=model, n_sims=n_sims, seed=seed, half_life=half_life,
        compare_models=compare_models, submitted_dir=submitted_dir,
    )
    as_of_showdate, as_of_show_index = _as_of(conn)

    horizon = features.future_show_ids(conn)
    cfg = SimConfig(n_sims=n_sims, seed=seed, model=model, half_life=half_life)

    # ONE simulation feeds both the tour table and samples.bin.
    result: SimResult = (
        simulate_horizon(conn, horizon, cfg)
        if horizon
        else SimResult([], [], [], {}, [[] for _ in range(n_sims)], cfg)
    )
    horizon_dates = result.horizon_dates

    # --- schedule.json + tour metadata --------------------------------------
    schedule_rows = _future_schedule(conn)
    published_dates = set(horizon_dates)
    schedule_shows = []
    tours: dict[str, dict] = {}
    for r in schedule_rows:
        tid = tour_id_for(r["tour_name"])
        has_data = str(r["showdate"]) in published_dates
        schedule_shows.append({
            "showdate": str(r["showdate"]), "venue_name": r["venue_name"],
            "city": r["city"], "state": r["state"], "tour_id": tid,
            "tour_name": r["tour_name"], "has_data": has_data,
        })
        t = tours.setdefault(tid, {"id": tid, "tour_name": r["tour_name"], "has_data": False})
        t["has_data"] = t["has_data"] or has_data
    _write_json(out / "schedule.json", {"shows": schedule_shows})

    # --- meta.json -----------------------------------------------------------
    meta = {
        "epoch": epoch, "created_at": created_at,
        "as_of_showdate": as_of_showdate, "as_of_show_index": as_of_show_index,
        "code_version": _components["code_version"],
        "models": [model] + compare_models, "headline_model": model,
        "n_sims": n_sims, "seed": seed, "half_life": half_life,
        "horizon_showdates": horizon_dates,
        "tours": list(tours.values()),
    }
    _write_json(out / "meta.json", meta)

    # --- tour.json (all future) + tour/{id}.json (per tour) -----------------
    # Both reduce the SAME single simulation: tour.json over the whole horizon,
    # each tour/{id}.json over that tour's positions (deploy plan §3).
    if horizon:
        _write_json(out / "tour.json", _tour_doc(tour_mode(conn, horizon, cfg, result=result), epoch))

        pos_of = {sid: i for i, sid in enumerate(result.horizon_showids)}
        per_tour: dict[str, list[int]] = {}
        for r in schedule_rows:  # ordered by showdate
            if r["showid"] in pos_of:
                per_tour.setdefault(tour_id_for(r["tour_name"]), []).append(r["showid"])
        for tid, showids in per_tour.items():
            positions = [pos_of[sid] for sid in showids]
            sub = _slice_result(result, positions, showids)
            fresh_doc = _tour_doc(tour_mode(conn, showids, cfg, result=sub), epoch)
            tracker = _tour_tracker(conn, tid, created_at)
            # Freeze-once (DEPLOY-CONTRACTS §3): a frozen doc (ideally a pre-tour
            # backcast) is authoritative — serve its rows verbatim; only the
            # tracker refreshes. Absent a frozen doc, freeze the fresh sim now
            # (the degenerate freeze-as-of-this-run fallback).
            frozen_doc = _load_frozen_tour(frozen_dir, tid)
            served = dict(frozen_doc) if frozen_doc is not None else dict(fresh_doc)
            served.pop("tracker", None)               # tracker is time-varying, never frozen
            stage = {k: v for k, v in served.items() if k != "tracker"}
            served["tracker"] = tracker
            _write_json(out / "tour" / f"{tid}.json", served)
            # Stage the frozen prediction (predictions only) for the workflow to
            # push to frozen/tour. Re-staging an existing frozen doc is
            # idempotent/immutable; staging the fresh sim seeds it the first time.
            _write_json(out / "tour_frozen" / f"{tid}.json", stage)
    else:
        _write_json(out / "tour.json", {"epoch": epoch, "horizon_showdates": [], "model": model,
                                        "n_sims": n_sims, "half_life": half_life, "rows": []})

    # --- per-show: show/{showdate}.json + setlist/{showdate}.json ------------
    # Setlist run-context threading (mirrors simulate._horizon_steps): walking
    # horizon_dates in order, consecutive horizon shows at the same canonical
    # venueid (result.horizon_venueids, parallel to horizon_dates) form a run.
    # Songs already placed in earlier PREDICTED nights of the current run are
    # hard-excluded; the previous predicted night's songs are soft-discouraged
    # when it was a DIFFERENT venue (same-run repeats are already excluded).
    # Actual mid-run history (already-ingested nights of the run) is handled
    # inside sample_setlist via strict_no_repeat. Each show gets its own
    # crc32-derived seed so consecutive nights decorrelate.
    show_docs: dict[str, dict] = {}
    skipped_sources: set[str] = set()  # compare models disabled after an LLMError
    run_played: set[int] = set()   # predicted songids earlier in the CURRENT run
    prev_night: set[int] = set()   # previous predicted night's songids
    prev_venueid = None
    for pos, showdate in enumerate(horizon_dates):
        # Headline-model LLM failures propagate: a snapshot without its headline
        # source is not publishable. Compare sources are best-effort (below).
        pred, headline_src = _show_prediction_source(conn, showdate, model, half_life)
        sources = {model: headline_src}
        for cm in compare_models:
            if cm in skipped_sources:
                continue
            try:
                _p, src = _show_prediction_source(conn, showdate, cm, half_life)
            except LLMError as exc:
                # An llm:* compare column must never take down the batch — the
                # scheduled workflow may run without the provider's API key.
                # Drop the source for the rest of the run (one warning).
                print(f"publish: skipping compare source {cm!r}: {exc}", file=sys.stderr)
                skipped_sources.add(cm)
                continue
            sources[cm] = src
        show_docs[showdate] = {
            "showdate": showdate, "venue_name": pred.venue_name,
            "city": pred.city, "state": pred.state, "epoch": epoch, "k": pred.k,
            "sources": sources,
        }
        # setlist (deterministic sampler)
        venueid = result.horizon_venueids[pos] if pos < len(result.horizon_venueids) else None
        same_run = venueid is not None and prev_venueid is not None and venueid == prev_venueid
        if not same_run:
            run_played = set()
        show_seed = zlib.crc32(f"{seed}:{showdate}".encode())
        setlist = sample_setlist(
            conn, showdate, half_life=half_life, seed=show_seed,
            exclude_songids=run_played if same_run else None,
            discourage_songids=None if same_run else (prev_night or None),
        )
        # Fold the sampled heuristic setlist onto the headline (statistical)
        # source so it is scored on the setlist benchmark too (§2/§8), exactly
        # like _fold_setlist emits for mcp sources ({"sets": {label:
        # [{"slug","song"},...]}}). Only the headline model has a sampled
        # setlist; compare_models sources do not. `headline_src` is the same
        # dict already stored in show_docs[showdate]["sources"][model], so this
        # mutation lands in the doc written after the loop.
        headline_src["setlist"] = {
            "sets": {
                label: [{"slug": s.slug, "song": s.song_name} for s in songs]
                for label, songs in setlist.sets.items()
            }
        }
        placed = {s.songid for songs in setlist.sets.values() for s in songs}
        run_played |= placed
        prev_night = placed
        prev_venueid = venueid
        setlist_doc = {
            "showdate": setlist.showdate, "venue_name": setlist.venue_name,
            "era": setlist.era, "model": setlist.model, "seed": show_seed,
            "skeleton": setlist.skeleton,
            "sets": {
                label: [
                    {"song_name": s.song_name, "slug": s.slug, "songid": s.songid,
                     "slot": s.slot, "prob": s.prob, "segue_mark": s.segue_mark}
                    for s in songs
                ]
                for label, songs in setlist.sets.items()
            },
        }
        _write_json(out / "setlist" / f"{showdate}.json", setlist_doc)

    _fold_submissions(conn, submitted_dir, show_docs, half_life)
    for showdate, doc in show_docs.items():
        _write_json(out / "show" / f"{showdate}.json", doc)

    # --- catalog.json (history for the client-side personalized view) -------
    if with_catalog:
        catalog = _catalog(conn)
        catalog["epoch"] = epoch
        _write_json(out / "catalog.json", catalog)

    # --- samples.bin + samples_meta.json ------------------------------------
    if with_samples and horizon:
        songids_sorted = sorted(result.songs_meta.keys())
        vocab_index = {sid: i for i, sid in enumerate(songids_sorted)}
        vocab = [
            {"i": i, "songid": sid, "slug": result.songs_meta[sid][0],
             "name": result.songs_meta[sid][1]}
            for i, sid in enumerate(songids_sorted)
        ]
        # Ship the first `sample_sims` simulations if downsampling was requested
        # (deterministic: they are the first spawned RNG streams).
        bin_samples = (
            result.samples[:sample_sims]
            if sample_sims and sample_sims < len(result.samples)
            else result.samples
        )
        (out / "samples.bin").write_bytes(encode_samples(bin_samples, vocab_index))
        _write_json(out / "samples_meta.json", {
            "epoch": epoch, "n_sims": len(bin_samples), "seed": seed,
            "horizon_showdates": horizon_dates,
            "horizon_showids": result.horizon_showids,
            "horizon_venueids": result.horizon_venueids,
            "vocab": vocab,
        })

    return meta
