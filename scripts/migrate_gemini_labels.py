"""One-off migration: consolidate the versioned Gemini MCP labels
(gemini-3-1-pro-high, gemini-3-5-flash-high, gemini-3-6-flash-high) into two
unversioned tracks (gemini-pro, gemini-flash), matching the claude-* labels'
convention (identity label, not a pinned model version).

Where a showdate has submissions under BOTH gemini-3-5-flash-high and
gemini-3-6-flash-high, 3.6 wins and the 3.5 entry is dropped entirely (user
call: 3.6 superseded 3.5 for those shows, don't keep both).

Touches only the two places gemini labels are durably stored:
  - submitted/<label>/<showdate>.json  (what the next publish folds in)
  - frozen/show/<showdate>.json        (sources dict, locked once a show plays)

scorecards/*.json + scoreboard.json are NOT hand-patched here — they're
DERIVED from frozen/show, so after this script's --apply step, pull the
corrected frozen docs locally and run `phishpred score --force` to rebuild
them under the new labels (see the printed next-steps).

Usage:
    python -m uv run dotenv -f .env.local run -- python scripts/migrate_gemini_labels.py --dry-run
    python -m uv run dotenv -f .env.local run -- python scripts/migrate_gemini_labels.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import r2_common  # noqa: E402

LABEL_MAP = {
    "gemini-3-1-pro-high": "gemini-pro",
    "gemini-3-5-flash-high": "gemini-flash",
    "gemini-3-6-flash-high": "gemini-flash",
}
# When a showdate has both, the second supersedes the first (dropped, not renamed).
SUPERSEDED_BY = {"gemini-3-5-flash-high": "gemini-3-6-flash-high"}


def _rewrite_submitted(dry_run: bool) -> None:
    """submitted/<old-label>/<showdate>.json -> submitted/<new-label>/<showdate>.json."""
    client = r2_common.get_client()
    keys_by_label = {old: r2_common.list_prefix(f"submitted/{old}/") for old in LABEL_MAP}
    six_dates = {Path(k).stem for k in keys_by_label["gemini-3-6-flash-high"]}

    plan: list[tuple[str, str]] = []
    drop: list[str] = []
    for old_label, keys in keys_by_label.items():
        new_label = LABEL_MAP[old_label]
        superseder_dates = six_dates if SUPERSEDED_BY.get(old_label) == "gemini-3-6-flash-high" else set()
        for key in keys:
            showdate = Path(key).stem
            if showdate in superseder_dates:
                drop.append(key)
                continue
            plan.append((key, f"submitted/{new_label}/{showdate}.json"))

    print(f"submitted/: {len(plan)} file(s) to migrate, {len(drop)} superseded 3.5 file(s) to drop")
    for old_key, new_key in plan:
        print(f"  {old_key} -> {new_key}")
    for old_key in drop:
        print(f"  DROP (superseded by 3.6): {old_key}")

    if dry_run:
        return

    for old_key, new_key in plan:
        obj = client.get_object(Bucket=r2_common.bucket(), Key=old_key)
        doc = json.loads(obj["Body"].read())
        doc["model_label"] = new_key.split("/")[1]
        r2_common.upload_bytes(json.dumps(doc, indent=2).encode(), new_key)
    for keys in keys_by_label.values():
        for key in keys:
            client.delete_object(Bucket=r2_common.bucket(), Key=key)


def _rewrite_frozen(dry_run: bool) -> None:
    """frozen/show/{showdate}.json: rename mcp:<old> source keys to mcp:<new>,
    dropping mcp:gemini-3-5-flash-high when mcp:gemini-3-6-flash-high is
    present in the same doc."""
    client = r2_common.get_client()
    keys = r2_common.list_prefix("frozen/show/")
    changed = 0
    for key in keys:
        obj = client.get_object(Bucket=r2_common.bucket(), Key=key)
        doc = json.loads(obj["Body"].read())
        sources = doc.get("sources", {})

        for old_label, superseder in SUPERSEDED_BY.items():
            old_src_key, superseder_src_key = f"mcp:{old_label}", f"mcp:{superseder}"
            if old_src_key in sources and superseder_src_key in sources:
                del sources[old_src_key]

        touched = False
        for old_label, new_label in LABEL_MAP.items():
            old_src_key = f"mcp:{old_label}"
            if old_src_key in sources:
                new_src_key = f"mcp:{new_label}"
                payload = sources.pop(old_src_key)
                payload["model"] = new_src_key
                sources[new_src_key] = payload
                touched = True

        if touched:
            changed += 1
            print(f"  {key}: rewrote source keys")
            if not dry_run:
                r2_common.upload_bytes(json.dumps(doc, indent=2).encode(), key)
    print(f"frozen/show/: {changed} doc(s) touched")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dry_run = not args.apply

    print("=== submitted/ ===")
    _rewrite_submitted(dry_run)
    print("\n=== frozen/show/ ===")
    _rewrite_frozen(dry_run)

    if dry_run:
        print("\nDRY RUN -- nothing written. Re-run with --apply to execute.")
    else:
        print(
            "\nDone. Next steps:\n"
            "  1. python scripts/r2_pull.py frozen/show/ data/frozen/show  (refresh local copy, "
            "trailing slash = prefix pull)\n"
            "  2. .\\.venv\\Scripts\\python.exe -m phishpred.cli score --frozen data/frozen/show "
            "--out data/scorecards --force  (rebuilds scorecards + scoreboard.json under the new labels)\n"
            "  3. python scripts/r2_push.py data/scorecards scorecards  (push the rebuilt scorecards back)\n"
            "  4. update agents/antigravity/*.py model_label constants + AGENTS.md/docs/MCP.md/"
            "GEMINI.md/DEPLOY-CONTRACTS.md to drop the version suffix (gemini-pro / gemini-flash)\n"
            "  5. regenerate web dev fixtures (scripts/gen_web_fixtures.py)"
        )


if __name__ == "__main__":
    main()
