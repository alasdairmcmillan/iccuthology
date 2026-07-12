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

## Ground rules

Phish essentially never repeats a song within a multi-night same-venue run,
and only rarely repeats the song that closed the immediately previous show.
The statistical models (`heuristic`, `lr`/`gbm`, the simulator) enforce this
automatically, but an external agent driving this MCP server has to respect
it explicitly -- nothing stops you from proposing a jointly-impossible
setlist across nights. When predicting or submitting, follow these rules:

1. **No repeats within a run.** Before assigning a high probability, check
   `candidate_features`' `played_in_run` flag (or cross-reference
   `run_context`'s already-played nights). A song with `played_in_run=1` has
   effectively ~0-5% probability of repeating that same run.
2. **Previous-night repeats are rare.** Check `played_prev_show`; a song that
   closed the immediately preceding show has only ~2% odds of repeating the
   very next night.
3. **Multi-night predictions must be jointly consistent.** If you're
   predicting or submitting for more than one show in the same run, a song
   you gave a high probability for night 1 should be heavily discounted for
   nights 2-3 of that run (and vice versa) -- don't independently maximize
   each night's prediction as if the others didn't exist.

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
| `show_length_stats(years=10)` | Songs-per-show averages (overall + by year) over the last `years` calendar years, anchored on the latest played show. Calibration context: `avg_distinct_songs` (~18–19 in the current era) is what a shortlist is scored against; a shortlist's probs sum to its expected hits (~6–9 for a 30-song list), never the full show size. |
| `slot_propensities(slugs)` | Batch set-position tendencies: per song, P(slot \| played) over `set{1,2,3}-{open,mid,close}`/`encore` (era-weighted) plus the current era's set-structure stats (sets per show, songs per set). The data behind placement scoring — check it before calling openers/closers/encores. Unknown slugs come back under `unknown_slugs`. |
| `backtest_shortlist(slugs, n_shows=20)` | Score a hypothetical shortlist against the last `n_shows` played shows: per-show hits/hit_rate/recall, means, and per-slug hit counts. Test a working hypothesis before submitting it. Leakage-free (played history only); remember rotation — past-window frequency ≠ next-show probability. |
| `scoreboard(model_label=None, recent=5)` | Your own track record + the heuristic baseline from the published scorecards tier, so you can calibrate before submitting. Returns the per-model aggregate `models` (incl. `avg_n_rows` and `vs_heuristic`) and a compact `recent_shows` summary. Leakage-safe (scorecards only exist for already-played shows). |
| `submit_prediction(showdate, model_label, predictions, rationale=None, setlist=None)` | **Write tool.** Submits per-song probabilities (20–40 songs) and a structured setlist call for a future show. |

### `submit_prediction`

```
submit_prediction(
    showdate: str,             # "2026-07-10"
    model_label: str,          # becomes source key "mcp:<model_label>" downstream
    predictions: [{"slug": str, "prob": float}],  # prob in (0, 1]
    rationale: str | None = None,   # optional narrative, e.g. "Fluffhead is due"
    setlist: {"sets": {"1": [slug, ...], "2": [...], "e": [...]}} | None = None,
)
```

`setlist` is a structured full-setlist call, scored as a SECOND benchmark
(DEPLOY-CONTRACTS.md §8: hits, set placement, opener/closer marquee calls,
exact positions) independent of `predictions`. Set labels must match
`^(\d+|e\d*)$`, each set is a non-empty ordered list of known slugs, no slug
may appear twice anywhere, and the total is capped at 40 songs. Technically
optional (omitting it just sits out the setlist benchmark), but **every live
model track should submit one** — the scoreboard's two-benchmark scorecards
only work if models actually call setlists. See the agent playbook below.

Behavior:
- Rejects empty submissions, unknown slugs, duplicate slugs,
  out-of-range/non-numeric probabilities, and a shortlist shorter than 20 or
  longer than 40 songs with a clear `ValueError`; an invalid `setlist` (bad set
  label, unknown/duplicate slug, >40 songs) also raises, before anything is
  written. The 20–40 shortlist bound is an MCP-submission rule only — it does
  not touch `heuristic_prediction` or the publish pipeline.
- Resubmitting for the same `{model_label}/{showdate}` never loses history:
  the prior file's content is folded into the new file's `versions` array
  (oldest first, at most 10 priors kept), so the scorecard can show the
  improvement arc across takes. Official metrics use only the latest take.
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
  ],
  "setlist": {
    "sets": {
      "1": ["free", "rift", "fluffhead", "..."],
      "2": ["down-with-disease", "harry-hood", "..."],
      "e": ["slave-to-the-traffic-light"]
    }
  }
}
```

### `scoreboard`

```
scoreboard(
    model_label: str | None = None,   # your track, WITHOUT the "mcp:" prefix
    recent: int = 5,                   # how many recent scored shows to summarize
)
```

Your own track record plus the heuristic baseline, so you can **compare yourself
against the heuristic before submitting** and calibrate. Reads the published
scorecards tier (leakage-safe: scorecards only exist for already-played shows).
Returns:

- `models` — the `scoreboard.json` `models` mapping: per-model aggregate metrics
  (`hit_rate_top20`, `recall`, `brier`, `log_loss`), the mean shortlist length
  `avg_n_rows`, and for non-heuristic models `vs_heuristic` (paired mean deltas
  vs the baseline over shows both scored).
- `recent_shows` — for the most recent `recent` scored shows (showdate DESC), a
  compact summary: `showdate`, `venue_name`, `n_played`, `missed_by_all`, and for
  the `heuristic` plus (when `model_label` is given) your `mcp:{model_label}`
  source, that source's `metrics`/`best_call`/`biggest_whiff`. Full row lists are
  omitted to keep the payload small.

A missing `scoreboard.json` / empty scorecards dir yields empty `models` /
`recent_shows` (nothing scored yet), never an error.

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

## Agent playbook — driving a live model track

This is the canonical recipe for having an external agent (Antigravity,
Claude Desktop, Cline, ...) submit a scoreboard-grade prediction. It exists
because the first live Antigravity run (2026-07-11) hit both failure modes
this section guards against: it submitted under the wrong `model_label`, and
the submission was never pushed to R2 so it never published. The future
automated pipeline (`docs/llm-auto-predictor-plan.md`) should follow the same
contract.

A condensed, harness-facing version of this playbook lives in the repo-root
`AGENTS.md` (the file agentic CLIs like agy/Codex auto-read), including a
direct-python fallback for harnesses without MCP configured. Keep the two in
sync.

### Model label rules

The label is the model's **permanent scoreboard identity** — it becomes the
source key `mcp:<label>` and a row on the public scoreboard.

1. Name the **model doing the reasoning**, not the client/IDE. `gemini-3-pro`,
   not `antigravity`; `claude-opus`, not `claude-desktop`.
2. Kebab-case, `[A-Za-z0-9_-]` only (anything else is sanitized into the
   directory name anyway).
3. **Reuse the existing label exactly** on every subsequent submission —
   a typo mints a new competitor instead of a new version. Existing tracks:
   `claude-fable`, `claude-sonnet`, `claude-opus`, `claude-haiku`,
   `gemini-3.5-flash-high`, `gemini-3.1-pro`. Every track is live per-show
   model reasoning: the flash track's original static-formula takes
   (`scripts/make_predictions.py`, now retired) were removed from its
   version history on 2026-07-11 when the model re-submitted with real
   reasoning. A different model is a different track — Gemini 3.1 Pro
   submits as `gemini-3.1-pro`, never under the flash label.
4. Pin the label IN THE PROMPT. Never let the agent pick its own label.

### What a submission must contain

Both benchmarks, every time:

- `predictions` — the honest per-song probability shortlist (20–40 songs,
  probs in (0, 1]). Not renormalized up at publish, so a sparse list keeps
  its stated probabilities. Calibration: the sum should equal the number of
  YOUR songs you expect to actually play — realistic recall for a 30-song
  list is 35–50% of an ~18-song show, so an honest sum is ~6–9, NOT the
  full setlist size K (only the whole catalog sums to K). Per-song
  probabilities above ~0.40 need exceptional evidence; hot-rotation base
  rates run ~0.20–0.35.
- `setlist` — a full structured setlist call (`sets` "1"/"2"/"e", ordered,
  opener/closer conscious, ~17–20 songs total). This is the second benchmark;
  a submission without it sits out the setlist scorecard.
- `rationale` — required, and **per show**: a short narrative specific to
  that show (what the call leaned on, where it disagrees with the baseline,
  how the run context shaped it). One blanket rationale copy-pasted across a
  run's submissions is not acceptable — the rationale is rendered per show in
  the source-compare view.

### Canonical prompt

Paste this into the agent's chat (fill in the `<...>` slots):

```
You are the model behind the "<MODEL_LABEL>" track on a Phish setlist
prediction scoreboard. Use the connected `phishpred` MCP server to research
and then submit your prediction for <SHOWDATE(S)>.

Research first (read tools, any order):
- scoreboard(model_label="<MODEL_LABEL>") — YOUR track record vs the
  heuristic baseline (paired vs_heuristic deltas, recent best calls and
  whiffs). Start here: know what you're beating and where you've been wrong.
- show_length_stats() — songs-per-show averages. Your shortlist is scored
  against ~18-19 distinct songs; your probs should sum to your EXPECTED
  HITS (~6-9 for a 30-song list), never to the full show size.
- upcoming_shows() — confirm the target show(s) and note the `epoch`.
- run_context(showdate) — the multi-night run; already-played nights matter.
- recent_setlists(10) — current tour context.
- candidate_features(showdate) — the model feature frame; note played_in_run
  and played_prev_show flags.
- venue_history(...) and song_history(slug) for songs you want to check.
- heuristic_prediction(showdate) — the statistical baseline. Beat it, don't
  copy it.
- slot_propensities([...your draft setlist...]) — where each song actually
  tends to sit (openers/closers/encore are scored; place them on data).
- backtest_shortlist([...candidate slugs...]) — how your working hypothesis
  would have scored over recent shows. Test before you submit; but remember
  rotation: a song that hit 5 of the last 10 may be the one cooling down.

Hard rules:
- Phish essentially never repeats a song within a multi-night same-venue
  run: played_in_run=1 means ~0-5% probability. Cross-check run_context.
- A song from the immediately previous show repeats only ~2% of the time
  (played_prev_show=1).
- If predicting multiple nights of one run, the nights must be jointly
  consistent: a song called high for night 1 gets discounted for nights 2-3.

Then submit ONE call per show:
submit_prediction(
  showdate="<SHOWDATE>",
  model_label="<MODEL_LABEL>",        # EXACTLY this string — it is your scoreboard identity
  predictions=[{"slug": ..., "prob": ...}, ...],
      # 20-40 songs, prob in (0,1], your honest per-song probabilities;
      # NOT renormalized up. Sum = your expected hits (~6-9 for a 30-song
      # list), NEVER ~18-20 — that's the whole catalog's sum, and inflating
      # to it wrecks your Brier. Probs >0.40 need exceptional evidence.
  setlist={"sets": {"1": [...], "2": [...], "e": [...]}},
      # REQUIRED for the setlist benchmark: your full setlist call.
      # Ordered slugs, opener/closer conscious (first/last of each set are
      # scored as marquee calls, exact positions earn the sharpshooter
      # badge). Typical era-4 shape: ~9 songs set 1, ~7-8 set 2, 1-2 encore.
      # No slug twice anywhere. Slugs must exist in candidate_features /
      # song_history — verify any you didn't see in a tool result.
  rationale="<2-5 sentences SPECIFIC TO THIS SHOW: what you leaned on, what
      you're calling against the baseline, how already-played nights shaped
      it. Write a fresh rationale for every show — never reuse one.>",
)

After submitting, verify the tool result: the echoed payload must show
model_label "<MODEL_LABEL>" and a "setlist" key. If either is wrong,
call submit_prediction again with the fix — resubmission preserves the
prior take as a version, it does not lose anything.
```

### Publish checklist (the part the agent can't do)

A submission is a **local file only** until it's pushed to R2. After the
agent finishes, from the repo root:

```
# 1. Sanity-check the file: right label dir, right showdate, setlist present
python -m uv run python - <<'EOF'
import json, pathlib
for p in sorted(pathlib.Path("data/predictions/submitted").glob("*/*.json")):
    d = json.loads(p.read_text(encoding="utf-8"))
    print(p, "| label:", d["model_label"], "| setlist:", "sets" in (d.get("setlist") or {}),
          "| versions:", len(d.get("versions", [])))
EOF

# 2. Push the inbox (R2_* creds live in .env.local)
python -m uv run dotenv -f .env.local run -- python scripts/r2_push.py data/predictions/submitted submitted
```

The push changes the epoch's `submitted_manifest_hash`, so the next scheduled
publish run folds it in automatically (or dispatch the publish workflow
manually to see it sooner). Verify on the live site: the show page should
grow an `mcp:<MODEL_LABEL>` source column.

## Testing

`tests/test_mcp.py` unit-tests `phishpred.mcp.tools` directly against a
small in-memory DB — no live MCP session, no network. Run with:

```
python -m uv run pytest tests/test_mcp.py -q
```
