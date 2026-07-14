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


def test_fold_carries_setlist_and_versions(conn, tmp_path):
    inbox = tmp_path / "inbox"
    _write_submission(inbox, "bot", "2026-07-10", {
        "model_label": "bot", "showdate": "2026-07-10",
        "rationale": "take 2",
        "predictions": [{"slug": "gin", "prob": 0.7}],
        "setlist": {"sets": {"1": ["tweezer", "gin"], "e": ["wilson"]}},
        "versions": [
            {"submitted_at": "2026-07-09T10:00:00Z", "rationale": "take 1",
             "predictions": [{"slug": "tweezer", "prob": 0.6}],
             "setlist": {"sets": {"1": ["yem"]}}},
        ],
    })
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    src = json.loads((out / "show" / "2026-07-10.json").read_text())["sources"]["mcp:bot"]

    # latest setlist resolved to {slug, song} objects
    assert src["setlist"]["sets"]["1"] == [
        {"slug": "tweezer", "song": "Tweezer"}, {"slug": "gin", "song": "Bathtub Gin"}
    ]
    assert src["setlist"]["sets"]["e"] == [{"slug": "wilson", "song": "Wilson"}]

    # one prior version folded, with its own resolved setlist
    assert len(src["versions"]) == 1
    v = src["versions"][0]
    assert v["rationale"] == "take 1"
    assert v["submitted_at"] == "2026-07-09T10:00:00Z"
    assert [r["slug"] for r in v["rows"]] == ["tweezer"]
    assert v["setlist"]["sets"]["1"] == [{"slug": "yem", "song": "YEM"}]


def test_fold_drops_partially_unknown_setlist_wholly(conn, tmp_path):
    inbox = tmp_path / "inbox"
    _write_submission(inbox, "bot", "2026-07-10", {
        "model_label": "bot", "showdate": "2026-07-10",
        "predictions": [{"slug": "gin", "prob": 0.7}],
        "setlist": {"sets": {"1": ["tweezer", "not-a-song"]}},  # one unknown slug
    })
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    src = json.loads((out / "show" / "2026-07-10.json").read_text())["sources"]["mcp:bot"]
    # source still folds (predictions valid), but the whole setlist is dropped
    assert "setlist" not in src
    assert [r["slug"] for r in src["rows"]] == ["gin"]


def test_fold_drops_invalid_version_individually(conn, tmp_path):
    inbox = tmp_path / "inbox"
    _write_submission(inbox, "bot", "2026-07-10", {
        "model_label": "bot", "showdate": "2026-07-10",
        "predictions": [{"slug": "gin", "prob": 0.7}],
        "versions": [
            {"submitted_at": "t1", "predictions": [{"slug": "gin", "prob": 0.5}, {"slug": "gin", "prob": 0.6}]},  # dup -> drop
            {"submitted_at": "t2", "predictions": [{"slug": "tweezer", "prob": 0.4}]},  # valid -> keep
        ],
    })
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    src = json.loads((out / "show" / "2026-07-10.json").read_text())["sources"]["mcp:bot"]
    # latest still folded; only the valid version survives
    assert len(src["versions"]) == 1
    assert src["versions"][0]["submitted_at"] == "t2"
    assert [r["slug"] for r in src["versions"][0]["rows"]] == ["tweezer"]


def test_fold_no_versions_key_when_none_valid(conn, tmp_path):
    inbox = tmp_path / "inbox"
    _write_submission(inbox, "bot", "2026-07-10", {
        "model_label": "bot", "showdate": "2026-07-10",
        "predictions": [{"slug": "gin", "prob": 0.7}],
        "versions": [
            {"submitted_at": "t1", "predictions": [{"slug": "gin", "prob": 0.5}, {"slug": "gin", "prob": 0.6}]},  # dup -> drop
        ],
    })
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, submitted_dir=inbox)
    src = json.loads((out / "show" / "2026-07-10.json").read_text())["sources"]["mcp:bot"]
    assert "versions" not in src  # every version dropped -> no key


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


# ---------------------------------------------------------------------------
# Feature A — headline source carries the sampled setlist (§2/§8)
# ---------------------------------------------------------------------------
def test_headline_source_carries_sampled_setlist(conn, tmp_path):
    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED)
    show = json.loads((tmp_path / "show" / "2026-07-10.json").read_text())
    src = show["sources"]["heuristic"]
    assert "setlist" in src, "headline source should carry the sampled setlist"
    sets = src["setlist"]["sets"]
    assert sets, "expected at least one set"
    # folded shape matches _fold_setlist: {"slug","song"} objects, keys in order.
    first_song = next(iter(sets.values()))[0]
    assert list(first_song.keys()) == ["slug", "song"]
    # equals the published setlist doc's songs (same deterministic sampler).
    setlist_doc = json.loads((tmp_path / "setlist" / "2026-07-10.json").read_text())
    for label, songs in setlist_doc["sets"].items():
        assert [s["slug"] for s in songs] == [x["slug"] for x in sets[label]]
        assert [s["song_name"] for s in songs] == [x["song"] for x in sets[label]]


def test_heuristic_setlist_scores_non_null(conn, tmp_path):
    """score_show now produces a non-null setlist_score for the statistical
    headline source (it used to always sit out)."""
    from phishpred.score import score_show

    publish(conn, tmp_path, n_sims=N_SIMS, seed=SEED)
    frozen = json.loads((tmp_path / "show" / "2026-07-10.json").read_text())
    sets = frozen["sources"]["heuristic"]["setlist"]["sets"]

    # Treat the sampled setlist itself as the played setlist so we get real hits.
    played, played_sets, seen = [], {}, set()
    for label, songs in sets.items():
        played_sets[label] = [{"slug": s["slug"], "song": s["song"]} for s in songs]
        for s in songs:
            if s["slug"] not in seen:
                seen.add(s["slug"])
                played.append({"slug": s["slug"], "song": s["song"]})

    sc = score_show(frozen, played, played_sets)
    ss = sc["sources"]["heuristic"]["setlist_score"]
    assert ss is not None
    assert ss["n_songs"] == sum(len(v) for v in sets.values())
    # every predicted song was "played" here -> all hits, all placed/exact.
    assert ss["hit_rate"] == pytest.approx(1.0)
    assert ss["weighted_score"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Feature B — freeze-once + tracker per-tour docs (§3)
# ---------------------------------------------------------------------------
def test_publish_stages_frozen_tour_when_absent(conn, tmp_path):
    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, created_at="2026-07-09T00:00:00Z")
    served = json.loads((out / "tour" / "summer-2026.json").read_text())
    stage = json.loads((out / "tour_frozen" / "summer-2026.json").read_text())
    # served carries a tracker; the staged frozen doc does NOT (time-varying).
    assert "tracker" in served
    assert "tracker" not in stage
    assert {k: v for k, v in served.items() if k != "tracker"} == stage
    assert served["tracker"]["as_of"] == "2026-07-09T00:00:00Z"


def test_publish_serves_frozen_tour_rows_and_refreshes_tracker(conn, tmp_path):
    frozen = tmp_path / "frozen"
    (frozen / "tour").mkdir(parents=True)
    frozen_doc = {
        "epoch": "backcast0000", "horizon_showdates": ["2026-07-10", "2026-07-11", "2026-07-18"],
        "model": "heuristic", "n_sims": 2000, "half_life": 50,
        "backcast": True, "as_of_showdate": "2026-07-06",
        "rows": [{"song": "Frozen Song", "slug": "frozen-song", "expected_plays": 5.0,
                  "p_at_least_one": 0.99,
                  "dist": {"0": 0.01, "1": 0.1, "2": 0.2, "3": 0.3, "4+": 0.39},
                  "bucket": "lock", "analytic_p": 4.0}],
    }
    (frozen / "tour" / "summer-2026.json").write_text(json.dumps(frozen_doc))

    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, frozen_dir=frozen,
            created_at="2026-07-09T00:00:00Z")
    served = json.loads((out / "tour" / "summer-2026.json").read_text())
    # frozen rows/metadata authoritative — NOT re-simulated over.
    assert served["rows"] == frozen_doc["rows"]
    assert served["backcast"] is True
    assert served["as_of_showdate"] == "2026-07-06"
    # tracker present + refreshed to created_at.
    assert served["tracker"]["as_of"] == "2026-07-09T00:00:00Z"
    # re-staged frozen doc is the frozen doc verbatim (idempotent, no tracker).
    stage = json.loads((out / "tour_frozen" / "summer-2026.json").read_text())
    assert "tracker" not in stage
    assert stage["rows"] == frozen_doc["rows"]
    assert stage["backcast"] is True


def test_tour_tracker_counts_played_shows(conn, tmp_path):
    # Make 2026-07-05 a PLAYED 2026 Summer Tour show (tweezer, wilson).
    conn.execute(
        "INSERT INTO shows (showid, showdate, venueid, tour_name, show_index, exclude) "
        "VALUES (?,?,?,?,?,0)",
        (1100, "2026-07-05", 1, "2026 Summer Tour", 10),
    )
    for pos, sid in enumerate((101, 103)):  # tweezer, wilson
        conn.execute(
            "INSERT INTO performances (showid, songid, set_label, position) VALUES (?,?,?,?)",
            (1100, sid, "1", pos),
        )
    conn.commit()

    out = tmp_path / "snap"
    publish(conn, out, n_sims=N_SIMS, seed=SEED, created_at="2026-07-09T00:00:00Z")
    tr = json.loads((out / "tour" / "summer-2026.json").read_text())["tracker"]
    assert tr["n_shows_played"] == 1          # the one played tour show
    assert tr["n_shows_total"] == 4           # 1 played + 3 future
    assert tr["played_counts"] == {"tweezer": 1, "wilson": 1}
    assert tr["played_dates"] == {"tweezer": ["2026-07-05"], "wilson": ["2026-07-05"]}
    assert tr["as_of"] == "2026-07-09T00:00:00Z"


def test_backcast_tour_scrubs_and_returns_full_horizon(conn):
    from phishpred.publish import backcast_tour

    doc = backcast_tour(conn, "summer-2026", n_sims=N_SIMS, seed=SEED)
    assert doc["backcast"] is True
    # Full tour horizon — all 3 tour shows simulated as of the pre-opener state.
    assert doc["horizon_showdates"] == ["2026-07-10", "2026-07-11", "2026-07-18"]
    # as_of = last pre-opener played show in the fixture history.
    assert doc["as_of_showdate"] == "2022-07-11"
    # The scrub un-indexed the tour shows on the working copy.
    assert conn.execute(
        "SELECT show_index FROM shows WHERE showdate='2026-07-10'"
    ).fetchone()["show_index"] is None


def test_backcast_tour_is_blind_to_played_tour_shows():
    """The backcast must be INDEPENDENT of what actually happened on the tour:
    injecting a bustout on the opener (indexed + a performance row) must not
    change the frozen prediction at all, since the scrub removes it."""
    from phishpred.publish import backcast_tour

    base = db.get_connection(":memory:")
    db.init_db(base)
    _populate(base)
    baseline = backcast_tour(base, "summer-2026", n_sims=N_SIMS, seed=SEED)
    base.close()

    leaked = db.get_connection(":memory:")
    db.init_db(leaked)
    _populate(leaked)
    # Opener actually played, featuring a rare bustout (filler=105).
    leaked.execute("UPDATE shows SET show_index = 10 WHERE showdate = '2026-07-10'")
    leaked.execute(
        "INSERT INTO performances (showid, songid, set_label, position) VALUES (?,?,?,?)",
        (1101, 105, "1", 0),
    )
    leaked.commit()
    scrubbed = backcast_tour(leaked, "summer-2026", n_sims=N_SIMS, seed=SEED)
    leaked.close()

    # Same rows despite the injected tour play -> the scrub fully blinded it.
    assert scrubbed["rows"] == baseline["rows"]
    assert scrubbed["as_of_showdate"] == baseline["as_of_showdate"] == "2022-07-11"


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
