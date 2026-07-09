"""Push a local file or directory up to an R2 key/prefix.

Usage:
    python scripts/r2_push.py <local_path> <r2_key_or_prefix>

A local file uploads to a single object key. A local directory uploads
recursively under the given prefix, with Content-Type resolved per file via
`content_type_for` (deploy plan §2, DEPLOY-CONTRACTS.md §2).

Examples:
    python scripts/r2_push.py build/snapshots snapshots/a1b2c3d4e5f6
    python scripts/r2_push.py data/phish.db state/phish.db
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import r2_common


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python scripts/r2_push.py <local_path> <r2_key_or_prefix>", file=sys.stderr)
        raise SystemExit(2)
    local, remote = args
    local_path = Path(local)

    if local_path.is_dir():
        keys = r2_common.upload_dir(local_path, remote)
        print(f"uploaded {len(keys)} object(s): {local_path} -> {remote}")
    elif local_path.is_file():
        r2_common.upload_file(local_path, remote)
        print(f"uploaded {local_path} -> {remote}")
    else:
        print(f"error: local path {local!r} does not exist", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
