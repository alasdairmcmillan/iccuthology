# Deploy — operator runbook

Operator setup for the compute/storage tier described in
`phish-predictor-deploy-plan.md` (§1-§4, §6) and `DEPLOY-CONTRACTS.md` (§1,
§2, §5). Covers standing up Cloudflare R2 and wiring the scheduled
`.github/workflows/publish.yml` GitHub Actions job. The serve tier
(Cloudflare Worker, §7) and MCP submission path (§5) are covered elsewhere
(`docs/MCP.md`) and not repeated here.

## 1. Create the R2 bucket

1. Cloudflare dashboard → **R2** → **Create bucket**.
2. Name it anything (e.g. `phish-predictor`); this becomes the `R2_BUCKET`
   secret below. Location: Automatic is fine.
3. No public access / custom domain is required for the compute tier — the
   bucket only needs to be reachable via the S3 API from GitHub Actions.
   (The Worker read path, §7, uses an R2 binding instead of the public S3
   API and is configured separately when that tier is built.)

## 2. Generate an R2 API token

1. Cloudflare dashboard → **R2** → **Manage R2 API Tokens** → **Create API
   token**.
2. Permissions: **Object Read & Write**, scoped to the bucket created above
   (avoid an account-wide token).
3. Save the generated **Access Key ID** and **Secret Access Key** — the
   secret is shown once.
4. Note your **Account ID** (Cloudflare dashboard sidebar, or the R2
   overview page) — this is `R2_ACCOUNT_ID`. The S3-compatible endpoint the
   scripts build from it is:
   ```
   https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com
   ```

## 3. Set GitHub Actions secrets

Repo → **Settings → Secrets and variables → Actions → New repository
secret**. Add:

| Secret                  | Value                                              |
|--------------------------|-----------------------------------------------------|
| `R2_ACCOUNT_ID`          | Cloudflare account ID                                |
| `R2_ACCESS_KEY_ID`       | from the R2 API token (step 2)                       |
| `R2_SECRET_ACCESS_KEY`   | from the R2 API token (step 2)                       |
| `R2_BUCKET`              | bucket name (step 1)                                 |
| `PHISHNET_API_KEY`       | phish.net API key (https://phish.net/api/keys)       |
| `ANTHROPIC_API_KEY`      | optional — used by the built-in `llm:*` prediction column when `--compare-models` includes an `llm:anthropic[:...]` source (see §8). If absent, publish skips that source with a warning instead of failing |

All four `R2_*` secrets are required by `scripts/r2_common.py`; a missing
one raises a clear `RuntimeError` naming which var is absent (fails fast in
CI logs rather than silently misbehaving).

## 4. First run — populate a fresh bucket

The workflow (`.github/workflows/publish.yml`) restores `state/phish.db`,
`submitted/`, and the `latest.json` epoch pointer (to
`data/predictions/latest.json`, which the epoch gate reads) from R2 at the
start of every run via `scripts/r2_pull.py`. On a brand-new, empty bucket
those keys don't exist yet — `r2_pull.py` treats that as non-fatal (a warning
to stderr, not a failure), so `phishpred refresh` runs against a fresh local
`data/phish.db` and the first gate reports `changed=true`. Nothing extra
needs to be done for the first run.

## 5. Triggering `workflow_dispatch`

The workflow runs on a `schedule` cron (every 6h UTC) and can also be run
on demand:

- **GitHub UI:** repo → **Actions** → **publish-predictions** → **Run
  workflow** → select the branch → **Run workflow**.
- **GitHub CLI:** `gh workflow run publish.yml`.

Use manual dispatch after a code/model change (new `--n-sims`/`--seed`/
model), or to force a republish without waiting for the next cron tick — the
epoch (§6 below) still gates whether the expensive `phishpred publish` step
actually runs.

## 6. How the epoch gate makes off-tour runs a no-op

Every run computes `phishpred epoch --emit-github-output --submitted
data/predictions/submitted $MODEL_PARAMS` (DEPLOY-CONTRACTS.md §1) — cheap,
no simulation. The model parameters live once in the workflow-level
`MODEL_PARAMS` env var and feed both the gate and the publish step, so the
gate's epoch always matches the epoch `publish` stamps into `meta.json`. The
gate hashes the current prediction state (last played show, the future
schedule, code version, model/n_sims/seed/half_life/compare_models, and a
hash of the submitted-predictions inbox) and compares it to the epoch
recorded in `latest.json` (pulled down at the start of the run).

- If unchanged (`changed=false`), every step gated on
  `steps.gate.outputs.changed == 'true'` is skipped — `phishpred publish`
  (the ~6-minute Monte-Carlo simulation), the R2 snapshot push, the
  `latest.json` pointer update, and the DB persist step all no-op. The job
  exits in seconds. This is what keeps the every-6h cron effectively free
  when Phish isn't on tour: nothing about the prediction state changed, so
  there's nothing new to compute or publish.
- If changed (a show was played, the schedule moved, the code/model
  changed, or a new MCP submission landed in `submitted/`), the full publish
  runs and pushes a new `snapshots/{epoch}/` tree plus an updated
  `latest.json` pointer.

To force a republish even when nothing changed (e.g. testing), trigger
`workflow_dispatch` after bumping `--seed` or `--model` in the workflow
file's single `MODEL_PARAMS` line, or after a code change — either changes
`epoch`'s `code_version` / `model`/`seed` inputs, which is by design (see
`DEPLOY-CONTRACTS.md` §1 for the exact epoch hash inputs).

## 7. Serve tier — deploy the Worker (site + API)

One Cloudflare Worker serves both the built React app (assets binding at
`web/dist`) and the read-only `/api/*` JSON API (R2 binding). Config lives in
`worker/wrangler.toml` — `account_id`, the `custom_domain` routes, and the
`SNAPSHOTS` bucket binding must be filled in (they are, as of this commit).
Two ways to deploy; pick one:

**A. Locally with wrangler** (fastest one-off):

```
cd web && npm run build          # wrangler serves ../web/dist as static assets
cd ../worker
npx wrangler login               # once; opens a browser OAuth flow
npx wrangler deploy
```

**B. Cloudflare git integration (Workers Builds)** — push-to-deploy, the
standing setup. Cloudflare dashboard → Workers → Create → import the GitHub
repo, then set (Advanced settings — the defaults will NOT work for this
monorepo):

| Setting          | Value                                             |
|------------------|----------------------------------------------------|
| Project name     | `phish-predictor-worker` (must match wrangler.toml `name`) |
| Root directory   | `/worker`                                          |
| Build command    | `npm ci --prefix ../web && npm run build --prefix ../web` |
| Deploy command   | `npx wrangler deploy`                              |

Either way, the first deploy attaches the `custom_domain` routes from
wrangler.toml, creating DNS records + certs on the zone automatically. If the
apex already has a DNS record (e.g. from domain parking), Cloudflare prompts
about the conflict — let it replace the record.

**Empty-bucket behavior:** until the first publish (§4-§5) lands a snapshot in
R2, every `/api/*` route 404s (no published epoch) and the site renders from
its bundled fixtures. The tell that live data has arrived: the Tours table's
dist header reads `0/1/2/3/4+` (fixtures predate the 4+ split) and
`/api/latest` returns the epoch JSON.

## 8. Agent / LLM predictions (`mcp:<label>` source columns)

The working path for LLM-generated predictions is the **MCP submission
inbox** — an agent (Claude Desktop/Code, or any MCP client) researches with
the read tools and submits per-song probabilities; publish folds them into the
show pages as extra source columns. Full tool reference: `docs/MCP.md`.

1. **Generate** — run the local MCP server (`python -m uv run phishpred-mcp`
   from the repo root; Claude Desktop config in `docs/MCP.md`) and have the
   agent call `submit_prediction(showdate, model_label, predictions,
   rationale)`. This writes
   `data/predictions/submitted/{model_label}/{showdate}.json` — validated,
   never touching core tables.
2. **Push** — upload the inbox to the R2 `submitted/` prefix (the four `R2_*`
   env vars from §2-§3):
   ```
   python -m uv run python scripts/r2_push.py data/predictions/submitted submitted
   ```
3. **Publish** — nothing else to do: the scheduled workflow pulls `submitted/`
   at the start of every run, and a new submission changes the epoch's
   submissions hash, so the gate reports `changed=true` and the next run
   republishes with a `sources["mcp:{label}"]` column on the matching show
   page. To see it sooner than the next cron tick, trigger
   `workflow_dispatch` (§5).

Two related-but-different LLM paths, for clarity:

- **Built-in `llm:*` per-song column** (`models/llm.py` `LLMSongModel`):
  wired into `predict_show` / `publish --compare-models` (and still available
  offline via `phishpred llm-backtest`). Pass a model string like
  `llm:anthropic` (provider default model) or
  `llm:anthropic:claude-sonnet-5`; the published show docs gain a
  `sources["llm:..."]` column with `kind: "llm"` (DEPLOY-CONTRACTS.md §2).
  Probabilities are floored/renormalized to K exactly like the statistical
  sources, and raw responses are cached on disk per
  `(showid, model, prompt_version)` so a republish of the same epoch never
  re-bills. If the provider key (e.g. `ANTHROPIC_API_KEY`, §3) is missing or
  the call fails, publish skips that source with a stderr warning — the batch
  never crashes on it.

  **Operator switch:** the epoch hash includes `compare_models`, so enabling
  the column is a one-line edit to the workflow's `MODEL_PARAMS` in
  `.github/workflows/publish.yml` — append `--compare-models llm:anthropic`
  (it feeds both the epoch gate and the publish step). The changed epoch makes
  the next run republish with the new column; no other wiring needed.
- **LLM setlist assembler** (`phishpred setlist <date> --llm`): CLI-only
  display; published setlist docs always come from the deterministic sampler.
