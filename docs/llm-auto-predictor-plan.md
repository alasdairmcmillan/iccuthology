# LLM auto-predictor — hands-off post-show re-prediction (plan)

Status: SPEC — not yet implemented; implementation intentionally ON HOLD
(owner's call, 2026-07-11) — do not build without an explicit go-ahead.
API keys are already wired up as GitHub Actions secrets (2026-07-11), so
the "Secrets" step below is DONE. Written alongside the
model-setlists-and-versions branch (submission versioning + setlist benchmark,
DEPLOY-CONTRACTS §2/§5/§8). Depends on that branch being merged.

## Goal

Replace the manual MCP-triggered prediction refresh with an automated one:
after each show is ingested, LLM models re-predict the remaining nights of the
current run (the "virtual Monty Hall" refresh), producing a new submission
VERSION per show so the scorecard shows the improvement arc. Zero human steps
during tour.

Wiring audit (2026-07-12, scorecard-ui-updates branch): all foundations this
plan depends on are in place — `models/llm.py` adapters (anthropic/openai/
gemini/openai-compat, structured JSON, disk cache, `context_fn` hook),
`predict_show` accepts `llm:*` specs, `publish` folds `llm:*` compare columns
with `LLMError` tolerance, `submit_prediction` validates + versions, and
`ANTHROPIC_API_KEY` reaches the publish step in `publish.yml`. Two tripwires
for the implementing branch:
1. **Shortlist bounds (NEW)**: `submit_prediction` now enforces 20–40
   predictions per submission. `PREDICTIONS_SCHEMA` has no `minItems` and
   `SYSTEM_PROMPT` doesn't state a count — the llm-submit prompt/schema must
   request 20–40 rows or every submission will be rejected. (Another reason
   for the `max_tokens` bump below.)
2. **Gemini key naming**: standardized on `GOOGLE_API_KEY` end-to-end —
   it's what `GeminiClient` reads, what `.env.example` documents, and the
   repo secret exists under that name (verified 2026-07-12). The workflow
   snippet below still says `GEMINI_API_KEY`; use `GOOGLE_API_KEY` when
   writing the real step.
Also newly available: `phishpred.mcp.tools.scoreboard(...)` returns each
label's past accuracy plus the heuristic baseline (paired `vs_heuristic`
deltas) — worth folding into the llm-submit prompt as calibration context.

Key insight from the 2026-07-11 session: the pipeline is already reactive —
`submitted_manifest_hash` is in the epoch key, so any new/updated submission
file triggers republish → refold → refreeze. The ONLY missing piece is the
thing that *generates* submissions automatically. Also note:
`scripts/make_predictions.py` ("gemini-3.5-flash-high") is a static formula,
NOT an API caller — real API clients already exist in
`src/phishpred/models/llm.py` (Anthropic/Gemini/OpenAI, structured JSON,
disk cache, prompt builder).

## Design

### New CLI command: `phishpred llm-submit`

```
phishpred llm-submit --model llm:anthropic:claude-opus-4-8 --label claude-opus
                     [--shows run|horizon]        # default: run
                     [--no-setlist]               # setlist call is ON by default (see below)
                     [--submitted DIR]            # default data/predictions/submitted
                     [--dry-run]
```

2026-07-11 decision: the structured setlist call is part of the standard
submission contract, not an add-on — every live model track submits both
benchmarks. So the flag is `--no-setlist` (opt OUT), not `--with-setlist`.
The submission contract, model-label rules, and the canonical agent prompt
this pipeline must reproduce live in `docs/MCP.md` § "Agent playbook —
driving a live model track"; keep the two in sync.

Per selected show:
1. Build the candidate frame the same way `predict`/`llm.py` do
   (`LLMSongModel` path — reuse, don't fork).
2. One `complete_json` call returning the §5 shapes: the existing
   `predictions` array, plus (unless `--no-setlist`) a `setlist.sets` object.
   Extend `PREDICTIONS_SCHEMA` + `SYSTEM_PROMPT` accordingly (see below).
3. Write via `phishpred.mcp.tools.submit_prediction(...)` — this inherits
   slug/prob/setlist validation AND versioning (prior takes preserved,
   10-cap) for free. Do NOT write files directly.

### Show selection

- `run` (default): future shows in the same run as the most recently played
  show (`tools.run_context`), i.e. the nights whose predictions benefit from
  last night's setlist. Typically 0–3 calls per model per day.
- `horizon`: every future show in the schedule (first-boot / new-tour seeding).

### Re-prediction vs. cache

`LLMSongModel`'s disk cache keys on `(showid, model_name, prompt_version)`.
For refresh semantics, derive the effective prompt_version from the knowledge
state: `f"{PROMPT_VERSION}:asof{max_played_show_index}"`. Within one epoch the
cache holds; ingesting a new show changes `max_played_show_index` and forces a
fresh call with the updated context. No manual cache busting.

### Prompt/schema extension (setlist call, on by default)

- `PREDICTIONS_SCHEMA` gains optional `setlist`:
  `{"sets": {"<label>": ["slug", ...]}}` — same §5 rules the submit tool
  validates (keys `^(\d+|e\d*)$`, known slugs, no dupes, ≤40 songs).
- `SYSTEM_PROMPT` gains: call a full setlist (set 1 / set 2 / encore, ordered,
  opener/closer conscious), independent of the probability shortlist; remind
  it of within-run no-repeat rotation rules.
- Bump `max_tokens` to 8192 for this path — the 2048 default truncates when
  every candidate gets a row plus a setlist.
- Include run context in the prompt (`context_fn`): the setlists of already-
  played nights in this run — this is the entire Monty Hall edge; without it
  the refresh is pointless.

### Workflow wiring (`.github/workflows/publish.yml`)

New step between `phishpred refresh` and the epoch gate:

```yaml
- name: LLM re-predictions
  run: |
    uv run phishpred llm-submit --model llm:anthropic:claude-opus-4-8 --label claude-opus
    uv run phishpred llm-submit --model llm:gemini:<current-flash-id> --label gemini-flash
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
  continue-on-error: true   # a provider outage must never block publish
```

Then push the (possibly updated) `data/predictions/submitted/` back to R2
`submitted/` so local and CI stay in sync (add to the existing sync steps).
Placement BEFORE the epoch gate is what makes it hands-off: new submissions
change `submitted_manifest_hash` → `changed=true` → publish/freeze runs.

Secrets: `ANTHROPIC_API_KEY` already exists in the workflow; **add
`GEMINI_API_KEY` to repo secrets** (the key in `.env.local` is local-only).

### Model labels (stable, they're the scoreboard identity)

- `claude-opus` → `llm:anthropic:claude-opus-4-8`
- `gemini-flash` → current Gemini Flash id (free tier: ~1.5k req/day, 15 rpm —
  our volume is single digits/day). `GeminiClient` defaults to
  `gemini-2.5-flash`; always pass the id explicitly.
- Keep the historical hand-driven labels (`claude-fable`, `claude-sonnet`,
  `gemini-3-5-flash-high`) untouched; they remain scored as-is.

### Cost envelope (sized 2026-07-11 from llm.py's prompt)

~10k input / ~2k output per show call. Opus 4.8 ($5/$25 per MTok) ≈ $0.10 per
call; ~50 calls/tour (refresh-remaining-run pattern) ≈ **$5/tour**; Sonnet 5
about half that; Gemini Flash free-tier $0. Batches API (50% off) is a
possible later optimization — volume doesn't justify the complexity yet.

### Failure tolerance

Per-show try/except: log to stderr and continue (mirror the fold's tolerance
philosophy). Validation errors from `submit_prediction` (bad slugs etc.)
skip that show but leave the prior submission version in place. Exit 0 always
when `--dry-run`; otherwise exit 0 unless zero shows could be processed.

### Tests

- Fake `LLMClient` returning canned payloads → assert submit files written
  with versioning + setlist; assert cache-key changes when
  `max_played_show_index` changes.
- Schema-extension unit tests (setlist present/absent/malformed → skipped
  with warning, predictions still submitted).
- Workflow step is `continue-on-error` — no CI test needed beyond the unit
  layer.

## Future: per-model tour predictions (Tours page model picker)

Status: idea to review, further out than the auto-predictor above. The Tours
page is heuristic-only today (its `MODEL:` label was removed 2026-07-11 until
this exists). Sketch:

- **What the heuristic has that others don't:** `tour.json` is a reduction
  over the Monte-Carlo `samples.bin` (joint simulation — expected plays,
  P(≥1), full play-count distributions). MCP/LLM sources only have per-show
  probability shortlists, so an exact joint reduction isn't possible for them.
- **Approximation that IS possible:** treat a model's per-show probs as
  independent across nights and reduce analytically over the tour horizon:
  `p_at_least_one = 1 − Π(1−p_i)`, `expected_plays = Σ p_i`. No distribution
  column (or a Poisson-binomial approximation if we want one). This mirrors
  the frontend's existing offline run fallback (`RunReport.approximate`), so
  the caveat pattern already exists in the UI vocabulary.
- **Coverage caveat:** a model only "covers" shows it submitted for. Publish
  a per-model `n_shows_covered / horizon` so the UI can badge partial
  coverage; exclude uncovered nights from the reduction rather than
  zero-filling them.
- **Publish shape:** either extra per-model tables `tour/{tour_id}.{label}.json`
  or a `sources` map inside the existing tour docs (mirroring
  `show/{showdate}.json`'s multi-source §2 shape — probably the cleaner
  precedent). Epoch-scoped like the rest of the snapshots. Contract addition
  to DEPLOY-CONTRACTS §2 before implementation.
- **UI:** model picker on the Tours page (same derive-from-sources pattern as
  the Shows screen), `approximate` note + coverage badge for non-heuristic
  models; the `MODEL:` label returns as the picker.
- **Prereq:** the auto-predictor above, so non-heuristic models actually have
  fresh per-show probs across the whole horizon worth aggregating.

## API tool-calling feasibility (research spike, 2026-07-14)

Status: research only, not implementation — see branch
`api-agent-tool-calling-research`, `scripts/api_agent_prototype.py`. Answers
two questions raised before committing to a design for the auto-predictor
above:

**Q1: Is the manual Claude Code/Antigravity + MCP tool-calling workflow (a
human drives an IDE that autonomously calls `phishpred-mcp`'s research tools
across multiple turns, then submits) replicable by calling a provider's API
directly, instead of driving an IDE by hand?**

**Yes — confirmed live, for two of three providers.** Anthropic's native
`mcp_servers` connector doesn't apply here (it only attaches to remote
URL-based MCP servers; `phishpred-mcp` is local stdio). The actual replication
path is simpler and more general: spawn the existing `phishpred-mcp` server as
a subprocess, connect an MCP client (`mcp.client.stdio.stdio_client` +
`mcp.ClientSession`), and drive a normal multi-turn tool-calling loop against
it with whichever provider's API — this is exactly what Claude Code/Antigravity
already do under the hood; nothing about the server needed to change.

Per-provider integration depth, from an actual run of each against a real
upcoming show (2026-07-14, Enmarket Arena):

| Provider | Integration | Live result |
|---|---|---|
| **Anthropic** (`claude-opus-4-8`) | Official SDK helper: `anthropic.lib.tools.mcp.async_mcp_tool` wraps each MCP tool directly for `client.beta.messages.tool_runner`, which drives the whole loop. Least glue of the three. | 11 autonomous tool calls in a sensible research order (`scoreboard` → `show_length_stats` → `upcoming_shows` → `run_context` → `recent_setlists` → `candidate_features` → `heuristic_prediction` → `venue_history` → `backtest_shortlist` → `slot_propensities` → `submit_prediction`), then a valid 30-song submission with a full setlist. |
| **Google** (`gemini-3.1-flash-lite`) | `google-genai` has *native* MCP support — `GenerateContentConfig(tools=[session])` lets the SDK list/call tools against a raw `ClientSession` itself, no manual loop. **But it's broken as tested**: `generate_content` deep-copies its config internally, and a live `ClientSession` embeds asyncio internals (`TypeError: cannot pickle '_asyncio.Future' object`) — a real bug hit by actually running it, not a hypothetical. Fallback: `FunctionDeclaration(parameters_json_schema=tool.inputSchema)` (raw MCP JSON Schema passed through untouched, no conversion needed) + a manual loop, same shape as OpenAI below. | 8 autonomous tool calls (`upcoming_shows` → `scoreboard` → `run_context` → `recent_setlists` → `candidate_features` → `heuristic_prediction` → `slot_propensities` → `submit_prediction`), valid 30-song submission with setlist. |
| **OpenAI** | No local MCP client support (only remote-URL MCP on the Responses API, same shape/limitation as Anthropic's connector). Manual loop; MCP `inputSchema` is already JSON Schema so the tool-schema conversion is a near-passthrough (`{"type":"function","function":{"name","description","parameters": tool.inputSchema}}`). Most glue of the three, but still small. | **Not run live** — no `OPENAI_API_KEY` in `.env.local`. Code path was written and reviewed (same manual-loop pattern proven live on Google) but not exercised against the real API. |

Both live submissions were verified: 20–40 songs, valid probs, a full
structured setlist, and — checked against `heuristic_prediction` for the same
show — meaningful song overlap (25–26/30, expected, since both draw on the
same rotation-heavy candidates) but **zero exact-probability matches**,
confirming each model computed its own numbers rather than copying the
baseline (the plagiarism-check pattern from `AGENTS.md`).

**Google free-tier note:** `gemini-3.5-flash` (the label already used
elsewhere in this project) hit its free-tier daily quota (20 requests) during
testing from repeated attempts across a multi-turn loop — free-tier RPM/RPD
limits are tight enough that a ~10-call research loop can burn through a
day's quota on one show. `gemini-3.1-flash-lite` had headroom. Worth knowing
before wiring any automated Google track to a free-tier key.

**Q2: Is the heuristic setlist already available to models via MCP as a
baseline for their own predictions?**

**Yes, already built — no new work needed.** `heuristic_prediction(conn,
showdate, ...)` in `src/phishpred/mcp/tools.py` wraps
`predict.predict_show(model="heuristic")` and is exposed as an MCP tool in
`server.py` (`heuristic_prediction`). Both live runs above called it as part
of their research (Anthropic 7th call, Google 6th call) before submitting —
exactly the "beat it, don't copy it" framing in the canonical prompt.

### Recommendation

This tool-calling architecture is a genuinely different (and stronger) design
than the single-shot completion `llm-submit` design earlier in this doc:
`models/llm.py`'s `LLMSongModel` stuffs candidate features into one prompt and
parses one JSON response — it never sees `song_history`, `venue_history`,
`slot_propensities`, `backtest_shortlist`, or `heuristic_prediction`, all of
which the live runs above used unprompted. If/when `llm-submit` moves off
hold, building it on the tool-calling loop (reusing `phishpred-mcp` as-is,
per-provider adapters as sketched above) would let the automated pipeline do
the same research a human-driven session does today, not a feature-stuffed
single call.

Per the user's stated fallback: since OpenAI/Google both required no more
than "convert MCP JSON Schema to the provider's tool format + a manual loop"
(proven live for Google; the OpenAI path is the same shape, just untested
live), none of the three providers is "wildly complicated" — the honest
ranking is Anthropic (official helper, zero glue) > OpenAI (small manual loop,
near-passthrough schema) > Google (small manual loop, but only after routing
around the native-but-broken `ClientSession` passthrough). There's no
provider here where staying on the human-driven MCP workflow is clearly
necessary; it remains an option for OpenAI specifically until it's been
verified live with a real key.
