"""Model-track lifecycle status (DEPLOY-CONTRACTS.md §8 ``models[*].status``).

The scoreboard discovers its models dynamically — every source key that appears
in any scorecard becomes an entry — so there is no roster to remove a track
from. Retiring one is therefore a *status*, not a deletion: the scored record
stays exactly where it is and gains a marker explaining why it stopped.

This module is that marker's only source of truth. Editing ``ELIMINATED`` here
is the whole operation; ``build_scoreboard`` stamps the fields onto each model
entry and the UI renders them.

Elimination bar (deliberately high — two conditions, BOTH required):

1. The track performs really poorly against the statistical baseline on its
   scored record, and
2. it is failing the actual task — producing a genuinely reasoned hypothetical
   setlist that tries to beat the heuristic.

Condition 2 alone is not enough, and this is the trap worth naming: judged on
submission-time forensics alone, ``gemini-flash`` looked mechanical (one
probability ladder reused across a 3-night run) while scoring level with the
heuristic on hit rate and 2nd of six on weighted score. A formula signature
describes how output was produced; it does not establish that a track is
failing. Condition 1 alone is not enough either — a track that reasons honestly
and still loses to the baseline is a calibration problem to work on, not a dead
track.
"""
from __future__ import annotations

from typing import Any

# Scoreboard keys carry a source-kind prefix (``mcp:claude-opus``); the registry
# is keyed on the bare model_label, so strip these before lookup.
_KEY_PREFIXES = ("mcp:", "llm:")

# label -> {"at": ISO date the track was retired, "reason": one line for the UI}
# Keep ``reason`` short and factual: it renders in a popover on the standings
# board, so it is public-facing text about a model's performance.
ELIMINATED: dict[str, dict[str, str]] = {
    "claude-haiku": {
        "at": "2026-08-18",
        "reason": (
            "Last of six tracks on every scored metric (hit rate -0.0700 and "
            "recall -0.1309 against the heuristic), and submitted formula "
            "output rather than per-show reasoning: three fall-tour shows at "
            "three different venues shared one identical probability vector, "
            "slug list and setlist. A resubmission repeated the failure, "
            "giving all 35 songs on one show the same probability."
        ),
    },
}


def bare_label(model_key: str) -> str:
    """``mcp:claude-haiku`` -> ``claude-haiku``; unprefixed keys pass through."""
    for prefix in _KEY_PREFIXES:
        if model_key.startswith(prefix):
            return model_key[len(prefix):]
    return model_key


def status_fields(model_key: str) -> dict[str, Any]:
    """Status fields to merge into a ``scoreboard.json`` ``models[*]`` entry.

    Active tracks get ``{"status": "active"}`` — always written, so a consumer
    never has to distinguish "active" from "this artifact predates the field"
    (``web/src/api.ts`` still defaults absent -> active for already-published
    artifacts, which cannot be rewritten retroactively).
    """
    entry = ELIMINATED.get(bare_label(model_key))
    if entry is None:
        return {"status": "active"}
    return {
        "status": "eliminated",
        "eliminated_at": entry["at"],
        "eliminated_reason": entry["reason"],
    }
