"""Epoch identity + gating (DEPLOY-CONTRACTS.md §1, deploy plan §6)."""
from __future__ import annotations

import json

import pytest

from phishpred import db
from phishpred.epoch import compute_epoch, epoch_status, read_latest


def _make_conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO venues (venueid, name, city) VALUES (10,'V10','C10')")
    c.execute("INSERT INTO venues (venueid, name, city) VALUES (11,'V11','C11')")
    # one played show (indexed) + two future shows (schedule)
    c.execute("INSERT INTO shows (showid, showdate, venueid, show_index, exclude) VALUES (1,'2026-06-01',10,0,0)")
    c.execute("INSERT INTO shows (showid, showdate, venueid, show_index, exclude) VALUES (2,'2026-07-10',11,NULL,0)")
    c.execute("INSERT INTO shows (showid, showdate, venueid, show_index, exclude) VALUES (3,'2026-07-11',11,NULL,0)")
    c.commit()
    return c


def test_epoch_is_deterministic():
    c = _make_conn()
    e1, comp1 = compute_epoch(c)
    e2, comp2 = compute_epoch(c)
    assert e1 == e2
    assert comp1 == comp2
    assert len(e1) == 12


@pytest.mark.parametrize("kwargs", [
    {"model": "lr"}, {"n_sims": 1000}, {"seed": 7}, {"half_life": 25},
])
def test_publishing_params_change_epoch(kwargs):
    c = _make_conn()
    base, _ = compute_epoch(c)
    other, _ = compute_epoch(c, **kwargs)
    assert base != other


def test_schedule_change_changes_epoch():
    c = _make_conn()
    base, _ = compute_epoch(c)
    c.execute("INSERT INTO shows (showid, showdate, venueid, show_index, exclude) VALUES (4,'2026-07-12',11,NULL,0)")
    c.commit()
    changed, _ = compute_epoch(c)
    assert base != changed


def test_playing_a_show_changes_epoch():
    c = _make_conn()
    base, _ = compute_epoch(c)
    # show 2 gets played -> gets a show_index (max_played_show_index advances)
    c.execute("UPDATE shows SET show_index = 1 WHERE showid = 2")
    c.commit()
    changed, _ = compute_epoch(c)
    assert base != changed


def test_compare_models_changes_epoch():
    c = _make_conn()
    base, _ = compute_epoch(c)
    other, _ = compute_epoch(c, compare_models=["lr"])
    assert base != other
    # order-insensitive: compare_models is sorted into the identity
    a, _ = compute_epoch(c, compare_models=["lr", "gbm"])
    b, _ = compute_epoch(c, compare_models=["gbm", "lr"])
    assert a == b
    # empty list is the default (no compare columns)
    assert compute_epoch(c, compare_models=[])[0] == base


def test_schedule_hash_tolerates_none_venueid_and_duplicate_dates():
    c = _make_conn()
    # two future shows on the SAME date, one with an unassigned (NULL) venue:
    # the schedule hash must sort without crashing on None vs int.
    c.execute("INSERT INTO shows (showid, showdate, venueid, show_index, exclude) VALUES (5,'2026-07-20',NULL,NULL,0)")
    c.execute("INSERT INTO shows (showid, showdate, venueid, show_index, exclude) VALUES (6,'2026-07-20',11,NULL,0)")
    c.commit()
    e1, _ = compute_epoch(c)
    e2, _ = compute_epoch(c)
    assert e1 == e2 and len(e1) == 12


def test_submission_changes_epoch(tmp_path):
    c = _make_conn()
    base, _ = compute_epoch(c, submitted_dir=tmp_path)  # empty inbox
    sub = tmp_path / "claude-desktop"
    sub.mkdir()
    (sub / "2026-07-10.json").write_text(json.dumps({"predictions": [{"slug": "x", "prob": 0.5}]}))
    changed, _ = compute_epoch(c, submitted_dir=tmp_path)
    assert base != changed


def test_epoch_status_and_pointer(tmp_path):
    c = _make_conn()
    pointer = tmp_path / "latest.json"
    assert read_latest(pointer) is None

    status = epoch_status(c, pointer_path=pointer)
    assert status["changed"] is True  # nothing published yet

    pointer.write_text(json.dumps({"epoch": status["epoch"], "created_at": "now"}))
    assert read_latest(pointer) == status["epoch"]
    status2 = epoch_status(c, pointer_path=pointer)
    assert status2["changed"] is False  # matches the published pointer
