# AGENTS.md — standing instructions for coding-agent harnesses

This repo is a Phish setlist predictor (`phishpred`, Python 3.12 + SQLite).
If a human runs you here with a minimal prompt like "submit your setlist
predictions", THIS file is your task spec. The authoritative long-form
contract is `docs/MCP.md` § "Agent playbook — driving a live model track"
(label rules, benchmarks, publish flow) — read it if anything below is
ambiguous. Data contracts: `DEPLOY-CONTRACTS.md` §5 (submission schema),
§8 (how you get scored).

## Environment notes (Windows)

- Run Python as `python -m uv run python ...` from the repo root; if `uv`
  isn't available, use the venv directly: `./.venv/Scripts/python.exe`.
- Never commit, push, or modify `data/phish.db`. Your ONLY write surface is
  `data/predictions/submitted/` (via the submit tool) plus the R2 push in
  step 6.
- Helper scripts and dumps you write along the way (context extracts, a
  submitter carrying your picks, etc.) go in `tmp/` (gitignored), NEVER the
  repo root. Delete them when you're done unless the human asks to keep them.
- Tooling you want to KEEP across sessions (your own analysis scripts,
  research helpers, prompt notes) goes in `agents/<your-workspace>/` — a
  committed, sandboxed workspace per harness/track (e.g.
  `agents/antigravity/`). Read its README for the rules; the short version:
  write freely inside your folder, never modify anything outside it, and
  nothing in `phishpred` core may import from it.

## The task: submit scoreboard predictions for every future show

### 1. Identify yourself (model label)

The human selects which model you are in the harness — you do NOT choose a
model, but you MUST log it correctly. `model_label` is your permanent public
scoreboard identity:

- It names the **model doing the reasoning** (e.g. `gemini-3.5-flash-high`,
  `claude-opus`), never the harness/CLI/IDE (`agy`, `antigravity`, `cursor`).
- Kebab-case-ish, characters `[A-Za-z0-9_.-]`.
- Check `data/predictions/submitted/` for existing track directories and
  **reuse an existing label exactly** if it's the same model; a typo mints a
  fake new competitor. A genuinely new model gets a new label.
- If you cannot determine which model you are, ASK the human before
  submitting anything.

### 2. Calculate the target shows

Default scope: **every future show** on the schedule. (The human may narrow
it, e.g. "just the rest of this run".)

```
python -m uv run python -c "from phishpred.db import get_connection; from phishpred.mcp import tools; c = get_connection('data/phish.db'); [print(s['showdate'], '|', s['venue_name'], s['city'], s['state'] or '') for s in tools.upcoming_shows(c, limit=50)['shows']]"
```

### 3. Research

If the `phishpred` MCP server is connected, use its tools. If not (typical
for CLI harnesses), call the same functions directly — `phishpred.mcp.tools`
is the identical code path:

```python
from phishpred.db import get_connection
from phishpred.mcp import tools
conn = get_connection("data/phish.db")

tools.scoreboard("data/scorecards", model_label=label)  # YOUR track record vs the heuristic baseline — start here
tools.show_length_stats(conn)            # songs/show averages: shortlist is scored against ~18-19 distinct songs
tools.run_context(conn, showdate)        # the multi-night run; played nights included
tools.recent_setlists(conn, n=10)        # tour context
tools.candidate_features(conn, showdate) # feature frame: decayed_rate, gap, played_in_run, played_prev_show, ...
tools.heuristic_prediction(conn, showdate)  # the statistical baseline — beat it, don't copy it
tools.song_history(conn, slug)           # deep-dive one song
tools.venue_history(conn, venue)         # what this venue tends to get
tools.slot_propensities(conn, slugs)     # where each song sits (open/mid/close/encore) — place your setlist call on data
tools.backtest_shortlist(conn, slugs)    # score a hypothesis against recent played shows BEFORE submitting it
```

Calibration notes from the scored record (why the first two tools matter):

- Your `predictions` list must be 20–40 songs; its scored hit rate uses your
  top 20. Shows average ~18–19 distinct songs in the current era.
- CALIBRATION (this is where the Brier is won or lost): your probs should
  sum to the number of YOUR songs you expect to actually play — realistic
  recall for a 30-song list is 35–50% of an 18-song show, so an honest sum
  is ~6–9, NOT ~18. Only the full 400-song catalog sums to the show size K.
  Historical base rates for even the hottest rotation songs are ~0.20–0.35
  per show (cross-check with `backtest_shortlist`); probabilities above
  ~0.40 need genuinely exceptional evidence. Under-sums are preserved
  as-is at publish (probs are never scaled UP).
- `scoreboard(...)` shows your `vs_heuristic` paired deltas — if you're
  losing to the baseline, study its recent takes before submitting more of
  the same.

Hard rules (Phish rotation, non-negotiable):

- No repeats within a multi-night same-venue run: `played_in_run=1` means
  ~0–5% probability. Cross-check `run_context`.
- A song from the immediately previous show repeats only ~2% of the time
  (`played_prev_show=1`).
- Multi-night submissions must be **jointly consistent**: a song you call
  high for night 1 gets discounted for later nights of that run, and vice
  versa. Do not maximize each night independently.

### 4. Submit — one call per show, ALL THREE parts every time

Submit YOUR OWN reasoning — the whole point of a model track is that the
predictions reflect the model's judgment. Do NOT write or run a batch script
that generates predictions from a formula, templates, or the repo's own
sampler, and never run `scripts/make_predictions.py` (a retired formula
generator kept for history — its takes were retired from the
`gemini-3.5-flash-high` track on 2026-07-11). Every live track is per-show
model reasoning; formula or template output submitted under a model's label
is disguised plagiarism of the pipeline and will be removed.

```python
tools.submit_prediction(
    showdate,                      # "YYYY-MM-DD"
    model_label,                   # from step 1 — EXACT string, every show
    predictions,                   # 20-40 of {"slug": str, "prob": float in (0,1]}
    rationale,                     # per-show narrative — see below
    setlist={"sets": {"1": [...], "2": [...], "e": [...]}},
    conn=conn,
    out_dir="data/predictions/submitted",
)
```

(Via MCP the tool has the same signature minus `conn`/`out_dir`.)

- `predictions`: your honest per-song probabilities. They are never
  renormalized upward, so a sparse list keeps its stated probs; the sum
  should land near the expected setlist size (~19–20 songs in the current
  era).
- `setlist`: your full structured setlist call — the second benchmark.
  Ordered slugs, opener/closer conscious (first/last of each set score as
  marquee calls; exact positions earn the sharpshooter badge and boost the
  weighted score). Check `slot_propensities` for your draft — an "encore
  song" called as the set 1 opener throws away marquee points. Typical
  shape: ~9 songs set 1, ~7–8 set 2, 1–2 encore (live numbers:
  `slot_propensities(...)["set_structure"]`); ≤40 total; no slug twice
  anywhere; only slugs you have seen in a tool result.
- `rationale`: REQUIRED and **specific to that show** — 2–5 sentences on
  what you leaned on, where you disagree with the heuristic baseline, and
  how already-played nights shaped it. Never reuse a rationale across shows.

Resubmitting for a show you (or a prior version of your track) already
covered is safe and encouraged after new setlists post: the prior take is
preserved in `versions` and the scoreboard tracks the improvement arc.

### 5. Verify before publishing

For each `data/predictions/submitted/<label>/<showdate>.json` you wrote,
confirm: `model_label` field is exactly your label, a `setlist.sets` key is
present, and rationales differ per show. Fix by resubmitting — nothing is
lost.

### 6. Publish (nothing is live until this runs)

Submissions are local files until pushed to the R2 `submitted/` prefix. From the repo root:

Using `uv` and `dotenv` CLI:
```bash
python -m uv run dotenv -f .env.local run -- python scripts/r2_push.py data/predictions/submitted submitted
```

Or using the direct Python fallback (recommended if `uv` or `dotenv` CLI is not available):
```bash
# If uv is not available, run: .\.venv\Scripts\python.exe ...
python -c "from phishpred.config import _load_env; _load_env(); from scripts.r2_push import main; main(['data/predictions/submitted', 'submitted'])"
```

(R2 credentials live in `.env.local`.) The push changes the epoch's
manifest hash, so the next scheduled publish run folds your track in as
source `mcp:<label>` automatically. If the push fails (missing creds, no
network), STOP and tell the human — do not consider the task done.

### 7. Report

End by telling the human: which shows you submitted, under which label, the
setlist song counts per show, verification results, and the r2_push output.
