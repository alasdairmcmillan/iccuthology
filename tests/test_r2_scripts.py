"""Unit tests for the R2 push/pull scripts (deploy plan §2, DEPLOY-CONTRACTS.md
§2) — no network. `scripts.r2_common.get_client` is monkeypatched to a fake
boto3-shaped client that records every call."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from scripts import r2_common, r2_pull, r2_push, r2_push_pointer


class FakeS3Client:
    """Records calls and simulates a bucket as an in-memory key -> bytes map."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs) -> None:
        self.calls.append(("put_object", kwargs))
        self.objects[kwargs["Key"]] = kwargs.get("Body", b"")

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None) -> None:
        self.calls.append(
            ("upload_file", {"Filename": Filename, "Bucket": Bucket, "Key": Key, "ExtraArgs": ExtraArgs})
        )
        self.objects[Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket, Key, Filename) -> None:
        self.calls.append(("download_file", {"Bucket": Bucket, "Key": Key, "Filename": Filename}))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "GetObject")
        Path(Filename).write_bytes(self.objects[Key])

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None) -> dict:
        self.calls.append(("list_objects_v2", {"Bucket": Bucket, "Prefix": Prefix}))
        matched = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in matched], "IsTruncated": False}


@pytest.fixture(autouse=True)
def r2_env(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key123")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret123")
    monkeypatch.setenv("R2_BUCKET", "test-bucket")


@pytest.fixture()
def fake_client(monkeypatch):
    client = FakeS3Client()
    monkeypatch.setattr(r2_common, "get_client", lambda: client)
    return client


@pytest.fixture()
def counting_client(monkeypatch):
    """Like `fake_client`, but tracks how many times `get_client()` is
    called — used to assert multi-file helpers reuse a single client."""
    client = FakeS3Client()
    calls = {"n": 0}

    def _get_client():
        calls["n"] += 1
        return client

    monkeypatch.setattr(r2_common, "get_client", _get_client)
    client.get_client_calls = calls
    return client


# -- r2_common: env / content-type -------------------------------------------


def test_missing_env_var_raises_clear_error(monkeypatch):
    monkeypatch.delenv("R2_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="R2_BUCKET"):
        r2_common.bucket()


def test_content_type_for_mapping():
    assert r2_common.content_type_for("meta.json") == "application/json"
    assert r2_common.content_type_for("samples.bin") == "application/octet-stream"
    assert r2_common.content_type_for("phish.db") == "application/octet-stream"
    assert r2_common.content_type_for(Path("snapshots/e1/show/2026-07-10.json")) == "application/json"


# -- r2_common: file / bytes / prefix helpers --------------------------------


def test_upload_file_sets_content_type_and_bucket(fake_client, tmp_path):
    f = tmp_path / "meta.json"
    f.write_text("{}")
    r2_common.upload_file(f, "snapshots/e1/meta.json")

    kind, kwargs = fake_client.calls[-1]
    assert kind == "upload_file"
    assert kwargs["Bucket"] == "test-bucket"
    assert kwargs["Key"] == "snapshots/e1/meta.json"
    assert kwargs["ExtraArgs"]["ContentType"] == "application/json"


def test_upload_bytes_put_object(fake_client):
    r2_common.upload_bytes(b'{"epoch":"abc"}', "latest.json", content_type="application/json")

    kind, kwargs = fake_client.calls[-1]
    assert kind == "put_object"
    assert kwargs["Bucket"] == "test-bucket"
    assert kwargs["Key"] == "latest.json"
    assert kwargs["Body"] == b'{"epoch":"abc"}'
    assert kwargs["ContentType"] == "application/json"


def test_download_file_missing_key_raises_clienterror(fake_client, tmp_path):
    with pytest.raises(ClientError):
        r2_common.download_file("state/does-not-exist.db", tmp_path / "out.db")


def test_list_prefix_and_download_prefix_preserve_relative_paths(fake_client, tmp_path):
    fake_client.objects["submitted/claude/2026-07-10.json"] = b'{"a":1}'
    fake_client.objects["submitted/claude/2026-07-12.json"] = b'{"a":2}'
    fake_client.objects["state/phish.db"] = b"unrelated"

    keys = r2_common.list_prefix("submitted/")
    assert set(keys) == {"submitted/claude/2026-07-10.json", "submitted/claude/2026-07-12.json"}

    out_dir = tmp_path / "local_submitted"
    downloaded = r2_common.download_prefix("submitted/", out_dir)
    assert set(downloaded) == set(keys)
    assert (out_dir / "claude/2026-07-10.json").read_bytes() == b'{"a":1}'
    assert (out_dir / "claude/2026-07-12.json").read_bytes() == b'{"a":2}'


def test_upload_dir_uses_posix_keys_recursively(fake_client, tmp_path):
    d = tmp_path / "snapshots"
    (d / "show").mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "show" / "2026-07-10.json").write_text("{}")

    keys = r2_common.upload_dir(d, "snapshots/abc123")
    assert set(keys) == {"snapshots/abc123/meta.json", "snapshots/abc123/show/2026-07-10.json"}
    # keys must always use forward slashes regardless of host OS
    assert all("\\" not in k for k in keys)


def test_upload_dir_reuses_one_client_across_files(counting_client, tmp_path):
    d = tmp_path / "snapshots"
    (d / "show").mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "show" / "2026-07-10.json").write_text("{}")
    (d / "show" / "2026-07-12.json").write_text("{}")

    r2_common.upload_dir(d, "snapshots/abc123")

    # one get_client() call for the whole upload, not one per file
    assert counting_client.get_client_calls["n"] == 1


def test_download_prefix_reuses_one_client_across_files(counting_client, tmp_path):
    counting_client.objects["submitted/claude/2026-07-10.json"] = b'{"a":1}'
    counting_client.objects["submitted/claude/2026-07-12.json"] = b'{"a":2}'

    r2_common.download_prefix("submitted/", tmp_path / "out")

    # one for list_prefix + one for the shared download client, not one per file
    assert counting_client.get_client_calls["n"] == 2


# -- r2_push_pointer: shape ----------------------------------------------------


def test_build_pointer_shape():
    pointer = r2_push_pointer.build_pointer("a1b2c3d4e5f6")
    assert set(pointer.keys()) == {"epoch", "created_at"}
    assert pointer["epoch"] == "a1b2c3d4e5f6"
    # created_at must be a UTC ISO-8601 string ending in "Z"
    assert pointer["created_at"].endswith("Z")
    datetime.strptime(pointer["created_at"], "%Y-%m-%dT%H:%M:%SZ")


def test_push_pointer_main_writes_latest_json(fake_client):
    r2_push_pointer.main(["a1b2c3d4e5f6"])

    kind, kwargs = fake_client.calls[-1]
    assert kind == "put_object"
    assert kwargs["Key"] == "latest.json"
    assert kwargs["ContentType"] == "application/json"
    body = json.loads(kwargs["Body"])
    assert body["epoch"] == "a1b2c3d4e5f6"
    assert "created_at" in body


def test_push_pointer_requires_exactly_one_arg():
    with pytest.raises(SystemExit):
        r2_push_pointer.main([])
    with pytest.raises(SystemExit):
        r2_push_pointer.main(["a", "b"])


# -- r2_push: file-vs-prefix logic --------------------------------------------


def test_push_single_file_uploads_to_key(fake_client, tmp_path):
    f = tmp_path / "phish.db"
    f.write_bytes(b"sqlitedata")

    r2_push.main([str(f), "state/phish.db"])

    kind, kwargs = fake_client.calls[-1]
    assert kind == "upload_file"
    assert kwargs["Key"] == "state/phish.db"


def test_push_directory_uploads_every_file_under_prefix(fake_client, tmp_path):
    d = tmp_path / "snapshots"
    (d / "show").mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "show" / "2026-07-10.json").write_text("{}")

    r2_push.main([str(d), "snapshots/e1"])

    uploaded_keys = {kwargs["Key"] for kind, kwargs in fake_client.calls if kind == "upload_file"}
    assert uploaded_keys == {"snapshots/e1/meta.json", "snapshots/e1/show/2026-07-10.json"}


def test_push_missing_local_path_errors(fake_client, tmp_path):
    with pytest.raises(SystemExit):
        r2_push.main([str(tmp_path / "does-not-exist"), "state/x"])


def test_push_requires_exactly_two_args(fake_client, tmp_path):
    with pytest.raises(SystemExit):
        r2_push.main([str(tmp_path)])


# -- r2_pull: file-vs-prefix logic, non-fatal missing keys -------------------


def test_pull_single_key_downloads_to_local_path(fake_client, tmp_path):
    fake_client.objects["state/phish.db"] = b"dbdata"
    out_db = tmp_path / "phish.db"

    r2_pull.main(["state/phish.db", str(out_db)])

    assert out_db.read_bytes() == b"dbdata"
    assert fake_client.calls[-1][0] == "download_file"


def test_pull_prefix_downloads_recursively(fake_client, tmp_path):
    fake_client.objects["submitted/claude/2026-07-10.json"] = b'{"a":1}'
    fake_client.objects["submitted/claude/2026-07-12.json"] = b'{"a":2}'
    out_dir = tmp_path / "submitted"

    r2_pull.main(["submitted/", str(out_dir)])

    assert (out_dir / "claude/2026-07-10.json").read_bytes() == b'{"a":1}'
    assert (out_dir / "claude/2026-07-12.json").read_bytes() == b'{"a":2}'


def test_pull_missing_key_is_nonfatal_warning(fake_client, tmp_path, capsys):
    out = tmp_path / "missing.db"

    r2_pull.main(["state/does-not-exist.db", str(out)])

    assert not out.exists()
    assert "warning" in capsys.readouterr().err.lower()


def test_pull_empty_prefix_is_nonfatal_warning(fake_client, tmp_path, capsys):
    out_dir = tmp_path / "empty"

    r2_pull.main(["submitted/", str(out_dir)])

    assert not out_dir.exists()
    assert "warning" in capsys.readouterr().err.lower()


def test_pull_multiple_pairs_in_one_call(fake_client, tmp_path):
    fake_client.objects["state/phish.db"] = b"dbdata"
    fake_client.objects["submitted/claude/2026-07-10.json"] = b'{"a":1}'
    db_out = tmp_path / "phish.db"
    sub_out = tmp_path / "submitted"

    r2_pull.main(["state/phish.db", str(db_out), "submitted/", str(sub_out)])

    assert db_out.read_bytes() == b"dbdata"
    assert (sub_out / "claude/2026-07-10.json").exists()


def test_pull_requires_even_number_of_args():
    with pytest.raises(SystemExit):
        r2_pull.main(["only-one-arg"])
