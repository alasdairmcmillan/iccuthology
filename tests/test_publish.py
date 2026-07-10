"""`phishpred publish` artifacts (DEPLOY-CONTRACTS.md §2/§3/§5).

Uses a small in-memory DB (same shape as tests/test_modes.py) with 2022 history
(era 4.0, matching the 2026 future shows so mean_setlist_size is non-zero) and a
handful of 2026 future shows. Small n_sims for speed/determinism.
"""
from __future__ import annotations

import json
import zlib

import pytest

from phishpred import db
from phishpred.config import era_for_year
from phishpred.publish import publish, tour_id_for
from phishpred.samples_codec import decode_samples
from phishpred.simulate import SimConfig, simulate_horizon
from phishpred import features

VENUES = [(1, "Alpha", "AlphaCity", 0), (2, "Beta", "BetaCity", 0)]
SONGS = [
    (101, "tweezer", "Tweezer", 1),
    (102, "yem", "YEM", 1),
    (103, "wilson", "Wilson", 1),
    (104, "gin", "Bathtub Gin", 1),
    (105, "filler", "Filler", 1),
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
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) VALUES (?,?,?,?,?,0)",
            (showid, showdate, vid, tour, idx),
        )
    for showid, showdate, vid, tourid, tour_name in FUTURE:
        conn.execute(
            "INSERT INTO shows (showid, showdate, venueid, tourid, tour_name, show_index, exclude) "
            "VALUES (?,?,?,?,?,NULL,0)",
            (showid, showdate, vid, tourid, tour_name),
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


N_SIMS = 60
SEED = 0


def test_publish_writes_full_tree(conn, tmp_path):
    meta = publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED, with_samples=True,
                   created_at="2026-07-09T00:00:00Z")

    for name in ("meta.json", "tour.json", "schedule.json", "samples.bin", "samples_meta.json"):
        assert (tmp_path / name).exists(), name
    assert sorted(p.stem for p in (tmp_path / "show").glob("*.json")) == \
        ["2026-07-10", "2026-07-11", "2026-07-18"]
    assert sorted(p.stem for p in (tmp_path / "setlist").glob("*.json")) == \
        ["2026-07-10", "2026-07-11", "2026-07-18"]

    assert meta["epoch"] and len(meta["epoch"]) == 12
    assert meta["created_at"] == "2026-07-09T00:00:00Z"
    assert meta["headline_model"] == "heuristic"
    assert meta["horizon_showdates"] == ["2026-07-10", "2026-07-11", "2026-07-18"]
    assert meta["tours"] == [{"id": "summer-2026", "tour_name": "2026 Summer Tour", "has_data": True}]


def test_tour_json_shape(conn, tmp_path):
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED)
    tour = json.loads((tmp_path / "tour.json").read_text())
    assert tour["rows"], "expected tour rows"
    # sorted by expected_plays desc
    exp = [r["expected_plays"] for r in tour["rows"]]
    assert exp == sorted(exp, reverse=True)
    row = tour["rows"][0]
    assert set(row) >= {"song", "slug", "expected_plays", "p_at_least_one", "dist", "bucket", "analytic_p"}
    assert set(row["dist"]) == {"0", "1", "2", "3", "4+"}
    assert row["bucket"] in {"lock", "likely", "bustout-watch", "longshot"}


def test_show_and_setlist_shape(conn, tmp_path):
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED)
    show = json.loads((tmp_path / "show" / "2026-07-10.json").read_text())
    assert show["venue_name"] == "Alpha"
    assert "heuristic" in show["sources"]
    src = show["sources"]["heuristic"]
    assert src["kind"] == "statistical"
    probs = [r["prob"] for r in src["rows"]]
    assert probs == sorted(probs, reverse=True)
    assert all(0.0 <= p <= 1.0 for p in probs)

    setlist = json.loads((tmp_path / "setlist" / "2026-07-10.json").read_text())
    assert setlist["model"] == "sampler"
    assert setlist["sets"], "expected at least one set"


# ---------------------------------------------------------------------------
# Setlist run-context threading (no repeats within a run, per-show seeds)
# ---------------------------------------------------------------------------
def _setlist_doc(out, showdate):
    doc = json.loads((out / "setlist" / f"{showdate}.json").read_text())
    songids = [s["songid"] for songs in doc["sets"].values() for s in songs]
    return doc, songids


def test_publish_same_venue_run_setlists_never_overlap(conn, tmp_path):
    # 2026-07-10 and 2026-07-11 are consecutive nights at Alpha (venue 1): the
    # night-2 sampler must exclude every song predicted for night 1.
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED)
    _d1, ids1 = _setlist_doc(tmp_path, "2026-07-10")
    _d2, ids2 = _setlist_doc(tmp_path, "2026-07-11")
    assert ids1, "night 1 should predict a non-empty setlist"
    assert not set(ids1) & set(ids2)


def test_publish_setlist_seed_is_crc32_derived_per_show(conn, tmp_path):
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED)
    seeds = {}
    for showdate in ("2026-07-10", "2026-07-11", "2026-07-18"):
        doc, _ids = _setlist_doc(tmp_path, showdate)
        assert doc["seed"] == zlib.crc32(f"{SEED}:{showdate}".encode())
        seeds[showdate] = doc["seed"]
    # decorrelation: every show gets its own seed, none reuse the global seed
    assert len(set(seeds.values())) == 3
    assert SEED not in seeds.values()


def test_publish_different_venue_setlists_decorrelated(conn, tmp_path):
    # 2026-07-11 (Alpha) -> 2026-07-18 (Beta): different venues, so night 3 is
    # a fresh run. It must not be a carbon copy of night 2 (per-show seeds),
    # and night-2 songs are discouraged (x0.02) rather than excluded.
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED)
    _d2, ids2 = _setlist_doc(tmp_path, "2026-07-11")
    d3, ids3 = _setlist_doc(tmp_path, "2026-07-18")
    assert ids3, "fresh-venue night should predict a non-empty setlist"
    assert ids2 != ids3
    # any night-2 song that does sneak into night 3 carries the discouraged
    # (0.02-scaled) probability, keeping it heavily underrepresented
    for songs in d3["sets"].values():
        for s in songs:
            if s["songid"] in set(ids2):
                assert s["prob"] < 0.05


def test_publish_twice_yields_byte_identical_setlists(conn, tmp_path):
    out1, out2 = tmp_path / "a", tmp_path / "b"
    publish(conn, out1, n_sims=N_SIMS, seed=SEED, created_at="2026-07-09T00:00:00Z")
    publish(conn, out2, n_sims=N_SIMS, seed=SEED, created_at="2026-07-09T00:00:00Z")
    docs1 = sorted((out1 / "setlist").glob("*.json"))
    assert docs1, "expected setlist docs"
    for p in docs1:
        assert p.read_bytes() == (out2 / "setlist" / p.name).read_bytes()


def test_samples_bin_matches_resimulation(conn, tmp_path):
    """The published samples.bin must decode back to the exact SimResult.samples
    a fresh simulate_horizon(seed) produces (proves the writer preserves the
    joint samples the Worker/browser will reduce)."""
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED, with_samples=True)

    dec = decode_samples((tmp_path / "samples.bin").read_bytes())
    smeta = json.loads((tmp_path / "samples_meta.json").read_text())
    idx_to_songid = {v["i"]: v["songid"] for v in smeta["vocab"]}

    horizon = features.future_show_ids(conn)
    cfg = SimConfig(n_sims=N_SIMS, seed=SEED, model="heuristic")
    result = simulate_horizon(conn, horizon, cfg)

    assert dec["n_sims"] == N_SIMS
    assert dec["n_shows"] == len(horizon)
    assert smeta["horizon_showdates"] == result.horizon_dates

    for m in range(N_SIMS):
        for t in range(len(horizon)):
            reconstructed = {idx_to_songid[i] for i in dec["samples"][m][t]}
            assert reconstructed == result.samples[m][t]


def test_run_union_reduction_matches_run_mode(conn, tmp_path):
    """A §4 union reduction over the published samples must equal modes.run_mode
    over the same full horizon (identical samples => identical joint numbers)."""
    from phishpred.modes import run_mode

    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED, with_samples=True)
    dec = decode_samples((tmp_path / "samples.bin").read_bytes())
    smeta = json.loads((tmp_path / "samples_meta.json").read_text())
    idx_to_slug = {v["i"]: v["slug"] for v in smeta["vocab"]}

    horizon = features.future_show_ids(conn)
    cfg = SimConfig(n_sims=N_SIMS, seed=SEED, model="heuristic")
    report = run_mode(conn, horizon, cfg)  # same seed/horizon => same samples

    # §4 union reduction over ALL nights, keyed by slug
    n = dec["n_sims"]
    union = {}
    for sim in dec["samples"]:
        seen = set()
        for step in sim:
            seen.update(step)
        for vi in seen:
            union[vi] = union.get(vi, 0) + 1
    reduced = {idx_to_slug[vi]: c / n for vi, c in union.items()}

    for r in report.rows:
        assert reduced.get(r.slug, 0.0) == pytest.approx(r.p_at_least_one, abs=1e-9)


def test_submission_folded_as_mcp_source(conn, tmp_path):
    inbox = tmp_path / "inbox"
    label_dir = inbox / "claude-desktop"
    label_dir.mkdir(parents=True)
    (label_dir / "2026-07-10.json").write_text(json.dumps({
        "model_label": "claude-desktop", "showdate": "2026-07-10",
        "epoch": "deadbeef", "submitted_at": "2026-07-09T13:00:00Z",
        "rationale": "Gin is due.",
        "predictions": [{"slug": "gin", "prob": 0.7}, {"slug": "tweezer", "prob": 0.9}],
    }))

    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    show = json.loads((out / "show" / "2026-07-10.json").read_text())

    assert "mcp:claude-desktop" in show["sources"]
    mcp = show["sources"]["mcp:claude-desktop"]
    assert mcp["kind"] == "mcp"
    assert mcp["rationale"] == "Gin is due."
    slugs = {r["slug"] for r in mcp["rows"]}
    assert {"gin", "tweezer"} <= slugs
    # rows sorted by prob desc
    ps = [r["prob"] for r in mcp["rows"]]
    assert ps == sorted(ps, reverse=True)


def test_submission_unknown_showdate_skipped(conn, tmp_path):
    inbox = tmp_path / "inbox"
    label_dir = inbox / "bot"
    label_dir.mkdir(parents=True)
    (label_dir / "2099-01-01.json").write_text(json.dumps({
        "model_label": "bot", "showdate": "2099-01-01",
        "predictions": [{"slug": "gin", "prob": 0.5}],
    }))
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    # no show doc for that date, and no crash
    assert not (out / "show" / "2099-01-01.json").exists()


def _write_submission(inbox, label, showdate, payload):
    label_dir = inbox / label
    label_dir.mkdir(parents=True, exist_ok=True)
    (label_dir / f"{showdate}.json").write_text(json.dumps(payload))


def test_fold_skips_non_list_predictions_without_crashing(conn, tmp_path):
    inbox = tmp_path / "inbox"
    # `predictions` is not a list -> iterating it raises TypeError; must be
    # swallowed (skip the file with a warning) rather than crash the batch.
    _write_submission(inbox, "bot", "2026-07-10", {
        "model_label": "bot", "showdate": "2026-07-10", "predictions": 42,
    })
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    show = json.loads((out / "show" / "2026-07-10.json").read_text())
    assert "mcp:bot" not in show["sources"]


def test_fold_skips_duplicate_slug_file(conn, tmp_path):
    inbox = tmp_path / "inbox"
    _write_submission(inbox, "bot", "2026-07-10", {
        "model_label": "bot", "showdate": "2026-07-10",
        "predictions": [{"slug": "gin", "prob": 0.5}, {"slug": "gin", "prob": 0.6}],
    })
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    show = json.loads((out / "show" / "2026-07-10.json").read_text())
    assert "mcp:bot" not in show["sources"]  # whole file rejected


def test_fold_sparse_submission_keeps_submitted_probs(conn, tmp_path):
    inbox = tmp_path / "inbox"
    submitted = {"gin": 0.4, "tweezer": 0.6, "yem": 0.5}  # sum 1.5 < K (~2.6)
    _write_submission(inbox, "bot", "2026-07-10", {
        "model_label": "bot", "showdate": "2026-07-10",
        "predictions": [{"slug": s, "prob": p} for s, p in submitted.items()],
    })
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    show = json.loads((out / "show" / "2026-07-10.json").read_text())
    rows = show["sources"]["mcp:bot"]["rows"]
    # published AS SUBMITTED: no scaling up, no 0.99 pinning
    assert {r["slug"]: r["prob"] for r in rows} == submitted


def test_fold_scales_down_when_sum_exceeds_k(conn, tmp_path):
    inbox = tmp_path / "inbox"
    high = [{"slug": s, "prob": 0.99} for s in ("gin", "tweezer", "yem", "wilson", "filler")]
    _write_submission(inbox, "bot", "2026-07-10", {
        "model_label": "bot", "showdate": "2026-07-10", "predictions": high,
    })
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    show = json.loads((out / "show" / "2026-07-10.json").read_text())
    probs = [r["prob"] for r in show["sources"]["mcp:bot"]["rows"]]

    k = features.mean_setlist_size(conn, era_for_year(2026))
    assert sum(probs) == pytest.approx(k, abs=1e-3)   # scaled DOWN to sum K
    assert all(p < 0.99 for p in probs)               # each below the submitted 0.99


def test_samples_meta_has_horizon_showids(conn, tmp_path):
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED, with_samples=True)
    smeta = json.loads((tmp_path / "samples_meta.json").read_text())
    horizon = features.future_show_ids(conn)
    assert smeta["horizon_showids"] == horizon


def test_catalog_json(conn, tmp_path):
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED, with_catalog=True)
    catalog = json.loads((tmp_path / "catalog.json").read_text())

    # songs sorted by plays desc, with slug/name/last
    plays = [s["plays"] for s in catalog["songs"]]
    assert plays == sorted(plays, reverse=True)
    tweezer = next(s for s in catalog["songs"] if s["slug"] == "tweezer")
    assert tweezer["plays"] == 10  # tweezer (101) is in all 10 hist setlists
    assert tweezer["last"] == "2022-07-11"

    # by_show maps each past show to its songids (for seen-computation)
    assert catalog["by_show"]["2022-06-01"] == sorted([101, 103, 104, 102])
    # future shows are NOT in the catalog (no performances yet)
    assert "2026-07-10" not in catalog["by_show"]


def test_catalog_absent_without_flag(conn, tmp_path):
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED)
    assert not (tmp_path / "catalog.json").exists()


def test_tour_id_for():
    # Year token is preserved as a suffix so tour identity stays distinct across
    # years (a "Summer Tour" in 2026 != one in 2027).
    assert tour_id_for("2026 Summer Tour") == "summer-2026"
    assert tour_id_for("2027 Summer Tour") == "summer-2027"
    assert tour_id_for("2026 Fall Tour") == "fall-2026"
    assert tour_id_for("New Year's Run 2026") == "new-years-2026"
    # year-less name -> plain slug (no suffix)
    assert tour_id_for("Baker's Dozen") == "bakers-dozen"
    assert tour_id_for(None) == "unknown"
