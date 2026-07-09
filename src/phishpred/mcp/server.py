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
         recent_setlists, run_context, heuristic_prediction
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
    """The candidate feature frame ``predict_show`` builds for a future show."""
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
    """The multi-night run a show belongs to, incl. already-played nights."""
    return tools.run_context(_get_conn(), showdate)


@mcp.tool()
def heuristic_prediction(showdate: str, half_life: int = 50, top: int = 30) -> dict[str, Any]:
    """The statistical heuristic baseline prediction for a show, so an agent
    can argue with it rather than starting from nothing."""
    return tools.heuristic_prediction(_get_conn(), showdate, half_life=half_life, top=top)


@mcp.tool()
def submit_prediction(
    showdate: str,
    model_label: str,
    predictions: list[dict[str, Any]],
    rationale: str | None = None,
) -> dict[str, Any]:
    """Submit per-song probabilities for a future show.

    ``predictions`` is a list of ``{"slug": str, "prob": float in (0, 1]}``.
    Unknown slugs and empty submissions are rejected. Probs are stored AS
    SUBMITTED and written to
    data/predictions/submitted/{model_label}/{showdate}.json for the next
    publish batch to fold in as source ``mcp:{model_label}``; at fold time they
    are published as submitted and scaled down only if their sum exceeds the
    show's expected setlist size K.
    """
    return tools.submit_prediction(
        showdate,
        model_label,
        predictions,
        rationale,
        conn=_get_conn(),
        out_dir=DEFAULT_SUBMIT_DIR,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
