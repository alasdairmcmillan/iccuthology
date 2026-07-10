"""LLM-as-model prediction path. See CONTRACTS.md / phish-predictor-modes-plan.md §7a.

Model-agnostic by design: instead of vendor SDKs, thin adapters call each
provider's REST API directly over ``httpx`` (already a project dependency).
Adding a new provider is just another small adapter implementing
``LLMClient``.

``LLMSongModel`` wraps any ``LLMClient`` and satisfies the
``CalibratedSongModel`` Protocol from ``models/ml.py`` (``name`` +
``predict_scores(df) -> np.ndarray``), so it drops into ``ml_predict`` and the
backtest machinery unchanged. One LLM call is made per distinct ``showid`` in
the input frame (never per row), and results are cached to disk keyed by
``(showid, model_name, prompt_version)`` so repeated runs/tests are free.

``llm_backtest`` mirrors ``backtest.predict_holdout`` for a single arbitrary
model so an LLM predictor can be scored on the exact same holdout/metrics as
heuristic/LR/GBM, without editing ``backtest.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

import httpx
import numpy as np
import pandas as pd

from phishpred import config
from phishpred.features import FEATURE_COLUMNS
from phishpred.models.ml import CalibratedSongModel

# Bump whenever the prompt/schema shape changes — it is part of the cache key,
# so a bump naturally invalidates all previously cached responses.
PROMPT_VERSION_DEFAULT = "v1"

# Probability assigned to a candidate the LLM's response omits entirely.
FLOOR_PROB = 0.01


class LLMError(RuntimeError):
    """Raised for any LLM call/parse failure: missing API key, HTTP error,
    malformed JSON, or a response that doesn't conform to the requested schema.
    """


# --------------------------------------------------------------------------- #
# Client protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class LLMClient(Protocol):
    """Structural type for a provider adapter."""

    model: str

    def complete_json(
        self, system: str, user: str, schema: dict, *, max_tokens: int = 2048
    ) -> dict:
        """Return a parsed JSON object conforming to ``schema``.

        Raises ``LLMError`` on any failure (network, auth, non-JSON response,
        schema violation from the provider's own structured-output feature).
        """
        ...  # pragma: no cover - protocol


def _require_key(env_var: str) -> str:
    config._load_env()
    key = os.getenv(env_var)
    if not key:
        raise LLMError(
            f"{env_var} is not set. Copy .env.example to .env (or .env.local) "
            "and add a key."
        )
    return key


# --------------------------------------------------------------------------- #
# Provider adapters — raw REST via httpx, no vendor SDKs.
# --------------------------------------------------------------------------- #
@dataclass
class AnthropicClient:
    """Adapter for the Anthropic Messages API. Structured output via forced
    tool use: the JSON schema is registered as a single tool's ``input_schema``
    and ``tool_choice`` forces that tool, so ``tool_use.input`` IS the JSON
    object — no free-text JSON parsing needed.
    """

    model: str = "claude-sonnet-5"
    api_key: str | None = None
    base_url: str = "https://api.anthropic.com"
    timeout: float = 60.0
    provider: str = "anthropic"

    def _key(self) -> str:
        return self.api_key or _require_key("ANTHROPIC_API_KEY")

    def complete_json(
        self, system: str, user: str, schema: dict, *, max_tokens: int = 2048
    ) -> dict:
        tool = {
            "name": "emit_predictions",
            "description": "Emit the structured prediction JSON object.",
            "input_schema": schema,
        }
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": "emit_predictions"},
        }
        headers = {
            "x-api-key": self._key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/messages", json=payload, headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc

        data = resp.json()
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "emit_predictions":
                return block["input"]
        raise LLMError(f"Anthropic response missing tool_use block: {data!r}")


@dataclass
class OpenAIClient:
    """Adapter for the OpenAI-shaped Chat Completions API.

    Also serves the ``openai-compat`` provider (arbitrary OpenAI-compatible
    endpoint via ``base_url`` — local servers, Together, OpenRouter, etc.);
    ``require_key=False`` lets that path skip the hard failure when no key is
    configured, since many local endpoints don't check one.
    """

    model: str = "gpt-4.1"
    api_key: str | None = None
    base_url: str = "https://api.openai.com"
    timeout: float = 60.0
    provider: str = "openai"
    require_key: bool = True

    def _key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.require_key:
            return _require_key("OPENAI_API_KEY")
        return os.getenv("OPENAI_API_KEY")

    def complete_json(
        self, system: str, user: str, schema: dict, *, max_tokens: int = 2048
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "predictions", "schema": schema, "strict": True},
            },
            "max_tokens": max_tokens,
        }
        headers = {"content-type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/chat/completions", json=payload, headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"OpenAI response missing content: {data!r}") from exc
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"OpenAI response not valid JSON: {content!r}") from exc


@dataclass
class GoogleClient:
    """Adapter for Gemini's ``generateContent`` with JSON-constrained output."""

    model: str = "gemini-2.5-flash"
    api_key: str | None = None
    base_url: str = "https://generativelanguage.googleapis.com"
    timeout: float = 60.0
    provider: str = "google"

    def _key(self) -> str:
        return self.api_key or _require_key("GOOGLE_API_KEY")

    def complete_json(
        self, system: str, user: str, schema: dict, *, max_tokens: int = 2048
    ) -> dict:
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "maxOutputTokens": max_tokens,
            },
        }
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        try:
            resp = httpx.post(
                url, params={"key": self._key()}, json=payload, timeout=self.timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Google request failed: {exc}") from exc

        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Google response missing text: {data!r}") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Google response not valid JSON: {text!r}") from exc


def get_client(
    provider: str, model: str, *, api_key: str | None = None, base_url: str | None = None,
    **kw: Any,
) -> LLMClient:
    """Construct an adapter. ``provider`` in {'anthropic','openai','google','openai-compat'}.

    ``openai-compat`` targets any OpenAI-compatible endpoint (open/local models)
    via ``base_url`` (falls back to the ``OPENAI_BASE_URL`` env var, then
    ``http://localhost:8000``); it does not hard-require an API key.
    """
    p = provider.lower()
    if p == "anthropic":
        return AnthropicClient(
            model=model, api_key=api_key, base_url=base_url or "https://api.anthropic.com", **kw
        )
    if p == "openai":
        return OpenAIClient(
            model=model, api_key=api_key, base_url=base_url or "https://api.openai.com", **kw
        )
    if p == "google":
        return GoogleClient(
            model=model, api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com", **kw,
        )
    if p == "openai-compat":
        resolved = base_url or os.getenv("OPENAI_BASE_URL") or "http://localhost:8000"
        return OpenAIClient(
            model=model, api_key=api_key, base_url=resolved, provider="openai-compat",
            require_key=False, **kw,
        )
    raise LLMError(
        f"Unknown provider: {provider!r}. Expected one of "
        "'anthropic', 'openai', 'google', 'openai-compat'."
    )


# Per-provider default model id, shared by the CLI's --provider options and
# "llm:<provider>" model specs. openai-compat endpoints serve arbitrary open
# models, so they must name theirs explicitly.
DEFAULT_MODELS: dict[str, str | None] = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4.1",
    "google": "gemini-2.5-flash",
    "openai-compat": None,
}


def parse_model_spec(spec: str) -> tuple[str, str]:
    """Parse an ``llm:<provider>[:<model-id>]`` model string into
    ``(provider, model_id)``, falling back to ``DEFAULT_MODELS`` when the model
    id is omitted.

    Raises ``LLMError`` on a malformed spec (bad prefix, empty provider, or a
    provider with no default and no explicit model id) so callers can treat a
    bad source string the same as any other LLM failure.
    """
    parts = spec.split(":", 2)
    if parts[0] != "llm" or len(parts) < 2 or not parts[1]:
        raise LLMError(
            f"Invalid LLM model spec {spec!r}; expected 'llm:<provider>[:<model-id>]'."
        )
    provider = parts[1]
    model_id = parts[2] if len(parts) == 3 and parts[2] else DEFAULT_MODELS.get(provider)
    if not model_id:
        raise LLMError(
            f"No default model id for provider {provider!r}; "
            f"use 'llm:{provider}:<model-id>'."
        )
    return provider, model_id


# --------------------------------------------------------------------------- #
# Structured output schema
# --------------------------------------------------------------------------- #
PREDICTIONS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "predictions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "prob": {"type": "number"},
                },
                "required": ["slug", "prob"],
            },
        }
    },
    "required": ["predictions"],
}


def _validate_predictions_payload(result: Any) -> list[dict]:
    if not isinstance(result, dict) or "predictions" not in result:
        raise LLMError(f"LLM response missing 'predictions' key: {result!r}")
    preds = result["predictions"]
    if not isinstance(preds, list):
        raise LLMError(f"'predictions' must be a list, got {type(preds).__name__}: {preds!r}")
    for i, p in enumerate(preds):
        if not isinstance(p, dict) or "slug" not in p or "prob" not in p:
            raise LLMError(f"predictions[{i}] must have 'slug' and 'prob' keys: {p!r}")
        if not isinstance(p["slug"], str):
            raise LLMError(f"predictions[{i}].slug must be a string: {p!r}")
        try:
            float(p["prob"])
        except (TypeError, ValueError) as exc:
            raise LLMError(f"predictions[{i}].prob is not numeric: {p!r}") from exc
    return preds


# --------------------------------------------------------------------------- #
# Prediction cache — one JSON file per (showid, model_name, prompt_version).
# --------------------------------------------------------------------------- #
@dataclass
class PredictionCache:
    """Disk-backed cache keyed by ``(showid, model_name, prompt_version)``.

    Files live under ``cache_dir`` (default ``config.RAW_DIR / "llm_cache"``),
    one JSON file per key. Filenames are a sanitized prefix (for debuggability)
    plus a short hash of the exact key (for collision-safety with arbitrary
    model-name strings). Clear the whole cache with ``clear()``, or just delete
    the directory.
    """

    cache_dir: Path = field(default_factory=lambda: config.RAW_DIR / "llm_cache")

    def _safe(self, text: str) -> str:
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)[:60]

    def _path(self, showid: int, model_name: str, prompt_version: str) -> Path:
        key = f"{showid}:{model_name}:{prompt_version}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{showid}_{self._safe(model_name)}_{self._safe(prompt_version)}_{digest}.json"

    def get(self, showid: int, model_name: str, prompt_version: str) -> dict | None:
        path = self._path(showid, model_name, prompt_version)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, showid: int, model_name: str, prompt_version: str, payload: dict) -> None:
        path = self._path(showid, model_name, prompt_version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def clear(self) -> None:
        if self.cache_dir.exists():
            for p in self.cache_dir.glob("*.json"):
                p.unlink()


# --------------------------------------------------------------------------- #
# Prompt rendering
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are an expert Phish setlist predictor. You will be given the full list "
    "of candidate songs for one specific upcoming show, each with historical "
    "statistics engineered from the band's play history (decayed play rate, gap "
    "since last played, whether it was played the previous show or earlier in "
    "the current run, venue history, tour-so-far plays, era rate, etc.). "
    "Estimate the probability that EACH candidate is played at THIS show. "
    "Respond only through the provided structured schema: a 'predictions' array "
    "with one {slug, prob} object per candidate slug you were given (do not "
    "invent slugs, do not omit any). prob must be a number in [0, 1]. Use the "
    "target setlist size only as a rough calibration anchor for how many songs "
    "should have meaningfully high probability — do not force probabilities to "
    "sum exactly to it. Respect the band's rotation rules: a candidate with "
    "played_in_run=1 has already been played earlier in this same multi-night "
    "run and gets an effectively ~0-5% probability tonight (within-run repeats "
    "are essentially unheard of); played_prev_show=1 means it closed the "
    "immediately preceding show, which is similarly rare (~2%). Never assign "
    "high probability to both a song and its within-run repeat across nights."
)


def _render_candidates(show_df: pd.DataFrame) -> str:
    lines: list[str] = []
    for _, row in show_df.iterrows():
        parts = [str(row["slug"]), f"name={row['song_name']}"]
        for c in FEATURE_COLUMNS:
            v = row[c]
            if isinstance(v, float):
                parts.append(f"{c}={v:.3f}")
            else:
                parts.append(f"{c}={v}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _build_user_prompt(show_df: pd.DataFrame, k_hint: float | None, context: str | None) -> str:
    showdate = show_df["showdate"].iloc[0]
    lines = [f"Show date: {showdate}"]
    if k_hint is not None:
        lines.append(f"Target setlist size (K, rough anchor): {k_hint:.1f}")
    lines.append(f"Number of candidates: {len(show_df)}")
    if context:
        lines.append("")
        lines.append(context)
    lines.append("")
    lines.append("Candidates (slug | name | features):")
    lines.append(_render_candidates(show_df))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# LLMSongModel — CalibratedSongModel implementation
# --------------------------------------------------------------------------- #
class LLMSongModel:
    """Wraps an ``LLMClient`` as a ``CalibratedSongModel``.

    Makes exactly ONE LLM call per distinct ``showid`` present in the frame
    passed to ``predict_scores`` (never per row), caching the raw response by
    ``(showid, name, prompt_version)`` so repeated calls/tests/backtests are
    free after the first. Missing candidates in the LLM's response (it should
    not omit any, but real models sometimes do) get ``floor_prob``.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        prompt_version: str = PROMPT_VERSION_DEFAULT,
        cache: "PredictionCache | None" = None,
        context_fn: Callable[[pd.DataFrame], str] | None = None,
        k_hint_fn: Callable[[pd.DataFrame], float] | None = None,
        floor_prob: float = FLOOR_PROB,
        provider: str | None = None,
    ) -> None:
        self.client = client
        self.prompt_version = prompt_version
        self.cache = cache if cache is not None else PredictionCache()
        self.context_fn = context_fn
        self.k_hint_fn = k_hint_fn
        self.floor_prob = floor_prob

        provider_label = provider or getattr(client, "provider", None) or "unknown"
        self.name = f"llm:{provider_label}:{client.model}"

    def _predict_show(self, showid: int, show_df: pd.DataFrame) -> dict[str, float]:
        cached = self.cache.get(showid, self.name, self.prompt_version)
        if cached is not None:
            preds = _validate_predictions_payload(cached)
            return {p["slug"]: float(p["prob"]) for p in preds}

        k_hint = self.k_hint_fn(show_df) if self.k_hint_fn else None
        context = self.context_fn(show_df) if self.context_fn else None
        user = _build_user_prompt(show_df, k_hint, context)

        try:
            result = self.client.complete_json(SYSTEM_PROMPT, user, PREDICTIONS_SCHEMA)
        except LLMError:
            raise
        except Exception as exc:  # pragma: no cover - defensive wrap
            raise LLMError(f"LLM call failed for show {showid}: {exc}") from exc

        preds = _validate_predictions_payload(result)
        self.cache.set(showid, self.name, self.prompt_version, result)
        return {p["slug"]: float(p["prob"]) for p in preds}

    def predict_scores(self, df: pd.DataFrame) -> np.ndarray:
        """Per-row probabilities in [0, 1], pre-renormalization.

        One LLM call per distinct ``showid`` in ``df``; results mapped back
        onto rows by ``slug``.
        """
        if len(df) == 0:
            return np.zeros(0, dtype=float)
        if "showid" not in df.columns or "slug" not in df.columns:
            raise LLMError("LLMSongModel.predict_scores requires 'showid' and 'slug' columns")

        out = np.full(len(df), self.floor_prob, dtype=float)
        for showid, idx in df.groupby("showid", sort=False).groups.items():
            show_df = df.loc[idx]
            slug_probs = self._predict_show(int(showid), show_df)
            pos = df.index.get_indexer(idx)
            for p, slug in zip(pos, show_df["slug"]):
                out[p] = float(np.clip(slug_probs.get(slug, self.floor_prob), 0.0, 1.0))
        return out


# --------------------------------------------------------------------------- #
# Backtest bake-off — mirrors backtest.predict_holdout for one arbitrary model.
# --------------------------------------------------------------------------- #
def llm_backtest(
    conn: sqlite3.Connection,
    model: "CalibratedSongModel",
    *,
    half_life: int = 50,
    holdout_tours: int = 2,
    k_for_year: Callable[[int], float] | None = None,
) -> dict:
    """Score ``model`` on the same holdout construction as ``run_backtest``.

    Reuses ``backtest.select_holdout`` + ``features.build_features`` +
    ``models.ml.ml_predict`` (for the per-show K renormalization) +
    ``backtest.compute_metrics`` / ``calibration_table`` unmodified, so the
    numbers are directly comparable to the heuristic/lr/gbm rows from
    ``run_backtest``. One call to ``model.predict_scores`` per holdout show
    (cached by the model itself, e.g. via ``LLMSongModel``'s ``PredictionCache``).

    Returns ``{'metrics': {...}, 'calibration': [...], 'holdout': str}``.
    """
    from phishpred import backtest as bt
    from phishpred import features as feat
    from phishpred.models.ml import ml_predict

    holdout = bt.select_holdout(conn, holdout_tours=holdout_tours)
    holdout_showids = set(holdout.showids)

    if k_for_year is None:
        era_k_cache: dict[str, float] = {}

        def _k_for_year(year: int) -> float:
            era = config.era_for_year(year)
            if era not in era_k_cache:
                era_k_cache[era] = float(feat.mean_setlist_size(conn, era))
            return era_k_cache[era]

        k_for_year = _k_for_year

    features_df = feat.build_features(conn, half_life=half_life)
    holdout_df = features_df[features_df["showid"].isin(holdout_showids)]

    if len(holdout_df) == 0:
        return {
            "metrics": bt.compute_metrics(holdout_df),
            "calibration": [],
            "holdout": holdout.description,
        }

    parts: list[pd.DataFrame] = []
    for _, show_rows in holdout_df.groupby("showid"):
        year = int(str(show_rows["showdate"].iloc[0])[:4])
        k = k_for_year(year)
        parts.append(ml_predict(model, show_rows, k))
    pred_df = pd.concat(parts, ignore_index=True)

    metrics = bt.compute_metrics(pred_df)
    calibration = bt.calibration_table(
        pred_df["y"].to_numpy(dtype=float), pred_df["prob"].to_numpy(dtype=float)
    )
    return {"metrics": metrics, "calibration": calibration, "holdout": holdout.description}


def render_llm_backtest(result: dict, model_name: str) -> str:
    """Plain-text one-row summary, formatted to align with BacktestReport's table."""
    m = result["metrics"]
    header = f"{'model':<28} {'rows':>7} {'shows':>6} {'Brier':>9} {'LogLoss':>9} {'Hit@20':>8} {'Hit@25':>8}"
    row = (
        f"{model_name:<28} {m['n_rows']:>7} {m['n_shows']:>6} "
        f"{m['brier']:>9.4f} {m['log_loss']:>9.4f} {m['hit20']:>8.2f} {m['hit25']:>8.2f}"
    )
    lines = [f"LLM backtest — {model_name}", result["holdout"], "", header, row]
    return "\n".join(lines) + "\n"
