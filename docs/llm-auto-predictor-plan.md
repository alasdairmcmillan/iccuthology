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
                     [--with-setlist]             # also ask for a structured setlist call
                     [--submitted DIR]            # default data/predictions/submitted
                     [--dry-run]
```

Per selected show:
1. Build the candidate frame the same way `predict`/`llm.py` do
   (`LLMSongModel` path — reuse, don't fork).
2. One `complete_json` call returning the §5 shapes: the existing
   `predictions` array, plus (with `--with-setlist`) a `setlist.sets` object.
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

### Prompt/schema extension (`--with-setlist`)

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
    uv run phishpred llm-submit --model llm:anthropic:claude-opus-4-8 --label claude-opus --with-setlist
    uv run phishpred llm-submit --model llm:gemini:<current-flash-id> --label gemini-flash --with-setlist
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
