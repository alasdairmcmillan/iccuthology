"""Personalized 'due to see' feature (phish.net seedfile complement)."""
from __future__ import annotations

import json

import pytest

from phishpred import db, features
from phishpred.personal import (
    PersonalReport,
    parse_seedfile,
    seen_songids,
    unlikely_unseen,
)
from phishpred.simulate import SimConfig

# Reuse the compact fixture shape from test_publish.
VENUES = [(1, "Alpha", "AlphaCity", 0), (2, "Beta", "BetaCity", 0)]
SONGS = [
    (101, "tweezer", "Tweezer", 1), (102, "yem", "YEM", 1), (103, "wilson", "Wilson", 1),
    (104, "gin", "Bathtub Gin", 1), (105, "filler", "Filler", 1),
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
        conn.execute("INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) VALUES (?,?,?,?,?,0)",
                     (showid, showdate, vid, tour, idx))
    for showid, showdate, vid, tourid, tour_name in FUTURE:
        conn.execute("INSERT INTO shows (showid, showdate, venueid, tourid, tour_name, show_index, exclude) "
                     "VALUES (?,?,?,?,?,NULL,0)", (showid, showdate, vid, tourid, tour_name))
    for showid, songs in HIST_SETLISTS.items():
        for pos, songid in enumerate(songs):
            conn.execute("INSERT INTO performances (showid, songid, set_label, position) VALUES (?,?,?,?)",
                         (showid, songid, "1", pos))
    conn.commit()


@pytest.fixture()
def conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    _populate(c)
    yield c
    c.close()


def test_parse_seedfile_dates_and_years():
    text = "firsttime\nFirst Year\n8/13/09\n6/18/19\n8/10/22\n12/31/1999\n"
    assert parse_seedfile(text) == ["1999-12-31", "2009-08-13", "2019-06-18", "2022-08-10"]


def test_parse_seedfile_ignores_non_date_noise():
    # a cdn href / query string must not be mistaken for a date
    text = '<a href="https://phish.net/cdn-cgi/content?id=abc-123.456-1.2.1.1"></a>\n7/20/25\n'
    assert parse_seedfile(text) == ["2025-07-20"]


def test_seen_songids(conn):
    # attended shows 1 and 8: songs {101,103,104,102} U {101,105,102}
    seen = seen_songids(conn, ["2022-06-01", "2022-07-02"])
    assert seen == {101, 102, 103, 104, 105}
    assert seen_songids(conn, []) == set()


def test_unlikely_unseen_excludes_seen_and_ranks_by_plays(conn):
    horizon = features.future_show_ids(conn)
    cfg = SimConfig(n_sims=40, seed=0, model="heuristic")
    # user attended show 3 only: saw {101 tweezer, 103 wilson}
    report = unlikely_unseen(conn, ["2022-06-03"], horizon, cfg, top=10, min_plays=1)

    assert isinstance(report, PersonalReport)
    assert report.n_attended == 1
    assert report.n_seen_songs == 2
    seen_slugs = {r.slug for r in report.rows}
    assert "tweezer" not in seen_slugs and "wilson" not in seen_slugs  # excluded (seen)
    # ranked by historical play count desc
    plays = [r.times_played for r in report.rows]
    assert plays == sorted(plays, reverse=True)
    # forward columns are well-formed
    for r in report.rows:
        assert 0.0 <= r.p_see_in_horizon <= 1.0
        if r.modal_next_show is not None:
            assert r.modal_next_show in report.horizon_showdates


def test_unlikely_unseen_json_render(conn):
    horizon = features.future_show_ids(conn)
    cfg = SimConfig(n_sims=30, seed=0)
    report = unlikely_unseen(conn, ["2022-06-03"], horizon, cfg, top=5, min_plays=1)
    payload = json.loads(report.render(json_out=True))
    assert payload["n_attended"] == 1
    assert "rows" in payload and isinstance(payload["rows"], list)
