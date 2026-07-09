"""Tests for phishpred.api.PhishNetClient — no network, everything routed
through httpx.MockTransport."""
from __future__ import annotations

import json

import httpx
import pytest

from phishpred.api import PhishNetClient, PhishNetError


def _counting_transport(handler):
    """Wrap a handler(request) -> httpx.Response in an httpx.MockTransport
    that also records how many requests actually hit the network."""
    calls: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    return httpx.MockTransport(wrapped), calls


def _success(data):
    return httpx.Response(200, json={"error": False, "error_message": "", "data": data})


def _error(message, code=2):
    return httpx.Response(200, json={"error": code, "error_message": message, "data": []})


# -- caching -------------------------------------------------------------


def test_get_caches_and_second_call_skips_network(tmp_path):
    def handler(request):
        return _success([{"showid": 1}])

    transport, calls = _counting_transport(handler)
    client = PhishNetClient(api_key="testkey", cache_dir=tmp_path, throttle_seconds=0, transport=transport)

    result1 = client.get("shows/showyear/2025")
    assert result1 == [{"showid": 1}]
    assert len(calls) == 1

    cache_file = tmp_path / "shows_showyear_2025.json"
    assert cache_file.exists()

    result2 = client.get("shows/showyear/2025")
    assert result2 == [{"showid": 1}]
    assert len(calls) == 1  # no new network call


def test_force_bypasses_cache(tmp_path):
    def handler(request):
        return _success([{"showid": 1}])

    transport, calls = _counting_transport(handler)
    client = PhishNetClient(api_key="testkey", cache_dir=tmp_path, throttle_seconds=0, transport=transport)

    client.get("shows/showyear/2025")
    assert len(calls) == 1
    client.get("shows/showyear/2025", force=True)
    assert len(calls) == 2


def test_cache_hit_needs_no_api_key(tmp_path):
    cache_file = tmp_path / "shows_showyear_2025.json"
    cache_file.write_text(json.dumps({"error": False, "error_message": "", "data": [{"showid": 7}]}))

    def handler(request):
        raise AssertionError("network should never be hit on a cache hit")

    client = PhishNetClient(api_key=None, cache_dir=tmp_path, throttle_seconds=0,
                             transport=httpx.MockTransport(handler))
    assert client.get("shows/showyear/2025") == [{"showid": 7}]


def test_cache_path_replaces_slashes(tmp_path):
    def handler(request):
        return _success([])

    transport, _calls = _counting_transport(handler)
    client = PhishNetClient(api_key="testkey", cache_dir=tmp_path, throttle_seconds=0, transport=transport)
    client.get("setlists/showdate/2025-06-20")
    assert (tmp_path / "setlists_showdate_2025-06-20.json").exists()


# -- error handling --------------------------------------------------------


def test_error_message_raises_phishneterror(tmp_path):
    def handler(request):
        return _error("Invalid API Key", code=2)

    transport, _ = _counting_transport(handler)
    client = PhishNetClient(api_key="bad-key", cache_dir=tmp_path, throttle_seconds=0, transport=transport)

    with pytest.raises(PhishNetError, match="Invalid API Key"):
        client.get("shows/showyear/2025")

    # Body must still have been cached (cache-before-parse), and re-reading
    # from cache re-raises the same error rather than silently succeeding.
    assert (tmp_path / "shows_showyear_2025.json").exists()
    with pytest.raises(PhishNetError, match="Invalid API Key"):
        client.get("shows/showyear/2025")


def test_empty_error_message_does_not_raise(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"error": False, "error_message": "", "data": [{"a": 1}]})

    transport, _ = _counting_transport(handler)
    client = PhishNetClient(api_key="testkey", cache_dir=tmp_path, throttle_seconds=0, transport=transport)
    assert client.get("songs") == [{"a": 1}]


# -- throttle & retries -----------------------------------------------------


def test_throttle_zero_means_no_wait(tmp_path):
    import time

    def handler(request):
        return _success([])

    transport, calls = _counting_transport(handler)
    client = PhishNetClient(api_key="testkey", cache_dir=tmp_path, throttle_seconds=0, transport=transport)

    start = time.monotonic()
    client.get("songs")
    client.get("venues")
    elapsed = time.monotonic() - start
    assert elapsed < 0.5
    assert len(calls) == 2


def test_throttle_enforced_between_requests(tmp_path):
    import time

    def handler(request):
        return _success([])

    transport, calls = _counting_transport(handler)
    client = PhishNetClient(api_key="testkey", cache_dir=tmp_path, throttle_seconds=0.3, transport=transport)

    start = time.monotonic()
    client.get("songs")
    client.get("venues")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.25
    assert len(calls) == 2


def test_retries_on_5xx_then_succeeds(tmp_path):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500, json={"error": 1, "error_message": "server error", "data": []})
        return _success([{"ok": True}])

    client = PhishNetClient(
        api_key="testkey", cache_dir=tmp_path, throttle_seconds=0,
        backoff_base=0.01, max_retries=4,
        transport=httpx.MockTransport(handler),
    )
    result = client.get("songs")
    assert result == [{"ok": True}]
    assert attempts["n"] == 3


def test_retries_on_429_then_succeeds(tmp_path):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(429, json={"error": 1, "error_message": "rate limited", "data": []})
        return _success([{"ok": True}])

    client = PhishNetClient(
        api_key="testkey", cache_dir=tmp_path, throttle_seconds=0,
        backoff_base=0.01, max_retries=4,
        transport=httpx.MockTransport(handler),
    )
    assert client.get("songs") == [{"ok": True}]
    assert attempts["n"] == 2


def test_retries_exhausted_raises(tmp_path):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(503, json={"error": 1, "error_message": "unavailable", "data": []})

    client = PhishNetClient(
        api_key="testkey", cache_dir=tmp_path, throttle_seconds=0,
        backoff_base=0.01, max_retries=2,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.get("songs")
    assert attempts["n"] == 3  # initial attempt + 2 retries


def test_transport_error_retried(tmp_path):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("boom", request=request)
        return _success([{"ok": True}])

    client = PhishNetClient(
        api_key="testkey", cache_dir=tmp_path, throttle_seconds=0,
        backoff_base=0.01, max_retries=4,
        transport=httpx.MockTransport(handler),
    )
    assert client.get("songs") == [{"ok": True}]
    assert attempts["n"] == 2


# -- convenience wrappers ---------------------------------------------------


def test_convenience_wrappers_hit_expected_paths(tmp_path):
    seen_paths: list[str] = []

    def handler(request):
        seen_paths.append(request.url.path)
        return _success([])

    client = PhishNetClient(api_key="testkey", cache_dir=tmp_path, throttle_seconds=0,
                             transport=httpx.MockTransport(handler))

    client.shows_by_year(2025)
    client.setlists_by_year(2025)
    client.setlists_by_showdate("2025-06-20")
    client.songs()
    client.venues()

    assert seen_paths == [
        "/v5/shows/showyear/2025.json",
        "/v5/setlists/showyear/2025.json",
        "/v5/setlists/showdate/2025-06-20.json",
        "/v5/songs.json",
        "/v5/venues.json",
    ]
