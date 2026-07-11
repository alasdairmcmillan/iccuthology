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
    (0, 1]. Unknown slugs, empty submissions, out-of-range/non-numeric probs,
    and duplicate slugs all raise ``ValueError`` with a clear message. Never
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
