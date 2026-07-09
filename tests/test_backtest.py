"""Tests for phishpred.backtest — pure metric checks + tiny in-memory DB holdout."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from phishpred import backtest
from phishpred.db import get_connection, init_db


@pytest.fixture
def memory_conn():
    conn = get_connection(":memory:")
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Metric functions on hand-computed tiny cases
# --------------------------------------------------------------------------- #
def test_brier_hand_computed():
    y = [1, 0]
    p = [0.8, 0.3]
    # ((0.8-1)^2 + (0.3-0)^2)/2 = (0.04 + 0.09)/2 = 0.065
    assert backtest.brier_score(y, p) == pytest.approx(0.065)


def test_log_loss_hand_computed():
    y = [1, 0]
    p = [0.8, 0.2]
    # -(log(0.8) + log(0.8))/2 = -log(0.8)
    assert backtest.log_loss_score(y, p) == pytest.approx(-np.log(0.8))


def test_log_loss_clips_extremes():
    # p=0 with y=1 would be -inf without clipping; must stay finite.
    val = backtest.log_loss_score([1], [0.0])
    assert np.isfinite(val)
    assert val == pytest.approx(-np.log(1e-6))


def test_hit_at_k_hand_computed():
    # One show, 5 candidates; top-2 by prob are songs with prob .9 and .8.
    df = pd.DataFrame(
        {
            "showid": [1, 1, 1, 1, 1],
            "prob": [0.9, 0.8, 0.1, 0.05, 0.02],
            "y": [1, 0, 1, 0, 0],
        }
    )
    # Top-2 -> {y=1, y=0} -> 1 hit.
    assert backtest.hit_at_k(df, 2) == pytest.approx(1.0)
    # Top-4 -> {1,0,1,0} -> 2 hits.
    assert backtest.hit_at_k(df, 4) == pytest.approx(2.0)


def test_hit_at_k_averages_over_shows():
    df = pd.DataFrame(
        {
            "showid": [1, 1, 2, 2],
            "prob": [0.9, 0.1, 0.4, 0.3],
            "y": [1, 0, 0, 0],
        }
    )
    # show1 top-1 hit=1, show2 top-1 hit=0 -> mean 0.5
    assert backtest.hit_at_k(df, 1) == pytest.approx(0.5)


def test_calibration_table_buckets():
    # probs land in buckets 0-10, 50-60, 90-100.
    y = np.array([0, 0, 1, 1, 1])
    p = np.array([0.05, 0.55, 0.55, 0.95, 0.99])
    table = backtest.calibration_table(y, p)
    assert len(table) == 10

    b0 = table[0]  # 0-10%
    assert b0["n"] == 1
    assert b0["mean_pred"] == pytest.approx(0.05)
    assert b0["empirical"] == pytest.approx(0.0)

    b5 = table[5]  # 50-60%
    assert b5["n"] == 2
    assert b5["mean_pred"] == pytest.approx(0.55)
    assert b5["empirical"] == pytest.approx(0.5)

    b9 = table[9]  # 90-100% (inclusive upper edge)
    assert b9["n"] == 2
    assert b9["empirical"] == pytest.approx(1.0)

    # All rows accounted for.
    assert sum(row["n"] for row in table) == len(p)


# --------------------------------------------------------------------------- #
# Holdout-tour selection on a tiny in-memory DB
# --------------------------------------------------------------------------- #
def _seed_db(conn):
    init_db(conn)
    conn.execute("INSERT INTO venues (venueid, name) VALUES (1, 'Venue')")
    conn.execute(
        "INSERT INTO songs (songid, slug, name, is_original) VALUES (1, 'song', 'Song', 1)"
    )

    # 3 tours (A oldest, B, C newest), 2 shows each, all with a performance.
    shows = [
        # showid, showdate,      show_index, tourid, tour_name
        (1, "2015-01-01", 0, 10, "Tour A"),
        (2, "2015-01-02", 1, 10, "Tour A"),
        (3, "2016-01-01", 2, 20, "Tour B"),
        (4, "2016-01-02", 3, 20, "Tour B"),
        (5, "2017-01-01", 4, 30, "Tour C"),
        (6, "2017-01-02", 5, 30, "Tour C"),
    ]
    for showid, date, idx, tourid, tname in shows:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, tour_name, "
            "exclude, show_index) VALUES (?,?,1,?,?,0,?)",
            (showid, date, tourid, tname, idx),
        )
        conn.execute(
            "INSERT INTO performances (showid, songid, set_label, position) "
            "VALUES (?, 1, '1', 1)",
            (showid,),
        )
    conn.commit()
    return conn


def test_select_holdout_picks_two_most_recent_tours(memory_conn):
    conn = _seed_db(memory_conn)
    sel = backtest.select_holdout(conn, holdout_tours=2)

    # Tours B and C are most recent; A excluded.
    assert set(sel.tour_labels) == {"Tour B", "Tour C"}
    assert set(sel.showids) == {3, 4, 5, 6}
    assert sel.start_index == 2
    assert sel.n_shows == 4
    assert sel.date_range == ("2016-01-01", "2017-01-02")


def test_select_holdout_skips_null_tour_shows(memory_conn):
    conn = _seed_db(memory_conn)
    # A newest show with NULL tour must NOT be selected despite the latest date.
    conn.execute(
        "INSERT INTO shows (showid, showdate, venueid, tourid, tour_name, "
        "exclude, show_index) VALUES (7, '2018-01-01', 1, NULL, NULL, 0, 6)"
    )
    conn.execute(
        "INSERT INTO performances (showid, songid, set_label, position) "
        "VALUES (7, 1, '1', 1)"
    )
    conn.commit()

    sel = backtest.select_holdout(conn, holdout_tours=2)
    assert 7 not in sel.showids
    assert set(sel.tour_labels) == {"Tour B", "Tour C"}


def test_holdout_description_nonempty(memory_conn):
    conn = _seed_db(memory_conn)
    sel = backtest.select_holdout(conn, holdout_tours=2)
    desc = sel.description
    assert "Tour B" in desc and "Tour C" in desc
    assert "Holdout" in desc


# --------------------------------------------------------------------------- #
# Report render
# --------------------------------------------------------------------------- #
def _fake_metrics(brier: float) -> dict[str, float]:
    return {
        "n_rows": 100,
        "n_shows": 4,
        "brier": brier,
        "log_loss": 0.5,
        "hit20": 12.3,
        "hit25": 14.1,
    }


def test_report_renders_with_model_names():
    report = backtest.BacktestReport(
        half_lives=(25, 50),
        model_names=list(backtest.MODEL_NAMES),
        holdout_description="Holdout: 2 tours [Tour B, Tour C]",
    )
    for name in backtest.MODEL_NAMES:
        for h in (25, 50):
            report.results[(name, h)] = _fake_metrics(0.1)
            report.calibration[(name, h)] = backtest.calibration_table(
                np.array([1, 0, 1]), np.array([0.9, 0.2, 0.8])
            )

    text = report.render()
    assert text.strip()
    for name in backtest.MODEL_NAMES:
        assert name in text
    assert "Brier" in text and "Hit@20" in text and "Calibration" in text
    # __str__ delegates to render.
    assert str(report) == text
