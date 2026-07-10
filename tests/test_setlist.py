"""Unit tests for phishpred.setlist. See phish-predictor-modes-plan.md §6c-6d
and CONTRACTS.md.

Hand-built in-memory DB (mirrors tests/test_slots.py / tests/test_features.py
style) with KNOWN segues, so mining/hard-pairing thresholds and the
structured sampler's constraint-honoring can be asserted precisely. No
network -- the LLM assembler is exercised against a small fake client
implementing ``models.llm.LLMClient``.
"""
from __future__ import annotations

import json

import pytest

from phishpred import db
from phishpred.models.llm import LLMError
from phishpred.setlist import (
    PREV_NIGHT_DISCOURAGE,
    SetlistPrediction,
    SetlistSong,
    actual_setlist,
    assemble_setlist_llm,
    evaluate_sampler,
    hard_pairings,
    mine_segue_bigrams,
    sample_setlist,
    score_setlist,
)

# ---------------------------------------------------------------------------
# Hand-crafted history
# ---------------------------------------------------------------------------
VENUES = [(1, "Venue", 0), (2, "Venue B", 0)]

SONGS = {
    1: "opener",
    2: "filler-mid",
    3: "closer1",
    4: "midjam",
    5: "tweezer",
    6: "tweezer-reprise",
    7: "closer2",
    8: "encore-song",
    9: "raresong1",
    10: "raresong2",
    11: "splitfollower",
}

# (songid, set_label, trans_mark) in show-global position order.
# tweezer(5) -> tweezer-reprise(6) segues via a hard mark every time.
MAIN_STRUCT = [
    (1, "1", ""), (2, "1", ""), (3, "1", ""),
    (4, "2", ""), (5, "2", " -> "), (6, "2", ""), (7, "2", ""),
    (8, "e", ""),
]
MAIN_DATES = [
    "2022-01-01", "2022-01-08", "2022-01-15", "2022-01-22",
    "2022-02-01", "2022-02-08", "2022-02-15", "2022-02-22",
]

# raresong1(9) -> raresong2(10): support=2, below the default min_support=5.
RARE_STRUCT = [(9, "1", " > "), (10, "1", "")]
RARE_DATES = ["2022-03-01", "2022-03-08"]

# splitfollower(11) follows opener(1) 6x and midjam(4) 4x -- support(1,11)=6
# clears min_support=5, but dominance = 6/10 = 0.6 < the default 0.9, so it
# must NOT be hard-paired at default thresholds (tests the dominance gate
# independently of the support gate).
SPLIT_A_STRUCT = [(1, "1", " > "), (11, "1", "")]
SPLIT_A_DATES = ["2022-03-15", "2022-03-16", "2022-03-17", "2022-03-18", "2022-03-19", "2022-03-20"]
SPLIT_B_STRUCT = [(4, "1", " > "), (11, "1", "")]
SPLIT_B_DATES = ["2022-03-21", "2022-03-22", "2022-03-23", "2022-03-24"]

# Two future shows. RUN_FUTURE continues the all-venue-1 "run" (every indexed
# show is at venue 1, contiguous in calendar order), so sample_setlist's
# default strict_no_repeat hard mask fires for EVERY catalog song there.
# FUTURE is at a fresh venue (2) so it carries no actual-history run context
# and the general sampler tests are unaffected by the mask.
RUN_FUTURE_DATE = "2022-03-25"
FUTURE_DATE = "2022-04-01"

MAIN_SHOWIDS = list(range(1, 1 + len(MAIN_DATES)))
RARE_SHOWIDS = list(range(100, 100 + len(RARE_DATES)))
SPLIT_A_SHOWIDS = list(range(200, 200 + len(SPLIT_A_DATES)))
SPLIT_B_SHOWIDS = list(range(300, 300 + len(SPLIT_B_DATES)))
RUN_FUTURE_SHOWID = 998
FUTURE_SHOWID = 999


def _insert_show(conn, showid, showdate, index):
    conn.execute(
        "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
        "VALUES (?,?,?,?,?,0)",
        (showid, showdate, 1, 900, index),
    )


def _insert_perf(conn, showid, struct):
    for pos, (songid, set_label, mark) in enumerate(struct, start=1):
        conn.execute(
            "INSERT INTO performances (showid, songid, set_label, position, trans_mark) "
            "VALUES (?,?,?,?,?)",
            (showid, songid, set_label, pos, mark),
        )


def _populate(conn):
    for vid, name, alias in VENUES:
        conn.execute("INSERT INTO venues (venueid, name, alias) VALUES (?,?,?)", (vid, name, alias))
    for sid, slug in SONGS.items():
        conn.execute(
            "INSERT INTO songs (songid, slug, name, is_original) VALUES (?,?,?,1)",
            (sid, slug, slug.replace("-", " ").title()),
        )

    index = 0
    for showid, date in zip(MAIN_SHOWIDS, MAIN_DATES):
        _insert_show(conn, showid, date, index)
        _insert_perf(conn, showid, MAIN_STRUCT)
        index += 1
    for showid, date in zip(RARE_SHOWIDS, RARE_DATES):
        _insert_show(conn, showid, date, index)
        _insert_perf(conn, showid, RARE_STRUCT)
        index += 1
    for showid, date in zip(SPLIT_A_SHOWIDS, SPLIT_A_DATES):
        _insert_show(conn, showid, date, index)
        _insert_perf(conn, showid, SPLIT_A_STRUCT)
        index += 1
    for showid, date in zip(SPLIT_B_SHOWIDS, SPLIT_B_DATES):
        _insert_show(conn, showid, date, index)
        _insert_perf(conn, showid, SPLIT_B_STRUCT)
        index += 1

    # Future shows: no performances, show_index NULL. RUN_FUTURE at venue 1
    # (continues the run formed by the whole venue-1 history); FUTURE at
    # venue 2 (fresh venue, no run context -- see the constants' comment).
    conn.execute(
        "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
        "VALUES (?,?,?,?,NULL,0)",
        (RUN_FUTURE_SHOWID, RUN_FUTURE_DATE, 1, 900),
    )
    conn.execute(
        "INSERT INTO shows (showid, showdate, venueid, tourid, show_index, exclude) "
        "VALUES (?,?,?,?,NULL,0)",
        (FUTURE_SHOWID, FUTURE_DATE, 2, 900),
    )
    conn.commit()


@pytest.fixture()
def conn():
    c = db.get_connection(":memory:")
    db.init_db(c)
    _populate(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# mine_segue_bigrams
# ---------------------------------------------------------------------------
def test_mine_segue_bigrams_finds_strong_pair(conn):
    bigrams = mine_segue_bigrams(conn)
    assert 5 in bigrams
    next_songs = dict(bigrams[5])
    assert next_songs[6] == pytest.approx(1.0)


def test_mine_segue_bigrams_min_support_filters_rare_pair(conn):
    # support(9 -> 10) == 2, below the default min_support=5.
    bigrams_default = mine_segue_bigrams(conn)
    assert 9 not in bigrams_default

    bigrams_lowered = mine_segue_bigrams(conn, min_support=2)
    assert 9 in bigrams_lowered
    assert dict(bigrams_lowered[9])[10] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# hard_pairings
# ---------------------------------------------------------------------------
def test_hard_pairings_detects_dominant_predecessor(conn):
    pairings = hard_pairings(conn)
    assert pairings[6] == 5  # tweezer-reprise always immediately follows tweezer


def test_hard_pairings_respects_min_support(conn):
    assert 10 not in hard_pairings(conn)  # support=2 < default min_support=5
    lowered = hard_pairings(conn, min_support=2)
    assert lowered[10] == 9


def test_hard_pairings_respects_dominance(conn):
    # splitfollower(11): predecessor opener(1) at frac=0.6, midjam(4) at 0.4 --
    # neither clears the default 0.9 dominance threshold.
    assert 11 not in hard_pairings(conn)
    relaxed = hard_pairings(conn, dominance=0.5)
    assert relaxed[11] == 1  # opener has the higher of the two fractions


# ---------------------------------------------------------------------------
# sample_setlist
# ---------------------------------------------------------------------------
SKELETON = {"1": 3, "2": 4, "e": 1}


def test_sample_setlist_deterministic(conn):
    a = sample_setlist(conn, FUTURE_DATE, seed=7, skeleton=SKELETON)
    b = sample_setlist(conn, FUTURE_DATE, seed=7, skeleton=SKELETON)
    ids_a = {label: [s.songid for s in songs] for label, songs in a.sets.items()}
    ids_b = {label: [s.songid for s in songs] for label, songs in b.sets.items()}
    assert ids_a == ids_b


def test_sample_setlist_respects_skeleton_lengths(conn):
    pred = sample_setlist(conn, FUTURE_DATE, seed=0, skeleton=SKELETON)
    assert pred.skeleton == SKELETON
    for label, length in SKELETON.items():
        assert len(pred.sets[label]) == length


def test_sample_setlist_no_repeats_within_show(conn):
    for seed in range(10):
        pred = sample_setlist(conn, FUTURE_DATE, seed=seed, skeleton=SKELETON)
        all_ids = [s.songid for songs in pred.sets.values() for s in songs]
        assert len(all_ids) == len(set(all_ids))


def test_sample_setlist_hard_pair_adjacency(conn):
    # tweezer(5) is the sole historically-observed set2-mid candidate (its
    # follower, reprise, is excluded from direct draws), so it is drawn
    # deterministically into set2's mid slot, and reprise(6) is force-placed
    # immediately after it every time -- this is exact, not seed-dependent.
    for seed in range(5):
        pred = sample_setlist(conn, FUTURE_DATE, seed=seed, skeleton=SKELETON)
        set2_ids = [s.songid for s in pred.sets["2"]]
        assert set2_ids == [4, 5, 6, 7]
        tweezer_song = pred.sets["2"][1]
        assert tweezer_song.segue_mark.strip() in (">", "->")


def test_sample_setlist_reprise_never_appears_without_tweezer(conn):
    for seed in range(10):
        # Skeletons without room for both songs adjacent should just never
        # place the follower alone.
        pred = sample_setlist(conn, FUTURE_DATE, seed=seed, skeleton={"1": 3, "e": 1})
        all_ids = [s.songid for songs in pred.sets.values() for s in songs]
        assert 6 not in all_ids


def test_sample_setlist_encore_always_high_propensity_song(conn):
    # encore-song(8) is the only candidate ever observed in an encore slot.
    for seed in range(5):
        pred = sample_setlist(conn, FUTURE_DATE, seed=seed, skeleton=SKELETON)
        assert [s.songid for s in pred.sets["e"]] == [8]


def test_sample_setlist_set1_open_mid_close_deterministic_middle_and_close(conn):
    pred = sample_setlist(conn, FUTURE_DATE, seed=0, skeleton=SKELETON)
    set1 = pred.sets["1"]
    # rank2/rank3 have exactly one historically-observed candidate each.
    assert set1[1].songid == 2  # filler-mid
    assert set1[2].songid == 3  # closer1


def test_sample_setlist_uses_sampled_skeleton_when_none_given(conn):
    pred = sample_setlist(conn, FUTURE_DATE, seed=1)
    assert pred.skeleton  # non-empty -- era "4.0" fixture always has data
    assert pred.era == "4.0"


def test_sample_setlist_unknown_date_raises(conn):
    with pytest.raises(ValueError):
        sample_setlist(conn, "1900-01-01")


# ---------------------------------------------------------------------------
# Run-scope no-repeat semantics (strict mask / exclude / discourage)
# ---------------------------------------------------------------------------
def _all_songids(pred: SetlistPrediction) -> set[int]:
    return {s.songid for songs in pred.sets.values() for s in songs}


def test_sample_setlist_strict_no_repeat_masks_actual_run_history(conn):
    # RUN_FUTURE continues the run formed by the entire venue-1 history, so
    # every catalog song has played_in_run=1 -> the default hard mask empties
    # the candidate pool entirely (no repeats within a run, ever).
    pred = sample_setlist(conn, RUN_FUTURE_DATE, seed=0, skeleton=SKELETON)
    assert _all_songids(pred) == set()

    # Opting out restores the old soft-downweight-only behavior.
    relaxed = sample_setlist(
        conn, RUN_FUTURE_DATE, seed=0, skeleton=SKELETON, strict_no_repeat=False
    )
    assert _all_songids(relaxed)


def test_sample_setlist_exclude_songids_never_placed(conn):
    for seed in range(10):
        pred = sample_setlist(
            conn, FUTURE_DATE, seed=seed, skeleton=SKELETON, exclude_songids={1, 8}
        )
        assert not _all_songids(pred) & {1, 8}


def test_sample_setlist_excluded_follower_not_force_placed(conn):
    # reprise(6) only ever enters via force-placement behind tweezer(5);
    # excluding it must keep it out even though its predecessor is placed.
    for seed in range(10):
        pred = sample_setlist(
            conn, FUTURE_DATE, seed=seed, skeleton=SKELETON, exclude_songids={6}
        )
        all_ids = _all_songids(pred)
        assert 6 not in all_ids
        assert 5 in all_ids  # predecessor still drawable on its own


def test_sample_setlist_excluding_predecessor_drops_follower_too(conn):
    # Followers are never drawn directly, so excluding tweezer(5) removes the
    # reprise(6) as well -- it can't appear without its predecessor before it.
    for seed in range(10):
        pred = sample_setlist(
            conn, FUTURE_DATE, seed=seed, skeleton=SKELETON, exclude_songids={5}
        )
        assert not _all_songids(pred) & {5, 6}


def test_sample_setlist_discourage_scales_published_prob(conn):
    base = sample_setlist(conn, FUTURE_DATE, seed=0, skeleton=SKELETON)
    base_prob = next(s.prob for s in base.sets["e"] if s.songid == 8)
    assert base_prob > 0

    dis = sample_setlist(
        conn, FUTURE_DATE, seed=0, skeleton=SKELETON, discourage_songids={8}
    )
    # encore-song(8) is the only encore-propensity candidate, so it still wins
    # the encore slot -- but its weight (and published prob) carries the 0.02
    # discourage multiplier.
    dis_prob = next(s.prob for s in dis.sets["e"] if s.songid == 8)
    assert dis_prob == pytest.approx(PREV_NIGHT_DISCOURAGE * base_prob)


def test_sample_setlist_discouraged_song_heavily_underrepresented(conn):
    # One-slot skeleton: opener(1) has by far the strongest set1-open history
    # (14 of 20 shows). Discouraging it should collapse its selection rate.
    skeleton = {"1": 1}

    def freq(discourage: set[int] | None) -> float:
        n = 100
        hits = sum(
            1 in _all_songids(
                sample_setlist(
                    conn, FUTURE_DATE, seed=seed, skeleton=skeleton,
                    discourage_songids=discourage,
                )
            )
            for seed in range(n)
        )
        return hits / n

    base = freq(None)
    dis = freq({1})
    assert base > 0.2
    assert dis < base / 4


# ---------------------------------------------------------------------------
# assemble_setlist_llm
# ---------------------------------------------------------------------------
class FakeSetlistClient:
    provider = "fake"

    def __init__(self, response: dict, model: str = "fake-1"):
        self.model = model
        self.response = response
        self.calls = 0
        self.last_user: str | None = None

    def complete_json(self, system, user, schema, *, max_tokens=2048):
        self.calls += 1
        self.last_user = user
        return self.response


CANNED_RESPONSE = {
    "sets": {
        "1": [
            {"slug": "opener", "segue_mark": ""},
            {"slug": "filler-mid", "segue_mark": ""},
            {"slug": "closer1", "segue_mark": ""},
        ],
        "2": [
            {"slug": "midjam", "segue_mark": ""},
            {"slug": "tweezer", "segue_mark": " -> "},
            {"slug": "tweezer-reprise", "segue_mark": ""},
            {"slug": "not-a-real-song", "segue_mark": ""},  # unknown -- dropped
            {"slug": "opener", "segue_mark": ""},  # duplicate slug -- dropped
        ],
        "e": [{"slug": "encore-song", "segue_mark": ""}],
    }
}


def test_assemble_setlist_llm_parses_and_maps_slugs(conn):
    client = FakeSetlistClient(dict(CANNED_RESPONSE))
    pred = assemble_setlist_llm(conn, FUTURE_DATE, client, skeleton=SKELETON)

    assert isinstance(pred, SetlistPrediction)
    assert client.calls == 1
    assert pred.model == "llm:fake:fake-1"

    assert [s.slug for s in pred.sets["1"]] == ["opener", "filler-mid", "closer1"]
    # unknown slug and duplicate slug both dropped -> 3 valid songs remain.
    assert [s.slug for s in pred.sets["2"]] == ["midjam", "tweezer", "tweezer-reprise"]
    assert [s.slug for s in pred.sets["e"]] == ["encore-song"]

    tweezer_song = pred.sets["2"][1]
    assert tweezer_song.songid == 5
    assert tweezer_song.segue_mark == " -> "


def test_assemble_setlist_llm_slot_classification_reflects_position(conn):
    client = FakeSetlistClient(dict(CANNED_RESPONSE))
    pred = assemble_setlist_llm(conn, FUTURE_DATE, client, skeleton=SKELETON)
    set1 = pred.sets["1"]
    assert set1[0].slot == "set1-open"
    assert set1[-1].slot == "set1-close"


def test_assemble_setlist_llm_malformed_response_raises(conn):
    client = FakeSetlistClient({"not_sets": []})
    with pytest.raises(LLMError):
        assemble_setlist_llm(conn, FUTURE_DATE, client, skeleton=SKELETON)


def test_assemble_setlist_llm_missing_slug_raises(conn):
    client = FakeSetlistClient({"sets": {"1": [{"segue_mark": ""}]}})
    with pytest.raises(LLMError):
        assemble_setlist_llm(conn, FUTURE_DATE, client, skeleton=SKELETON)


# ---------------------------------------------------------------------------
# assemble_setlist_llm -- run-scope no-repeat
# ---------------------------------------------------------------------------
def _candidate_lines(prompt: str) -> list[str]:
    return prompt.splitlines()


def _prompt_prob(prompt: str, slug: str) -> float:
    line = next(l for l in prompt.splitlines() if l.startswith(f"{slug} |"))
    return float(line.split("prob=")[1].split(" ")[0])


def test_assemble_setlist_llm_strict_exclusion_masks_shortlist_and_prompt(conn):
    client = FakeSetlistClient(dict(CANNED_RESPONSE))
    pred = assemble_setlist_llm(
        conn, FUTURE_DATE, client, skeleton=SKELETON,
        exclude_songids={5},  # tweezer played on an earlier night of the run
    )
    lines = _candidate_lines(client.last_user)
    # tweezer's candidate line is gone; tweezer-reprise (distinct slug) stays.
    assert not any(l.startswith("tweezer |") for l in lines)
    assert any(l.startswith("tweezer-reprise |") for l in lines)
    # The prompt names the exclusion explicitly.
    assert "already played earlier in this run" in client.last_user.lower()
    assert "tweezer" in client.last_user
    # The canned response still emits tweezer -- the resolution guard drops it.
    all_ids = [s.songid for songs in pred.sets.values() for s in songs]
    assert 5 not in all_ids


def test_assemble_setlist_llm_no_exclusions_no_prompt_line(conn):
    client = FakeSetlistClient(dict(CANNED_RESPONSE))
    assemble_setlist_llm(conn, FUTURE_DATE, client, skeleton=SKELETON)
    assert "already played earlier in this run" not in client.last_user.lower()


def test_assemble_setlist_llm_discourage_downweights_prompt_probs(conn):
    base = FakeSetlistClient(dict(CANNED_RESPONSE))
    assemble_setlist_llm(conn, FUTURE_DATE, base, skeleton=SKELETON)
    disc = FakeSetlistClient(dict(CANNED_RESPONSE))
    pred = assemble_setlist_llm(
        conn, FUTURE_DATE, disc, skeleton=SKELETON, discourage_songids={4},
    )
    p_base = _prompt_prob(base.last_user, "midjam")
    p_disc = _prompt_prob(disc.last_user, "midjam")
    assert p_disc == pytest.approx(p_base * PREV_NIGHT_DISCOURAGE, abs=2e-3)
    # Discouraged, not banned: no "do not select" line, and the LLM's pick
    # of midjam is still honored.
    assert "already played earlier in this run" not in disc.last_user.lower()
    all_ids = [s.songid for songs in pred.sets.values() for s in songs]
    assert 4 in all_ids


def test_assemble_setlist_llm_soft_no_repeat_keeps_excluded_selectable(conn):
    client = FakeSetlistClient(dict(CANNED_RESPONSE))
    pred = assemble_setlist_llm(
        conn, FUTURE_DATE, client, skeleton=SKELETON,
        strict_no_repeat=False, exclude_songids={5},
    )
    # Soft mode: the excluded song stays in the shortlist (down-weighted, not
    # masked) and the LLM's pick of it is honored.
    assert any(l.startswith("tweezer |") for l in _candidate_lines(client.last_user))
    all_ids = [s.songid for songs in pred.sets.values() for s in songs]
    assert 5 in all_ids


# ---------------------------------------------------------------------------
# score_setlist / actual_setlist / evaluate_sampler
# ---------------------------------------------------------------------------
def _pred_from_songids(songids: list[int]) -> SetlistPrediction:
    songs = [
        SetlistSong(song_name=str(sid), slug=str(sid), songid=sid, slot="set1-mid", prob=0.5)
        for sid in songids
    ]
    return SetlistPrediction(
        showdate="2022-01-01", venue_name="Venue", era="4.0", model="test", skeleton={"1": len(songids)},
        sets={"1": songs},
    )


def test_score_setlist_identical_order_is_perfect(conn):
    pred = _pred_from_songids([1, 2, 3, 4])
    metrics = score_setlist(pred, [1, 2, 3, 4])

    assert metrics["hit_at_k"] == pytest.approx(1.0)
    assert metrics["jaccard"] == pytest.approx(1.0)
    assert metrics["kendall_tau"] == pytest.approx(1.0)
    assert metrics["lcs_len"] == 4
    assert metrics["lcs_ratio"] == pytest.approx(1.0)
    assert metrics["slot_accuracy"] == pytest.approx(1.0)


def test_score_setlist_reversed_order_known_tau(conn):
    pred = _pred_from_songids([1, 2, 3, 4])
    metrics = score_setlist(pred, [4, 3, 2, 1])

    assert metrics["hit_at_k"] == pytest.approx(1.0)  # same song set
    assert metrics["jaccard"] == pytest.approx(1.0)
    assert metrics["kendall_tau"] == pytest.approx(-1.0)  # fully reversed
    assert metrics["lcs_len"] == 1  # strict reversal of 4 distinct items
    assert metrics["slot_accuracy"] == pytest.approx(0.0)  # opener/closer both mismatch


def test_score_setlist_partial_overlap(conn):
    pred = _pred_from_songids([1, 2, 3])
    metrics = score_setlist(pred, [1, 2, 99])

    assert metrics["hit_count"] == 2
    assert metrics["hit_at_k"] == pytest.approx(2 / 3)
    assert metrics["jaccard"] == pytest.approx(2 / 4)  # union = {1,2,3,99}


def test_actual_setlist_orders_by_position(conn):
    assert actual_setlist(conn, MAIN_SHOWIDS[0]) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_evaluate_sampler_aggregates_over_past_shows(conn):
    # Leakage-free: candidate probs come from build_features (walk-forward), so
    # each target is scored using only PRIOR shows. Use MAIN_SHOWIDS[1:4]
    # (show_index 1,2,3) -- all have prior history, so all are scoreable.
    result = evaluate_sampler(conn, MAIN_SHOWIDS[1:4], seed=0)
    assert result["n_shows"] == 3
    for key in (
        "mean_hit_at_k", "mean_jaccard", "mean_kendall_tau",
        "mean_lcs_len", "mean_lcs_ratio", "mean_slot_accuracy",
    ):
        assert key in result
    assert 0.0 <= result["mean_hit_at_k"] <= 1.0
    assert 0.0 <= result["mean_jaccard"] <= 1.0
    assert 0.0 <= result["mean_slot_accuracy"] <= 1.0


def test_evaluate_sampler_first_show_unscoreable_leakage_free(conn):
    # The first indexed show (show_index 0) has no prior history, so
    # build_features emits no candidate rows for it -> correctly unscoreable.
    assert evaluate_sampler(conn, [MAIN_SHOWIDS[0]], seed=0) == {"n_shows": 0}


def test_evaluate_sampler_empty_showids_returns_zero(conn):
    assert evaluate_sampler(conn, []) == {"n_shows": 0}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_table_contains_sets_and_songs(conn):
    pred = sample_setlist(conn, FUTURE_DATE, seed=0, skeleton=SKELETON)
    text = pred.render(json_out=False)
    assert text.strip()
    assert "Set 1" in text
    assert "Set 2" in text
    assert "Encore" in text
    assert "Tweezer" in text or "tweezer" in text.lower()


def test_render_json_round_trips(conn):
    pred = sample_setlist(conn, FUTURE_DATE, seed=0, skeleton=SKELETON)
    text = pred.render(json_out=True)
    payload = json.loads(text)

    assert payload["showdate"] == FUTURE_DATE
    assert payload["model"] == "sampler"
    assert set(payload["skeleton"].keys()) == set(SKELETON.keys())
    assert set(payload["sets"].keys()) == set(SKELETON.keys())
    for songs in payload["sets"].values():
        for s in songs:
            assert set(("song_name", "slug", "songid", "slot", "prob", "segue_mark")) <= set(s.keys())
