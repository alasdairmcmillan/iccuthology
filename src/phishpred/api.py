"""Thin client for the phish.net v5 API.

See CONTRACTS.md for the exact signatures this module must expose. Every
successful HTTP response is cached to disk (raw, pre-parse) so re-running the
CLI never hits the network twice for the same method/params combination
unless ``force=True`` is passed.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from .config import BASE_URL, RAW_DIR, get_api_key

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 4

# Module-level timestamp (monotonic clock) of the last real network request,
# shared across client instances so the throttle holds even if callers build
# multiple PhishNetClient objects in the same process.
_last_request_monotonic: float | None = None


class PhishNetError(Exception):
    """Raised when the phish.net API responds with a non-empty error_message."""


class PhishNetClient:
    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path = RAW_DIR,
        throttle_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = 1.0,
    ) -> None:
        # Deliberately do NOT resolve the API key here — cache-only usage
        # (e.g. tests, or re-reading already-cached years) must work without
        # PHISHNET_API_KEY / .env being present. Resolved lazily in _request().
        self._api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.throttle_seconds = throttle_seconds
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._transport = transport
        self._http: httpx.Client | None = None

    # -- internals ---------------------------------------------------------

    def _resolve_api_key(self) -> str:
        if self._api_key is None:
            self._api_key = get_api_key()
        return self._api_key

    def _http_client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(transport=self._transport)
        return self._http

    def _cache_path(self, method_path: str) -> Path:
        return self.cache_dir / (method_path.replace("/", "_") + ".json")

    @staticmethod
    def _parse(body: dict) -> list[dict]:
        error_message = body.get("error_message") if isinstance(body, dict) else None
        if error_message:
            raise PhishNetError(error_message)
        if not isinstance(body, dict):
            return []
        return body.get("data") or []

    def _throttle(self) -> None:
        global _last_request_monotonic
        if self.throttle_seconds <= 0 or _last_request_monotonic is None:
            return
        elapsed = time.monotonic() - _last_request_monotonic
        remaining = self.throttle_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _request(self, url: str, params: dict) -> dict:
        global _last_request_monotonic
        client = self._http_client()
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                _last_request_monotonic = time.monotonic()
                resp = client.get(url, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    logger.warning(
                        "transport error on %s (attempt %d/%d): %s",
                        url, attempt + 1, self.max_retries, exc,
                    )
                    time.sleep(self.backoff_base * (2 ** attempt))
                    continue
                raise

            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"unretryable status {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
                if attempt < self.max_retries:
                    logger.warning(
                        "status %d on %s (attempt %d/%d), retrying",
                        resp.status_code, url, attempt + 1, self.max_retries,
                    )
                    time.sleep(self.backoff_base * (2 ** attempt))
                    continue
                resp.raise_for_status()

            resp.raise_for_status()
            return resp.json()

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unreachable: retry loop exited without result")  # pragma: no cover

    # -- public API ----------------------------------------------------------

    def get(self, method_path: str, force: bool = False, **params) -> list[dict]:
        """GET {BASE_URL}/{method_path}.json, returning response['data'].

        Raw body is cached to disk before parsing; a cache hit skips the
        network entirely (no API key needed) unless ``force=True``.
        """
        cache_path = self._cache_path(method_path)

        if cache_path.exists() and not force:
            logger.debug("cache hit: %s -> %s", method_path, cache_path)
            body = json.loads(cache_path.read_text(encoding="utf-8"))
            return self._parse(body)

        query = dict(params)
        query["apikey"] = self._resolve_api_key()
        url = f"{BASE_URL}/{method_path}.json"

        body = self._request(url, query)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(body), encoding="utf-8")

        return self._parse(body)

    def shows_by_year(self, year: int, force: bool = False) -> list[dict]:
        return self.get(f"shows/showyear/{year}", force=force)

    def setlists_by_year(self, year: int, force: bool = False) -> list[dict]:
        return self.get(f"setlists/showyear/{year}", force=force)

    def setlists_by_showdate(self, date: str, force: bool = False) -> list[dict]:
        return self.get(f"setlists/showdate/{date}", force=force)

    def songs(self, force: bool = False) -> list[dict]:
        return self.get("songs", force=force)

    def venues(self, force: bool = False) -> list[dict]:
        return self.get("venues", force=force)
