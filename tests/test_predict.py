"""Unit tests for phishpred.predict. See CONTRACTS.md `predict.py` section.

features.py / models/heuristic.py / models/ml.py are owned by other agents and
may still be stubs, so the ML paths under test are exercised entirely via
monkeypatching -- no real feature/model code is required for these tests to
pass.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest

import phishpred.features as features
import phishpred.models.heuristic as heuristic_mod
from phishpred.db import get_connection, init_db
from phishpred.predict import predict_show, render_prediction, upcoming_shows

TODAY = date.today()


@pytest.fixture()
def conn():
    conn = get_connection(":memory:")
    init_db(conn)
    yield conn
    conn.close()


def _insert_venue(conn, venueid, name, city, state="IN", country="USA"):
    conn.execute(
        "INSERT INTO venues (venueid, name, city, state, country) VALUES (?, ?, ?, ?, ?)",
        (venueid, name, city, state, country),
    )


def _insert_show(
    conn,
    showid,
    showdate,
    venueid,
    artistid=1,
    exclude=0,
    tourid=None,
    tour_name=None,
    show_index=None,
):
    conn.execute(
        """INSERT INTO shows
           (showid, showdate, venueid, tourid, tour_name, artistid, exclude, show_index)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (showid, showdate, venueid, tourid, tour_name, artistid, exclude, show_index),
    )


# ---------------------------------------------------------------------------
# upcoming_shows
# ---------------------------------------------------------------------------

def test_upcoming_shows_filters_past_shows(conn):
    _insert_venue(conn, 1, "Ruoff Music Center", "Noblesville")
    _insert_show(conn, 1, (TODAY - timedelta(days=30)).isoformat(), 1)
    _insert_show(conn, 2, (TODAY + timedelta(days=10)).isoformat(), 1)
    conn.commit()

    rows = upcoming_shows(conn)
    dates = [r["showdate"] for r in rows]
    assert (TODAY + timedelta(days=10)).isoformat() in dates
    assert (TODAY - timedelta(days=30)).isoformat() not in dates


def test_upcoming_shows_excludes_flagged_rows(conn):
    _insert_venue(conn, 1, "Ruoff Music Center", "Noblesville")
    _insert_show(conn, 1, (TODAY + timedelta(days=5)).isoformat(), 1, exclude=1)
    conn.commit()

    rows = upcoming_shows(conn)
    assert rows == []


def test_upcoming_shows_venue_substring_case_insensitive(conn):
    _insert_venue(conn, 1, "Ruoff Music Center", "Noblesville")
    _insert_venue(conn, 2, "Madison Square Garden", "New York")
    _insert_show(conn, 1, (TODAY + timedelta(days=5)).isoformat(), 1)
    _insert_show(conn, 2, (TODAY + timedelta(days=6)).isoformat(), 2)
    conn.commit()

    rows = upcoming_shows(conn, venue_query="RUOFF")
    assert len(rows) == 1
    assert rows[0]["venue_name"] == "Ruoff Music Center"


def test_upcoming_shows_venue_substring_matches_city(conn):
    _insert_venue(conn, 1, "Ruoff Music Center", "Noblesville")
    _insert_venue(conn, 2, "Madison Square Garden", "New York")
    _insert_show(conn, 1, (TODAY + timedelta(days=5)).isoformat(), 1)
    _insert_show(conn, 2, (TODAY + timedelta(days=6)).isoformat(), 2)
    conn.commit()

    rows = upcoming_shows(conn, venue_query="noblesville")
    assert len(rows) == 1
    assert rows[0]["city"] == "Noblesville"


def test_upcoming_shows_respects_limit_and_order(conn):
    _insert_venue(conn, 1, "Ruoff Music Center", "Noblesville")
    for i in range(5):
        _insert_show(conn, i + 1, (TODAY + timedelta(days=i + 1)).isoformat(), 1)
    conn.commit()

    rows = upcoming_shows(conn, limit=2)
    assert len(rows) == 2
    assert rows[0]["showdate"] < rows[1]["showdate"]


# ---------------------------------------------------------------------------
# predict_show / render_prediction (heuristic path, fully monkeypatched)
# ---------------------------------------------------------------------------

SONGS = [
    # song_name, slug, decayed_rate, gap
    ("Tweezer", "tweezer", 0.123, 1),
    ("Ghost", "ghost", 0.222, 10),
    ("Wilson", "wilson", 0.05, 5),
    ("Bathtub Gin", "bathtub-gin", 0.3, 50),
    ("Harry Hood", "harry-hood", 0.01, 3),
]

# multipliers keyed by position matching SONGS, chosen so each driver type
# fires exactly once across the top few rows.
M_PREV_SHOW = [0.02, 1.0, 1.0, 1.0, 1.0]
M_IN_RUN = [1.0, 1.0, 1.0, 1.0, 1.0]
M_VENUE = [1.0, 0.3, 1.0, 1.0, 1.0]
M_DUE = [1.0, 1.0, 1.45, 1.0, 1.0]
PROBS = [0.5, 0.3, 0.15, 0.04, 0.01]


def _make_future_show(conn, showid=100, showdate=None, venueid=1):
    showdate = showdate or (TODAY + timedelta(days=14)).isoformat()
    _insert_venue(conn, venueid, "Ruoff Music Center", "Noblesville")
    _insert_show(conn, showid, showdate, venueid)
    conn.commit()
    return showdate


def _fake_features_for_future_show(conn, showid, half_life=50):
    n = len(SONGS)
    return pd.DataFrame(
        {
            "showid": [showid] * n,
            "showdate": ["2026-07-23"] * n,
            "show_index": [None] * n,
            "venueid": [1] * n,
            "songid": list(range(1, n + 1)),
            "slug": [s[1] for s in SONGS],
            "song_name": [s[0] for s in SONGS],
            "y": [float("nan")] * n,
            "decayed_rate": [s[2] for s in SONGS],
            "gap": [s[3] for s in SONGS],
            "gap_ratio": [1.0] * n,
            "played_prev_show": [0] * n,
            "played_in_run": [0] * n,
            "venue_gap": [999] * n,
            "plays_this_tour": [0] * n,
            "plays_last_10": [0] * n,
            "plays_last_50": [0] * n,
            "song_age_shows": [100] * n,
            "era_rate": [0.1] * n,
            "is_original": [1] * n,
        }
    )


def _fake_heuristic_predict(df, k):
    out = df.copy()
    out["m_prev_show"] = M_PREV_SHOW
    out["m_in_run"] = M_IN_RUN
    out["m_venue"] = M_VENUE
    out["m_due"] = M_DUE
    out["score"] = out["decayed_rate"]
    out["prob"] = PROBS
    return out


@pytest.fixture()
def patched_heuristic(monkeypatch):
    monkeypatch.setattr(features, "features_for_future_show", _fake_features_for_future_show)
    monkeypatch.setattr(features, "mean_setlist_size", lambda conn, era=None: 20.0)
    monkeypatch.setattr(heuristic_mod, "heuristic_predict", _fake_heuristic_predict)


def test_predict_show_raises_for_unknown_date(conn):
    with pytest.raises(ValueError):
        predict_show(conn, "1900-01-01")


def test_predict_show_resolves_and_sorts_and_truncates(conn, patched_heuristic):
    showdate = _make_future_show(conn)

    pred = predict_show(conn, showdate, model="heuristic", top=3)

    assert pred.showdate == showdate
    assert pred.venue_name == "Ruoff Music Center"
    assert pred.city == "Noblesville"
    assert pred.model == "heuristic"
    assert pred.k == pytest.approx(20.0)

    # top=3 truncation and prob-descending sort
    assert len(pred.rows) == 3
    assert [r.song for r in pred.rows] == ["Tweezer", "Ghost", "Wilson"]
    assert [r.prob for r in pred.rows] == pytest.approx([0.5, 0.3, 0.15])


def test_predict_show_driver_formatting(conn, patched_heuristic):
    showdate = _make_future_show(conn)
    pred = predict_show(conn, showdate, model="heuristic", top=5)

    tweezer, ghost, wilson, gin, hood = pred.rows

    # rate= always present, 3 decimals, prepended
    assert tweezer.drivers[0] == "rate=0.123"
    # prev-show fires for Tweezer only
    assert "prev-show x0.02" in tweezer.drivers
    assert not any(d.startswith("prev-show") for d in ghost.drivers)

    # venue fires for Ghost only
    assert "venue x0.3" in ghost.drivers
    assert not any(d.startswith("venue") for d in tweezer.drivers)

    # due fires for Wilson only
    assert "due x1.45" in wilson.drivers
    assert not any(d.startswith("due") for d in ghost.drivers)

    # no drivers beyond rate= for rows where nothing fired
    assert gin.drivers == ["rate=0.300"]
    assert hood.drivers == ["rate=0.010"]


def test_predict_show_resolves_phish_among_multiple_same_date_shows(conn, patched_heuristic):
    showdate = (TODAY + timedelta(days=20)).isoformat()
    _insert_venue(conn, 1, "Ruoff Music Center", "Noblesville")
    _insert_venue(conn, 2, "Some Other Venue", "Elsewhere")
    _insert_show(conn, 200, showdate, 2, artistid=2)  # non-Phish artist, inserted first
    _insert_show(conn, 201, showdate, 1, artistid=1)  # Phish
    conn.execute("INSERT INTO meta (key, value) VALUES ('phish_artistid', '1')")
    conn.commit()

    pred = predict_show(conn, showdate, model="heuristic", top=5)
    assert pred.venue_name == "Ruoff Music Center"


def test_predict_show_gap_passthrough(conn, patched_heuristic):
    showdate = _make_future_show(conn)
    pred = predict_show(conn, showdate, model="heuristic", top=5)
    gaps = {r.song: r.gap for r in pred.rows}
    assert gaps["Tweezer"] == 1
    assert gaps["Ghost"] == 10


def test_render_prediction_json_round_trip(conn, patched_heuristic):
    showdate = _make_future_show(conn)
    pred = predict_show(conn, showdate, model="heuristic", top=3)

    text = render_prediction(pred, json_out=True)
    payload = json.loads(text)

    assert payload["showdate"] == showdate
    assert payload["venue_name"] == "Ruoff Music Center"
    assert payload["city"] == "Noblesville"
    assert payload["model"] == "heuristic"
    assert payload["half_life"] == 50
    assert len(payload["rows"]) == 3
    for row in payload["rows"]:
        assert set(("song", "slug", "prob", "gap", "drivers")) <= set(row.keys())
    assert payload["rows"][0]["song"] == "Tweezer"
    assert payload["rows"][0]["prob"] == pytest.approx(0.5)


def test_render_prediction_table_contains_venue_and_songs(conn, patched_heuristic):
    showdate = _make_future_show(conn)
    pred = predict_show(conn, showdate, model="heuristic", top=5)

    text = render_prediction(pred, json_out=False)

    assert "Ruoff Music Center" in text
    assert "Tweezer" in text
    assert "Ghost" in text
    assert "Wilson" in text
