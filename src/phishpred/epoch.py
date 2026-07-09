"""Epoch identity + gating (deploy plan §6, DEPLOY-CONTRACTS.md §1).

The *epoch* is the identity of a prediction state: a pure function of the data
state + publishing parameters. Predictions are recomputed only when the epoch
changes (a show is played, the schedule changes, the code/model/params change,
or a new agent submission arrives). `phishpred epoch` compares the current epoch
to the last published one so the scheduled workflow can skip most runs in
seconds.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT


def utc_now_iso() -> str:
    """UTC now as ``YYYY-MM-DDTHH:MM:SSZ`` — the shared publish/submit timestamp
    format (moved here so publish.py and mcp/tools.py agree on one spelling)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def code_version() -> str:
    """`git rev-parse --short HEAD`, or "nogit" if unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=5,
        )
        sha = out.stdout.strip()
        if out.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return "nogit"


def _max_played_show_index(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(show_index) AS m FROM shows WHERE show_index IS NOT NULL"
    ).fetchone()
    if row is None or row["m"] is None:
        return -1
    return int(row["m"])


def _schedule_hash(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT showdate, venueid FROM shows WHERE show_index IS NULL AND exclude = 0"
    ).fetchall()
    # Sort on comparable tuples (None venueids -> -1 so a scheduled show with an
    # unassigned venue never crashes the sort); canonical output stays list-shaped.
    pairs = sorted(
        (str(r["showdate"]), -1 if r["venueid"] is None else int(r["venueid"]))
        for r in rows
    )
    return _sha12(_canonical([[d, v] for d, v in pairs]))


def _submitted_manifest_hash(submitted_dir: Path | str | None) -> str:
    """Hash of the submissions inbox (path + content hash of each file), so a
    new agent submission changes the epoch and triggers a republish (§6)."""
    if submitted_dir is None:
        return _sha12("[]")
    root = Path(submitted_dir)
    if not root.exists():
        return _sha12("[]")
    entries = []
    for f in sorted(root.rglob("*.json")):
        content_hash = hashlib.sha256(f.read_bytes()).hexdigest()[:12]
        entries.append([f.relative_to(root).as_posix(), content_hash])
    entries.sort()
    return _sha12(_canonical(entries))


def compute_epoch(
    conn: sqlite3.Connection,
    *,
    model: str = "heuristic",
    n_sims: int = 2000,
    seed: int = 0,
    half_life: int = 50,
    compare_models: list[str] | None = None,
    submitted_dir: Path | str | None = None,
) -> tuple[str, dict]:
    """Return (epoch_hex12, components). Deterministic; no simulation.

    ``compare_models`` (the extra per-show statistical columns publish emits)
    is part of the identity: changing the published model set must re-publish.
    """
    components = {
        "max_played_show_index": _max_played_show_index(conn),
        "schedule_hash": _schedule_hash(conn),
        "code_version": code_version(),
        "model": model,
        "n_sims": n_sims,
        "seed": seed,
        "half_life": half_life,
        "compare_models": sorted(compare_models or []),
        "submitted_manifest_hash": _submitted_manifest_hash(submitted_dir),
    }
    return _sha12(_canonical(components)), components


def read_latest(pointer_path: Path | str) -> str | None:
    """Read the last-published epoch from a `latest.json` pointer, or None."""
    p = Path(pointer_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("epoch")
    except (json.JSONDecodeError, OSError):
        return None


def epoch_status(
    conn: sqlite3.Connection,
    *,
    pointer_path: Path | str,
    model: str = "heuristic",
    n_sims: int = 2000,
    seed: int = 0,
    half_life: int = 50,
    compare_models: list[str] | None = None,
    submitted_dir: Path | str | None = None,
) -> dict:
    """{"epoch", "changed", "components"} — changed vs the pointer's epoch."""
    epoch, components = compute_epoch(
        conn, model=model, n_sims=n_sims, seed=seed, half_life=half_life,
        compare_models=compare_models, submitted_dir=submitted_dir,
    )
    last = read_latest(pointer_path)
    return {"epoch": epoch, "changed": epoch != last, "components": components}


def emit_github_output(epoch: str, changed: bool) -> None:
    """Append `epoch=`/`changed=` to $GITHUB_OUTPUT so a workflow can gate."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"epoch={epoch}\n")
        f.write(f"changed={'true' if changed else 'false'}\n")
