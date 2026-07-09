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
| `ANTHROPIC_API_KEY`      | optional — only needed for the `llm:*` prediction column |

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
