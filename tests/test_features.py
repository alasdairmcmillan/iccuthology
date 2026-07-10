"""Unit tests for phishpred.features.

A small hand-crafted history is used to assert exact, hand-computed feature
values, plus a fast-vs-reference property test for decayed_rate over a random
synthetic history.
"""
from __future__ import annotations

import random

import pytest

from phishpred import db
from phishpred.features import (
    FEATURE_COLUMNS,
    RECENT_RATE_WINDOW,
    build_features,
    features_for_future_show,
    mean_setlist_size,
)

# --------------------------------------------------------------------------
# Hand-crafted history
# --------------------------------------------------------------------------
# venueid, name, alias (0 = self, else canonical id). 20 -> 21 (renamed venue).
VENUES = [
    (1, "Alpha", 0),
    (2, "Beta", 0),
    (3, "Gamma", 0),
    (20, "Old Gamma Name", 21),
    (21, "New Gamma Name", 0),
]

# songid, slug, name, is_original (None -> 0.5 feature)
SONGS = [
    (101, "tweezer", "Tweezer", 1),
    (102, "yem", "YEM", 1),
    (103, "wilson", "Wilson", 1),
    (104, "gin", "Bathtub Gin", 1),
    (105, "cover", "Cover", 0),
    (106, "rare", "Rare", 1),
    (107, "icculus", "Icculus", None),
]

# showid, show_index, showdate, venueid, tourid
SHOWS = [
    (1, 0, "2010-06-01", 1, 100),
    (2, 1, "2010-06-02", 1, 100),   # consecutive @ Alpha -> run with show 1
    (3, 2, "2010-06-10", 2, 100),
    (4, 3, "2010-06-11", 2, 100),   # run @ Beta
    (5, 4, "2010-06-20", 1, 100),   # repeat visit to Alpha
    (6, 5, "2010-07-01", 3, 101),
    (7, 6, "2010-07-02", 20, 101),  # venue 20 aliases to 21
    (8, 7, "2010-07-03", 21, 101),  # consecutive @ canonical 21 -> run with show 7
    (9, 8, "2010-07-15", 2, 101),
    (10, 9, "2010-07-16", 2, 101),  # run @ Beta
]

SETLISTS = {
    1: [101, 102, 103, 107],
    2: [102, 104, 105, 107],
    3: [101, 103, 106],
    4: [102, 105],
    5: [101, 104],
    6: [101, 102, 103, 104],
    7: [101, 105],
    8: [102, 103],
    9: [101, 104, 106, 107],
    10: [102, 103],
}

# future show: immediately after show 10 (idx 9) at the same venue (Beta) -> run
FUTURE = (11, None, "2010-07-17", 2, 101)


def _populate(conn, shows, setlists, future=None):
    for vid, name, alias in VENUES:
        conn.execute(
            "INSERT INTO venues (venueid, name, alias) VALUES (?,?,?)", (vid, name, alias)
        )
    for sid, slug, name, iso in SONGS:
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,?)",
            (sid, slug, name, iso),
        )
    for showid, idx, date, vid, tour in shows:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
            "VALUES (?,?,?,?,?,0)",
            (showid, date, vid, tour, idx),
        )
    if future is not None:
        showid, idx, date, vid, tour = future
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
            "VALUES (?,?,?,?,?,0)",
            (showid, date, vid, tour, idx),
        )
    for showid, songs in setlists.items():
        for pos, songid in enumerate(songs):
            conn.execute(
                "INSERT INTO performances (showid, songid, set_label, position) "
                "VALUES (?,?,?,?)",
                (showid, songid, "1", pos),
            )
    conn.commit()


@pytest.fixture()
def conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    _populate(c, SHOWS, SETLISTS, FUTURE)
    yield c
    c.close()


def _row(df, show_index, songid):
    sub = df[(df["show_index"] == show_index) & (df["songid"] == songid)]
    assert len(sub) == 1, f"expected 1 row for (show {show_index}, song {songid}), got {len(sub)}"
    return sub.iloc[0]


def _ref_decayed(play_idxs, all_idxs, t, half_life):
    r = 0.5 ** (1.0 / half_life)
    num = sum(r ** (t - i) for i in play_idxs if i < t)
    den = sum(r ** (t - j) for j in all_idxs if j < t)
    return num / den if den > 0 else 0.0


# --------------------------------------------------------------------------
# gap / played_prev_show / played_in_run
# --------------------------------------------------------------------------

def test_gap_prev_run_within_run(conn):
    df = build_features(conn, half_life=50)
    # YEM at show idx1 (night 2 of Alpha run); played night 1 (idx0).
    row = _row(df, 1, 102)
    assert row["gap"] == 1
    assert row["played_prev_show"] == 1
    assert row["played_in_run"] == 1


def test_gap_prev_run_across_venue_change(conn):
    df = build_features(conn, half_life=50)
    # Tweezer at idx2 (Beta) — previous show was Alpha, so not the same run.
    row = _row(df, 2, 101)
    assert row["gap"] == 2          # last played idx0
    assert row["played_prev_show"] == 0
    assert row["played_in_run"] == 0


def test_run_detected_across_venue_alias(conn):
    df = build_features(conn, half_life=50)
    # Tweezer at idx7 (venue 21). Played at idx6 (venue 20, which aliases to 21).
    row = _row(df, 7, 101)
    assert row["gap"] == 1
    assert row["played_prev_show"] == 1
    assert row["played_in_run"] == 1


# --------------------------------------------------------------------------
# venue_gap (incl. alias equivalence + sentinel)
# --------------------------------------------------------------------------

def test_venue_gap_alias_same_venue(conn):
    df = build_features(conn, half_life=50)
    # Tweezer at idx7 venue 21; last played at venue 20 (its alias) at idx6,
    # which was the only prior show at canonical venue 21 -> 0 shows since.
    row = _row(df, 7, 101)
    assert row["venue_gap"] == 0


def test_venue_gap_counts_intervening_venue_shows(conn):
    df = build_features(conn, half_life=50)
    # Alpha shows before idx4: idx0, idx1 (n_v = 2).
    # Tweezer last played at Alpha at idx0 (1st Alpha show) -> gap = 2 - 1 = 1.
    assert _row(df, 4, 101)["venue_gap"] == 1
    # Bathtub Gin last played at Alpha at idx1 (2nd Alpha show) -> gap = 2 - 2 = 0.
    assert _row(df, 4, 104)["venue_gap"] == 0


def test_venue_gap_sentinel_when_never_at_venue(conn):
    df = build_features(conn, half_life=50)
    # YEM at idx2 (Beta); YEM never played at Beta before -> sentinel 999.
    assert _row(df, 2, 102)["venue_gap"] == 999


# --------------------------------------------------------------------------
# decayed_rate (against explicit slow reference)
# --------------------------------------------------------------------------

def test_decayed_rate_matches_reference(conn):
    H = 50
    df = build_features(conn, half_life=H)
    # YEM at idx5: plays at 0,1,3; all shows before = 0..4.
    row = _row(df, 5, 102)
    expected = _ref_decayed([0, 1, 3], [0, 1, 2, 3, 4], 5, H)
    assert row["decayed_rate"] == pytest.approx(expected, rel=1e-9)


# --------------------------------------------------------------------------
# gap_ratio (long-gap "due" song) + is_original NULL -> 0.5
# --------------------------------------------------------------------------

def test_gap_ratio_and_is_original_null(conn):
    df = build_features(conn, half_life=50)
    # Icculus at idx8: prior plays 0,1 -> one historical gap [1], median 1.
    # gap at idx8 = 8 - 1 = 7 -> gap_ratio = 7.0.
    row = _row(df, 8, 107)
    assert row["gap"] == 7
    assert row["gap_ratio"] == pytest.approx(7.0)
    assert row["is_original"] == pytest.approx(0.5)  # is_original NULL
    assert row["y"] == 1  # Icculus is in show idx8's setlist


def test_gap_ratio_defaults_to_one_with_few_plays(conn):
    df = build_features(conn, half_life=50)
    # Rare at idx3: only one prior play (idx2), no historical gap -> 1.0.
    row = _row(df, 3, 106)
    assert row["gap_ratio"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# era_rate, plays_last_10, plays_this_tour
# --------------------------------------------------------------------------

def test_era_rate(conn):
    df = build_features(conn, half_life=50)
    # All shows are era 3.0. YEM at idx5: 3 prior plays / 5 prior shows in era.
    assert _row(df, 5, 102)["era_rate"] == pytest.approx(3 / 5)


def test_plays_last_10(conn):
    df = build_features(conn, half_life=50)
    # Tweezer prior plays before idx8: 0,2,4,5,6 -> all within last 10 -> 5.
    assert _row(df, 8, 101)["plays_last_10"] == 5


def test_plays_this_tour(conn):
    df = build_features(conn, half_life=50)
    # Tour 101 begins at idx5. Tweezer plays in tour 101 before idx8: idx5, idx6.
    assert _row(df, 8, 101)["plays_this_tour"] == 2


# --------------------------------------------------------------------------
# plays_last_150
# --------------------------------------------------------------------------

def test_plays_last_150_in_feature_columns():
    assert "plays_last_150" in FEATURE_COLUMNS
    assert RECENT_RATE_WINDOW == 150


def test_plays_last_150_short_history_equals_total_prior_plays(conn):
    # All 10 shows fit in the 150-window, so the count == total prior plays.
    # Tweezer prior plays before idx8: 0,2,4,5,6 -> 5.
    row = _row(build_features(conn, half_life=50), 8, 101)
    assert row["plays_last_150"] == 5
    assert row["plays_last_150"] == row["plays_last_50"]


def test_plays_last_150_window_counts_constructed_sweep():
    """>150-show history with a song played at known indexes: the 150-show
    window must count exactly the plays with index >= t - 150 (and < t)."""
    target_plays = [0, 10, 40, 60, 100, 155, 180]
    n_shows = 200

    c = db.get_connection(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO venues (venueid, name, alias) VALUES (1,'A',0)")
    c.execute("INSERT INTO songs (songid, slug, name, is_original) VALUES (900,'rare','Rare',1)")
    c.execute("INSERT INTO songs (songid, slug, name, is_original) VALUES (901,'staple','Staple',1)")
    for idx in range(n_shows):
        showid = 5000 + idx
        date = f"2015-{1 + idx // 28:02d}-{1 + idx % 28:02d}"
        c.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
            "VALUES (?,?,1,700,?,0)",
            (showid, date, idx),
        )
        picks = [901] + ([900] if idx in target_plays else [])
        for pos, sid in enumerate(picks):
            c.execute(
                "INSERT INTO performances (showid, songid, set_label, position) "
                "VALUES (?,?,?,?)",
                (showid, sid, "1", pos),
            )
    c.commit()
    df = build_features(c, half_life=50)
    c.close()

    def expected(t):
        return sum(1 for i in target_plays if t - 150 <= i < t)

    # idx 100: window [-50, 100) -> plays 0,10,40,60 -> 4.
    assert _row(df, 100, 900)["plays_last_150"] == expected(100) == 4
    # idx 190: window [40, 190) -> plays 40,60,100,155,180 -> 5 (0 and 10 aged out).
    assert _row(df, 190, 900)["plays_last_150"] == expected(190) == 5
    # Staple played at every prior show: exactly 150 in-window at idx 190.
    assert _row(df, 190, 901)["plays_last_150"] == 150


# --------------------------------------------------------------------------
# candidate-set membership: no debut rows
# --------------------------------------------------------------------------

def test_no_row_for_song_at_its_debut(conn):
    df = build_features(conn, half_life=50)
    # Rare debuts at idx2 -> no candidate row there (candidates need >=1 prior play).
    assert len(df[(df["show_index"] == 2) & (df["songid"] == 106)]) == 0
    # It becomes a candidate at idx3 (y=0, not played there).
    assert _row(df, 3, 106)["y"] == 0
    # Every emitted candidate has a real prior gap.
    assert (df["gap"] >= 1).all()


# --------------------------------------------------------------------------
# no leakage
# --------------------------------------------------------------------------

def test_no_leakage_future_shows_do_not_affect_past_rows(conn):
    full = build_features(conn, half_life=50)

    # A DB containing only shows up to idx5 (and their setlists), nothing later.
    partial_conn = db.get_connection(":memory:")
    db.init_db(partial_conn)
    early_shows = [s for s in SHOWS if s[1] <= 5]
    early_setlists = {sid: songs for sid, songs in SETLISTS.items() if sid <= 6}
    _populate(partial_conn, early_shows, early_setlists, future=None)
    partial = build_features(partial_conn, half_life=50)
    partial_conn.close()

    a = full[full["show_index"] == 5].sort_values("songid").reset_index(drop=True)
    b = partial[partial["show_index"] == 5].sort_values("songid").reset_index(drop=True)
    # Same candidates, same feature values — later shows are invisible to idx5.
    from pandas.testing import assert_frame_equal
    assert_frame_equal(a, b)


# --------------------------------------------------------------------------
# features_for_future_show run context
# --------------------------------------------------------------------------

def test_future_show_run_context(conn):
    df = features_for_future_show(conn, showid=11, half_life=50)
    # y is NaN for all rows.
    assert df["y"].isna().all()
    # effective index is 10 (max idx 9 + 1 + rank 0).
    assert (df["show_index"] == 10).all()

    # YEM last played idx9 (show 10) -> gap 1, prev-show fires, in-run fires.
    yem = _row(df, 10, 102)
    assert yem["gap"] == 1
    assert yem["played_prev_show"] == 1
    assert yem["played_in_run"] == 1

    # Tweezer last played idx8 (show 9) -> gap 2, not prev-show, but in-run
    # (idx8/idx9 are the Beta run immediately preceding the future night).
    tw = _row(df, 10, 101)
    assert tw["gap"] == 2
    assert tw["played_prev_show"] == 0
    assert tw["played_in_run"] == 1
    assert tw["plays_this_tour"] == 3  # tour 101: idx5, idx6, idx8

    # Cover last played idx6 (before the Beta run) -> not in this run.
    cover = _row(df, 10, 105)
    assert cover["played_in_run"] == 0


def test_future_show_venue_gap_uses_canonical_venue(conn):
    df = features_for_future_show(conn, showid=11, half_life=50)
    # Beta shows so far: idx2, idx3, idx8, idx9 (n_v = 4).
    # Bathtub last played at Beta at idx8 (3rd Beta show) -> gap = 4 - 3 = 1.
    assert _row(df, 10, 104)["venue_gap"] == 1


# --------------------------------------------------------------------------
# mean_setlist_size
# --------------------------------------------------------------------------

def test_mean_setlist_size(conn):
    # distinct songs per show: 4,4,3,2,2,4,2,2,4,2 -> mean 2.9
    assert mean_setlist_size(conn) == pytest.approx(2.9)
    assert mean_setlist_size(conn, era="3.0") == pytest.approx(2.9)
    assert mean_setlist_size(conn, era="1.0") == 0.0


# --------------------------------------------------------------------------
# fast-vs-reference decayed_rate over a random synthetic history
# --------------------------------------------------------------------------

def test_decayed_rate_fast_matches_reference_random():
    rng = random.Random(1234)
    n_shows = 60
    song_ids = list(range(200, 215))  # 15 songs
    venue_ids = [1, 2, 20, 21]        # includes an aliased pair

    c = db.get_connection(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO venues (venueid, name, alias) VALUES (1,'A',0)")
    c.execute("INSERT INTO venues (venueid, name, alias) VALUES (2,'B',0)")
    c.execute("INSERT INTO venues (venueid, name, alias) VALUES (20,'C-old',21)")
    c.execute("INSERT INTO venues (venueid, name, alias) VALUES (21,'C',0)")
    for sid in song_ids:
        c.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,1)",
            (sid, f"s{sid}", f"Song {sid}"),
        )

    setlists: dict[int, list[int]] = {}
    for idx in range(n_shows):
        showid = 1000 + idx
        date = f"2015-{1 + idx // 28:02d}-{1 + idx % 28:02d}"
        venue = rng.choice(venue_ids)
        tour = 500 + idx // 10
        c.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
            "VALUES (?,?,?,?,?,0)",
            (showid, date, venue, tour, idx),
        )
        k = rng.randint(5, 12)
        picks = rng.sample(song_ids, k)
        setlists[idx] = picks
        for pos, sid in enumerate(picks):
            c.execute(
                "INSERT INTO performances (showid, songid, set_label, position) "
                "VALUES (?,?,?,?)",
                (showid, sid, "1", pos),
            )
    c.commit()

    H = 37
    df = build_features(c, half_life=H)
    c.close()

    # Ground-truth play indexes per song and the full index universe.
    plays_by_song: dict[int, list[int]] = {s: [] for s in song_ids}
    for idx in range(n_shows):
        for s in setlists[idx]:
            plays_by_song[s].append(idx)
    all_idxs = list(range(n_shows))

    assert len(df) > 500  # sanity: a substantial candidate set was produced

    sample = df.sample(n=min(300, len(df)), random_state=7)
    for _, row in sample.iterrows():
        t = int(row["show_index"])
        s = int(row["songid"])
        expected = _ref_decayed(plays_by_song[s], all_idxs, t, H)
        assert row["decayed_rate"] == pytest.approx(expected, rel=1e-9, abs=1e-12)
