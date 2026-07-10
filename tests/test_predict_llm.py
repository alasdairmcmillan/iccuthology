"""LLM model strings end-to-end: `predict_show("llm:...")` and
`publish --compare-models llm:...` (DEPLOY-CONTRACTS.md §2 show sources).

No network — a FakeLLMClient (same pattern as tests/test_llm.py) is patched in
for `models.llm.get_client`. The DB fixture mirrors tests/test_publish.py:
2022 history + 2026 future shows.
"""
from __future__ import annotations

import json
import re

import pytest

from phishpred import config, db
from phishpred.models.llm import LLMError, PredictionCache, parse_model_spec
from phishpred.predict import predict_show
from phishpred.publish import publish

VENUES = [(1, "Alpha", "AlphaCity", 0), (2, "Beta", "BetaCity", 0)]
SONGS = [
    (101, "tweezer", "Tweezer", 1),
    (102, "yem", "YEM", 1),
    (103, "wilson", "Wilson", 1),
    (104, "gin", "Bathtub Gin", 1),
    (105, "filler", "Filler", 1),
]
HIST_SHOWS = [
    (1, 0, "2022-06-01", 1, 1), (2, 1, "2022-06-02", 1, 1), (3, 2, "2022-06-03", 1, 1),
    (4, 3, "2022-06-10", 2, 1), (5, 4, "2022-06-11", 2, 1), (6, 5, "2022-06-20", 1, 1),
    (7, 6, "2022-07-01", 1, 2), (8, 7, "2022-07-02", 1, 2), (9, 8, "2022-07-10", 2, 2),
    (10, 9, "2022-07-11", 2, 2),
]
HIST_SETLISTS = {
    1: [101, 103, 104, 102], 2: [101, 102], 3: [101, 103], 4: [101, 104, 102],
    5: [101, 103], 6: [101, 102], 7: [101, 103, 102], 8: [101, 105, 102],
    9: [101, 102], 10: [101, 103, 102],
}
FUTURE = [
    (1101, "2026-07-10", 1, 10, "2026 Summer Tour"),
    (1102, "2026-07-11", 1, 10, "2026 Summer Tour"),
    (1103, "2026-07-18", 2, 10, "2026 Summer Tour"),
]


def _populate(conn):
    for vid, name, city, alias in VENUES:
        conn.execute("INSERT INTO venues (venueid, name, city, alias) VALUES (?,?,?,?)", (vid, name, city, alias))
    for sid, slug, name, iso in SONGS:
        conn.execute("INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,?)", (sid, slug, name, iso))
    for showid, idx, showdate, vid, tour in HIST_SHOWS:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) VALUES (?,?,?,?,?,0)",
            (showid, showdate, vid, tour, idx),
        )
    for showid, showdate, vid, tourid, tour_name in FUTURE:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, tour_name, show_index, exclude) "
            "VALUES (?,?,?,?,?,NULL,0)",
            (showid, showdate, vid, tourid, tour_name),
        )
    for showid, songs in HIST_SETLISTS.items():
        for pos, songid in enumerate(songs):
            conn.execute(
                "INSERT INTO performances (showid, songid, set_label, position) VALUES (?,?,?,?)",
                (showid, songid, "1", pos),
            )
    conn.commit()


@pytest.fixture()
def conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    _populate(c)
    yield c
    c.close()


class FakeLLMClient:
    """Canned-JSON LLMClient keyed by the 'Show date:' line of the prompt.

    ``fail_with`` simulates a call-time failure (e.g. a missing API key raises
    LLMError inside complete_json, exactly like the real adapters).
    """

    provider = "fake"

    def __init__(self, responses, model="fake-1", fail_with=None):
        self.model = model
        self.responses = responses
        self.fail_with = fail_with
        self.calls = []

    def complete_json(self, system, user, schema, *, max_tokens=2048):
        m = re.search(r"Show date: (\S+)", user)
        showdate = m.group(1) if m else None
        self.calls.append(showdate)
        if self.fail_with is not None:
            raise LLMError(self.fail_with)
        return self.responses[showdate]


# Every future show gets the same response; filler is omitted on purpose so it
# must fall back to LLMSongModel's floor probability. Raw sum (2.41 with the
# floor) sits near K (~2.6) so renormalization never hits the 0.99 cap and the
# LLM's strict ordering survives into the published probs.
RESPONSES = {
    d: {
        "predictions": [
            {"slug": "tweezer", "prob": 0.9},
            {"slug": "yem", "prob": 0.7},
            {"slug": "wilson", "prob": 0.5},
            {"slug": "gin", "prob": 0.3},
        ]
    }
    for d in ("2026-07-10", "2026-07-11", "2026-07-18")
}


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(
        "phishpred.models.llm.get_client", lambda provider, model, **kw: fake
    )


# --------------------------------------------------------------------------- #
# parse_model_spec
# --------------------------------------------------------------------------- #
def test_parse_model_spec_defaults_and_explicit():
    assert parse_model_spec("llm:anthropic") == ("anthropic", "claude-sonnet-5")
    assert parse_model_spec("llm:anthropic:claude-x") == ("anthropic", "claude-x")
    assert parse_model_spec("llm:openai-compat:qwen3") == ("openai-compat", "qwen3")


@pytest.mark.parametrize("spec", ["llm:", "llm", "lr", "llm:openai-compat"])
def test_parse_model_spec_rejects_bad_specs(spec):
    with pytest.raises(LLMError):
        parse_model_spec(spec)


# --------------------------------------------------------------------------- #
# predict_show with an llm:* model string
# --------------------------------------------------------------------------- #
def test_predict_show_llm_end_to_end(conn, tmp_path, monkeypatch):
    fake = FakeLLMClient(dict(RESPONSES))
    _patch_client(monkeypatch, fake)
    cache = PredictionCache(cache_dir=tmp_path / "cache")

    pred = predict_show(conn, "2026-07-10", model="llm:anthropic", llm_cache=cache)

    # Resolved name: defaulted model id filled in from the (fake) client.
    assert pred.model == "llm:anthropic:fake-1"
    assert len(fake.calls) == 1

    by_slug = {r.slug: r for r in pred.rows}
    assert set(by_slug) == {"tweezer", "yem", "wilson", "gin", "filler"}
    # Renormalized to K per show, same as every other source.
    assert sum(r.prob for r in pred.rows) == pytest.approx(pred.k, abs=1e-6)
    # LLM ranking preserved; the omitted candidate floored before renormalization.
    probs = [by_slug[s].prob for s in ("tweezer", "yem", "wilson", "gin", "filler")]
    assert probs == sorted(probs, reverse=True)
    assert len(set(probs)) == 5
    assert 0.0 < by_slug["filler"].prob < 0.05  # floor-scaled, not zero
    # No per-song drivers on the LLM path (as with GBM).
    assert all(r.drivers == [] for r in pred.rows)


def test_predict_show_llm_reuses_disk_cache(conn, tmp_path, monkeypatch):
    fake = FakeLLMClient(dict(RESPONSES))
    _patch_client(monkeypatch, fake)
    cache = PredictionCache(cache_dir=tmp_path / "cache")

    p1 = predict_show(conn, "2026-07-10", model="llm:anthropic", llm_cache=cache)
    p2 = predict_show(conn, "2026-07-10", model="llm:anthropic", llm_cache=cache)
    assert len(fake.calls) == 1  # second call served from the cache
    assert [(r.slug, r.prob) for r in p1.rows] == [(r.slug, r.prob) for r in p2.rows]


def test_predict_show_llm_prompt_includes_k_hint(conn, tmp_path, monkeypatch):
    prompts = []

    class RecordingClient(FakeLLMClient):
        def complete_json(self, system, user, schema, *, max_tokens=2048):
            prompts.append(user)
            return super().complete_json(system, user, schema, max_tokens=max_tokens)

    fake = RecordingClient(dict(RESPONSES))
    _patch_client(monkeypatch, fake)

    pred = predict_show(
        conn, "2026-07-10", model="llm:anthropic",
        llm_cache=PredictionCache(cache_dir=tmp_path / "cache"),
    )
    assert f"Target setlist size (K, rough anchor): {pred.k:.1f}" in prompts[0]


def test_predict_show_unknown_model_still_value_error(conn):
    with pytest.raises(ValueError, match="Unknown model"):
        predict_show(conn, "2026-07-10", model="xgboost")


def test_predict_show_bad_llm_spec_raises_llm_error(conn):
    with pytest.raises(LLMError):
        predict_show(conn, "2026-07-10", model="llm:")


# --------------------------------------------------------------------------- #
# publish --compare-models llm:...
# --------------------------------------------------------------------------- #
N_SIMS = 30


@pytest.fixture()
def llm_cache_dir(tmp_path, monkeypatch):
    """Point the default PredictionCache (config.RAW_DIR/llm_cache) at tmp."""
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    return tmp_path / "raw" / "llm_cache"


def test_publish_folds_llm_compare_source(conn, tmp_path, monkeypatch, llm_cache_dir):
    fake = FakeLLMClient(dict(RESPONSES))
    _patch_client(monkeypatch, fake)

    out = tmp_path / "snap"
    meta = publish(conn, out, n_sims=N_SIMS, seed=0, compare_models=["llm:anthropic"])
    assert meta["models"] == ["heuristic", "llm:anthropic"]

    for showdate in ("2026-07-10", "2026-07-11", "2026-07-18"):
        show = json.loads((out / "show" / f"{showdate}.json").read_text())
        assert show["sources"]["heuristic"]["kind"] == "statistical"
        src = show["sources"]["llm:anthropic"]
        assert src["kind"] == "llm"
        assert src["model"] == "llm:anthropic:fake-1"
        probs = [r["prob"] for r in src["rows"]]
        assert probs == sorted(probs, reverse=True)
        assert sum(probs) == pytest.approx(show["k"], abs=1e-3)
    assert len(fake.calls) == 3  # one LLM call per horizon show

    # Republishing reuses the on-disk PredictionCache — no re-billing.
    fake2 = FakeLLMClient(dict(RESPONSES))
    _patch_client(monkeypatch, fake2)
    publish(conn, tmp_path / "snap2", n_sims=N_SIMS, seed=0, compare_models=["llm:anthropic"])
    assert fake2.calls == []


def test_publish_skips_llm_source_when_key_missing(conn, tmp_path, monkeypatch, capsys, llm_cache_dir):
    fake = FakeLLMClient({}, fail_with="ANTHROPIC_API_KEY is not set.")
    _patch_client(monkeypatch, fake)

    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=0, compare_models=["llm:anthropic"])

    # The batch completes; the llm source is absent, the headline is intact.
    for showdate in ("2026-07-10", "2026-07-11", "2026-07-18"):
        show = json.loads((out / "show" / f"{showdate}.json").read_text())
        assert "llm:anthropic" not in show["sources"]
        assert show["sources"]["heuristic"]["rows"]
    # One warning, one attempt — the source is disabled after the first failure.
    assert len(fake.calls) == 1
    err = capsys.readouterr().err
    assert err.count("skipping compare source 'llm:anthropic'") == 1
    assert "ANTHROPIC_API_KEY" in err


def test_publish_statistical_compare_models_unaffected(conn, tmp_path, llm_cache_dir):
    # Guard: lr as a compare model still publishes (no LLM machinery involved).
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=0, compare_models=["lr"])
    show = json.loads((out / "show" / "2026-07-10.json").read_text())
    assert show["sources"]["lr"]["kind"] == "statistical"
    assert show["sources"]["lr"]["model"] == "lr"
