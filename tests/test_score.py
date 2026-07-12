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
    assert m["top_n"] == 20                                   # published so consumers don't hardcode N
    assert m["hits_top20"] == 2
    assert m["hit_rate_top20"] == pytest.approx(0.5)          # 2 / min(20, 4)
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
    assert src["metrics"]["hits_top20"] == 0
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
    assert src["metrics"]["hit_rate_top20"] == pytest.approx(1.0)


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
    assert mcp["metrics"]["hits_top20"] == 2
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
# Pure core: setlist benchmark (setlist_score)
# ---------------------------------------------------------------------------

# Predicted structured setlist (frozen/folded shape: {slug, song} objects).
SETLIST_PRED = {
    "sets": {
        "1": [
            {"slug": "tweezer", "song": "Tweezer"},
            {"slug": "wilson", "song": "Wilson"},
            {"slug": "gin", "song": "Bathtub Gin"},
        ],
        "e": [
            {"slug": "hood", "song": "Harry Hood"},
            {"slug": "ghost", "song": "Ghost"},
        ],
    }
}
# Actual played sets (distinct within set, position order).
SL_PLAYED_SETS = {
    "1": [
        {"slug": "tweezer", "song": "Tweezer"},
        {"slug": "ghost", "song": "Ghost"},
        {"slug": "wilson", "song": "Wilson"},
    ],
    "e": [{"slug": "hood", "song": "Harry Hood"}],
}
SL_PLAYED = [
    {"slug": "tweezer", "song": "Tweezer"},
    {"slug": "ghost", "song": "Ghost"},
    {"slug": "wilson", "song": "Wilson"},
    {"slug": "hood", "song": "Harry Hood"},
]


def test_score_show_setlist_score_hand_computed():
    src = {
        "model": "mcp:a", "kind": "mcp", "rationale": None, "submitted_at": None,
        "rows": [{"song": "Tweezer", "slug": "tweezer", "prob": 0.7}],
        "setlist": SETLIST_PRED,
    }
    sc = score_show(_frozen_payload({"mcp:a": src}), SL_PLAYED, SL_PLAYED_SETS)
    ss = sc["sources"]["mcp:a"]["setlist_score"]

    assert ss["n_songs"] == 5
    assert ss["hits"] == 4                       # tweezer, wilson, hood, ghost played
    assert ss["hit_rate"] == pytest.approx(0.8)  # 4 / 5
    assert ss["placed"] == 3                     # tweezer, wilson (set1), hood (e)
    assert ss["placed_rate"] == pytest.approx(0.75)  # 3 / 4 hits
    # weighted_score = (hits + placed + exact_calls) / (3 * n_songs) = (4+3+2)/15
    assert ss["weighted_score"] == pytest.approx(0.6)

    assert ss["marquee"]["opener"] is True       # tweezer opens both
    assert ss["marquee"]["encore"] is True       # hood in both encores
    assert ss["marquee"]["set1_closer"] is False  # gin != wilson
    assert ss["marquee"]["set2_opener"] is False  # no set 2 on either side
    assert ss["marquee_calls"] == 2

    # exact_calls: set1 pos0 tweezer==tweezer, encore pos0 hood==hood -> 2
    assert ss["exact_calls"] == 2
    assert ss["sharpshooter"] is True            # >= 2

    ann1 = {r["slug"]: r for r in ss["sets"]["1"]}
    assert ann1["gin"]["hit"] is False and ann1["gin"]["placed"] is False   # unplayed
    assert ann1["gin"]["exact"] is False
    assert ann1["tweezer"]["exact"] is True      # tweezer opens both -> exact slot
    assert ann1["wilson"]["placed"] is True and ann1["wilson"]["exact"] is False  # right set, wrong slot
    anne = {r["slug"]: r for r in ss["sets"]["e"]}
    assert anne["ghost"]["hit"] is True and anne["ghost"]["placed"] is False  # played, wrong set
    assert anne["ghost"]["exact"] is False
    assert anne["hood"]["exact"] is True         # encore pos0 hood==hood

    # exact_calls is exactly the count of exact-annotated rows.
    n_exact_rows = sum(1 for rows in ss["sets"].values() for r in rows if r["exact"])
    assert n_exact_rows == ss["exact_calls"]
    # nesting invariant: exact ⊆ placed ⊆ hit for every annotated row.
    for rows in ss["sets"].values():
        for r in rows:
            if r["exact"]:
                assert r["placed"] is True
            if r["placed"]:
                assert r["hit"] is True

    # played_sets echoed onto the scorecard for the UI
    assert sc["played_sets"]["1"][0]["slug"] == "tweezer"


def test_score_show_setlist_weighted_all_exact_is_one():
    # Every called song lands in its exact slot -> hit==placed==exact for all,
    # so weighted_score = (n + n + n) / (3n) = 1.0 and every row flags exact.
    pred = {"sets": {
        "1": [{"slug": "tweezer", "song": "Tweezer"}, {"slug": "wilson", "song": "Wilson"}],
        "e": [{"slug": "hood", "song": "Harry Hood"}],
    }}
    played_sets = {
        "1": [{"slug": "tweezer", "song": "Tweezer"}, {"slug": "wilson", "song": "Wilson"}],
        "e": [{"slug": "hood", "song": "Harry Hood"}],
    }
    played = [{"slug": "tweezer", "song": "Tweezer"}, {"slug": "wilson", "song": "Wilson"},
              {"slug": "hood", "song": "Harry Hood"}]
    src = {"model": "mcp:a", "kind": "mcp", "rationale": None, "submitted_at": None,
           "rows": [{"song": "Tweezer", "slug": "tweezer", "prob": 0.7}], "setlist": pred}
    ss = score_show(_frozen_payload({"mcp:a": src}), played, played_sets)["sources"]["mcp:a"]["setlist_score"]

    assert ss["hits"] == 3 and ss["placed"] == 3 and ss["exact_calls"] == 3
    assert ss["weighted_score"] == pytest.approx(1.0)
    assert all(r["exact"] for rows in ss["sets"].values() for r in rows)


def test_score_show_setlist_weighted_no_hit_is_zero():
    # No called song plays at all -> hits==placed==exact==0 -> weighted_score 0.0.
    pred = {"sets": {"1": [{"slug": "gin", "song": "Bathtub Gin"},
                           {"slug": "reba", "song": "Reba"}]}}
    played_sets = {"1": [{"slug": "tweezer", "song": "Tweezer"}]}
    played = [{"slug": "tweezer", "song": "Tweezer"}]
    src = {"model": "mcp:a", "kind": "mcp", "rationale": None, "submitted_at": None,
           "rows": [{"song": "Bathtub Gin", "slug": "gin", "prob": 0.4}], "setlist": pred}
    ss = score_show(_frozen_payload({"mcp:a": src}), played, played_sets)["sources"]["mcp:a"]["setlist_score"]

    assert ss["hits"] == 0 and ss["placed"] == 0 and ss["exact_calls"] == 0
    assert ss["weighted_score"] == pytest.approx(0.0)
    assert not any(r["exact"] for rows in ss["sets"].values() for r in rows)


def test_score_show_setlist_exact_calls_matches_old_loop_semantics():
    # exact_calls counts (set, position) matches over shared keys up to the min
    # length. Exercise: a called set absent on the played side ("3"), a played
    # set absent on the called side (handled by iterating pred sets), and length
    # mismatches (called longer than played and vice versa).
    pred = {"sets": {
        "1": [{"slug": "a", "song": "A"}, {"slug": "b", "song": "B"}, {"slug": "c", "song": "C"}],  # played "1" shorter
        "2": [{"slug": "x", "song": "X"}],                                                          # played "2" longer
        "3": [{"slug": "z", "song": "Z"}],                                                          # no played "3"
    }}
    played_sets = {
        "1": [{"slug": "a", "song": "A"}, {"slug": "q", "song": "Q"}],   # pos0 a==a exact, pos1 b!=q
        "2": [{"slug": "x", "song": "X"}, {"slug": "y", "song": "Y"}],   # pos0 x==x exact
    }
    played = [{"slug": "a", "song": "A"}, {"slug": "q", "song": "Q"},
              {"slug": "x", "song": "X"}, {"slug": "y", "song": "Y"}]
    src = {"model": "mcp:a", "kind": "mcp", "rationale": None, "submitted_at": None,
           "rows": [{"song": "A", "slug": "a", "prob": 0.5}], "setlist": pred}

    played_by_set = {k: [s["slug"] for s in v] for k, v in played_sets.items()}
    # Reference: the pre-refactor exact_calls loop, verbatim.
    old = 0
    for key, songs in pred["sets"].items():
        actual = played_by_set.get(key)
        if not actual:
            continue
        for i in range(min(len(songs), len(actual))):
            if songs[i].get("slug") == actual[i]:
                old += 1

    ss = score_show(_frozen_payload({"mcp:a": src}), played, played_sets)["sources"]["mcp:a"]["setlist_score"]
    assert ss["exact_calls"] == old == 2   # a (set1 pos0) + x (set2 pos0); "3" sits out


def test_score_show_setlist_score_null_without_setlist():
    payload = _frozen_payload(
        {"heuristic": {"model": "heuristic", "kind": "statistical", "rows": HEURISTIC_ROWS}}
    )
    sc = score_show(payload, PLAYED, SL_PLAYED_SETS)
    # A source that carries no setlist sits out the benchmark.
    assert sc["sources"]["heuristic"]["setlist_score"] is None


# ---------------------------------------------------------------------------
# Pure core: version scoring + after_showdate boundaries
# ---------------------------------------------------------------------------

def test_score_show_version_scoring_and_after_showdate():
    src = {
        "model": "mcp:a", "kind": "mcp", "rationale": "final",
        "submitted_at": "2026-07-04T12:00:00Z",
        "rows": [
            {"song": "Tweezer", "slug": "tweezer", "prob": 0.8},
            {"song": "Wilson", "slug": "wilson", "prob": 0.6},
        ],
        "versions": [
            {"submitted_at": "2026-06-20T12:00:00Z",   # pre-run (before window)
             "rows": [{"song": "Ghost", "slug": "ghost", "prob": 0.5}]},
            {"submitted_at": "2026-07-04T09:00:00Z",   # after night 1 (2026-07-03)
             "rows": [{"song": "Tweezer", "slug": "tweezer", "prob": 0.7}]},
        ],
    }
    # showdate is 2026-07-05; night 1 played 2026-07-03 (within 10 days).
    sc = score_show(_frozen_payload({"mcp:a": src}), PLAYED,
                    played_showdates=["2026-07-01", "2026-07-03"])
    entry = sc["sources"]["mcp:a"]
    # top-level entry is the FINAL take (rationale kept verbatim)
    assert entry["rationale"] == "final"

    vs = entry["versions"]
    assert len(vs) == 2                                 # oldest first
    assert vs[0]["after_showdate"] is None              # pre-run
    assert vs[0]["metrics"]["hits_top20"] == 0          # ghost not played
    assert vs[1]["after_showdate"] == "2026-07-03"      # latest played in window
    assert vs[1]["metrics"]["hits_top20"] == 1          # tweezer played
    assert vs[1]["rows"][0]["hit"] is True              # rows carry hit flags


def test_score_show_after_showdate_null_beyond_10_day_gap():
    src = {
        "model": "mcp:a", "kind": "mcp", "rationale": None,
        "submitted_at": "2026-07-04T09:00:00Z",
        "rows": [{"song": "Tweezer", "slug": "tweezer", "prob": 0.7}],
        "versions": [
            {"submitted_at": "2026-07-04T09:00:00Z",
             "rows": [{"song": "Tweezer", "slug": "tweezer", "prob": 0.7}]},
        ],
    }
    # The only played show is 34 days before the show -> outside the 10-day window.
    sc = score_show(_frozen_payload({"mcp:a": src}), PLAYED,
                    played_showdates=["2026-06-01"])
    assert sc["sources"]["mcp:a"]["versions"][0]["after_showdate"] is None


# ---------------------------------------------------------------------------
# Pure core: build_scoreboard aggregation
# ---------------------------------------------------------------------------

def test_build_scoreboard_means_and_desc_order():
    sc_a = {
        "showdate": "2026-07-05", "venue_name": "V1", "city": "C1", "state": "S1", "n_played": 20,
        "sources": {
            "heuristic": {"kind": "statistical", "n_rows": 40, "metrics": {
                "hit_rate_top20": 0.6, "recall": 0.4, "brier": 0.10, "log_loss": 0.30}},
        },
    }
    sc_b = {
        "showdate": "2026-07-08", "venue_name": "V2", "city": "C2", "state": "S2", "n_played": 22,
        "sources": {
            "heuristic": {"kind": "statistical", "n_rows": 30, "metrics": {
                "hit_rate_top20": 0.4, "recall": 0.6, "brier": 0.20, "log_loss": 0.50}},
            "mcp:agent": {"kind": "mcp", "n_rows": 24, "metrics": {
                "hit_rate_top20": 0.8, "recall": 0.5, "brier": 0.05, "log_loss": 0.10}},
        },
    }
    board = build_scoreboard([sc_a, sc_b])

    # shows showdate DESC
    assert [s["showdate"] for s in board["shows"]] == ["2026-07-08", "2026-07-05"]
    assert board["shows"][0]["source_keys"] == ["heuristic", "mcp:agent"]

    # heuristic appears in both -> unweighted means
    h = board["models"]["heuristic"]
    assert h["n_shows"] == 2
    assert h["hit_rate_top20"] == pytest.approx(0.5)
    assert h["recall"] == pytest.approx(0.5)
    assert h["brier"] == pytest.approx(0.15)
    assert h["log_loss"] == pytest.approx(0.40)
    assert h["avg_n_rows"] == pytest.approx(35.0)  # mean(40, 30)
    assert "vs_heuristic" not in h                  # omitted for the heuristic itself
    # mcp appears in one show only
    agent = board["models"]["mcp:agent"]
    assert agent["n_shows"] == 1
    assert agent["kind"] == "mcp"
    assert agent["avg_n_rows"] == pytest.approx(24.0)
    # vs_heuristic: paired over the ONE show where both appear (sc_b)
    vh = agent["vs_heuristic"]
    assert vh["n_shows"] == 1
    assert vh["hit_rate_top20_delta"] == pytest.approx(0.8 - 0.4)
    assert vh["recall_delta"] == pytest.approx(0.5 - 0.6)


def test_build_scoreboard_empty():
    board = build_scoreboard([])
    assert board["shows"] == []
    assert board["models"] == {}
    assert "updated_at" in board


def test_build_scoreboard_setlist_and_refresh_gain_aggregates():
    sc_a = {
        "showdate": "2026-07-05", "venue_name": "V1", "city": "C1", "state": "S1", "n_played": 20,
        "sources": {
            "mcp:a": {
                "kind": "mcp",
                "metrics": {"hit_rate_top20": 0.6, "recall": 0.5, "brier": 0.1, "log_loss": 0.3},
                "setlist_score": {"hit_rate": 0.5, "placed_rate": 0.6, "weighted_score": 0.5,
                                  "marquee_calls": 2, "exact_calls": 1, "sharpshooter": False},
                "versions": [  # first take -> refresh_gain compares against it
                    {"metrics": {"hit_rate_top20": 0.4, "recall": 0.3, "brier": 0.2, "log_loss": 0.5}},
                ],
            },
        },
    }
    sc_b = {
        "showdate": "2026-07-08", "venue_name": "V2", "city": "C2", "state": "S2", "n_played": 22,
        "sources": {
            "mcp:a": {
                "kind": "mcp",
                "metrics": {"hit_rate_top20": 0.8, "recall": 0.7, "brier": 0.05, "log_loss": 0.1},
                "setlist_score": {"hit_rate": 0.7, "placed_rate": 0.8, "weighted_score": 0.7,
                                  "marquee_calls": 3, "exact_calls": 2, "sharpshooter": True},
                # no versions here -> excluded from refresh_gain
            },
        },
    }
    m = build_scoreboard([sc_a, sc_b])["models"]["mcp:a"]

    # setlist aggregate over BOTH shows: rates are means, calls/sharpshooters totals
    sl = m["setlist"]
    assert sl["n_shows"] == 2
    assert sl["hit_rate"] == pytest.approx(0.6)      # mean(0.5, 0.7)
    assert sl["placed_rate"] == pytest.approx(0.7)   # mean(0.6, 0.8)
    assert sl["weighted_score"] == pytest.approx(0.6)  # mean(0.5, 0.7)
    assert sl["marquee_calls"] == 5                  # 2 + 3
    assert sl["exact_calls"] == 3                    # 1 + 2
    assert sl["sharpshooters"] == 1                  # only sc_b

    # refresh_gain over the ONE show with a prior version (final - first take)
    rg = m["refresh_gain"]
    assert rg["n_shows"] == 1
    assert rg["mean_hit_rate_top20_delta"] == pytest.approx(0.6 - 0.4)
    assert rg["mean_recall_delta"] == pytest.approx(0.5 - 0.3)


def test_build_scoreboard_omits_setlist_and_refresh_gain_when_absent():
    sc = {
        "showdate": "2026-07-05", "venue_name": "V", "city": "C", "state": "S", "n_played": 20,
        "sources": {
            "heuristic": {
                "kind": "statistical",
                "metrics": {"hit_rate_top20": 0.5, "recall": 0.4, "brier": 0.1, "log_loss": 0.3},
                "setlist_score": None,   # sits out the setlist benchmark
            },
        },
    }
    m = build_scoreboard([sc])["models"]["heuristic"]
    assert "setlist" not in m        # no setlist-scored shows
    assert "refresh_gain" not in m   # no multi-take shows


def test_build_scoreboard_vs_heuristic_omitted_when_no_paired_shows():
    # mcp:agent is scored on a show where the heuristic is absent -> no pairing.
    sc = {
        "showdate": "2026-07-05", "venue_name": "V", "city": "C", "state": "S", "n_played": 20,
        "sources": {
            "mcp:agent": {
                "kind": "mcp", "n_rows": 25,
                "metrics": {"hit_rate_top20": 0.5, "recall": 0.4, "brier": 0.1, "log_loss": 0.3},
            },
        },
    }
    m = build_scoreboard([sc])["models"]["mcp:agent"]
    assert "vs_heuristic" not in m   # heuristic never co-scored -> zero paired shows
    assert m["avg_n_rows"] == pytest.approx(25.0)


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


def test_score_all_force_rescores_old_cards_outside_window(conn, tmp_path):
    frozen = tmp_path / "frozen"
    out = tmp_path / "scorecards"
    out.mkdir(parents=True, exist_ok=True)
    _write_frozen(frozen, "2026-06-01")   # old played show, well outside the window

    # Pre-existing scorecard with a sentinel marker (a stale, pre-cutover card).
    (out / "2026-06-01.json").write_text(
        json.dumps({"showdate": "2026-06-01", "marker": True, "sources": {}}), encoding="utf-8"
    )

    # Without force the old card is skipped; with force it is rewritten so a
    # metric-definition change propagates to every card.
    assert score_all(conn, frozen, out, today=date(2026, 7, 10)) == []
    assert json.loads((out / "2026-06-01.json").read_text(encoding="utf-8")).get("marker") is True

    written = score_all(conn, frozen, out, today=date(2026, 7, 10), force=True)
    assert written == ["2026-06-01"]
    recent = json.loads((out / "2026-06-01.json").read_text(encoding="utf-8"))
    assert "marker" not in recent                       # rewritten as a real scorecard
    assert recent["sources"]["heuristic"]["metrics"]["top_n"] == 20


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
