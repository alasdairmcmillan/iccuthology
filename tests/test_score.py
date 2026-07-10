"""Unit tests for phishpred.score -- post-show accuracy scorecards (deploy
plan §8, DEPLOY-CONTRACTS.md §8).

The pure core (`score_show` / `build_scoreboard`) is exercised over hand-built
frozen payloads with hand-computed metrics; the DB-backed driver (`score_all`)
uses a small in-memory DB built the same way tests/test_mcp.py does. Dates are
frozen (injected `today=`) rather than depending on the real clock, matching the
repo convention (see tests/test_modes.py / test_mcp.py).
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from phishpred import db
from phishpred.score import build_scoreboard, score_all, score_show


# ---------------------------------------------------------------------------
# Pure core: score_show metric math
# ---------------------------------------------------------------------------

def _frozen_payload(sources: dict) -> dict:
    return {
        "showdate": "2026-07-05",
        "venue_name": "Ruoff Music Center",
        "city": "Noblesville",
        "state": "IN",
        "epoch": "228c7eb3a0e9",
        "k": 22.4,
        "sources": sources,
    }


# played: tweezer, wilson, yem (distinct, setlist order)
PLAYED = [
    {"slug": "tweezer", "song": "Tweezer"},
    {"slug": "wilson", "song": "Wilson"},
    {"slug": "yem", "song": "YEM"},
]

# heuristic shortlist, prob desc: two hits (tweezer, wilson), two misses (ghost, hood)
HEURISTIC_ROWS = [
    {"song": "Tweezer", "slug": "tweezer", "prob": 0.9},
    {"song": "Ghost", "slug": "ghost", "prob": 0.6},
    {"song": "Wilson", "slug": "wilson", "prob": 0.4},
    {"song": "Harry Hood", "slug": "hood", "prob": 0.2},
]


def test_score_show_metric_math_hand_computed():
    payload = _frozen_payload(
        {"heuristic": {"model": "heuristic", "kind": "statistical", "rows": HEURISTIC_ROWS}}
    )
    sc = score_show(payload, PLAYED)

    assert sc["showdate"] == "2026-07-05"
    assert sc["frozen_epoch"] == "228c7eb3a0e9"
    assert sc["venue_name"] == "Ruoff Music Center"
    assert sc["phishnet_url"] == "https://phish.net/setlists/?d=2026-07-05"
    assert sc["n_played"] == 3

    m = sc["sources"]["heuristic"]["metrics"]
    assert sc["sources"]["heuristic"]["n_rows"] == 4
    assert m["hits_top10"] == 2
    assert m["hit_rate_top10"] == pytest.approx(0.5)          # 2 / min(10, 4)
    assert m["recall"] == pytest.approx(2 / 3)                # {tweezer,wilson} / 3 played
    # brier = mean[(0.9-1)^2, (0.6-0)^2, (0.4-1)^2, (0.2-0)^2] = 0.77/4
    assert m["brier"] == pytest.approx(0.1925)
    expected_ll = (
        -math.log(0.9) - math.log(0.4) - math.log(0.4) - math.log(0.8)
    ) / 4
    assert m["log_loss"] == pytest.approx(expected_ll)

    # best_call = hit with the LOWEST prob (wilson 0.4); biggest_whiff = miss with
    # the HIGHEST prob (ghost 0.6).
    assert sc["sources"]["heuristic"]["best_call"] == {
        "song": "Wilson", "slug": "wilson", "prob": 0.4
    }
    assert sc["sources"]["heuristic"]["biggest_whiff"] == {
        "song": "Ghost", "slug": "ghost", "prob": 0.6
    }

    # rows carry hit flags in frozen (prob desc) order.
    hits = {r["slug"]: r["hit"] for r in sc["sources"]["heuristic"]["rows"]}
    assert hits == {"tweezer": True, "ghost": False, "wilson": True, "hood": False}


def test_score_show_best_call_null_when_no_hits():
    rows = [
        {"song": "Ghost", "slug": "ghost", "prob": 0.6},
        {"song": "Harry Hood", "slug": "hood", "prob": 0.2},
    ]
    payload = _frozen_payload({"heuristic": {"model": "heuristic", "kind": "statistical", "rows": rows}})
    sc = score_show(payload, PLAYED)
    src = sc["sources"]["heuristic"]
    assert src["best_call"] is None                 # no hits
    assert src["biggest_whiff"] == {"song": "Ghost", "slug": "ghost", "prob": 0.6}
    assert src["metrics"]["hits_top10"] == 0
    assert src["metrics"]["recall"] == pytest.approx(0.0)


def test_score_show_biggest_whiff_null_when_all_hit():
    rows = [
        {"song": "Tweezer", "slug": "tweezer", "prob": 0.9},
        {"song": "Wilson", "slug": "wilson", "prob": 0.4},
    ]
    payload = _frozen_payload({"heuristic": {"model": "heuristic", "kind": "statistical", "rows": rows}})
    sc = score_show(payload, PLAYED)
    src = sc["sources"]["heuristic"]
    assert src["biggest_whiff"] is None             # every row hit
    assert src["best_call"] == {"song": "Wilson", "slug": "wilson", "prob": 0.4}
    assert src["metrics"]["hit_rate_top10"] == pytest.approx(1.0)


def test_score_show_multi_source_and_mcp_keeps_rationale():
    payload = _frozen_payload(
        {
            "heuristic": {"model": "heuristic", "kind": "statistical", "rows": HEURISTIC_ROWS},
            "mcp:claude-fable": {
                "model": "mcp:claude-fable",
                "kind": "mcp",
                "rationale": "Fluffhead is due; opener energy points to Tweezer.",
                "submitted_at": "2026-07-04T13:00:00Z",
                "rows": [
                    {"song": "Tweezer", "slug": "tweezer", "prob": 0.7},
                    {"song": "YEM", "slug": "yem", "prob": 0.5},
                ],
            },
        }
    )
    sc = score_show(payload, PLAYED)

    assert set(sc["sources"]) == {"heuristic", "mcp:claude-fable"}
    mcp = sc["sources"]["mcp:claude-fable"]
    # mcp source keeps its frozen rationale / submitted_at verbatim.
    assert mcp["rationale"] == "Fluffhead is due; opener energy points to Tweezer."
    assert mcp["submitted_at"] == "2026-07-04T13:00:00Z"
    # both mcp rows hit (tweezer, yem played)
    assert mcp["metrics"]["hits_top10"] == 2
    assert mcp["metrics"]["recall"] == pytest.approx(2 / 3)
    # statistical source does NOT carry rationale keys
    assert "rationale" not in sc["sources"]["heuristic"]


def test_score_show_missed_by_all():
    # yem is played but appears in NO source's shortlist -> missed_by_all.
    payload = _frozen_payload(
        {
            "heuristic": {"model": "heuristic", "kind": "statistical", "rows": HEURISTIC_ROWS},
            "mcp:agent": {
                "model": "mcp:agent", "kind": "mcp", "rationale": None, "submitted_at": None,
                "rows": [{"song": "Wilson", "slug": "wilson", "prob": 0.5}],
            },
        }
    )
    sc = score_show(payload, PLAYED)
    assert sc["missed_by_all"] == [{"slug": "yem", "song": "YEM"}]


# ---------------------------------------------------------------------------
# Pure core: build_scoreboard aggregation
# ---------------------------------------------------------------------------

def test_build_scoreboard_means_and_desc_order():
    sc_a = {
        "showdate": "2026-07-05", "venue_name": "V1", "city": "C1", "state": "S1", "n_played": 20,
        "sources": {
            "heuristic": {"kind": "statistical", "metrics": {
                "hit_rate_top10": 0.6, "recall": 0.4, "brier": 0.10, "log_loss": 0.30}},
        },
    }
    sc_b = {
        "showdate": "2026-07-08", "venue_name": "V2", "city": "C2", "state": "S2", "n_played": 22,
        "sources": {
            "heuristic": {"kind": "statistical", "metrics": {
                "hit_rate_top10": 0.4, "recall": 0.6, "brier": 0.20, "log_loss": 0.50}},
            "mcp:agent": {"kind": "mcp", "metrics": {
                "hit_rate_top10": 0.8, "recall": 0.5, "brier": 0.05, "log_loss": 0.10}},
        },
    }
    board = build_scoreboard([sc_a, sc_b])

    # shows showdate DESC
    assert [s["showdate"] for s in board["shows"]] == ["2026-07-08", "2026-07-05"]
    assert board["shows"][0]["source_keys"] == ["heuristic", "mcp:agent"]

    # heuristic appears in both -> unweighted means
    h = board["models"]["heuristic"]
    assert h["n_shows"] == 2
    assert h["hit_rate_top10"] == pytest.approx(0.5)
    assert h["recall"] == pytest.approx(0.5)
    assert h["brier"] == pytest.approx(0.15)
    assert h["log_loss"] == pytest.approx(0.40)
    # mcp appears in one show only
    assert board["models"]["mcp:agent"]["n_shows"] == 1
    assert board["models"]["mcp:agent"]["kind"] == "mcp"


def test_build_scoreboard_empty():
    board = build_scoreboard([])
    assert board["shows"] == []
    assert board["models"] == {}
    assert "updated_at" in board


# ---------------------------------------------------------------------------
# DB-backed driver: score_all
# ---------------------------------------------------------------------------

VENUES = [(1, "Ruoff Music Center", "Noblesville")]
SONGS = [
    (101, "tweezer", "Tweezer"),
    (102, "yem", "YEM"),
    (103, "wilson", "Wilson"),
    (104, "ghost", "Ghost"),
    (105, "hood", "Harry Hood"),
]

# showid, show_index, showdate  -- indexed (played)
PLAYED_SHOWS = [
    (1, 0, "2026-06-01"),   # old played show (before rescore cutoff)
    (2, 1, "2026-07-05"),   # recent played show (inside rescore window)
]
PLAYED_SETLISTS = {
    1: [101, 103, 102],   # tweezer, wilson, yem
    2: [101, 103, 102],
}
# an unplayed past show (no setlist yet, show_index NULL)
UNPLAYED_SHOW = (3, "2026-07-08")


def _populate(conn):
    for vid, name, city in VENUES:
        conn.execute("INSERT INTO venues (venueid, name, city) VALUES (?,?,?)", (vid, name, city))
    for sid, slug, name in SONGS:
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,1)", (sid, slug, name)
        )
    for showid, idx, showdate in PLAYED_SHOWS:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, show_index, exclude) VALUES (?,?,1,?,0)",
            (showid, showdate, idx),
        )
    conn.execute(
        "INSERT INTO shows (showid, showdate, venueid, show_index, exclude) VALUES (?,?,1,NULL,0)",
        UNPLAYED_SHOW,
    )
    for showid, songs in PLAYED_SETLISTS.items():
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


def _write_frozen(frozen_dir: Path, showdate: str, rows=None) -> None:
    rows = rows if rows is not None else HEURISTIC_ROWS
    frozen_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "showdate": showdate,
        "venue_name": "Ruoff Music Center", "city": "Noblesville", "state": "IN",
        "epoch": "228c7eb3a0e9", "k": 22.4,
        "sources": {"heuristic": {"model": "heuristic", "kind": "statistical", "rows": rows}},
    }
    (frozen_dir / f"{showdate}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_score_all_scores_played_skips_unplayed_and_future(conn, tmp_path):
    frozen = tmp_path / "frozen"
    out = tmp_path / "scorecards"
    _write_frozen(frozen, "2026-07-05")   # played + indexed
    _write_frozen(frozen, "2026-07-08")   # past but unplayed (show_index NULL) -> skip
    _write_frozen(frozen, "2026-07-20")   # future (>= today) -> skip

    written = score_all(conn, frozen, out, today=date(2026, 7, 10))

    assert written == ["2026-07-05"]
    assert (out / "2026-07-05.json").exists()
    assert not (out / "2026-07-08.json").exists()
    assert not (out / "2026-07-20.json").exists()

    sc = json.loads((out / "2026-07-05.json").read_text(encoding="utf-8"))
    assert sc["n_played"] == 3
    assert [p["slug"] for p in sc["played"]] == ["tweezer", "wilson", "yem"]
    # scoreboard always rebuilt
    board = json.loads((out / "scoreboard.json").read_text(encoding="utf-8"))
    assert [s["showdate"] for s in board["shows"]] == ["2026-07-05"]


def test_score_all_rescore_window_rewrite_vs_old_skip(conn, tmp_path):
    frozen = tmp_path / "frozen"
    out = tmp_path / "scorecards"
    out.mkdir(parents=True, exist_ok=True)
    _write_frozen(frozen, "2026-06-01")   # old played show
    _write_frozen(frozen, "2026-07-05")   # recent played show

    # Pre-existing scorecards with a sentinel marker so we can detect a rewrite.
    for d in ("2026-06-01", "2026-07-05"):
        (out / f"{d}.json").write_text(json.dumps({"showdate": d, "marker": True, "sources": {}}), encoding="utf-8")

    # today=2026-07-10, rescore_days=7 -> cutoff 2026-07-03.
    written = score_all(conn, frozen, out, rescore_days=7, today=date(2026, 7, 10))

    # only the in-window show is rewritten
    assert written == ["2026-07-05"]
    old = json.loads((out / "2026-06-01.json").read_text(encoding="utf-8"))
    recent = json.loads((out / "2026-07-05.json").read_text(encoding="utf-8"))
    assert old.get("marker") is True          # old show skipped -> marker survives
    assert "marker" not in recent             # recent show rewritten -> real scorecard
    assert recent["frozen_epoch"] == "228c7eb3a0e9"

    # scoreboard reflects BOTH scorecards present on disk (desc order)
    board = json.loads((out / "scoreboard.json").read_text(encoding="utf-8"))
    assert [s["showdate"] for s in board["shows"]] == ["2026-07-05", "2026-06-01"]


def test_score_all_empty_frozen_dir_writes_empty_scoreboard(conn, tmp_path):
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    out = tmp_path / "scorecards"

    written = score_all(conn, frozen, out, today=date(2026, 7, 10))

    assert written == []
    board = json.loads((out / "scoreboard.json").read_text(encoding="utf-8"))
    assert board["shows"] == []
    assert board["models"] == {}


def test_score_all_skips_malformed_frozen_without_crash(conn, tmp_path):
    frozen = tmp_path / "frozen"
    out = tmp_path / "scorecards"
    _write_frozen(frozen, "2026-07-05")
    frozen.mkdir(parents=True, exist_ok=True)
    (frozen / "broken.json").write_text("{not valid json", encoding="utf-8")

    written = score_all(conn, frozen, out, today=date(2026, 7, 10))

    # good file scored, malformed one skipped, no crash
    assert written == ["2026-07-05"]
    assert (out / "scoreboard.json").exists()
