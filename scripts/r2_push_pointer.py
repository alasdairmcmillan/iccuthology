"""Write the `latest.json` pointer object to the R2 bucket root.

Usage:
    python scripts/r2_push_pointer.py <epoch>

Writes {"epoch": <epoch>, "created_at": <utc iso Z>} — the Worker resolves the
current epoch from this object, and the publish workflow's "Restore data from
R2" step pulls it back down to data/predictions/latest.json next run so
`phishpred epoch` can gate on whether the epoch actually changed (deploy plan
§4/§6, DEPLOY-CONTRACTS.md §1/§2).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import r2_common

POINTER_KEY = "latest.json"


def build_pointer(epoch: str) -> dict:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"epoch": epoch, "created_at": created_at}


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python scripts/r2_push_pointer.py <epoch>", file=sys.stderr)
        raise SystemExit(2)
    epoch = args[0]
    pointer = build_pointer(epoch)
    data = json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode("utf-8")
    r2_common.upload_bytes(data, POINTER_KEY, content_type="application/json")
    print(f"wrote {POINTER_KEY}: {pointer}")


if __name__ == "__main__":
    main()
