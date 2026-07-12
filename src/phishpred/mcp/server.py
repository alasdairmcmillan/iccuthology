"""MCP server exposing phishpred's read-only prediction tools plus one write
tool (``submit_prediction``) over stdio, so an external agent (Claude
Desktop, antigravity, a local model behind an MCP bridge, ...) can explore
this project's Phish setlist data and submit a prediction. See deploy plan
§5 and DEPLOY-CONTRACTS.md §5 for the design/contract; docs/MCP.md for a
fuller walkthrough.

Point an MCP-capable client at this process over stdio. Example
``claude_desktop_config.json`` entry:

    {
      "mcpServers": {
        "phishpred": {
          "command": "python",
          "args": ["-m", "uv", "run", "--project", "D:/dev/iccuthology", "phishpred-mcp"]
        }
      }
    }

Or run it directly from the repo root:

    python -m uv run phishpred-mcp

Tools (deploy plan §5a):
  read:  upcoming_shows, candidate_features, song_history, venue_history,
         recent_setlists, run_context, heuristic_prediction, show_length_stats,
         scoreboard
  write: submit_prediction  (writes to data/predictions/submitted/, never to
         core tables -- see deploy plan §9: treat submissions as untrusted)

Importing this module never opens a DB connection or touches the network;
the connection is created lazily on first tool call.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import DATA_DIR, DB_PATH
from ..db import get_connection
from . import tools

DEFAULT_SUBMIT_DIR = DATA_DIR / "predictions" / "submitted"
DEFAULT_SCORECARDS_DIR = DATA_DIR / "scorecards"

mcp = FastMCP("phishpred")

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = get_connection(DB_PATH)
    return _conn


@mcp.tool()
def upcoming_shows(limit: int = 50) -> dict[str, Any]:
    """Future, non-excluded shows (showdate/venue) plus the current epoch."""
    return tools.upcoming_shows(_get_conn(), limit=limit)


@mcp.tool()
def candidate_features(showdate: str, half_life: int = 50, top: int = 50) -> dict[str, Any]:
    """The candidate feature frame ``predict_show`` builds for a future show.

    Ground rules: check each row's ``played_in_run`` (already played earlier
    this run -- essentially never repeats) and ``played_prev_show`` (played
    the immediately preceding show -- ~2% events) before predicting high
    probabilities. See docs/MCP.md "Ground rules".
    """
    return tools.candidate_features(_get_conn(), showdate, half_life=half_life, top=top)


@mcp.tool()
def song_history(slug: str, half_life: int = 50) -> dict[str, Any]:
    """Gaps, decayed play rate, per-era rates, and venue history for a song."""
    return tools.song_history(_get_conn(), slug, half_life=half_life)


@mcp.tool()
def venue_history(venue: str, top: int = 30) -> dict[str, Any]:
    """Songs that tend to get played at a venue (name/city substring match)."""
    return tools.venue_history(_get_conn(), venue, top=top)


@mcp.tool()
def recent_setlists(n: int = 10) -> dict[str, Any]:
    """The last n played shows' setlists, oldest first (tour context)."""
    return tools.recent_setlists(_get_conn(), n=n)


@mcp.tool()
def run_context(showdate: str) -> dict[str, Any]:
    """The multi-night run a show belongs to, incl. already-played nights.

    Use the already-played nights' setlists to rule out same-run repeats when
    predicting a later night -- see docs/MCP.md "Ground rules".
    """
    return tools.run_context(_get_conn(), showdate)


@mcp.tool()
def heuristic_prediction(showdate: str, half_life: int = 50, top: int = 30) -> dict[str, Any]:
    """The statistical heuristic baseline prediction for a show, so an agent
    can argue with it rather than starting from nothing."""
    return tools.heuristic_prediction(_get_conn(), showdate, half_life=half_life, top=top)


@mcp.tool()
def show_length_stats(years: int = 10) -> dict[str, Any]:
    """Songs-per-show averages over the last ``years`` calendar years.

    Calibration context for sizing your shortlist (20-40 songs) and its total
    probability mass: probs should sum near the expected setlist size, and
    ``avg_distinct_songs`` (~18-19 in the current era) is what your shortlist
    is actually scored against.
    """
    return tools.show_length_stats(_get_conn(), years=years)


@mcp.tool()
def scoreboard(model_label: str | None = None, recent: int = 5) -> dict[str, Any]:
    """Your track record + the heuristic baseline, so you can calibrate before
    submitting. Use this to compare yourself against the heuristic baseline: the
    per-model ``models`` aggregates include ``vs_heuristic`` (paired deltas vs the
    baseline) and ``avg_n_rows``, and ``recent_shows`` shows the last few scored
    shows' metrics/best_call/biggest_whiff for the heuristic plus your own track.

    Pass ``model_label`` (your scoreboard identity, WITHOUT the ``mcp:`` prefix)
    to see your own track alongside the heuristic; omit it for the baseline only.
    Leakage-safe: scorecards only exist for already-played shows.
    """
    return tools.scoreboard(DEFAULT_SCORECARDS_DIR, model_label=model_label, recent=recent)


@mcp.tool()
def submit_prediction(
    showdate: str,
    model_label: str,
    predictions: list[dict[str, Any]],
    rationale: str | None = None,
    setlist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit per-song probabilities for a future show.

    ``predictions`` is a list of ``{"slug": str, "prob": float in (0, 1]}`` with
    between 20 and 40 songs. Unknown slugs, empty submissions, and shortlists
    outside 20–40 songs are rejected. Probs are stored AS SUBMITTED and written to
    data/predictions/submitted/{model_label}/{showdate}.json for the next
    publish batch to fold in as source ``mcp:{model_label}``; at fold time they
    are published as submitted and scaled down only if their sum exceeds the
    show's expected setlist size K.

    ``setlist`` is an OPTIONAL structured setlist call scored as a SECOND
    benchmark (set placement + marquee slots + exact positions), independent of
    ``predictions``. Shape: ``{"sets": {"1": [slug, ...], "2": [...],
    "e": [...]}}`` — set labels match ``^(\\d+|e\\d*)$``, each a non-empty list
    of known slugs, no slug repeated anywhere, <= 40 songs total. Omit it to sit
    out the setlist benchmark.

    Resubmitting for the same show preserves prior takes: the previous file's
    content is folded into a ``versions`` array so the UI can show the
    improvement arc; official metrics use only the latest take.

    Ground rules: don't submit high probabilities for a song already flagged
    ``played_in_run``/``played_prev_show`` in ``candidate_features``, and if
    submitting for multiple nights of one run, keep the submissions jointly
    consistent (discount a song for later nights once it's predicted high for
    an earlier one) -- see docs/MCP.md "Ground rules".
    """
    return tools.submit_prediction(
        showdate,
        model_label,
        predictions,
        rationale,
        setlist=setlist,
        conn=_get_conn(),
        out_dir=DEFAULT_SUBMIT_DIR,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
