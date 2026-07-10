"""Unit tests for phishpred.mcp.tools -- the pure functions backing the
phishpred-mcp read/write tools. See deploy plan §5a and DEPLOY-CONTRACTS.md
§5. No network, no live MCP session; small in-memory DB built the same way
tests/test_modes.py does.

"Today" is fixed at 2026-07-09 in this environment (see tests/test_modes.py
for the same assumption), so the future/past split below is deliberate: show
10 (2026-07-08) is already played, shows 1001/1002 (2026-07-09/10) are the
still-future run nights.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from phishpred import db, features, probs
from phishpred.config import era_for_year
from phishpred.mcp import tools

VENUES = [(1, "Run Venue", "RunCity", 0), (2, "Home Venue", "HomeCity", 0)]

SONGS = [
    (101, "tweezer", "Tweezer", 1),
    (102, "yem", "YEM", 1),
    (103, "wilson", "Wilson", 1),
]

# showid, show_index, showdate, venueid -- all indexed/played.
HIST_SHOWS = [
    (1, 0, "2022-06-01", 2),
    (2, 1, "2022-06-02", 2),
    (3, 2, "2022-06-03", 2),
    (4, 3, "2022-06-10", 2),
    (5, 4, "2022-06-11", 2),
    (10, 5, "2026-07-08", 1),  # night 1 of the run -- already played
]

HIST_SETLISTS = {
    1: [101, 102, 103],
    2: [101, 102],
    3: [101, 103],
    4: [101, 102, 103],
    5: [101, 102],
    10: [101, 103],
}

# showid, showdate, venueid, tour_name -- not yet indexed (future).
FUTURE_SHOWS = [
    (1001, "2026-07-09", 1, "2026 Summer Tour"),  # night 2 of the run
    (1002, "2026-07-10", 1, "2026 Summer Tour"),  # night 3 of the run
]


def _populate(conn):
    for vid, name, city, alias in VENUES:
        conn.execute(
            "INSERT INTO venues (venueid, name, city, alias) VALUES (?,?,?,?)",
            (vid, name, city, alias),
        )
    for sid, slug, name, iso in SONGS:
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,?)",
            (sid, slug, name, iso),
        )
    for showid, idx, showdate, vid in HIST_SHOWS:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, show_index, exclude) "
            "VALUES (?,?,?,?,0)",
            (showid, showdate, vid, idx),
        )
    for showid, showdate, vid, tour_name in FUTURE_SHOWS:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tour_name, show_index, exclude) "
            "VALUES (?,?,?,?,NULL,0)",
            (showid, showdate, vid, tour_name),
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


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def test_upcoming_shows_returns_future_shows_and_epoch_key(conn):
    from unittest.mock import patch
    import datetime
    with patch("phishpred.predict.date") as mock_date:
        mock_date.today.return_value = datetime.date(2026, 7, 9)
        result = tools.upcoming_shows(conn)
    dates = [s["showdate"] for s in result["shows"]]
    assert "2026-07-09" in dates
    assert "2026-07-10" in dates
    assert "2026-07-08" not in dates  # already played -- not "upcoming"
    # phishpred.epoch now exists: the key is always present and stamps the
    # current 12-hex epoch (deploy plan §6 / DEPLOY-CONTRACTS §1).
    assert "epoch" in result
    assert isinstance(result["epoch"], str) and len(result["epoch"]) == 12


def test_candidate_features_returns_compact_frame_for_future_show(conn):
    result = tools.candidate_features(conn, "2026-07-09")
    assert result["showdate"] == "2026-07-09"
    slugs = {row["slug"] for row in result["rows"]}
    assert {"tweezer", "yem", "wilson"} <= slugs
    for row in result["rows"]:
        assert "decayed_rate" in row
        assert "gap" in row
        assert "showid" not in row  # bulky/id plumbing dropped
        assert "y" not in row


def test_song_history_reports_plays_gap_and_venue_history(conn):
    result = tools.song_history(conn, "wilson")
    assert result["slug"] == "wilson"
    assert result["never_played"] is False
    # wilson (103) played in shows 1, 3, 4, 10
    assert result["historical_play_count"] == 4
    # last played show10 (index 5); "now" is index 6 -> gap 1
    assert result["current_gap"] == 1
    venues_by_name = {v["venue_name"]: v["plays"] for v in result["venue_history"]}
    assert venues_by_name["Home Venue"] == 3
    assert venues_by_name["Run Venue"] == 1


def test_song_history_never_played(conn):
    conn.execute(
        "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,?)",
        (999, "bustout-only", "Bustout Only", 1),
    )
    conn.commit()
    result = tools.song_history(conn, "bustout-only")
    assert result["never_played"] is True
    assert result["historical_play_count"] == 0
    assert result["current_gap"] is None


def test_venue_history_songs_and_play_rate(conn):
    result = tools.venue_history(conn, "Home")
    assert result["venue_name"] == "Home Venue"
    assert result["total_shows"] == 5
    songs_by_slug = {s["slug"]: s for s in result["songs"]}
    assert songs_by_slug["tweezer"]["n_shows_played"] == 5
    assert songs_by_slug["tweezer"]["play_rate"] == pytest.approx(1.0)
    assert songs_by_slug["wilson"]["n_shows_played"] == 3


def test_venue_history_unknown_venue_raises(conn):
    with pytest.raises(ValueError):
        tools.venue_history(conn, "Nonexistent Arena")


def test_recent_setlists_chronological_order(conn):
    result = tools.recent_setlists(conn, n=3)
    dates = [s["showdate"] for s in result["shows"]]
    assert dates == ["2022-06-10", "2022-06-11", "2026-07-08"]
    last = result["shows"][-1]
    assert {row["slug"] for row in last["setlist"]} == {"tweezer", "wilson"}


def test_run_context_includes_played_and_future_nights(conn):
    result = tools.run_context(conn, "2026-07-09")
    assert result["venue_name"] == "Run Venue"
    dates = [n["showdate"] for n in result["nights"]]
    assert dates == ["2026-07-08", "2026-07-09", "2026-07-10"]

    night1, night2, night3 = result["nights"]
    assert night1["played"] is True
    assert {s["slug"] for s in night1["setlist"]} == {"tweezer", "wilson"}
    assert night2["is_target"] is True
    assert night2["played"] is False
    assert "setlist" not in night2
    assert night3["played"] is False


def test_heuristic_prediction_returns_ranked_rows(conn):
    result = tools.heuristic_prediction(conn, "2026-07-09")
    assert result["showdate"] == "2026-07-09"
    assert result["model"] == "heuristic"
    slugs = [row["slug"] for row in result["rows"]]
    assert "tweezer" in slugs
    probs_desc = [row["prob"] for row in result["rows"]]
    assert probs_desc == sorted(probs_desc, reverse=True)


# ---------------------------------------------------------------------------
# submit_prediction (write tool)
# ---------------------------------------------------------------------------

def test_submit_prediction_writes_expected_schema(conn, tmp_path):
    result = tools.submit_prediction(
        "2026-07-09",
        "claude-desktop",
        [{"slug": "tweezer", "prob": 0.6}, {"slug": "wilson", "prob": 0.3}],
        rationale="due for a wilson",
        conn=conn,
        out_dir=tmp_path,
    )

    path = Path(result["path"])
    assert path == tmp_path / "claude-desktop" / "2026-07-09.json"
    assert path.exists()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload.keys()) == {
        "model_label", "showdate", "epoch", "submitted_at", "rationale", "predictions",
    }
    assert payload["model_label"] == "claude-desktop"
    assert payload["showdate"] == "2026-07-09"
    assert isinstance(payload["epoch"], str) and len(payload["epoch"]) == 12  # stamped from phishpred.epoch
    assert payload["rationale"] == "due for a wilson"
    assert payload["submitted_at"].endswith("Z")

    for row in payload["predictions"]:
        assert set(row.keys()) == {"slug", "prob"}

    # Probs are stored AS SUBMITTED (clamped), NOT renormalized — publish does
    # the single authoritative renormalize-to-K at fold time.
    by_slug = {row["slug"]: row["prob"] for row in payload["predictions"]}
    assert by_slug == {"tweezer": 0.6, "wilson": 0.3}

    # rows sorted by prob desc
    probs_list = [row["prob"] for row in payload["predictions"]]
    assert probs_list == sorted(probs_list, reverse=True)


def test_submit_prediction_rejects_unknown_slug(conn, tmp_path):
    with pytest.raises(ValueError):
        tools.submit_prediction(
            "2026-07-09",
            "agent-x",
            [{"slug": "not-a-real-song", "prob": 0.5}],
            conn=conn,
            out_dir=tmp_path,
        )
    assert not any((tmp_path / "agent-x").glob("*.json"))


def test_submit_prediction_rejects_empty_predictions(conn, tmp_path):
    with pytest.raises(ValueError):
        tools.submit_prediction("2026-07-09", "agent-x", [], conn=conn, out_dir=tmp_path)


def test_submit_prediction_rejects_out_of_range_prob(conn, tmp_path):
    with pytest.raises(ValueError):
        tools.submit_prediction(
            "2026-07-09", "agent-x", [{"slug": "tweezer", "prob": 1.5}],
            conn=conn, out_dir=tmp_path,
        )
    with pytest.raises(ValueError):
        tools.submit_prediction(
            "2026-07-09", "agent-x", [{"slug": "tweezer", "prob": 0.0}],
            conn=conn, out_dir=tmp_path,
        )


def test_submit_prediction_rejects_duplicate_slug(conn, tmp_path):
    with pytest.raises(ValueError):
        tools.submit_prediction(
            "2026-07-09",
            "agent-x",
            [{"slug": "tweezer", "prob": 0.3}, {"slug": "tweezer", "prob": 0.4}],
            conn=conn,
            out_dir=tmp_path,
        )


def test_submit_prediction_sanitizes_model_label_for_path(conn, tmp_path):
    result = tools.submit_prediction(
        "2026-07-09",
        "Claude Desktop / v2!",
        [{"slug": "tweezer", "prob": 0.5}],
        conn=conn,
        out_dir=tmp_path,
    )
    assert Path(result["path"]).parent.name == "Claude-Desktop-v2"


def test_tool_docstrings_state_run_repeat_ground_rules():
    """The rotation ground rules must reach an agent that only reads tool
    descriptions (docs/MCP.md "Ground rules")."""
    assert "played_in_run" in tools.candidate_features.__doc__
    assert "played_prev_show" in tools.candidate_features.__doc__
    assert "Ground rules" in tools.submit_prediction.__doc__
    assert "played_in_run" in tools.submit_prediction.__doc__
    assert "run" in tools.run_context.__doc__


def test_submit_prediction_honors_explicit_epoch_and_timestamp(conn, tmp_path):
    result = tools.submit_prediction(
        "2026-07-09",
        "agent-x",
        [{"slug": "tweezer", "prob": 0.5}],
        conn=conn,
        out_dir=tmp_path,
        epoch="deadbeef1234",
        submitted_at="2026-07-09T12:00:00Z",
    )
    assert result["payload"]["epoch"] == "deadbeef1234"
    assert result["payload"]["submitted_at"] == "2026-07-09T12:00:00Z"
