"""Cloudflare R2 (S3-compatible) client + shared helpers for the publish
workflow (deploy plan §2, §4; DEPLOY-CONTRACTS.md §2).

R2 credentials come from environment variables — never hardcoded:
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
See docs/DEPLOY.md for how to generate them.
"""
from __future__ import annotations

import os
from pathlib import Path

import boto3
from botocore.config import Config

_REQUIRED_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def _require_env() -> dict[str, str]:
    missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            "Missing required R2 environment variable(s): " + ", ".join(missing) +
            ". Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET "
            "(see docs/DEPLOY.md)."
        )
    return {k: os.environ[k] for k in _REQUIRED_ENV}


def get_client():
    """Return a boto3 S3 client configured for Cloudflare R2."""
    env = _require_env()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def bucket() -> str:
    """Return the configured R2 bucket name."""
    return _require_env()["R2_BUCKET"]


def content_type_for(path: str | Path) -> str:
    """Best-effort Content-Type for an R2 object key or local path."""
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return "application/json"
    if suffix == ".bin":
        return "application/octet-stream"
    return "application/octet-stream"


def _posix_key(*parts: str) -> str:
    """Join key/prefix parts with forward slashes (R2 keys are POSIX-style
    regardless of the host OS)."""
    joined = "/".join(p.strip("/") for p in parts if p)
    return joined


def upload_file(local: str | Path, key: str, client=None) -> None:
    """Upload a single local file to R2 at `key`. Pass `client` to reuse an
    existing boto3 client across many calls (e.g. `upload_dir`)."""
    local = Path(local)
    (client or get_client()).upload_file(
        str(local), bucket(), key,
        ExtraArgs={"ContentType": content_type_for(local)},
    )


def download_file(key: str, local: str | Path, client=None) -> None:
    """Download a single R2 object to a local path, creating parent dirs.
    Pass `client` to reuse an existing boto3 client across many calls (e.g.
    `download_prefix`)."""
    local = Path(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    (client or get_client()).download_file(bucket(), key, str(local))


def upload_bytes(data: bytes, key: str, content_type: str | None = None) -> None:
    """Upload an in-memory byte string to R2 at `key`."""
    get_client().put_object(
        Bucket=bucket(), Key=key, Body=data,
        ContentType=content_type or content_type_for(key),
    )


def list_prefix(prefix: str) -> list[str]:
    """List every object key under `prefix` (paginated)."""
    client = get_client()
    keys: list[str] = []
    continuation_token: str | None = None
    while True:
        kwargs = {"Bucket": bucket(), "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        resp = client.list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            break
        continuation_token = resp.get("NextContinuationToken")
    return keys


def download_prefix(prefix: str, local_dir: str | Path) -> list[str]:
    """Recursively download every object under `prefix` into `local_dir`,
    preserving the relative path under the prefix. Returns the keys
    downloaded (empty list if the prefix has no objects)."""
    local_dir = Path(local_dir)
    client = get_client()
    keys = list_prefix(prefix)
    for key in keys:
        rel = key[len(prefix):] if key.startswith(prefix) else key
        rel = rel.lstrip("/")
        if not rel:
            continue
        download_file(key, local_dir / rel, client=client)
    return keys


def upload_dir(local_dir: str | Path, prefix: str) -> list[str]:
    """Recursively upload every file under `local_dir` to R2 under `prefix`.
    Returns the keys uploaded."""
    local_dir = Path(local_dir)
    client = get_client()
    keys: list[str] = []
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        key = _posix_key(prefix, rel)
        upload_file(path, key, client=client)
        keys.append(key)
    return keys
