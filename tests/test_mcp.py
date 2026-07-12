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


def _n_valid_predictions(conn, n=20):
    """``n`` valid ``{slug, prob}`` rows, registering filler songs in ``conn`` as
    needed, so a submission clears the 20-40 shortlist-length bound (§5)."""
    rows = []
    for i in range(n):
        slug = f"pred-song-{i}"
        conn.execute(
            "INSERT OR IGNORE INTO songs (songid, slug, name, is_original) VALUES (?,?,?,1)",
            (5000 + i, slug, f"Pred Song {i}"),
        )
        rows.append({"slug": slug, "prob": round(0.9 - i * 0.01, 4)})
    conn.commit()
    return rows


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
    predictions = _n_valid_predictions(conn, 20)
    predictions[0] = {"slug": "tweezer", "prob": 0.6}
    predictions[1] = {"slug": "wilson", "prob": 0.3}
    result = tools.submit_prediction(
        "2026-07-09",
        "claude-desktop",
        predictions,
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
    assert len(payload["predictions"]) == 20  # within the 20-40 shortlist bound

    for row in payload["predictions"]:
        assert set(row.keys()) == {"slug", "prob"}

    # Probs are stored AS SUBMITTED (clamped), NOT renormalized — publish does
    # the single authoritative renormalize-to-K at fold time.
    by_slug = {row["slug"]: row["prob"] for row in payload["predictions"]}
    assert by_slug["tweezer"] == 0.6 and by_slug["wilson"] == 0.3

    # rows sorted by prob desc
    probs_list = [row["prob"] for row in payload["predictions"]]
    assert probs_list == sorted(probs_list, reverse=True)


def test_submit_prediction_rejects_too_few_predictions(conn, tmp_path):
    with pytest.raises(ValueError, match="between 20 and 40"):
        tools.submit_prediction(
            "2026-07-09", "agent-x", _n_valid_predictions(conn, 19),
            conn=conn, out_dir=tmp_path,
        )
    assert not any((tmp_path / "agent-x").glob("*.json"))


def test_submit_prediction_rejects_too_many_predictions(conn, tmp_path):
    with pytest.raises(ValueError, match="between 20 and 40"):
        tools.submit_prediction(
            "2026-07-09", "agent-x", _n_valid_predictions(conn, 41),
            conn=conn, out_dir=tmp_path,
        )


def test_submit_prediction_accepts_max_shortlist(conn, tmp_path):
    result = tools.submit_prediction(
        "2026-07-09", "agent-x", _n_valid_predictions(conn, 40),
        conn=conn, out_dir=tmp_path,
    )
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert len(payload["predictions"]) == 40


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
        _n_valid_predictions(conn, 20),
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


# ---------------------------------------------------------------------------
# submit_prediction — structured setlist (§5)
# ---------------------------------------------------------------------------

def test_submit_prediction_writes_setlist(conn, tmp_path):
    result = tools.submit_prediction(
        "2026-07-09",
        "agent-x",
        _n_valid_predictions(conn, 20),
        setlist={"sets": {"1": ["tweezer", "yem"], "e": ["wilson"]}},
        conn=conn,
        out_dir=tmp_path,
    )
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["setlist"] == {"sets": {"1": ["tweezer", "yem"], "e": ["wilson"]}}
    # setlist is independent of predictions; both are present here
    assert len(payload["predictions"]) == 20


def test_submit_prediction_setlist_omitted_when_absent(conn, tmp_path):
    result = tools.submit_prediction(
        "2026-07-09", "agent-x", _n_valid_predictions(conn, 20),
        conn=conn, out_dir=tmp_path,
    )
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert "setlist" not in payload


def test_submit_prediction_setlist_rejects_unknown_slug(conn, tmp_path):
    with pytest.raises(ValueError, match="unknown slug"):
        tools.submit_prediction(
            "2026-07-09", "agent-x", [{"slug": "tweezer", "prob": 0.6}],
            setlist={"sets": {"1": ["tweezer", "not-a-song"]}},
            conn=conn, out_dir=tmp_path,
        )


def test_submit_prediction_setlist_rejects_duplicate_slug(conn, tmp_path):
    # tweezer appears in two different sets -> duplicate anywhere is rejected.
    with pytest.raises(ValueError, match="duplicate slug"):
        tools.submit_prediction(
            "2026-07-09", "agent-x", [{"slug": "tweezer", "prob": 0.6}],
            setlist={"sets": {"1": ["tweezer"], "2": ["tweezer"]}},
            conn=conn, out_dir=tmp_path,
        )


def test_submit_prediction_setlist_rejects_bad_set_key(conn, tmp_path):
    with pytest.raises(ValueError, match="invalid set label"):
        tools.submit_prediction(
            "2026-07-09", "agent-x", [{"slug": "tweezer", "prob": 0.6}],
            setlist={"sets": {"encore": ["wilson"]}},  # must be \d+ or e\d*
            conn=conn, out_dir=tmp_path,
        )


def test_submit_prediction_setlist_rejects_over_40_songs(conn, tmp_path):
    # 41 references to the 3 known slugs would first trip the duplicate rule, so
    # register enough unique slugs to isolate the >40 cap.
    for i in range(41):
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,1)",
            (2000 + i, f"song-{i}", f"Song {i}", ),
        )
    conn.commit()
    with pytest.raises(ValueError, match="max 40"):
        tools.submit_prediction(
            "2026-07-09", "agent-x", [{"slug": "tweezer", "prob": 0.6}],
            setlist={"sets": {"1": [f"song-{i}" for i in range(41)]}},
            conn=conn, out_dir=tmp_path,
        )


def test_submit_prediction_setlist_rejects_empty_set(conn, tmp_path):
    with pytest.raises(ValueError, match="non-empty"):
        tools.submit_prediction(
            "2026-07-09", "agent-x", [{"slug": "tweezer", "prob": 0.6}],
            setlist={"sets": {"1": []}},
            conn=conn, out_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# submit_prediction — versioning (§5)
# ---------------------------------------------------------------------------

def test_first_submission_omits_versions_key(conn, tmp_path):
    result = tools.submit_prediction(
        "2026-07-09", "agent-x", _n_valid_predictions(conn, 20),
        conn=conn, out_dir=tmp_path,
    )
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert "versions" not in payload  # legacy-shaped output for first submissions


def test_resubmission_appends_prior_to_versions(conn, tmp_path):
    tools.submit_prediction(
        "2026-07-09", "agent-x", _n_valid_predictions(conn, 20),
        rationale="take 1", conn=conn, out_dir=tmp_path,
        submitted_at="2026-07-08T10:00:00Z",
    )
    result = tools.submit_prediction(
        "2026-07-09", "agent-x", _n_valid_predictions(conn, 22),
        rationale="take 2", conn=conn, out_dir=tmp_path,
        submitted_at="2026-07-09T10:00:00Z",
    )
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    # latest take is the top level
    assert len(payload["predictions"]) == 22
    assert payload["rationale"] == "take 2"
    # one prior take carried into versions (oldest first), stripped of its own key
    assert len(payload["versions"]) == 1
    prior = payload["versions"][0]
    assert prior["rationale"] == "take 1"
    assert len(prior["predictions"]) == 20
    assert "versions" not in prior


def test_resubmission_keeps_only_10_most_recent_priors(conn, tmp_path):
    for i in range(12):  # 12 submissions -> the 12th keeps 10 priors (drops 2 oldest)
        tools.submit_prediction(
            "2026-07-09", "agent-x", _n_valid_predictions(conn, 20),
            rationale=f"take {i}", conn=conn, out_dir=tmp_path,
            submitted_at=f"2026-07-09T{i:02d}:00:00Z",
        )
    payload = json.loads((tmp_path / "agent-x" / "2026-07-09.json").read_text(encoding="utf-8"))
    assert len(payload["versions"]) == 10  # capped
    # oldest-first ordering; the two oldest (take 0, take 1) were dropped
    rationales = [v["rationale"] for v in payload["versions"]]
    assert rationales == [f"take {i}" for i in range(1, 11)]
    assert payload["rationale"] == "take 11"  # latest


def test_resubmission_unreadable_prior_treated_as_no_history(conn, tmp_path, capsys):
    dest = tmp_path / "agent-x"
    dest.mkdir(parents=True)
    (dest / "2026-07-09.json").write_text("{not valid json", encoding="utf-8")
    result = tools.submit_prediction(
        "2026-07-09", "agent-x", _n_valid_predictions(conn, 20),
        conn=conn, out_dir=tmp_path,
    )
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert "versions" not in payload  # unparseable prior -> no history
    assert "treating as no history" in capsys.readouterr().err


def test_submit_prediction_honors_explicit_epoch_and_timestamp(conn, tmp_path):
    result = tools.submit_prediction(
        "2026-07-09",
        "agent-x",
        _n_valid_predictions(conn, 20),
        conn=conn,
        out_dir=tmp_path,
        epoch="deadbeef1234",
        submitted_at="2026-07-09T12:00:00Z",
    )
    assert result["payload"]["epoch"] == "deadbeef1234"
    assert result["payload"]["submitted_at"] == "2026-07-09T12:00:00Z"


# ---------------------------------------------------------------------------
# slot_propensities (read tool) — set-position tendencies + era structure
# ---------------------------------------------------------------------------

def test_slot_propensities_per_song_and_structure(conn):
    # tweezer opens every fixture set (position 0); wilson always closes.
    result = tools.slot_propensities(conn, ["tweezer", "wilson"])
    assert result["unknown_slugs"] == []
    tw = result["songs"]["tweezer"]
    assert tw["n_plays"] == 6
    assert tw["slots"] == {"set1-open": 1.0}
    wi = result["songs"]["wilson"]
    assert wi["n_plays"] == 4
    assert wi["slots"] == {"set1-close": 1.0}
    # Era of the latest played show (2026) is 4.0; every fixture show qualifies.
    st = result["set_structure"]
    assert st["era"] == "4.0"
    assert st["n_shows"] == 6
    assert st["set_lengths"]["1"]["mean"] == pytest.approx(14 / 6, abs=0.01)
    assert st["num_sets_dist"] == {"1": 6}


def test_slot_propensities_collects_unknown_slugs(conn):
    result = tools.slot_propensities(conn, ["tweezer", "not-a-song"])
    assert result["unknown_slugs"] == ["not-a-song"]
    assert list(result["songs"]) == ["tweezer"]


# ---------------------------------------------------------------------------
# backtest_shortlist (read tool) — hypothesis check over played shows
# ---------------------------------------------------------------------------

def test_backtest_shortlist_scores_recent_shows(conn):
    # Last 3 played by show_index desc: show 10 (101,103), 5 (101,102), 4 (all).
    result = tools.backtest_shortlist(conn, ["tweezer", "yem"], n_shows=3)
    assert result["n_shows"] == 3
    assert [r["showdate"] for r in result["shows"]] == [
        "2026-07-08", "2022-06-11", "2022-06-10",
    ]
    assert [r["hits"] for r in result["shows"]] == [1, 2, 2]
    assert result["shows"][0]["hit_rate"] == pytest.approx(0.5)
    assert result["shows"][0]["recall"] == pytest.approx(0.5)
    assert result["shows"][2]["recall"] == pytest.approx(2 / 3, abs=0.001)
    assert result["mean_hit_rate"] == pytest.approx((0.5 + 1.0 + 1.0) / 3, abs=0.001)
    assert result["per_slug"] == {"tweezer": 3, "yem": 2}


def test_backtest_shortlist_rejects_bad_input(conn):
    with pytest.raises(ValueError, match="empty"):
        tools.backtest_shortlist(conn, [])
    with pytest.raises(ValueError, match="unknown slug"):
        tools.backtest_shortlist(conn, ["tweezer", "not-a-song"])
    with pytest.raises(ValueError, match="duplicate"):
        tools.backtest_shortlist(conn, ["tweezer", "tweezer"])
    with pytest.raises(ValueError, match="at most"):
        tools.backtest_shortlist(conn, [f"s{i}" for i in range(41)])


# ---------------------------------------------------------------------------
# show_length_stats (read tool) — songs-per-show calibration context
# ---------------------------------------------------------------------------

def test_show_length_stats_by_year_and_overall(conn):
    stats = tools.show_length_stats(conn)  # anchored on 2026 -> since 2017-01-01
    assert stats["since"] == "2017-01-01"
    assert stats["overall"]["shows"] == 6
    assert stats["overall"]["avg_songs"] == pytest.approx(14 / 6, abs=0.01)
    years = {y["year"]: y for y in stats["by_year"]}
    assert list(years) == ["2022", "2026"]  # ascending
    assert years["2022"]["shows"] == 5
    assert years["2022"]["avg_songs"] == pytest.approx(2.4)
    assert years["2022"]["min_songs"] == 2
    assert years["2022"]["max_songs"] == 3
    assert years["2026"] == {
        "year": "2026", "shows": 1, "avg_songs": 2.0,
        "avg_distinct_songs": 2.0, "min_songs": 2, "max_songs": 2,
    }


def test_show_length_stats_window_narrows(conn):
    stats = tools.show_length_stats(conn, years=1)  # 2026 only
    assert stats["since"] == "2026-01-01"
    assert stats["overall"]["shows"] == 1
    assert [y["year"] for y in stats["by_year"]] == ["2026"]


def test_show_length_stats_counts_repeats_vs_distinct(conn):
    # A reprise: tweezer (101) played twice in show 10 -> avg_songs counts both,
    # avg_distinct_songs counts it once.
    conn.execute(
        "INSERT INTO performances (showid, songid, set_label, position) VALUES (10, 101, 'e', 2)"
    )
    conn.commit()
    stats = tools.show_length_stats(conn, years=1)
    y = stats["by_year"][0]
    assert y["avg_songs"] == 3.0
    assert y["avg_distinct_songs"] == 2.0


def test_show_length_stats_empty_db():
    c = db.get_connection(":memory:")
    db.init_db(c)
    stats = tools.show_length_stats(c)
    assert stats == {"since": None, "overall": {"shows": 0}, "by_year": []}
    c.close()


# ---------------------------------------------------------------------------
# scoreboard (read tool) — over a tmp scorecards dir with small fixtures
# ---------------------------------------------------------------------------

def _write_scorecards(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    board = {
        "updated_at": "2026-07-11T06:00:00Z",
        "shows": [
            {"showdate": "2026-07-08", "venue_name": "Ruoff", "city": "N", "state": "IN",
             "n_played": 21, "source_keys": ["heuristic", "mcp:claude-opus"]},
            {"showdate": "2026-07-05", "venue_name": "Ruoff", "city": "N", "state": "IN",
             "n_played": 20, "source_keys": ["heuristic", "mcp:claude-opus"]},
        ],
        "models": {
            "heuristic": {"kind": "statistical", "n_shows": 2, "hit_rate_top20": 0.5,
                          "recall": 0.4, "brier": 0.1, "log_loss": 0.3, "avg_n_rows": 40.0},
            "mcp:claude-opus": {"kind": "mcp", "n_shows": 2, "hit_rate_top20": 0.6,
                                "recall": 0.5, "brier": 0.08, "log_loss": 0.25, "avg_n_rows": 25.0,
                                "vs_heuristic": {"n_shows": 2, "hit_rate_top20_delta": 0.1,
                                                 "recall_delta": 0.1}},
        },
    }
    (dir_path / "scoreboard.json").write_text(json.dumps(board), encoding="utf-8")
    for showdate, nplayed in (("2026-07-05", 20), ("2026-07-08", 21)):
        card = {
            "showdate": showdate, "venue_name": "Ruoff", "city": "N", "state": "IN",
            "n_played": nplayed,
            "sources": {
                "heuristic": {
                    "model": "heuristic", "kind": "statistical", "n_rows": 40,
                    "metrics": {"top_n": 20, "hits_top20": 6, "hit_rate_top20": 0.5,
                                "recall": 0.4, "brier": 0.1, "log_loss": 0.3},
                    "best_call": {"song": "Harry Hood", "slug": "hood", "prob": 0.12},
                    "biggest_whiff": None,
                    "rows": [{"song": "Harry Hood", "slug": "hood", "prob": 0.6, "hit": True}],
                },
                "mcp:claude-opus": {
                    "model": "mcp:claude-opus", "kind": "mcp", "n_rows": 25,
                    "metrics": {"top_n": 20, "hits_top20": 7, "hit_rate_top20": 0.6,
                                "recall": 0.5, "brier": 0.08, "log_loss": 0.25},
                    "best_call": None,
                    "biggest_whiff": {"song": "Ghost", "slug": "ghost", "prob": 0.5},
                    "rows": [{"song": "Ghost", "slug": "ghost", "prob": 0.5, "hit": False}],
                },
            },
            "missed_by_all": [{"slug": "yem", "song": "YEM"}],
        }
        (dir_path / f"{showdate}.json").write_text(json.dumps(card), encoding="utf-8")


def test_scoreboard_returns_models_and_compact_recent_shows(tmp_path):
    _write_scorecards(tmp_path)
    result = tools.scoreboard(tmp_path, model_label="claude-opus", recent=5)

    # models mapping passes through verbatim (incl. avg_n_rows / vs_heuristic)
    assert "heuristic" in result["models"]
    assert result["models"]["mcp:claude-opus"]["vs_heuristic"]["hit_rate_top20_delta"] == 0.1
    assert result["models"]["heuristic"]["avg_n_rows"] == 40.0

    # recent shows showdate DESC
    dates = [s["showdate"] for s in result["recent_shows"]]
    assert dates == ["2026-07-08", "2026-07-05"]

    show = result["recent_shows"][0]
    assert show["n_played"] == 21
    assert set(show["sources"]) == {"heuristic", "mcp:claude-opus"}
    assert show["sources"]["mcp:claude-opus"]["metrics"]["hit_rate_top20"] == 0.6
    assert show["sources"]["mcp:claude-opus"]["biggest_whiff"]["slug"] == "ghost"
    assert show["missed_by_all"] == [{"slug": "yem", "song": "YEM"}]
    # compact: full per-source row lists are omitted
    assert "rows" not in show["sources"]["heuristic"]


def test_scoreboard_without_model_label_shows_only_heuristic(tmp_path):
    _write_scorecards(tmp_path)
    result = tools.scoreboard(tmp_path)
    show = result["recent_shows"][0]
    assert set(show["sources"]) == {"heuristic"}  # own track omitted without a label


def test_scoreboard_respects_recent_limit(tmp_path):
    _write_scorecards(tmp_path)
    result = tools.scoreboard(tmp_path, recent=1)
    assert [s["showdate"] for s in result["recent_shows"]] == ["2026-07-08"]


def test_scoreboard_missing_dir_returns_empty(tmp_path):
    result = tools.scoreboard(tmp_path / "does-not-exist", model_label="claude-opus")
    assert result == {"models": {}, "recent_shows": []}


def test_scoreboard_empty_dir_returns_empty(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = tools.scoreboard(empty)
    assert result["models"] == {}
    assert result["recent_shows"] == []
