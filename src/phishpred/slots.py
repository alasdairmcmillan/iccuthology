"""Slot / set-structure model — see phish-predictor-modes-plan.md §6a-6b.

Two independent pieces:

1. **Slot propensities** — for each song, P(slot | song played), estimated from
   history. ``performances.position`` is ordered across the WHOLE show (not
   per set), so we first regroup by (showid, set_label), sort by position, and
   derive each performance's rank within its own set and that set's length.

2. **Set-structure model** — the show "skeleton": how many sets, how long each
   one runs, and the encore length, estimated per era from history, plus a
   sampler that draws a plausible skeleton for one hypothetical show.

No leakage concerns here (unlike features.py): these are aggregate historical
statistics, not per-show chronological features, so there is no walk-forward
requirement.
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import Counter, defaultdict

import numpy as np

from .config import ERAS, era_for_year

# ---------------------------------------------------------------------------
# Slot taxonomy
# ---------------------------------------------------------------------------

SLOTS = [
    "set1-open", "set1-mid", "set1-close",
    "set2-open", "set2-mid", "set2-close",
    "set3-open", "set3-mid", "set3-close",
    "encore",
]


def classify_slot(set_label: str, rank_in_set: int, set_len: int) -> str:
    """Classify one performance's slot given its rank within its set.

    Rule: rank == 1 -> ``*-open``; rank == set_len -> ``*-close``; else
    ``*-mid``. The open check is evaluated first, so a single-song set
    (rank == 1 == set_len) always resolves to ``*-open``, never ``*-close`` —
    a deliberate, documented tie-break, not an accident of dict ordering.

    Encore-type labels (``e``, ``e2``, ``e3``, ...  i.e. anything starting
    with ``e``) always map to ``"encore"`` regardless of rank/length — an
    encore is treated as one undifferentiated bucket, not open/mid/close,
    since encores are short and position within them isn't a meaningful
    signal the way it is within a full set.

    Set label ``'4'`` (rare — a handful of very old, unusually long shows)
    has no dedicated bucket; it folds into the ``set3-*`` bucket. Rationale:
    by the time a show reaches a 4th set it's already an outlier, and giving
    it its own near-empty bucket would just fragment the sparse data with no
    modeling benefit; "the last main set before the encore" is closer in
    spirit to set3 than to set1/set2.
    """
    label = str(set_label).strip().lower()
    if label.startswith("e"):
        return "encore"
    try:
        set_num = int(label)
    except ValueError:
        # Defensive: any other unexpected non-numeric, non-encore label.
        # Not expected given the confirmed data facts (labels are '1'..'4' or
        # e/e2/e3), but fold into set3 rather than raising, matching the
        # set-'4' choice above.
        set_num = 3
    if set_num == 1:
        base = "set1"
    elif set_num == 2:
        base = "set2"
    else:
        base = "set3"  # 3, 4, or anything higher

    if rank_in_set == 1:
        return f"{base}-open"
    if rank_in_set == set_len:
        return f"{base}-close"
    return f"{base}-mid"


# ---------------------------------------------------------------------------
# Shared DB access
# ---------------------------------------------------------------------------

def _show_set_groups(conn: sqlite3.Connection):
    """Yield (showid, show_year, set_label, songids_in_position_order) for
    every (show, set_label) group of a non-excluded show that has at least
    one performance. Performances with no set_label (NULL/empty) are skipped
    -- shouldn't occur per the confirmed data facts, but we don't want a
    stray NULL group silently polluting a bucket.
    """
    rows = conn.execute(
        "SELECT p.showid AS showid, p.songid AS songid, p.set_label AS set_label, "
        "p.position AS position, s.showdate AS showdate "
        "FROM performances p JOIN shows s ON s.showid = p.showid "
        "WHERE s.exclude = 0 AND p.set_label IS NOT NULL AND p.set_label != '' "
        "ORDER BY p.showid, p.set_label, p.position"
    ).fetchall()

    groups: dict[tuple[int, str], list[int]] = defaultdict(list)
    show_years: dict[int, int] = {}
    for row in rows:
        showid = row["showid"]
        show_years[showid] = int(str(row["showdate"])[:4])
        groups[(showid, row["set_label"])].append(row["songid"])

    for (showid, set_label), songids in groups.items():
        yield showid, show_years[showid], set_label, songids


# ---------------------------------------------------------------------------
# Per-song slot propensities
# ---------------------------------------------------------------------------

# Discrete era-bucket weighting scheme (used when era_weighted=True and no
# half_life_years is given): each era's performances count for 2x the era
# before it, so era "4.0" (most recent) outweighs era "1.0" 16:1. Simple,
# monotone in recency, and easy to reason about/test.
_ERA_WEIGHTS: dict[str, float] = {name: 2.0 ** i for i, (name, _, _) in enumerate(ERAS)}


def _performance_weight(
    show_year: int, anchor_year: int, era_weighted: bool, half_life_years: float | None
) -> float:
    """Weight for one performance, applied uniformly to every song in that
    show/set (the weight only depends on when the show happened).

    - ``era_weighted=False``: weight 1.0 always (raw counts/fractions).
      ``half_life_years`` is ignored in this mode.
    - ``era_weighted=True`` and ``half_life_years`` given: continuous
      exponential decay in calendar years, anchored to the most recent show
      year present in the queried data: ``0.5 ** (years_ago / half_life_years)``.
    - ``era_weighted=True`` and ``half_life_years=None`` (default): discrete
      per-era weight from ``_ERA_WEIGHTS``.
    """
    if not era_weighted:
        return 1.0
    if half_life_years is not None:
        years_ago = max(0.0, anchor_year - show_year)
        return 0.5 ** (years_ago / half_life_years)
    return _ERA_WEIGHTS[era_for_year(show_year)]


def slot_counts(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """Raw observed (unweighted) per-song, per-slot performance counts.

    songid -> {slot: count}. Only non-excluded shows with performances.
    """
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _showid, _year, set_label, songids in _show_set_groups(conn):
        set_len = len(songids)
        for rank, songid in enumerate(songids, start=1):
            slot = classify_slot(set_label, rank, set_len)
            counts[songid][slot] += 1
    return {songid: dict(slots) for songid, slots in counts.items()}


def slot_propensities(
    conn: sqlite3.Connection,
    *,
    era_weighted: bool = True,
    half_life_years: float | None = None,
) -> dict[int, dict[str, float]]:
    """songid -> {slot: P(slot | song played)}.

    Sums to ~1 per song, over the slots it has actually appeared in. Only
    non-excluded shows with performances are considered; only songs with
    >=1 observed play are returned (songs with zero plays never enter the
    weighted-count map in the first place).

    See ``_performance_weight`` for the era_weighted / half_life_years
    semantics.
    """
    groups = list(_show_set_groups(conn))
    if not groups:
        return {}
    anchor_year = max(year for _showid, year, _set_label, _songids in groups)

    weighted: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for _showid, year, set_label, songids in groups:
        w = _performance_weight(year, anchor_year, era_weighted, half_life_years)
        set_len = len(songids)
        for rank, songid in enumerate(songids, start=1):
            slot = classify_slot(set_label, rank, set_len)
            weighted[songid][slot] += w

    result: dict[int, dict[str, float]] = {}
    for songid, slot_weights in weighted.items():
        total = sum(slot_weights.values())
        if total <= 0:
            continue
        result[songid] = {slot: wt / total for slot, wt in slot_weights.items()}
    return result


# ---------------------------------------------------------------------------
# Set-structure model
# ---------------------------------------------------------------------------

def _show_structures(conn: sqlite3.Connection, era: str | None):
    """Yield (showid, show_year, {set_label: length}) per non-excluded show
    with performances, optionally restricted to ``era``."""
    rows = conn.execute(
        "SELECT p.showid AS showid, p.set_label AS set_label, "
        "s.showdate AS showdate "
        "FROM performances p JOIN shows s ON s.showid = p.showid "
        "WHERE s.exclude = 0 AND p.set_label IS NOT NULL AND p.set_label != '' "
        "ORDER BY p.showid"
    ).fetchall()

    shows: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    show_years: dict[int, int] = {}
    for row in rows:
        showid = row["showid"]
        show_years[showid] = int(str(row["showdate"])[:4])
        shows[showid][row["set_label"]] += 1

    for showid, set_lengths in shows.items():
        year = show_years[showid]
        if era is not None and era_for_year(year) != era:
            continue
        yield showid, year, dict(set_lengths)


def _summary(values: list[int]) -> dict:
    if not values:
        return {"mean": 0.0, "std": 0.0, "hist": Counter()}
    return {
        "mean": statistics.mean(values),
        "std": statistics.pstdev(values),
        "hist": Counter(values),
    }


def set_structure_stats(conn: sqlite3.Connection, era: str | None = None) -> dict:
    """Summarize the show skeleton, optionally restricted to one era.

    Returns::

        {
          "n_shows": int,
          "num_sets_dist": Counter,       # count of distinct MAIN set labels
                                           # ('1','2','3','4',...) per show
          "num_encores_dist": Counter,    # count of distinct encore labels
                                           # ('e','e2','e3') per show
          "set_lengths": {
              "1": {"mean": float, "std": float, "hist": Counter},
              "2": {...}, "3": {...}, "4": {...},   # only labels observed
              "encore": {...},            # combined e+e2+e3 length per show
          },
        }

    Encore length is the *combined* song count across all of a show's encore
    labels (e.g. e + e2), matching how ``classify_slot`` treats all encores as
    one bucket -- a show with a 1-song 'e' and a 1-song 'e2' contributes a
    single encore length of 2, not two separate lengths of 1.
    Only non-excluded shows with performances are considered.
    """
    main_lengths: dict[str, list[int]] = defaultdict(list)
    encore_lengths: list[int] = []
    num_sets_counter: Counter = Counter()
    num_encores_counter: Counter = Counter()
    n_shows = 0

    for _showid, _year, set_lengths in _show_structures(conn, era):
        n_shows += 1
        main_labels = [lbl for lbl in set_lengths if not lbl.lower().startswith("e")]
        encore_labels = [lbl for lbl in set_lengths if lbl.lower().startswith("e")]

        num_sets_counter[len(main_labels)] += 1
        num_encores_counter[len(encore_labels)] += 1

        for lbl in main_labels:
            main_lengths[lbl].append(set_lengths[lbl])
        if encore_labels:
            encore_lengths.append(sum(set_lengths[lbl] for lbl in encore_labels))

    set_lengths_summary = {lbl: _summary(vals) for lbl, vals in main_lengths.items()}
    set_lengths_summary["encore"] = _summary(encore_lengths)

    return {
        "n_shows": n_shows,
        "num_sets_dist": num_sets_counter,
        "num_encores_dist": num_encores_counter,
        "set_lengths": set_lengths_summary,
    }


def _sample_from_counter(counter: Counter, rng: np.random.Generator) -> int:
    """Draw one value from an empirical histogram, weighted by its counts."""
    if not counter:
        return 0
    values = list(counter.keys())
    weights = np.array([counter[v] for v in values], dtype=float)
    weights = weights / weights.sum()
    idx = rng.choice(len(values), p=weights)
    return int(values[idx])


def sample_set_structure(conn: sqlite3.Connection, era: str, rng: np.random.Generator) -> dict[str, int]:
    """Sample a plausible show skeleton for ``era``: set_label -> song count.

    Draws the number of main sets from ``num_sets_dist``, then each main
    set's length independently from its own empirical histogram in
    ``set_lengths``, then (independently) whether/how long the encore is from
    ``num_encores_dist`` / the combined ``"encore"`` length histogram.
    Deterministic given ``rng``'s state (same seed -> same draw).

    Simplifications (documented, not hidden):
    - Main sets sampled this way are assumed labeled contiguously '1'..'n'
      (the overwhelmingly common case: sets 1, 2, maybe 3). The rare
      non-contiguous case (e.g. sets '1','2','4' with no '3', from old
      shows) is not reproduced exactly -- if a set-length histogram for a
      contiguous label like '3' has no observations in `era`, that label is
      simply omitted from the sampled skeleton rather than raising.
    - All encore labels (e/e2/e3) are collapsed into a single 'e' key
      carrying the combined encore length, matching ``set_structure_stats``'
      combined encore-length distribution. We don't attempt to reconstruct
      how a multi-encore night split across e/e2/e3.
    """
    stats = set_structure_stats(conn, era=era)
    n_sets = _sample_from_counter(stats["num_sets_dist"], rng)
    n_encores = _sample_from_counter(stats["num_encores_dist"], rng)

    skeleton: dict[str, int] = {}
    for i in range(1, n_sets + 1):
        label = str(i)
        summary = stats["set_lengths"].get(label)
        length = _sample_from_counter(summary["hist"], rng) if summary else 0
        if length > 0:
            skeleton[label] = length

    if n_encores > 0:
        enc_summary = stats["set_lengths"].get("encore")
        enc_len = _sample_from_counter(enc_summary["hist"], rng) if enc_summary else 0
        if enc_len > 0:
            skeleton["e"] = enc_len

    return skeleton
