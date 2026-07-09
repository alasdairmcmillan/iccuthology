# phishpred-mcp

A local, read-only MCP server (plus one write tool) over the Phish predictor's
SQLite database. It lets an external agent — Claude Desktop, antigravity,
Cline, a local model behind an MCP bridge — explore the same data
`phishpred predict` / `phishpred tour` / etc. use, and submit a per-song
prediction for a future show. See `phish-predictor-deploy-plan.md` §5 for the
full design and `DEPLOY-CONTRACTS.md` §5 for the submission file schema.

## Running it

From the repo root (`D:\dev\iccuthology`):

```
python -m uv run phishpred-mcp
```

This starts the server on stdio (the `mcp` SDK's default transport for local
clients) using `data/phish.db` (via `phishpred.config.DB_PATH`). It never
touches the network on its own; it only reads/writes the local SQLite DB and
the `data/predictions/submitted/` directory.

## Pointing Claude Desktop at it

Add an entry to Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "phishpred": {
      "command": "python",
      "args": [
        "-m", "uv", "run",
        "--project", "D:/dev/iccuthology",
        "phishpred-mcp"
      ]
    }
  }
}
```

Restart Claude Desktop; `phishpred` should show up as a connected MCP server
with the tools listed below available in chat.

Other MCP-capable clients (antigravity, Cline, etc.) work the same way —
point them at the `phishpred-mcp` command (or `python -m uv run phishpred-mcp`)
over stdio.

## Tools

All read tools are leakage-safe: they only ever see history as-of the
current data state (the same guarantee `predict_show` / `features.py`
enforce for the CLI), so an agent can't accidentally "see the future."

| Tool | Purpose |
|------|---------|
| `upcoming_shows(limit=50)` | Future, non-excluded shows (date/venue/tour) plus the current publish epoch. |
| `candidate_features(showdate, half_life=50, top=50)` | The exact feature frame `predict_show` builds for a future show, compacted (decayed_rate, gap, gap_ratio, played_prev_show, played_in_run, venue_gap, plays_this_tour/last_10/last_50, song_age_shows, era_rate, is_original per candidate song). |
| `song_history(slug, half_life=50)` | Historical play count, current gap, median historical gap, current decayed rate, per-era play rates, and venue-by-venue play history for one song. |
| `venue_history(venue, top=30)` | Songs that tend to get played at a venue (name/city substring match), with play counts and play rate. |
| `recent_setlists(n=10)` | The last `n` played shows' setlists, oldest first, for tour context. |
| `run_context(showdate)` | The multi-night run a show belongs to (same venue, contiguous), including already-played nights' setlists and still-future nights. |
| `heuristic_prediction(showdate, half_life=50, top=30)` | The statistical heuristic baseline (`predict_show`, model="heuristic") as JSON, so the agent has something concrete to agree or disagree with. |
| `submit_prediction(showdate, model_label, predictions, rationale=None)` | **Write tool.** Submits per-song probabilities for a future show. |

### `submit_prediction`

```
submit_prediction(
    showdate: str,             # "2026-07-10"
    model_label: str,          # becomes source key "mcp:<model_label>" downstream
    predictions: [{"slug": str, "prob": float}],  # prob in (0, 1]
    rationale: str | None = None,   # optional narrative, e.g. "Fluffhead is due"
)
```

Behavior:
- Rejects empty submissions, unknown slugs, duplicate slugs, and
  out-of-range/non-numeric probabilities with a clear `ValueError`.
- Stores the probabilities AS SUBMITTED (validated, rounded to 4 decimals) —
  no renormalization at write time. At publish fold time they are clamped to
  <=0.99 and scaled *down* only if their sum exceeds the show's expected
  setlist size K, never scaled up, so a sparse shortlist keeps the
  probabilities the agent actually stated.
- Stamps the submission with the published epoch read from
  `data/predictions/latest.json` (the pointer synced from R2 — i.e. the epoch
  of the snapshot the agent was actually looking at), falling back to a
  locally recomputed epoch (`phishpred.epoch.compute_epoch`), or `null` if
  neither is available; plus a `submitted_at` UTC timestamp.
- Writes `data/predictions/submitted/{model_label}/{showdate}.json`
  (`model_label` sanitized to safe filename characters for the directory
  name; the JSON's `model_label` field keeps your original string).
- Never writes to any core table — only to the submissions inbox.

Example JSON written (matches DEPLOY-CONTRACTS.md §5):

```json
{
  "model_label": "claude-desktop",
  "showdate": "2026-07-10",
  "epoch": "a1b2c3d4e5f6",
  "submitted_at": "2026-07-09T13:00:00Z",
  "rationale": "Fluffhead is due; last played 3 tours ago and the venue has a history of it.",
  "predictions": [
    {"slug": "harry-hood", "prob": 0.55},
    {"slug": "fluffhead", "prob": 0.18}
  ]
}
```

## How submissions flow into publish

Submitted files land in `data/predictions/submitted/{model_label}/{showdate}.json`.
That directory is git-ignored and local — for a submission to reach the deployed
site it must be pushed to the R2 `submitted/` prefix, which the scheduled publish
workflow pulls at the start of every run:

```
python -m uv run python scripts/r2_push.py data/predictions/submitted submitted
```

(Requires the four `R2_*` env vars — see `docs/DEPLOY.md` §2-§3. A new submission
changes the epoch's `submitted_manifest_hash`, so the next scheduled run republishes
automatically.)

Publish then reads the submissions inbox, re-validates each file (known slugs,
probs in (0,1], no duplicate slugs, safe label; probs published as submitted and
only scaled down if their sum exceeds K), resolves slug → song_name, and folds it
into `show/{showdate}.json` under `sources["mcp:" + model_label]`, right alongside
the `heuristic`/`lr`/`gbm`/`llm:*` sources. The web UI then renders every source as
a comparable column, and the backtest can score any of them once the real setlist
posts.

## Testing

`tests/test_mcp.py` unit-tests `phishpred.mcp.tools` directly against a
small in-memory DB — no live MCP session, no network. Run with:

```
python -m uv run pytest tests/test_mcp.py -q
```
