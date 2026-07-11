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

tools.run_context(conn, showdate)        # the multi-night run; played nights included
tools.recent_setlists(conn, n=10)        # tour context
tools.candidate_features(conn, showdate) # feature frame: decayed_rate, gap, played_in_run, played_prev_show, ...
tools.heuristic_prediction(conn, showdate)  # the statistical baseline — beat it, don't copy it
tools.song_history(conn, slug)           # deep-dive one song
tools.venue_history(conn, venue)         # what this venue tends to get
```

Hard rules (Phish rotation, non-negotiable):

- No repeats within a multi-night same-venue run: `played_in_run=1` means
  ~0–5% probability. Cross-check `run_context`.
- A song from the immediately previous show repeats only ~2% of the time
  (`played_prev_show=1`).
- Multi-night submissions must be **jointly consistent**: a song you call
  high for night 1 gets discounted for later nights of that run, and vice
  versa. Do not maximize each night independently.

### 4. Submit — one call per show, ALL THREE parts every time

```python
tools.submit_prediction(
    showdate,                      # "YYYY-MM-DD"
    model_label,                   # from step 1 — EXACT string, every show
    predictions,                   # 25-40 of {"slug": str, "prob": float in (0,1]}
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
  marquee calls; exact positions earn the sharpshooter badge). Typical
  shape: ~9 songs set 1, ~7–8 set 2, 1–2 encore; ≤40 total; no slug twice
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

Submissions are local files until pushed to the R2 `submitted/` prefix. From
the repo root:

```
python -m uv run dotenv -f .env.local run -- python scripts/r2_push.py data/predictions/submitted submitted
```

(R2 credentials live in `.env.local`.) The push changes the epoch's
manifest hash, so the next scheduled publish run folds your track in as
source `mcp:<label>` automatically. If the push fails (missing creds, no
network), STOP and tell the human — do not consider the task done.

### 7. Report

End by telling the human: which shows you submitted, under which label, the
setlist song counts per show, verification results, and the r2_push output.
