"""Pull one or more R2 keys/prefixes down to local paths.

Usage:
    python scripts/r2_pull.py <r2_key_or_prefix> <local_path> [more pairs...]

A trailing "/" on the R2 key means "prefix" — download every object under it
recursively. Otherwise it's a single object key. Missing keys/empty prefixes
are non-fatal (warning to stderr) so this works against a fresh, empty bucket
(deploy plan §2, DEPLOY-CONTRACTS.md §2).

Example (restore state before a publish run):
    python scripts/r2_pull.py state/phish.db data/phish.db submitted/ data/predictions/submitted/
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import ClientError

from scripts import r2_common


def pull_one(remote: str, local: str) -> None:
    local_path = Path(local)
    if remote.endswith("/"):
        keys = r2_common.list_prefix(remote)
        if not keys:
            print(f"warning: no objects found under r2 prefix {remote!r} (skipping)", file=sys.stderr)
            return
        r2_common.download_prefix(remote, local_path)
        print(f"downloaded {len(keys)} object(s): {remote} -> {local_path}")
    else:
        try:
            r2_common.download_file(remote, local_path)
            print(f"downloaded {remote} -> {local_path}")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey"):
                print(f"warning: r2 key {remote!r} not found (skipping)", file=sys.stderr)
                return
            raise


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or len(args) % 2 != 0:
        print(
            "usage: python scripts/r2_pull.py <r2_key_or_prefix> <local_path> [more pairs...]",
            file=sys.stderr,
        )
        raise SystemExit(2)
    for remote, local in zip(args[0::2], args[1::2]):
        pull_one(remote, local)


if __name__ == "__main__":
    main()
