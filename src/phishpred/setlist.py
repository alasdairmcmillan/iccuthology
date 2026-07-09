"""Setlist mode — a full, ordered, plausible setlist for ONE show. See
phish-predictor-modes-plan.md §6c (sequence assembly) and §6d (scoring).

Two independent assemblers share the same candidate-probability + skeleton +
slot-propensity inputs:

1. **Structured sampler** (`sample_setlist`, §6c-i) — deterministic given a
   seed, no network. The priority deliverable per the user's answer in plan
   §9: "prioritize the deterministic sampler ... guessing there will be a
   pretty hard ceiling on potential accuracy there." Ship the honest
   baseline + metrics rather than chase 100% ordered-hit, which plan §6d
   explicitly calls "effectively impossible."
2. **LLM assembler** (`assemble_setlist_llm`, §6c-ii) — behind a flag at the
   CLI layer. Takes an injected `models.llm.LLMClient` so it is fully
   testable offline with a fake.

Both return the same `SetlistPrediction` shape, and `score_setlist` (§6d)
scores either one against a real setlist with honest, non-inflated metrics:
song-set overlap (Hit@K, Jaccard) plus sequence metrics (Kendall-tau,
longest-common-subsequence) and a coarse slot-accuracy check.

Segue/pairing constraints (`mine_segue_bigrams`, `hard_pairings`) are mined
directly from `performances.trans_mark` rather than hardcoded — real data
has values like `', '`, `' > '`, `' -> '`, `''` (see CONTRACTS.md /
schema.sql); we normalize by stripping whitespace before comparing, so both
spaced (real ingested data) and unspaced (some fixtures) forms match.
"""
from __future__ import annotations

import io
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

import numpy as np
from rich import box
from rich.console import Console
from rich.table import Table

from . import features
from .config import era_for_year
from .models import heuristic as heuristic_mod
from .models.llm import FLOOR_PROB, LLMError
from .slots import classify_slot, sample_set_structure, slot_propensities

# ---------------------------------------------------------------------------
# Mining defaults (used both by the public mining functions and internally by
# sample_setlist -- kept as module constants so both stay in sync).
# ---------------------------------------------------------------------------
HARD_PAIRING_DOMINANCE = 0.9
HARD_PAIRING_MIN_SUPPORT = 5
BIGRAM_MIN_SUPPORT = 5

# Segue marks in performances.trans_mark, after stripping whitespace. Real
# ingested data stores ' > ' / ' -> ' (with spaces); some test fixtures use
# the unspaced '>' / '->' -- stripping before comparing handles both.
_SEGUE_MARKS = {">", "->"}


def _normalize_mark(raw) -> str:
    return (raw or "").strip()


# ---------------------------------------------------------------------------
# Show resolution -- small, self-contained (mirrors predict.py's private
# _resolve_show/_resolve_venue) rather than reaching into that module's
# underscore-prefixed internals.
# ---------------------------------------------------------------------------
def _resolve_show(conn: sqlite3.Connection, showdate: str) -> sqlite3.Row:
    rows = conn.execute("SELECT * FROM shows WHERE showdate = ?", (showdate,)).fetchall()
    if not rows:
        raise ValueError(
            f"No show found for {showdate!r}. Check the date is yyyy-mm-dd and that "
            "it has been ingested (run `phishpred ingest` / `phishpred refresh`)."
        )
    if len(rows) == 1:
        return rows[0]

    try:
        meta_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'phish_artistid'"
        ).fetchone()
        phish_id = int(meta_row["value"]) if meta_row is not None else 1
    except (sqlite3.Error, TypeError, ValueError):
        phish_id = 1

    for row in rows:
        try:
            if row["artistid"] == phish_id:
                return row
        except (IndexError, KeyError):
            continue
    return rows[0]


def _resolve_venue(conn: sqlite3.Connection, venueid) -> sqlite3.Row | None:
    if venueid is None:
        return None
    return conn.execute("SELECT * FROM venues WHERE venueid = ?", (venueid,)).fetchone()


# ---------------------------------------------------------------------------
# Segue mining -- single pass over `performances`, shared by the two public
# mining functions and by sample_setlist (which needs all three views: pair
# counts, per-song appearance counts, and the dominant real mark per pair).
# ---------------------------------------------------------------------------
@dataclass
class _SegueStats:
    pair_counts: dict[tuple[int, int], int]
    appearances: dict[int, int]
    mark_lookup: dict[tuple[int, int], str]  # (prev, next) -> ' > ' or ' -> '


def _show_performance_sequences(conn: sqlite3.Connection) -> list[list[tuple[int, str]]]:
    """Each non-excluded show's performances as [(songid, trans_mark), ...],
    ordered by the show-global `position` column."""
    rows = conn.execute(
        "SELECT p.showid AS showid, p.songid AS songid, p.trans_mark AS trans_mark "
        "FROM performances p JOIN shows s ON s.showid = p.showid "
        "WHERE s.exclude = 0 ORDER BY p.showid, p.position"
    ).fetchall()
    by_show: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        by_show[row["showid"]].append((row["songid"], row["trans_mark"]))
    return list(by_show.values())


def _compute_segue_stats(conn: sqlite3.Connection) -> _SegueStats:
    pair_counts: Counter = Counter()
    appearances: Counter = Counter()
    mark_counts: Counter = Counter()  # (prev, next, normalized_mark) -> count

    for seq in _show_performance_sequences(conn):
        for songid, _mark in seq:
            appearances[songid] += 1
        for i in range(len(seq) - 1):
            songid, mark = seq[i]
            next_songid, _next_mark = seq[i + 1]
            norm = _normalize_mark(mark)
            if norm in _SEGUE_MARKS:
                pair_counts[(songid, next_songid)] += 1
                mark_counts[(songid, next_songid, norm)] += 1

    mark_lookup: dict[tuple[int, int], str] = {}
    best_count: dict[tuple[int, int], int] = {}
    for (prev, nxt, mark), c in mark_counts.items():
        if c > best_count.get((prev, nxt), -1):
            best_count[(prev, nxt)] = c
            mark_lookup[(prev, nxt)] = " -> " if mark == "->" else " > "

    return _SegueStats(dict(pair_counts), dict(appearances), mark_lookup)


def _bigrams_from_stats(
    stats: _SegueStats, min_support: int
) -> dict[int, list[tuple[int, float]]]:
    out_totals: Counter = Counter()
    for (prev, _nxt), c in stats.pair_counts.items():
        out_totals[prev] += c

    result: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for (prev, nxt), c in stats.pair_counts.items():
        if c < min_support:
            continue
        result[prev].append((nxt, c / out_totals[prev]))
    for prev in result:
        result[prev].sort(key=lambda t: (-t[1], t[0]))
    return dict(result)


def _pairings_from_stats(
    stats: _SegueStats, dominance: float, min_support: int
) -> dict[int, int]:
    best: dict[int, tuple[int, float]] = {}
    for (prev, nxt), c in stats.pair_counts.items():
        if c < min_support:
            continue
        total = stats.appearances.get(nxt, 0)
        if total <= 0:
            continue
        frac = c / total
        if frac < dominance:
            continue
        current = best.get(nxt)
        if current is None or frac > current[1]:
            best[nxt] = (prev, frac)
    return {follower: prev for follower, (prev, _frac) in best.items()}


def mine_segue_bigrams(
    conn: sqlite3.Connection, *, min_support: int = 5
) -> dict[int, list[tuple[int, float]]]:
    """Directed segue bigrams mined from consecutive performances joined by a
    segue mark (' > ' or ' -> ', any whitespace stripped before comparing).

    Returns ``prev_songid -> [(next_songid, conditional_prob), ...]`` sorted
    by descending conditional probability, where ``conditional_prob`` =
    P(next | prev, prev segued out) = count(prev->next) / total segue-outs
    from prev. Only pairs with raw support >= ``min_support`` are kept, but
    the conditional-probability denominator uses ALL of prev's segue-outs
    (not just the ones individually meeting the threshold) so it stays a
    proper probability. Used both as a standalone signal and to assign
    plausible segue marks between adjacently-drawn songs in
    ``sample_setlist``.
    """
    return _bigrams_from_stats(_compute_segue_stats(conn), min_support)


def hard_pairings(
    conn: sqlite3.Connection, *, dominance: float = 0.9, min_support: int = 5
) -> dict[int, int]:
    """Near-deterministic "X must immediately follow Y" pairs, e.g. Tweezer
    Reprise after Tweezer.

    For each ordered segue pair (prev, next) with raw support >=
    ``min_support``, compute ``frac = count(prev->next) / total_appearances(next)``
    (fraction of `next`'s appearances, anywhere, that are this specific segue).
    If ``frac >= dominance``, `next` (the follower) is recorded as hard-paired
    to `prev` (its predecessor); ties/multiple qualifying predecessors keep
    the highest-fraction one. Returns ``{follower_songid: predecessor_songid}``.

    Mined from data rather than hardcoded, per plan §6c ("a tiny documented
    fallback list is acceptable but data-mined is preferred") -- on real data
    this reliably recovers pairs like Tweezer Reprise/Tweezer without a
    fallback list, so none is implemented here.
    """
    return _pairings_from_stats(_compute_segue_stats(conn), dominance, min_support)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------
@dataclass
class SetlistSong:
    song_name: str
    slug: str
    songid: int
    slot: str
    prob: float
    segue_mark: str = ""  # '' | ' > ' | ' -> ' -- mark AFTER this song, into the next


@dataclass
class SetlistPrediction:
    showdate: str
    venue_name: str | None
    era: str
    model: str
    skeleton: dict[str, int]
    sets: dict[str, list[SetlistSong]] = field(default_factory=dict)

    def render(self, json_out: bool = False) -> str:
        """Render as JSON or a rich table per set (returned as text), mirroring
        predict.render_prediction's conventions (buffered Console, ASCII box
        so output survives cp1252 stdout on Windows when redirected)."""
        if json_out:
            payload = asdict(self)
            for songs in payload["sets"].values():
                for s in songs:
                    s["prob"] = round(s["prob"], 4)
            return json.dumps(payload)

        console = Console(record=True, width=120, file=io.StringIO())
        header = self.showdate
        if self.venue_name:
            header += f" - {self.venue_name}"
        header += f" | model={self.model}  era={self.era}"
        console.print(header)

        for label in _set_render_order(self.sets.keys()):
            songs = self.sets[label]
            console.print(f"\n{_set_title(label)}:")

            table = Table(box=box.ASCII)
            table.add_column("Song")
            table.add_column("Segue", justify="center")
            table.add_column("Slot")
            table.add_column("Prob", justify="right")
            for s in songs:
                table.add_row(s.song_name, s.segue_mark.strip() or "-", s.slot, f"{s.prob * 100:.1f}%")
            console.print(table)
            console.print(_flow_line(songs))

        return console.export_text()


def _set_render_order(labels) -> list[str]:
    """Main sets ascending numerically, encore label(s) last."""
    labels = list(labels)
    main = sorted((l for l in labels if not str(l).lower().startswith("e")), key=lambda l: int(l))
    encore = sorted(l for l in labels if str(l).lower().startswith("e"))
    return main + encore


def _set_title(label: str) -> str:
    if label.lower().startswith("e"):
        suffix = label[1:]
        return "Encore" if not suffix else f"Encore {suffix}"
    return f"Set {label}"


def _flow_line(songs: list[SetlistSong]) -> str:
    """Compact 'Tweezer -> Tweezer Reprise, Harry Hood' style summary line."""
    if not songs:
        return "(no songs)"
    parts = [songs[0].song_name]
    for i in range(1, len(songs)):
        mark = songs[i - 1].segue_mark.strip()
        parts.append(f" {mark} " if mark else ", ")
        parts.append(songs[i].song_name)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Structured sampler (§6c-i)
# ---------------------------------------------------------------------------
def _positions_by_set(skeleton: dict[str, int]) -> dict[str, list[tuple[int, int, str]]]:
    """label -> [(rank, set_len, slot_type), ...], in show order (main sets
    ascending, encore last). Reuses slots.classify_slot so the taxonomy here
    is identical to the one slot_propensities was estimated against."""
    order = _set_render_order(skeleton.keys())
    by_set: dict[str, list[tuple[int, int, str]]] = {}
    for label in order:
        length = skeleton[label]
        by_set[label] = [
            (rank, length, classify_slot(label, rank, length)) for rank in range(1, length + 1)
        ]
    return by_set


def _fill_skeleton(
    rng: np.random.Generator,
    skeleton: dict[str, int],
    probs: dict[int, float],
    slot_props: dict[int, dict[str, float]],
    predecessor_to_followers: dict[int, list[int]],
    followers_set: set[int],
) -> tuple[dict[tuple[str, int], int], dict[str, list[tuple[int, int, str]]]]:
    """Draw a songid for every (set_label, rank) slot in the skeleton.

    Fill order: within each set, opener/closer/encore slots first (most
    constrained -- fewest songs realistically fit there), then mid slots.
    Songs are drawn without replacement, weighted by
    P(song) * P(slot | song). Songs that are hard-paired followers are never
    drawn directly (excluded from the weighted pool); instead, right after
    their exact predecessor is placed, the follower is force-placed in the
    very next position of the same set (chains resolve iteratively), and if
    no room exists there the follower is simply dropped -- it never appears
    without its predecessor immediately before it.
    """
    by_set = _positions_by_set(skeleton)
    assigned: dict[tuple[str, int], int] = {}
    used: set[int] = set()

    def draw(slot_type: str) -> int | None:
        pool = [sid for sid in probs if sid not in used and sid not in followers_set]
        if not pool:
            return None
        weights = np.array(
            [probs[sid] * slot_props.get(sid, {}).get(slot_type, 0.0) for sid in pool],
            dtype=float,
        )
        if weights.sum() <= 0:
            weights = np.array([probs[sid] for sid in pool], dtype=float)
        if weights.sum() <= 0:
            weights = np.ones(len(pool))
        weights = weights / weights.sum()
        idx = rng.choice(len(pool), p=weights)
        return pool[idx]

    def try_force(label: str, rank: int, set_len: int, songid: int) -> None:
        current_rank, current_song = rank, songid
        while True:
            followers = predecessor_to_followers.get(current_song)
            if not followers or current_rank >= set_len:
                return
            next_rank = current_rank + 1
            if (label, next_rank) in assigned:
                return
            placed = next((f for f in followers if f not in used), None)
            if placed is None:
                return
            assigned[(label, next_rank)] = placed
            used.add(placed)
            current_rank, current_song = next_rank, placed

    for label, entries in by_set.items():
        anchors = [
            e for e in entries if e[2].endswith("open") or e[2].endswith("close") or e[2] == "encore"
        ]
        anchors.sort(key=lambda e: (0 if e[2].endswith("open") else 1 if e[2].endswith("close") else 2, e[0]))
        mids = sorted((e for e in entries if e not in anchors), key=lambda e: e[0])

        for rank, set_len, slot_type in anchors + mids:
            if (label, rank) in assigned:
                continue
            songid = draw(slot_type)
            if songid is None:
                continue
            assigned[(label, rank)] = songid
            used.add(songid)
            try_force(label, rank, set_len, songid)

    return assigned, by_set


def _build_sets(
    assigned: dict[tuple[str, int], int],
    by_set: dict[str, list[tuple[int, int, str]]],
    probs: dict[int, float],
    slugs: dict[int, str],
    names: dict[int, str],
    mark_lookup: dict[tuple[int, int], str],
    bigrams: dict[int, list[tuple[int, float]]],
    predecessor_to_followers: dict[int, list[int]],
) -> dict[str, list[SetlistSong]]:
    forced_pairs = {
        (prev, follower)
        for prev, followers in predecessor_to_followers.items()
        for follower in followers
    }

    sets: dict[str, list[SetlistSong]] = {}
    for label, entries in by_set.items():
        ordered = sorted(entries, key=lambda e: e[0])
        seq = [assigned.get((label, rank)) for rank, _l, _t in ordered]

        songs: list[SetlistSong] = []
        for i, (_rank, _set_len, slot_type) in enumerate(ordered):
            songid = seq[i]
            if songid is None:
                continue
            mark = ""
            if i < len(ordered) - 1 and seq[i + 1] is not None:
                nxt = seq[i + 1]
                is_bigram = any(cand == nxt for cand, _p in bigrams.get(songid, []))
                if (songid, nxt) in forced_pairs or is_bigram:
                    mark = mark_lookup.get((songid, nxt), " > ")
            songs.append(
                SetlistSong(
                    song_name=names.get(songid, str(songid)),
                    slug=slugs.get(songid, str(songid)),
                    songid=songid,
                    slot=slot_type,
                    prob=float(probs.get(songid, 0.0)),
                    segue_mark=mark,
                )
            )
        sets[label] = songs
    return sets


def sample_setlist(
    conn: sqlite3.Connection,
    showdate: str,
    *,
    half_life: int = 50,
    seed: int = 0,
    skeleton: dict[str, int] | None = None,
) -> SetlistPrediction:
    """Deterministic (given ``seed``) structured sampler -- plan §6c-i.

    1. Candidate P(song) via ``features.features_for_future_show`` +
       ``models.heuristic.heuristic_predict`` (reused from show-mode).
    2. Skeleton from ``skeleton`` if given, else ``slots.sample_set_structure``.
    3. Fill every slot, weighted by P(song) * P(slot|song), without
       replacement, honoring mined hard pairings and preferring mined segue
       bigrams for marks between adjacent songs (see ``_fill_skeleton`` /
       ``_build_sets``).

    Encore/opener/closer songs naturally skew toward historically "clean"
    (high encore-propensity) or jam-vehicle (high set2-mid-propensity) songs
    because that's exactly what P(slot|song) encodes from real data -- no
    separate heuristic is needed for plan §6c's "encore usually clean,
    set-2 heavy on jam vehicles" note.
    """
    show = _resolve_show(conn, showdate)
    venue = _resolve_venue(conn, show["venueid"])
    venue_name = venue["name"] if venue is not None else None
    year = int(str(show["showdate"])[:4])
    era = era_for_year(year)

    feat_df = features.features_for_future_show(conn, show["showid"], half_life)
    k = features.mean_setlist_size(conn, era)
    pred_df = heuristic_mod.heuristic_predict(feat_df, k)

    probs = {int(sid): float(p) for sid, p in zip(pred_df["songid"], pred_df["prob"])}
    slugs = {int(sid): slug for sid, slug in zip(pred_df["songid"], pred_df["slug"])}
    names = {int(sid): name for sid, name in zip(pred_df["songid"], pred_df["song_name"])}

    return _assemble_from_probs(
        conn,
        showdate=str(show["showdate"]),
        venue_name=venue_name,
        era=era,
        probs=probs,
        slugs=slugs,
        names=names,
        seed=seed,
        skeleton=skeleton,
    )


def _assemble_from_probs(
    conn: sqlite3.Connection,
    *,
    showdate: str,
    venue_name: str | None,
    era: str,
    probs: dict[int, float],
    slugs: dict[int, str],
    names: dict[int, str],
    seed: int,
    skeleton: dict[str, int] | None,
) -> SetlistPrediction:
    """Assembly core shared by ``sample_setlist`` (future-show path, probs
    from ``features_for_future_show``) and ``evaluate_sampler`` (backtest
    path, leakage-free contemporaneous probs from ``features.build_features``).

    Deterministic given ``seed``. Draws a skeleton if ``skeleton is None``,
    then fills every slot weighted by P(song) * P(slot|song) without
    replacement, honoring mined hard pairings and preferring mined segue
    bigrams for adjacency marks (see ``_fill_skeleton`` / ``_build_sets``).
    Everything from the RNG through the returned ``SetlistPrediction`` lives
    here so both callers share identical assembly semantics; only the source
    of ``probs`` differs.
    """
    rng = np.random.default_rng(seed)
    if skeleton is None:
        skeleton = sample_set_structure(conn, era, rng)

    props = slot_propensities(conn)

    stats = _compute_segue_stats(conn)
    pairings = _pairings_from_stats(stats, HARD_PAIRING_DOMINANCE, HARD_PAIRING_MIN_SUPPORT)
    bigrams = _bigrams_from_stats(stats, BIGRAM_MIN_SUPPORT)

    predecessor_to_followers: dict[int, list[int]] = defaultdict(list)
    for follower, predecessor in pairings.items():
        predecessor_to_followers[predecessor].append(follower)
    followers_set = set(pairings.keys())

    assigned, by_set = _fill_skeleton(
        rng, skeleton, probs, props, predecessor_to_followers, followers_set
    )
    sets = _build_sets(
        assigned, by_set, probs, slugs, names, stats.mark_lookup, bigrams, predecessor_to_followers
    )

    return SetlistPrediction(
        showdate=showdate,
        venue_name=venue_name,
        era=era,
        model="sampler",
        skeleton=dict(skeleton),
        sets=sets,
    )


# ---------------------------------------------------------------------------
# LLM assembler (§6c-ii) -- behind a flag at the CLI layer
# ---------------------------------------------------------------------------
SETLIST_SYSTEM_PROMPT = (
    "You are an expert Phish setlist assembler. You will be given a shortlist "
    "of candidate songs for one upcoming show, each with an estimated play "
    "probability and its historical slot propensities (where in a show it "
    "tends to be played: opener, mid-set, closer, or encore), plus the "
    "target show's 'skeleton' (how many songs go in each set/encore). "
    "Assemble a plausible, ORDERED setlist honoring: the skeleton's per-set "
    "song counts as closely as possible; songs with strong opener propensity "
    "should open sets; songs with strong closer/encore propensity should "
    "close sets or the encore; well-known segue pairs (a song that is almost "
    "always followed by one specific other song) should be placed adjacently "
    "with an appropriate segue mark. Use ONLY the given slugs -- never invent "
    "one. Do not repeat a slug. Respond only through the provided structured "
    "schema: a 'sets' object whose keys are the skeleton's set labels and "
    "whose values are ordered arrays of {slug, segue_mark}, where segue_mark "
    "is '' for a normal transition, ' > ' for a segue, or ' -> ' for a hard "
    "segue -- the mark AFTER this song, into the next one ('' for the last "
    "song of a set)."
)

SETLIST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "sets": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "segue_mark": {"type": "string"},
                    },
                    "required": ["slug", "segue_mark"],
                },
            },
        },
    },
    "required": ["sets"],
}


def _render_llm_candidates(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        top_slots = ", ".join(f"{slot}={p:.2f}" for slot, p in r["slot_props"])
        lines.append(f"{r['slug']} | name={r['name']} | prob={r['prob']:.3f} | slots: {top_slots}")
    return "\n".join(lines)


def _build_llm_user_prompt(showdate: str, skeleton: dict[str, int], candidates: list[dict]) -> str:
    lines = [f"Show date: {showdate}"]
    lines.append("Skeleton (set label -> song count): " + ", ".join(f"{k}={v}" for k, v in skeleton.items()))
    lines.append(f"Number of candidates: {len(candidates)}")
    lines.append("")
    lines.append("Candidates (slug | name | prob | slot propensities):")
    lines.append(_render_llm_candidates(candidates))
    return "\n".join(lines)


def _validate_setlist_payload(result) -> dict[str, list[dict]]:
    if not isinstance(result, dict) or "sets" not in result:
        raise LLMError(f"LLM setlist response missing 'sets' key: {result!r}")
    sets = result["sets"]
    if not isinstance(sets, dict):
        raise LLMError(f"'sets' must be an object, got {type(sets).__name__}: {sets!r}")

    out: dict[str, list[dict]] = {}
    for label, items in sets.items():
        if not isinstance(items, list):
            raise LLMError(f"sets[{label!r}] must be a list: {items!r}")
        cleaned = []
        for i, item in enumerate(items):
            if not isinstance(item, dict) or "slug" not in item:
                raise LLMError(f"sets[{label!r}][{i}] must have a 'slug' key: {item!r}")
            if not isinstance(item["slug"], str):
                raise LLMError(f"sets[{label!r}][{i}].slug must be a string: {item!r}")
            mark = item.get("segue_mark", "")
            cleaned.append({"slug": item["slug"], "segue_mark": mark if isinstance(mark, str) else ""})
        out[label] = cleaned
    return out


def assemble_setlist_llm(
    conn: sqlite3.Connection,
    showdate: str,
    client,
    *,
    half_life: int = 50,
    n_candidates: int = 40,
    skeleton: dict[str, int] | None = None,
) -> SetlistPrediction:
    """LLM-driven ordered assembly -- plan §6c-ii. ``client`` implements
    ``models.llm.LLMClient`` (injected so this is testable with a fake, no
    network in tests). Exactly ONE ``client.complete_json`` call is made for
    the whole show.

    The LLM sees the top ``n_candidates`` songs by P(song), each with its
    probability and top slot propensities, plus the target skeleton, and
    must emit an ordered setlist through ``SETLIST_SCHEMA`` (a 'sets' object
    of set-label -> ordered [{slug, segue_mark}]). Returned slugs are mapped
    back to songids (first against the candidate shortlist, then against the
    full `songs` table as a defensive fallback); unknown slugs and repeated
    slugs are dropped, and a resolved-but-uncandidated song's probability is
    floored to ``models.llm.FLOOR_PROB`` since we have no real P(song) for it.
    """
    show = _resolve_show(conn, showdate)
    venue = _resolve_venue(conn, show["venueid"])
    venue_name = venue["name"] if venue is not None else None
    year = int(str(show["showdate"])[:4])
    era = era_for_year(year)

    feat_df = features.features_for_future_show(conn, show["showid"], half_life)
    k = features.mean_setlist_size(conn, era)
    pred_df = heuristic_mod.heuristic_predict(feat_df, k)
    pred_df = pred_df.sort_values("prob", ascending=False).head(n_candidates)

    if skeleton is None:
        rng = np.random.default_rng(0)
        skeleton = sample_set_structure(conn, era, rng)

    props = slot_propensities(conn)

    slug_to_songid: dict[str, int] = {}
    probs: dict[int, float] = {}
    names: dict[int, str] = {}
    candidates_payload: list[dict] = []
    for _, row in pred_df.iterrows():
        songid = int(row["songid"])
        slug = row["slug"]
        slug_to_songid[slug] = songid
        probs[songid] = float(row["prob"])
        names[songid] = row["song_name"]
        top_slots = sorted(props.get(songid, {}).items(), key=lambda kv: -kv[1])[:3]
        candidates_payload.append(
            {"slug": slug, "name": row["song_name"], "prob": float(row["prob"]), "slot_props": top_slots}
        )

    # Defensive fallback: resolve a valid-but-uncandidated slug against the
    # full songs table rather than dropping it outright.
    global_slug_lookup = {r["slug"]: r["songid"] for r in conn.execute("SELECT slug, songid FROM songs")}

    user = _build_llm_user_prompt(str(show["showdate"]), skeleton, candidates_payload)
    try:
        result = client.complete_json(SETLIST_SYSTEM_PROMPT, user, SETLIST_SCHEMA)
    except LLMError:
        raise
    except Exception as exc:  # pragma: no cover - defensive wrap
        raise LLMError(f"LLM setlist call failed for {showdate}: {exc}") from exc

    sets_payload = _validate_setlist_payload(result)

    sets: dict[str, list[SetlistSong]] = {}
    used: set[int] = set()
    for label, items in sets_payload.items():
        resolved: list[tuple[int, str, str]] = []
        for item in items:
            slug = item["slug"]
            songid = slug_to_songid.get(slug, global_slug_lookup.get(slug))
            if songid is None or songid in used:
                continue  # unknown or duplicate slug -- drop
            used.add(songid)
            mark = item["segue_mark"] if item["segue_mark"] in ("", " > ", " -> ") else ""
            resolved.append((songid, slug, mark))

        set_len = len(resolved)
        songs: list[SetlistSong] = []
        for rank, (songid, slug, mark) in enumerate(resolved, start=1):
            name = names.get(songid)
            if name is None:
                row = conn.execute("SELECT name FROM songs WHERE songid = ?", (songid,)).fetchone()
                name = row["name"] if row is not None else slug
            songs.append(
                SetlistSong(
                    song_name=name,
                    slug=slug,
                    songid=songid,
                    slot=classify_slot(label, rank, set_len),
                    prob=probs.get(songid, FLOOR_PROB),
                    segue_mark=mark,
                )
            )
        sets[label] = songs

    provider = getattr(client, "provider", None) or "unknown"
    model_label = f"llm:{provider}:{client.model}"

    return SetlistPrediction(
        showdate=str(show["showdate"]),
        venue_name=venue_name,
        era=era,
        model=model_label,
        skeleton=dict(skeleton),
        sets=sets,
    )


# ---------------------------------------------------------------------------
# Sequence scoring (§6d)
# ---------------------------------------------------------------------------
def _flatten_predicted(predicted: SetlistPrediction) -> list[int]:
    order = _set_render_order(predicted.sets.keys())
    songs: list[int] = []
    for label in order:
        songs.extend(s.songid for s in predicted.sets[label])
    return songs


def _kendall_tau(seq_a: list[int], seq_b: list[int]) -> float | None:
    """Kendall's tau-a over the relative order of the elements common to both
    sequences (``seq_a``/``seq_b`` must already be restricted to that common
    set and be permutations of each other). None if fewer than 2 common
    songs (tau is undefined with 0-1 items to compare)."""
    n = len(seq_a)
    if n < 2 or n != len(seq_b):
        return None
    rank_b = {song: i for i, song in enumerate(seq_b)}
    order = [rank_b[song] for song in seq_a]
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            if (order[i] - order[j]) * (i - j) > 0:
                concordant += 1
            else:
                discordant += 1
    total = n * (n - 1) // 2
    return (concordant - discordant) / total if total else None


def _lcs_length(a: list[int], b: list[int]) -> int:
    """Longest common subsequence length via plain DP (no scipy dependency;
    setlists are short, O(n*m) is negligible)."""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        ai = a[i - 1]
        row_i, row_prev = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                row_i[j] = row_prev[j - 1] + 1
            else:
                row_i[j] = max(row_prev[j], row_i[j - 1])
    return dp[n][m]


def actual_setlist(conn: sqlite3.Connection, showid: int) -> list[int]:
    """Ordered songids actually played at ``showid``, by show-global
    ``position`` (includes repeats, e.g. a song played in both a set and the
    encore appears twice, matching how a real setlist reads)."""
    rows = conn.execute(
        "SELECT songid FROM performances WHERE showid = ? ORDER BY position", (showid,)
    ).fetchall()
    return [row["songid"] for row in rows]


def score_setlist(predicted: SetlistPrediction, actual_ordered_songids: list[int]) -> dict:
    """Honest scoring -- plan §6d explicitly rejects pretending 100%
    ordered-hit is attainable. Reports:

    - ``hit_at_k`` / ``hit_count`` / ``jaccard``: song-SET overlap (recall
      against the actual show, and Jaccard over the union).
    - ``kendall_tau``: relative-order agreement (-1..1) over just the songs
      common to both, i.e. "among the songs we got right, did we get the
      order right too" -- None if fewer than 2 songs are common.
    - ``lcs_len`` / ``lcs_ratio``: longest common subsequence length (and as
      a fraction of the actual show's length) between the FULL predicted and
      actual sequences (not restricted to the common set), so out-of-order
      insertions are penalized the way a real setlist reader would notice.
    - ``slot_accuracy``: coarse opener/closer match. ``actual_ordered_songids``
      is a flat list (no set boundaries), so this only checks the show's
      overall opener (index 0) and overall closer (index -1, typically the
      encore closer) -- documented as approximate, not a full slot-by-slot
      comparison.
    """
    pred_songs = _flatten_predicted(predicted)
    pred_set = set(pred_songs)
    actual_set = set(actual_ordered_songids)

    inter = pred_set & actual_set
    union = pred_set | actual_set

    k_predicted = len(pred_songs)
    k_actual = len(actual_ordered_songids)

    hit_at_k = (len(inter) / k_actual) if k_actual else 0.0
    jaccard = (len(inter) / len(union)) if union else 0.0

    common_pred = [s for s in pred_songs if s in actual_set]
    common_actual = [s for s in actual_ordered_songids if s in pred_set]
    tau = _kendall_tau(common_pred, common_actual)

    lcs_len = _lcs_length(pred_songs, actual_ordered_songids)
    lcs_ratio = (lcs_len / k_actual) if k_actual else 0.0

    checks: list[bool] = []
    if pred_songs and actual_ordered_songids:
        checks.append(pred_songs[0] == actual_ordered_songids[0])
        checks.append(pred_songs[-1] == actual_ordered_songids[-1])
    slot_accuracy = (sum(checks) / len(checks)) if checks else 0.0

    return {
        "k_predicted": k_predicted,
        "k_actual": k_actual,
        "hit_count": len(inter),
        "hit_at_k": hit_at_k,
        "jaccard": jaccard,
        "kendall_tau": tau,
        "lcs_len": lcs_len,
        "lcs_ratio": lcs_ratio,
        "slot_accuracy": slot_accuracy,
    }


def _actual_skeleton(conn: sqlite3.Connection, showid: int) -> dict[str, int]:
    """The show's REAL skeleton from ``performances``: set_label -> song count,
    collapsing all encore labels (e/e2/e3/...) into a single ``'e'`` key
    (matching how ``slots`` treats encores as one bucket) and keeping main
    labels ('1'..'4') as-is. Empty if the show has no performances."""
    rows = conn.execute(
        "SELECT set_label, COUNT(*) AS cnt FROM performances "
        "WHERE showid = ? AND set_label IS NOT NULL AND set_label != '' "
        "GROUP BY set_label",
        (showid,),
    ).fetchall()
    skeleton: dict[str, int] = defaultdict(int)
    for row in rows:
        label = str(row["set_label"]).strip().lower()
        key = "e" if label.startswith("e") else label
        skeleton[key] += row["cnt"]
    return dict(skeleton)


def evaluate_sampler(
    conn: sqlite3.Connection, showids: list[int], *, seed: int = 0, half_life: int = 50
) -> dict:
    """Aggregate ``score_setlist`` metrics over past shows to report the
    sampler's honest accuracy ceiling on real data (plan §9: the user expects,
    and accepts, a hard ceiling here).

    LEAKAGE-FREE: candidate probabilities come from
    ``features.build_features`` (one chronological, walk-forward sweep -- every
    feature for show T uses only shows with a smaller show_index), NOT from
    ``features.features_for_future_show`` (which collapses any indexed past
    show to "the day after all history" and so gives every past show the same
    prediction). ``build_features`` is called ONCE and sliced per target show.

    What this measures: song-selection + ordering quality GIVEN the correct
    set structure (each show is assembled against its OWN real skeleton, via
    ``_actual_skeleton``) and leakage-free contemporaneous candidate probs.
    It is therefore an UPPER BOUND on the end-to-end sampler, which in
    production must also guess the skeleton -- but it is an honest ceiling on
    the assembly logic itself, not a feature-reuse artifact.

    A show with no candidate rows in ``build_features`` (e.g. the very first
    indexed show, which has no prior history, or a future/unindexed showid) is
    correctly unscoreable and skipped -- right behavior, not a bug.
    """
    hist = features.build_features(conn, half_life=half_life)

    per_show: list[dict] = []
    for showid in showids:
        actual = actual_setlist(conn, showid)
        if not actual:
            continue
        g = hist[hist["showid"] == showid]
        if len(g) == 0:
            continue  # no leakage-free candidates (e.g. first show) -> unscoreable

        skeleton = _actual_skeleton(conn, showid)
        if not skeleton:
            continue

        year = int(str(g["showdate"].iloc[0])[:4])
        era = era_for_year(year)
        k = features.mean_setlist_size(conn, era)
        pred_df = heuristic_mod.heuristic_predict(g, k)

        probs = {int(sid): float(p) for sid, p in zip(pred_df["songid"], pred_df["prob"])}
        slugs = {int(sid): slug for sid, slug in zip(pred_df["songid"], pred_df["slug"])}
        names = {int(sid): name for sid, name in zip(pred_df["songid"], pred_df["song_name"])}

        predicted = _assemble_from_probs(
            conn,
            showdate=str(g["showdate"].iloc[0]),
            venue_name=None,
            era=era,
            probs=probs,
            slugs=slugs,
            names=names,
            seed=seed,
            skeleton=skeleton,
        )
        per_show.append(score_setlist(predicted, actual))

    if not per_show:
        return {"n_shows": 0}

    taus = [m["kendall_tau"] for m in per_show if m["kendall_tau"] is not None]
    return {
        "n_shows": len(per_show),
        "mean_hit_at_k": statistics.mean(m["hit_at_k"] for m in per_show),
        "mean_jaccard": statistics.mean(m["jaccard"] for m in per_show),
        "mean_kendall_tau": statistics.mean(taus) if taus else float("nan"),
        "mean_lcs_len": statistics.mean(m["lcs_len"] for m in per_show),
        "mean_lcs_ratio": statistics.mean(m["lcs_ratio"] for m in per_show),
        "mean_slot_accuracy": statistics.mean(m["slot_accuracy"] for m in per_show),
    }
