"""End-to-end (no-network) tests for phishpred.ingest, backed by JSON fixtures
under tests/fixtures/ that mirror the real phish.net v5 payload shapes."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from phishpred.api import PhishNetClient
from phishpred.db import get_connection, init_db
from phishpred.ingest import IngestStats, compute_show_indexes, first_key, full_ingest, refresh

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


VENUES = _load("venues.json")
SONGS = _load("songs.json")
SHOWS_2025 = _load("shows_2025.json")
SHOWS_2099 = _load("shows_2099.json")
SETLISTS_2025 = _load("setlists_2025.json")
SETLISTS_2099_ERROR = _load("setlists_2099_error.json")
SETLISTS_SHOWDATE_2099 = _load("setlists_showdate_2099-08-15.json")


def _ok(body):
    return httpx.Response(200, json=body)


def make_handler():
    """Route requests to fixtures by URL path; record every call for
    cache/force assertions. Any unrecognised year returns a benign 'no data'
    error so a wide start_year..end_year sweep doesn't explode."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        routes = {
            "/v5/venues.json": VENUES,
            "/v5/songs.json": SONGS,
            "/v5/shows/showyear/2025.json": SHOWS_2025,
            "/v5/shows/showyear/2099.json": SHOWS_2099,
            "/v5/setlists/showyear/2025.json": SETLISTS_2025,
            "/v5/setlists/showyear/2099.json": SETLISTS_2099_ERROR,
            "/v5/setlists/showdate/2099-08-15.json": SETLISTS_SHOWDATE_2099,
        }
        if path in routes:
            return _ok(routes[path])
        return _ok({"error": 1, "error_message": "no data for that year", "data": []})

    return handler, calls


def make_client(tmp_path, handler):
    return PhishNetClient(
        api_key="testkey", cache_dir=tmp_path, throttle_seconds=0,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture()
def conn():
    c = get_connection(":memory:")
    init_db(c)
    return c


# -- full_ingest end-to-end -------------------------------------------------


def test_full_ingest_end_to_end(tmp_path, conn):
    handler, calls = make_handler()
    client = make_client(tmp_path, handler)

    stats = full_ingest(conn, client, start_year=2025, end_year=2099, force=False)
    assert isinstance(stats, IngestStats)

    # -- artist filtering: only Phish shows survive into `shows` -----------
    show_ids = {r["showid"] for r in conn.execute("SELECT showid FROM shows").fetchall()}
    assert show_ids == {1001, 1002, 1003, 1005, 2001}
    assert 1004 not in show_ids  # Trey Anastasio Band show, filtered out
    assert stats.non_phish_shows_skipped == 1
    assert stats.non_phish_performances_skipped == 1  # the 46 Days row tied to 1004

    assert stats.observed_artistid == 1
    meta_artistid = conn.execute("SELECT value FROM meta WHERE key='phish_artistid'").fetchone()
    assert meta_artistid["value"] == "1"

    # -- venue master data wins over show/setlist-row-derived fallback -----
    msg = conn.execute("SELECT * FROM venues WHERE venueid=10").fetchone()
    assert msg["name"] == "Madison Square Garden"  # from venues.json, not "...- MSG"
    assert msg["country"] == "USA"

    # venueid 30 (future show) and 40 (forced-exclude show) are NOT in
    # venues.json -> must be filled in from show-row fallback fields.
    future_venue = conn.execute("SELECT * FROM venues WHERE venueid=30").fetchone()
    assert future_venue["name"] == "Some Future Arena"
    side_stage = conn.execute("SELECT * FROM venues WHERE venueid=40").fetchone()
    assert side_stage["name"] == "Some Side Stage"

    # -- exclusion rules -----------------------------------------------------
    rows = {r["showid"]: r for r in conn.execute("SELECT * FROM shows").fetchall()}

    # 1001/1002: real multi-night run, both included.
    assert rows[1001]["exclude"] == 0
    assert rows[1002]["exclude"] == 0

    # 1003: past show whose only setlist row is itself row-excluded -> no
    # real performances -> excluded.
    assert rows[1003]["exclude"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM performances WHERE showid=1003").fetchone()["c"] == 0

    # 1005: exclude_from_stats=1 from the API, despite having a real,
    # non-excluded performance -> forced exclusion wins.
    assert rows[1005]["exclude"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM performances WHERE showid=1005").fetchone()["c"] == 1

    # 2001: future show, not excluded, but no show_index (no performances yet).
    assert rows[2001]["exclude"] == 0
    assert rows[2001]["show_index"] is None

    # -- show_index: dense 0..N over excluded=0, past, has-performances -----
    assert rows[1001]["show_index"] == 0
    assert rows[1002]["show_index"] == 1
    assert rows[1003]["show_index"] is None  # excluded
    assert rows[1005]["show_index"] is None  # excluded (forced)

    # -- performances / songs -------------------------------------------------
    assert stats.performances_inserted == 15 + 15 + 1  # night1 + night2 + 1005's row
    # 1003's row was excluded at the API level -> not counted as a real
    # performance, but still tallied for visibility.
    assert stats.excluded_performance_rows_skipped == 1

    total_songs = conn.execute("SELECT COUNT(*) c FROM songs").fetchone()["c"]
    assert total_songs == len(SONGS["data"])

    # is_original aggregated from setlist rows, NOT from the songs() artist
    # string (songs() has no is_original field at all per live discovery).
    wilson = conn.execute("SELECT is_original FROM songs WHERE songid=1").fetchone()
    assert wilson["is_original"] == 1
    loving_cup_id = next(s["songid"] for s in SONGS["data"] if s["slug"] == "loving-cup")
    loving_cup = conn.execute("SELECT is_original FROM songs WHERE songid=?", (loving_cup_id,)).fetchone()
    assert loving_cup["is_original"] == 0

    # -- caching: a second full_ingest over the same range makes zero new ---
    # -- network calls (force=False) -----------------------------------------
    calls_before = len(calls)
    stats2 = full_ingest(conn, client, start_year=2025, end_year=2099, force=False)
    assert len(calls) == calls_before  # nothing new hit the transport
    assert stats2.performances_inserted == stats.performances_inserted


def test_full_ingest_setlist_year_fallback_to_showdate(tmp_path, conn):
    handler, calls = make_handler()
    client = make_client(tmp_path, handler)

    stats = full_ingest(conn, client, start_year=2099, end_year=2099, force=False)
    assert stats.setlist_fallback_years == 1
    # confirms the per-showdate fallback path was actually exercised
    assert "/v5/setlists/showdate/2099-08-15.json" in calls
    # future show kept, no performances, no crash
    row = conn.execute("SELECT * FROM shows WHERE showid=2001").fetchone()
    assert row is not None
    assert row["show_index"] is None


def test_compute_show_indexes_is_idempotent_and_standalone(conn):
    # Build a tiny hand-made DB state directly (no network / ingest at all)
    # to test compute_show_indexes() in isolation, per its own contract.
    conn.execute("INSERT INTO venues (venueid, name) VALUES (1, 'V')")
    conn.executemany(
        "INSERT INTO shows (showid, showdate, venueid, exclude) VALUES (?, ?, 1, 0)",
        [(1, "2020-01-01"), (2, "2020-01-02"), (3, "2020-01-03")],
    )
    conn.execute("INSERT INTO songs (songid, slug, name) VALUES (1, 'wilson', 'Wilson')")
    # show 1 and 2 have performances, show 3 does not (and isn't marked
    # exclude=1 by hand -- compute_show_indexes must still skip it because
    # of the EXISTS(performances) guard, matching the contract wording).
    conn.executemany(
        "INSERT INTO performances (showid, songid, position) VALUES (?, 1, 1)",
        [(1,), (2,)],
    )
    conn.commit()

    compute_show_indexes(conn)
    rows = {r["showid"]: r["show_index"] for r in conn.execute("SELECT showid, show_index FROM shows").fetchall()}
    assert rows[1] == 0
    assert rows[2] == 1
    assert rows[3] is None

    # idempotent
    compute_show_indexes(conn)
    rows2 = {r["showid"]: r["show_index"] for r in conn.execute("SELECT showid, show_index FROM shows").fetchall()}
    assert rows2 == rows


# -- refresh -----------------------------------------------------------------


def test_refresh_repulls_current_year_and_years_since_last_refresh(tmp_path, conn):
    handler, calls = make_handler()
    client = make_client(tmp_path, handler)

    full_ingest(conn, client, start_year=2025, end_year=2025, force=False)
    assert conn.execute("SELECT COUNT(*) c FROM shows").fetchone()["c"] == 4  # 1001,1002,1003,1005

    # Simulate a stale last_refresh far in the past so `refresh` decides
    # year 2025 (which has shows with showdate > that date) needs re-pulling,
    # in addition to the always-included current year.
    conn.execute(
        "UPDATE meta SET value = '2000-01-01T00:00:00+00:00' WHERE key = 'last_refresh'"
    )
    conn.commit()

    calls_before = len(calls)
    stats = refresh(conn, client)

    # force=True during refresh must bypass the on-disk cache even though
    # shows/showyear/2025 + setlists/showyear/2025 were already cached above.
    assert "/v5/shows/showyear/2025.json" in calls[calls_before:]
    assert "/v5/setlists/showyear/2025.json" in calls[calls_before:]
    assert stats.years_processed >= 1

    # Idempotent: still exactly the same shows/performances as before.
    assert conn.execute("SELECT COUNT(*) c FROM shows").fetchone()["c"] == 4
    assert conn.execute("SELECT COUNT(*) c FROM performances").fetchone()["c"] == 15 + 15 + 1

    new_last_refresh = conn.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()["value"]
    assert new_last_refresh != "2000-01-01T00:00:00+00:00"


def test_refresh_on_fresh_db_falls_back_to_full_ingest(tmp_path, conn):
    handler, calls = make_handler()
    client = make_client(tmp_path, handler)

    # Brand-new DB: no meta.last_refresh, zero indexed shows. `refresh` must
    # detect this and run a full 1983..current-year backfill instead of the
    # incremental current-year-only path (the empty-R2-bucket first-run case).
    stats = refresh(conn, client)
    assert isinstance(stats, IngestStats)

    # The full sweep reached all the way back to 1983...
    assert "/v5/shows/showyear/1983.json" in calls
    # ...and picked up the 2025 fixture data, not just the current year.
    show_ids = {r["showid"] for r in conn.execute("SELECT showid FROM shows").fetchall()}
    assert {1001, 1002, 1003, 1005} <= show_ids
    rows = {r["showid"]: r for r in conn.execute("SELECT * FROM shows").fetchall()}
    assert rows[1001]["show_index"] == 0
    assert rows[1002]["show_index"] == 1

    last_refresh = conn.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
    assert last_refresh is not None and last_refresh["value"]

    # A second refresh takes the incremental path: no re-sweep back to 1983.
    sweep_calls_before = calls.count("/v5/shows/showyear/1983.json")
    refresh(conn, client)
    assert calls.count("/v5/shows/showyear/1983.json") == sweep_calls_before


def test_refresh_persists_forced_exclusion_across_untouched_years(tmp_path, conn):
    handler, _calls = make_handler()
    client = make_client(tmp_path, handler)

    full_ingest(conn, client, start_year=2025, end_year=2025, force=False)
    assert conn.execute("SELECT exclude FROM shows WHERE showid=1005").fetchone()["exclude"] == 1

    # A refresh that doesn't re-touch 2025 at all (no shows newer than
    # last_refresh, current year is some other year with no fixture data)
    # must not lose the previously-observed forced exclusion for show 1005.
    refresh(conn, client)
    assert conn.execute("SELECT exclude FROM shows WHERE showid=1005").fetchone()["exclude"] == 1


# -- first_key helper (used for tour_name/tourname, tourid/tour_id, etc.) ----


def test_first_key_prefers_first_present_non_empty_value():
    assert first_key({"tourname": "Foo"}, "tour_name", "tourname") == "Foo"
    assert first_key({"tour_name": "Bar", "tourname": "Foo"}, "tour_name", "tourname") == "Bar"
    assert first_key({"tour_name": None}, "tour_name", "tourname") is None
    assert first_key({"tour_name": ""}, "tour_name", "tourname") is None
    assert first_key({}, "tour_name", "tourname") is None


# -- missing songid: fail loudly ---------------------------------------------


def test_missing_songid_raises(tmp_path, conn):
    bad_row = {
        "showid": 1001, "showdate": "2025-06-20", "songid": None,
        "song": "Mystery Song", "slug": "mystery-song", "set": "1", "position": 99,
        "artist_name": "Phish", "artistid": 1,
    }

    def handler(request):
        path = request.url.path
        if path == "/v5/venues.json":
            return _ok(VENUES)
        if path == "/v5/songs.json":
            return _ok(SONGS)
        if path == "/v5/shows/showyear/2025.json":
            return _ok(SHOWS_2025)
        if path == "/v5/setlists/showyear/2025.json":
            return _ok({"error": False, "error_message": "", "data": [bad_row]})
        return _ok({"error": 1, "error_message": "no data", "data": []})

    client = make_client(tmp_path, handler)
    with pytest.raises(ValueError, match="songid"):
        full_ingest(conn, client, start_year=2025, end_year=2025, force=False)
