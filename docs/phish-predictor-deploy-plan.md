# Phish Predictor — Deployment & Publishing Plan

Companion to `phish-predictor-plan.md` (§7 "Later phases") and
`phish-predictor-modes-plan.md`. Covers how the predictor runs and publishes on a
deployed site **without** live multi-minute compute or metered always-on infra.

**Chosen stack (tentative):** compute on **GitHub Actions**, serve on **Cloudflare**
(a single Worker with static assets + a read API), storage in **Cloudflare R2**
(+ optional **D1**). Domain already on Cloudflare. Target cost: **~$0/month**,
with no expiry cliff (nothing depends on depleting credit).

Two functional requirements shape the design and are called out throughout:
- **Dynamic run selection** — the web UI lets a user multi-select any set of upcoming
  shows and get correct *joint* run-mode numbers, without live simulation (§4a, §7).
- **Multiple prediction sources incl. agentic MCP** — publish and compare predictions
  from the statistical models, the API LLM path, *and* external agents (Claude,
  antigravity, local models) driven through an MCP server (§5).

---

## 0. Guiding principle

**Predictions are a batch artifact, not a live computation.** Every prediction is a
pure function of the ingested data state, which changes only on three events:

1. A show is played (a new setlist is ingested → state advances).
2. The schedule changes (future shows announced / moved / cancelled).
3. The model or code changes (new version, new `n_sims`/`seed`).

Between those, every prediction is byte-identical. So we **compute once per event,
snapshot to storage, and serve cheap static reads.** No model or simulator ever runs
in a web request. This is what makes it both fast for users and cheap to host.

The expensive piece is the Monte-Carlo simulator (measured: ~6 min for the tour
mode at `--n-sims 2000` over a 19-show horizon). Precomputing takes it off the UX
critical path entirely; it runs a few times a day at most, gated so it usually
no-ops.

**Key enabler for dynamic queries:** we publish not just the *reduced* tables but the
**raw simulation samples**. Because those samples already encode the full joint
distribution over the tour, any per-user question — an arbitrary multi-night run, a
different chaser song — is an *exact reduction* over the samples, computable in the
browser in milliseconds. Precompute the physics once; reduce it many ways for free.

---

## 1. Architecture overview

```
                    ┌───────────────────────────────────────────┐
   schedule (cron)  │  GitHub Actions  (compute tier)            │
   + manual dispatch│  ─────────────────────────────────────    │
        ──────────► │  1. restore data (phish.db, raw/) from R2  │
                    │  2. phishpred refresh   (incremental)      │
                    │  3. fold in submitted MCP predictions      │
                    │  4. compute epoch → gate (skip if same)    │
                    │  5. phishpred publish (modes + raw samples │
                    │       + all prediction sources → JSON)     │
                    │  6. push snapshots → R2 (+ rows → D1)       │
                    │  7. push updated phish.db → R2             │
                    └───────────────┬───────────────────────────┘
                                    │  (writes)
   ┌─────────────────────┐          ▼
   │ MCP server (local)  │   ┌─────────────────────────────────────────────┐
   │ Claude / antigravity│   │  Cloudflare R2   snapshots/{epoch}/*.json     │
   │ / local models      │──►│                  snapshots/{epoch}/samples.*  │
   │ explore + submit    │   │  Cloudflare D1   normalized rows (optional)   │
   │ predictions ────────┼──►│  submitted/{model}/{showdate}.json            │
   └─────────────────────┘   │  latest.json / epoch marker                   │
                             └───────────────┬─────────────────────────────┘
                                             │  (reads, bindings)
                                             ▼
             ┌─────────────────────────────────────────────┐
             │  Cloudflare Worker  (serve tier)             │
             │  • static assets  → React frontend           │
             │  • /api/*         → reads R2/D1, returns JSON │
             │  • /api/run       → reduces samples on the fly│
             │  routed on your Cloudflare domain            │
             └─────────────────────────────────────────────┘
                             ▲
                             │  fetch /api/...   (multi-select, model compare)
                        end users
```

Tiers, cleanly separated:
- **Compute** (GitHub Actions) — runs the Python CLI natively, no timeout limits, no
  container needed. Also folds in agent-submitted predictions. Free at this scale.
- **Storage** (R2 + optional D1) — timestamped snapshots, raw samples, submitted
  predictions; zero egress fees.
- **Serve** (one Cloudflare Worker) — serves the static app *and* a read-only JSON API,
  including on-the-fly run reductions over the samples. No compute in the request path.
- **Agent side-channel** (MCP server, local) — external models query the DB and submit
  predictions that become extra comparison columns (§5).

---

## 2. Compute tier — GitHub Actions

A single scheduled workflow (`.github/workflows/publish.yml`) does refresh → fold →
gate → publish. Sketch:

```yaml
name: publish-predictions
on:
  schedule:
    - cron: "0 */6 * * *"     # every 6h (UTC). Off-tour runs no-op in seconds.
  workflow_dispatch: {}        # manual trigger for reruns / model changes

concurrency: publish           # never overlap two runs

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5           # or setup-python + pip install uv
      - name: Restore data from R2
        run: python scripts/r2_pull.py data/phish.db data/raw/   # rclone/boto3/aws-cli to R2 (S3 API)
        env: { R2_* : ${{ secrets.R2_* }} }
      - name: Incremental refresh
        run: uv run phishpred refresh
        env: { PHISHNET_API_KEY: ${{ secrets.PHISHNET_API_KEY }} }
      - name: Pull agent-submitted predictions
        run: python scripts/r2_pull.py submitted/ data/predictions/submitted/   # MCP submissions (§5)
      - name: Compute epoch + gate
        id: gate
        run: uv run phishpred epoch --emit-github-output   # changed=true|false, epoch=...
      - name: Publish snapshots
        if: steps.gate.outputs.changed == 'true'
        run: uv run phishpred publish --out build/snapshots --n-sims 2000 --model lr --seed 0 --with-samples
        env:                                   # LLM keys optional; only for the API-LLM column
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      - name: Sync to R2 (+ D1)
        if: steps.gate.outputs.changed == 'true'
        run: |
          python scripts/r2_push.py build/snapshots/ snapshots/${{ steps.gate.outputs.epoch }}/
          python scripts/r2_push_pointer.py latest.json ${{ steps.gate.outputs.epoch }}
          # optional: npx wrangler d1 execute PHISH_DB --file build/snapshots/rows.sql
      - name: Persist updated DB to R2
        if: steps.gate.outputs.changed == 'true'
        run: python scripts/r2_push.py data/phish.db state/phish.db
      - name: Commit JSON snapshot to repo (free git-history / drift backup)
        if: steps.gate.outputs.changed == 'true'
        run: |                                 # optional but recommended — see §6
          git add data/predictions/ && git commit -m "predictions: epoch ${{ steps.gate.outputs.epoch }}" && git push
```

**Data persistence between runs.** A full ingest is expensive and rude to the
phish.net API. Keep the durable state (`data/phish.db` + `data/raw/` cache) in **R2**;
pull at job start, push at end. `refresh` then only re-fetches the current year plus
anything new (incremental, seconds). `actions/cache` is a lighter alternative but can
be evicted; R2 is durable and we're already using it.

**Cadence.** Every 6h year-round is simplest; the epoch gate (§6) makes off-tour runs
exit in seconds, so frequency is nearly free. Bump to every 2–3h during an active tour
if you want the new setlist reflected faster (Phish setlists usually post within hours).
Cron is UTC — pick offsets that land a run mid-morning US time, after overnight setlist
entry.

**Secrets** (GitHub Actions repo secrets): `PHISHNET_API_KEY`, R2 credentials, a
Cloudflare API token for D1 (if used), and optional `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `GOOGLE_API_KEY`. None ever reach the Worker or the browser.

---

## 3. New CLI pieces — `phishpred publish` + `phishpred epoch`

Two small commands to add (build item, not yet built). Both are thin wrappers over the
existing library, keeping prediction logic import-clean.

```
phishpred epoch [--emit-github-output]
    Print the current epoch key (§6) and whether it differs from the last published
    epoch (read from R2 latest.json / a local marker). With --emit-github-output,
    write `changed=` and `epoch=` to $GITHUB_OUTPUT so the workflow can gate. Cheap:
    no simulation, just reads DB state + submitted-prediction manifest.

phishpred publish --out DIR [--n-sims N] [--model M] [--seed S] [--with-samples]
    Compute every publishable artifact for the current epoch and write JSON to DIR:
      meta.json                      epoch, created_at, as_of_show, models, n_sims, seed, code_version
      show/{showdate}.json           per upcoming show: { sources: { <model>: [ {slug, prob, ...} ] } }
                                       sources = heuristic, lr, gbm, llm:<api-model>, mcp:<label> (§5)
      tour.json                      tour mode: expected plays / P(>=1) / buckets (headline reduction)
      setlist/{showdate}.json        sampled setlist (+ LLM assembler column if keys present)
      samples.bin (--with-samples)   compact raw joint samples for dynamic reductions (§4a)
      samples_meta.json              vocab (songid<->slug), horizon showdates/venues, n_sims, seed
      rows.sql (optional)            D1 upserts for normalized history (§4)
    Deterministic given seed. ONE simulate_horizon run per epoch feeds tour + chaser +
    every run subset (they're all reductions over the same samples) — the big cost saver.
```

Design notes:
- **Compute the sims once, reduce many ways.** `tour.json` is a convenience reduction;
  `samples.bin` is the source of truth that lets the UI answer any run/chaser/subset
  query exactly (§4a, §7). Do not run separate simulations per mode.
- **Multi-source `show/{showdate}.json`.** Each upcoming show carries a `sources` object
  keyed by model label, so the UI can render columns side by side and the backtest can
  score any of them. Statistical sources come from `predict_show`; the API-LLM source
  from `models/llm.py`; agent sources from submitted predictions (§5).
- The API-LLM column uses the existing per-(showid, model, prompt_version) cache, so
  republishing an unchanged epoch makes zero LLM calls.

---

## 4. Storage — Cloudflare R2 (+ optional D1)

**R2 (primary, v1).** Object storage, S3-compatible, **zero egress**. Layout:

```
snapshots/{epoch}/meta.json
snapshots/{epoch}/tour.json
snapshots/{epoch}/show/{showdate}.json          # multi-source per-show predictions
snapshots/{epoch}/setlist/{showdate}.json
snapshots/{epoch}/samples.bin                   # compact raw joint samples (§4a)
snapshots/{epoch}/samples_meta.json
submitted/{model_label}/{showdate}.json         # inbox for agent/MCP predictions (§5)
latest.json                                     # { "epoch": "...", "created_at": "..." }
state/phish.db                                  # durable DB between Actions runs
```

Keeping every `{epoch}/` prefix (never overwriting) gives **timestamped history for
free** → drift charts (§6). The Worker reads `latest.json` to resolve the current epoch.

**D1 (optional, phase 2).** SQLite at the edge — matches the local `schema.sql` and the
plan's "portable SQL" rule, so it runs on D1 now *or* Neon later with no rewrite. Add it
when you want queryable per-song drift and cross-source comparison rather than fetching
JSON files. Portable schema:

```sql
CREATE TABLE prediction_snapshots (
  epoch TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  as_of_showdate TEXT, as_of_show_index INTEGER,
  code_version TEXT, n_sims INTEGER, seed INTEGER
);
CREATE TABLE show_predictions (
  epoch TEXT NOT NULL, showdate TEXT NOT NULL, source TEXT NOT NULL,   -- source = model label
  songid INTEGER NOT NULL, slug TEXT, song_name TEXT, prob REAL,
  PRIMARY KEY (epoch, showdate, source, songid)
);
CREATE INDEX idx_show_pred_song ON show_predictions(showdate, songid, source);   -- drift + compare
```

### 4a. Raw joint samples (enables dynamic multi-select)

Run mode is a **joint** quantity: `P(hear ≥1 across nights S)` is not the sum/product of
per-night marginals — nights are coupled by forward state and the no-repeat mask. There
are 2^N possible subsets, so we can't precompute a table per subset. Instead we publish
the simulator's raw output and reduce on demand.

`SimResult.samples[m][t]` is already exactly this: the set of songs sampled in
simulation `m` on horizon night `t`, with all joint dynamics (no-repeat within
venue-runs, forward state, decay) baked in across the whole tour. Given the samples, any
subset `S` of nights reduces **exactly**:

```
P(≥1 across S)      = mean over m of [ song ∈ ∪_{t∈S} samples[m][t] ]
per-night prob(t)   = mean over m of [ song ∈ samples[m][t] ]
most-likely night   = argmax_t of the above, restricted to t ∈ S
```

…plus chaser (first-hit index) and tour (count) reductions — all over the same samples.

- **Encoding (`samples.bin`):** per (sim, show) a varint list of songid-vocab indices
  (~18 ids × ~2 B ≈ 36 B). ~2000 × 19 ≈ 1.4 MB raw, <1 MB gzipped. `samples_meta.json`
  holds the vocab map + horizon dates/venues. Downloaded once, CDN-cached. Optionally
  publish 1000 sims for a smaller interactive file.
- **Where it reduces:** in the browser (download samples once, reduce for any selection
  in <10 ms) or in the Worker (`/api/run`, §7) for thin clients. No Python, no simulator.
- **Semantics note (honest):** the samples reduce *in tour context* — a mid-tour night
  reflects songs already burned earlier in the tour, which is *more* correct than
  simulating those nights in isolation, and identical to standalone run-mode for a
  contiguous run at the tour's start (e.g. the 3 Deer Creek nights). No-repeat applies
  within each natural venue-run; repeats are allowed across the tour — real band behavior.

---

## 5. Prediction sources & the MCP submission path

Every prediction in this system is "a per-(song, show) probability source." The storage,
UI, and backtest treat all sources uniformly, so adding a new one is free. Sources:

| Source label            | Origin                                   | Path        |
|-------------------------|------------------------------------------|-------------|
| `heuristic` / `lr` / `gbm` | statistical models (`predict_show`)   | §7a (built) |
| `llm:<model>`           | API LLM-as-model (`models/llm.py`)       | §7a (built) |
| `mcp:<label>`           | external agent via MCP (this section)    | §7b (new)   |

The **agentic path** is `phish-predictor-modes-plan.md` §7b (build-order step 6, not yet
built). You point an MCP-capable client — Claude Desktop, antigravity, Cline, a local
model behind an MCP bridge — at a local MCP server; the model explores the data, forms a
prediction, and submits it. It's **host-agnostic** (MCP is an open protocol), which is
how you compare Claude / Gemini / GPT / open models on the same upcoming shows. It
coexists with the API path (§7a): API = automated benchmarkable columns; MCP =
human-in-the-loop agentic columns.

### 5a. `phishpred-mcp` — read-only tools + one write tool
A small MCP server over the existing SQLite DB + feature/predict functions:

```
# read tools (leakage-safe; the model sees only history <= as-of)
upcoming_shows()                       -> list of upcoming showdates/venues + current epoch
candidate_features(showdate)           -> the exact feature frame `predict` builds (compact)
song_history(slug)                     -> gaps, decayed rate, era rates, venue history
venue_history(venue)                   -> what tends to get played there
recent_setlists(n=10)                  -> last n shows' setlists (tour context)
run_context(showdate)                  -> the run this show belongs to + already-played nights
heuristic_prediction(showdate)         -> the statistical baseline, so the model can argue with it

# write tool
submit_prediction(showdate, model_label, predictions[{slug, prob}], rationale?)
    -> schema-validate, renormalize to K (probs.renormalize_to_k), stamp with the CURRENT
       epoch + submitted_at, store to submitted/{model_label}/{showdate}.json (local, then
       pushed to R2). Records the agent's optional narrative ("why Fluffhead is due").
```

Predictions are **pinned to the epoch the model saw**, so comparisons are fair and the
submission slots into the drift/history timeline correctly.

### 5b. Flow into the site
1. Locally: pull `phish.db` from R2, run `phishpred-mcp` (stdio), point your agent at it.
2. Agent explores via read tools, submits per-song probs (or an ordered setlist) via
   `submit_prediction`. Files land under `submitted/{model_label}/{showdate}.json`.
3. The publish batch (§2) pulls `submitted/`, folds each into the matching
   `show/{showdate}.json` under `sources.mcp:<label>`, and publishes.
4. The web UI renders all sources side by side; the backtest scores each once the real
   setlist posts.

The deployed **site stays read-only static** — submissions flow through storage + the
next publish, so no live write endpoint is needed for v1. (A later option: one
authenticated Worker route so an agent can submit straight to the live site; defer it.)

---

## 6. Epoch definition, gating & timestamped history

The **epoch** is the identity of a prediction state. Recompute only when it changes.

```
epoch = f(max_played_show_index,   # advances when a show is played
          schedule_hash,           # hash of upcoming (showdate, venueid) list
          code_version,            # git short SHA of the predictor
          model, n_sims, seed,     # publishing parameters
          submitted_manifest_hash) # so new agent submissions trigger a republish
```

Gating (in `phishpred epoch`): after `refresh` + pulling submissions, compute `epoch`;
compare to the last published epoch in `latest.json`; if equal → `changed=false` and the
workflow skips `publish` (exits in seconds); if different → run the batch. Most scheduled
runs are no-ops, so the expensive sim runs **only after a show is played, the schedule
changes, the code/model changes, or a new agent prediction arrives** — exactly the
"recalculate after each show" behavior, plus republish-on-new-comparison.

**Timestamped history / drift (free):** because `{epoch}/` prefixes are never overwritten
(and/or each publish commits JSON to `data/predictions/`), you get the full history of
what every source believed over time → drift charts ("how did Hood's 7/12 odds move after
the 7/10 show?"), source-vs-source divergence over a tour, and reproducibility (every
snapshot pins `seed` + `code_version`). Committing JSON to git makes `git log` itself the
archive. This same ground truth resolves the eventual prediction market (M2) and scores
Brier via the existing backtest harness.

---

## 7. Serve tier — Cloudflare Worker

A **single Worker** serves both the static frontend and the read API (Workers support a
static-assets binding, so no separate Pages project is required — Pages is a fine
alternative). Bindings: `ASSETS` (React build), `R2` (snapshots), optional `DB` (D1),
optional `KV` (latest pointer / hot cache).

```
GET  /api/latest                       → meta.json for the current epoch
GET  /api/tour                         → current tour-mode table
GET  /api/show/{showdate}              → per-show ranked probs for ALL sources (compare view)
GET  /api/setlist/{showdate}           → sampled setlist (+ LLM column)
GET  /api/samples                      → compact samples.bin + samples_meta (client-side reduction)
POST /api/run   { showdates: [...] }   → EXACT joint run reduction over samples for an
                                          arbitrary multi-selected set (dynamic run selection)
GET  /api/chaser/{slug}                → next-play distribution (reduced from samples)
GET  /api/history/{showdate}/{songid}  → prob over time, per source (drift)  [needs D1]
```

- **Dynamic run selection** is `/api/run` (or done entirely client-side after
  `/api/samples`): set-membership counting over the stored samples — milliseconds, no
  simulator. This is what lets the UI's show-multi-select return correct joint numbers.
- No compute, no secrets, no external API calls in the request path. `Cache-Control` lets
  Cloudflare's CDN cache responses between epochs (they change only a few times a day).

**Domain routing** (already on Cloudflare): app on the apex/`www`, API under `/api/*` on
the same Worker (or split to `api.yourdomain`). DNS + Workers Routes handle it natively.

---

## 8. Cost model (~$0/month, no cliff)

At hobby scale everything sits inside permanent free tiers (verify current limits, but
roughly):
- **GitHub Actions:** free minutes cover a ~6-min job a few times/day; unlimited if the
  repo is public. Gating means most runs are seconds.
- **Cloudflare Workers:** ~100k requests/day free — far above hobby traffic.
- **R2:** generous free storage + **zero egress**; snapshots + samples are ~1 MB/epoch.
- **D1 (if used):** free tier covers hobby read/write volumes.
- **Static assets / Pages:** free, CDN-cached.
- **MCP server:** runs locally on your machine during an agent session — $0.

Nothing depends on Azure credit, so **credit expiry is a non-event** (see appendix).

---

## 9. Security

- The read API is public (predictions are public data) and read-only — no auth for v1.
- All API keys (phish.net, LLM providers) live **only** in GitHub Actions secrets and are
  used **only** in the batch. They never reach R2, the Worker, or the browser.
- R2/D1 write credentials live only in Actions secrets; the Worker gets scoped read
  bindings, not account credentials.
- The **MCP server is local and read-only** except `submit_prediction`, which writes to a
  submissions inbox (validated + renormalized) — never to core tables. Treat submitted
  predictions as untrusted input: schema-validate, clamp probs, label by source.
- LLM/agent calls happen outside the request path (batch or local MCP session), so a
  site visit can never trigger paid model usage.

---

## 10. Build order

1. **`phishpred publish` + `phishpred epoch`** — emit all artifacts incl. multi-source
   `show/*.json` and `samples.bin`; epoch gating. (Pure library reuse; testable offline.)
2. **R2 bucket + push/pull scripts** — durable `phish.db`, snapshots, samples, submissions
   inbox. Wire the Actions workflow; verify gated no-op vs forced publish.
3. **Cloudflare Worker (API)** — serve `latest` + per-mode JSON from R2; implement
   `/api/run` sample reduction. Point a route at it; confirm CDN caching.
4. **React frontend** — probability bars, **show multi-select** (→ `/api/run`), and a
   **source-compare view** per show. Deploy as Worker static assets; wire the domain.
5. **`phishpred-mcp` server + `submit_prediction`** — read tools + submission; fold
   submissions into publish; render `mcp:<label>` columns. (Modes plan §7b / step 6.)
6. **D1 + drift/compare endpoints** — normalized rows + `/api/history/...` for drift and
   source-vs-source timelines. (Optional; do when you want the timeline UI.)
7. **(Later, M2)** prediction market — live transactional writes; revisit the DB choice
   (Postgres/Neon likely) then. Out of scope here.

---

## 11. Open questions / decisions

- **Headline source:** which model is the default shown number (`lr` = best backtest, or
  `heuristic` = explainable), with others behind a compare toggle?
- **`n_sims` for publishing / samples:** 2000 for reductions is fine off the critical
  path; publish the same 2000 samples, or a downsampled 1000 for a smaller client file?
- **Chaser watchlist:** which songs get a prebuilt `chaser/{slug}.json` vs computed
  client-side from `samples.bin` on demand? (Samples make a fixed watchlist unnecessary.)
- **History store:** R2 prefixes only, commit-JSON-to-git, and/or D1? (All three cheap;
  git is the nicest free archive; D1 needed for the drift/compare *query* UI.)
- **MCP submission trust/UX:** anonymous local submissions vs a signed `model_label`; how
  to display the agent's `rationale`; whether to accept ordered setlists as well as
  per-song probs.
- **Frontend framework:** plain React/Vite as Worker assets vs Cloudflare Pages + Next.js.

---

## Appendix — Azure Container Apps route (only if spending credit)

If you later want to use the existing ACR + Azure credit, the **only** thing that changes
is the compute tier: run the batch (§2) as an **Azure Container Apps Job** (scale-to-zero,
on schedule), image pulled from your ACR, writing the same snapshots/samples/submissions
to R2/D1. Storage, serve, and MCP tiers are unchanged. Watch-outs that create a
post-credit cost: **ACR Basic ~$5/mo** (GitHub's GHCR is free) and the Log Analytics
workspace Container Apps provisions. Because it's just the compute tier, you can adopt or
drop it without touching the rest — so there's no reason to decide now. GitHub Actions
stays the zero-infra default.
