"""Tests for phishpred.models.llm — no network. FakeLLMClient stands in for a
real provider adapter; caching and the backtest bake-off are exercised against
a tiny in-memory DB, following the same seeding pattern as test_backtest.py.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from phishpred.db import get_connection, init_db
from phishpred.features import FEATURE_COLUMNS
from phishpred.models.llm import (
    FLOOR_PROB,
    LLMError,
    LLMSongModel,
    PredictionCache,
    llm_backtest,
)
from phishpred.models.ml import ml_predict


# --------------------------------------------------------------------------- #
# Fake client
# --------------------------------------------------------------------------- #
class FakeLLMClient:
    """Deterministic canned-JSON stand-in for a real LLMClient.

    Responses are keyed by the show date embedded in the rendered prompt
    ("Show date: <date>" line), so the same fake can serve a multi-show
    backtest without needing showid threaded through the LLMClient protocol.
    """

    provider = "fake"

    def __init__(
        self,
        responses: dict[str, dict],
        malformed_dates: set[str] | None = None,
        model: str = "fake-1",
    ) -> None:
        self.model = model
        self.responses = responses
        self.malformed_dates = malformed_dates or set()
        self.calls: list[str | None] = []

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 2048) -> dict:
        m = re.search(r"Show date: (\S+)", user)
        showdate = m.group(1) if m else None
        self.calls.append(showdate)
        if showdate in self.malformed_dates:
            return {"not_predictions": "oops"}
        return self.responses[showdate]


# --------------------------------------------------------------------------- #
# Hand-built candidate frames (no DB needed for the unit-level tests)
# --------------------------------------------------------------------------- #
def _make_show_frame(showid: int, showdate: str, slugs: list[str]) -> pd.DataFrame:
    n = len(slugs)
    data: dict[str, list] = {
        "showid": [showid] * n,
        "showdate": [showdate] * n,
        "show_index": list(range(n)),
        "venueid": [1] * n,
        "songid": list(range(100, 100 + n)),
        "slug": slugs,
        "song_name": [s.title() for s in slugs],
        "y": [0] * n,
    }
    rng = np.random.default_rng(abs(hash((showid, showdate))) % (2**32))
    for c in FEATURE_COLUMNS:
        data[c] = rng.uniform(0.0, 1.0, size=n)
    return pd.DataFrame(data)


@pytest.fixture
def two_show_df() -> pd.DataFrame:
    show1 = _make_show_frame(1, "2015-06-01", ["tweezer", "hood", "wilson"])
    show2 = _make_show_frame(2, "2015-06-02", ["mikes", "weekapaug"])
    return pd.concat([show1, show2], ignore_index=True)


RESPONSES = {
    "2015-06-01": {
        "predictions": [
            {"slug": "tweezer", "prob": 0.7},
            {"slug": "hood", "prob": 0.5},
            {"slug": "wilson", "prob": 0.2},
        ]
    },
    # weekapaug intentionally omitted -> should fall back to floor_prob.
    "2015-06-02": {"predictions": [{"slug": "mikes", "prob": 0.9}]},
}


# --------------------------------------------------------------------------- #
# predict_scores: one call per show, correct mapping, floor for omissions
# --------------------------------------------------------------------------- #
def test_predict_scores_one_call_per_show_and_maps_by_slug(tmp_path, two_show_df):
    fake = FakeLLMClient(dict(RESPONSES))
    cache = PredictionCache(cache_dir=tmp_path / "cache")
    model = LLMSongModel(fake, cache=cache)

    scores = model.predict_scores(two_show_df)

    assert len(fake.calls) == 2
    assert set(fake.calls) == {"2015-06-01", "2015-06-02"}

    assert len(scores) == len(two_show_df)
    assert np.all(scores >= 0.0) and np.all(scores <= 1.0)

    by_slug = dict(zip(two_show_df["slug"], scores))
    assert by_slug["tweezer"] == pytest.approx(0.7)
    assert by_slug["hood"] == pytest.approx(0.5)
    assert by_slug["wilson"] == pytest.approx(0.2)
    assert by_slug["mikes"] == pytest.approx(0.9)
    # Omitted from the fake's response -> floor probability.
    assert by_slug["weekapaug"] == pytest.approx(FLOOR_PROB)


def test_predict_scores_name_includes_provider_and_model(tmp_path, two_show_df):
    fake = FakeLLMClient(dict(RESPONSES))
    model = LLMSongModel(fake, cache=PredictionCache(cache_dir=tmp_path / "cache"))
    assert model.name == "llm:fake:fake-1"


# --------------------------------------------------------------------------- #
# Caching: second call must not hit the client again
# --------------------------------------------------------------------------- #
def test_cache_avoids_second_call(tmp_path, two_show_df):
    fake = FakeLLMClient(dict(RESPONSES))
    cache = PredictionCache(cache_dir=tmp_path / "cache")
    model = LLMSongModel(fake, cache=cache)

    model.predict_scores(two_show_df)
    assert len(fake.calls) == 2

    model.predict_scores(two_show_df)
    assert len(fake.calls) == 2  # no new calls

    # A fresh model instance sharing the same on-disk cache also hits no calls.
    fake2 = FakeLLMClient(dict(RESPONSES))
    model2 = LLMSongModel(fake2, cache=PredictionCache(cache_dir=tmp_path / "cache"))
    model2.predict_scores(two_show_df)
    assert len(fake2.calls) == 0


# --------------------------------------------------------------------------- #
# ml_predict renormalizes an LLMSongModel's scores to K per show
# --------------------------------------------------------------------------- #
def test_ml_predict_renormalizes_llm_model_to_k(tmp_path, two_show_df):
    fake = FakeLLMClient(dict(RESPONSES))
    model = LLMSongModel(fake, cache=PredictionCache(cache_dir=tmp_path / "cache"))

    k = 1.5  # below n*cap for both shows (min group size is 2 rows)
    out = ml_predict(model, two_show_df, k)
    assert "prob" in out.columns
    for _, g in out.groupby("showid"):
        assert g["prob"].sum() == pytest.approx(k, abs=1e-6)
        assert np.all((g["prob"] >= 0.0) & (g["prob"] <= 0.99 + 1e-9))


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
def test_malformed_response_raises_llm_error(tmp_path):
    frame = _make_show_frame(9, "2020-01-01", ["ac-dc-bag"])
    fake = FakeLLMClient({}, malformed_dates={"2020-01-01"})
    model = LLMSongModel(fake, cache=PredictionCache(cache_dir=tmp_path / "cache_bad"))

    with pytest.raises(LLMError):
        model.predict_scores(frame)


def test_missing_slug_or_prob_raises_llm_error(tmp_path):
    frame = _make_show_frame(10, "2020-02-01", ["fluffhead"])
    fake = FakeLLMClient({"2020-02-01": {"predictions": [{"slug": "fluffhead"}]}})
    model = LLMSongModel(fake, cache=PredictionCache(cache_dir=tmp_path / "cache_bad2"))

    with pytest.raises(LLMError):
        model.predict_scores(frame)


# --------------------------------------------------------------------------- #
# llm_backtest end-to-end on a tiny in-memory DB
# --------------------------------------------------------------------------- #
@pytest.fixture
def memory_conn():
    conn = get_connection(":memory:")
    try:
        yield conn
    finally:
        conn.close()


def _seed_llm_db(conn):
    init_db(conn)
    conn.execute("INSERT INTO venues (venueid, name) VALUES (1, 'Venue')")
    songs = [(1, "alpha", "Alpha", 1), (2, "beta", "Beta", 1), (3, "gamma", "Gamma", 1)]
    for songid, slug, name, iso in songs:
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,?)",
            (songid, slug, name, iso),
        )

    # showid, showdate, show_index, tourid, tour_name, setlist songids
    shows = [
        (1, "2015-01-01", 0, 10, "Tour A", [1, 2]),
        (2, "2015-01-02", 1, 10, "Tour A", [1, 3]),
        (3, "2016-01-01", 2, 20, "Tour B", [1, 2]),
        (4, "2016-01-02", 3, 20, "Tour B", [2, 3]),
        (5, "2017-01-01", 4, 30, "Tour C", [1, 3]),
        (6, "2017-01-02", 5, 30, "Tour C", [1, 2, 3]),
    ]
    for showid, date, idx, tourid, tname, setlist in shows:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, tour_name, "
            "exclude, show_index) VALUES (?,?,1,?,?,0,?)",
            (showid, date, tourid, tname, idx),
        )
        for pos, songid in enumerate(setlist):
            conn.execute(
                "INSERT INTO performances (showid, songid, set_label, position) "
                "VALUES (?,?,'1',?)",
                (showid, songid, pos),
            )
    conn.commit()
    return conn


def test_llm_backtest_end_to_end(memory_conn, tmp_path):
    conn = _seed_llm_db(memory_conn)

    # Holdout = Tour B + Tour C (4 shows). Each show's candidate set is all
    # three songs (each already played earlier, in Tour A).
    holdout_dates = ["2016-01-01", "2016-01-02", "2017-01-01", "2017-01-02"]
    responses = {
        d: {
            "predictions": [
                {"slug": "alpha", "prob": 0.6},
                {"slug": "beta", "prob": 0.3},
                {"slug": "gamma", "prob": 0.4},
            ]
        }
        for d in holdout_dates
    }
    fake = FakeLLMClient(responses)
    model = LLMSongModel(fake, cache=PredictionCache(cache_dir=tmp_path / "cache_bt"))

    result = llm_backtest(conn, model, holdout_tours=2)

    assert set(result.keys()) == {"metrics", "calibration", "holdout"}
    m = result["metrics"]
    for key in ("n_rows", "n_shows", "brier", "log_loss", "hit20", "hit25"):
        assert key in m

    assert m["n_shows"] == 4
    assert m["n_rows"] == 12  # 3 candidates x 4 holdout shows
    assert np.isfinite(m["brier"])
    assert np.isfinite(m["log_loss"])
    assert result["calibration"]  # non-empty list of bucket dicts

    # One LLM call per holdout show, not per row.
    assert len(fake.calls) == 4
    assert set(fake.calls) == set(holdout_dates)
