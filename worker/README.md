# phish-predictor-worker

Serve tier: a single Cloudflare Worker that serves the built React app
(`../web/dist`, `ASSETS` binding) and a read-only `/api/*` JSON API backed by
a Cloudflare R2 binding (`SNAPSHOTS`). No Python, no simulator, no secrets in
the request path -- see `../DEPLOY-CONTRACTS.md` §6 for the endpoint
contract and `../phish-predictor-deploy-plan.md` §7 for the overall
architecture.

## Layout

```
worker/
  src/samples.ts   samples.bin decoder (§3) + §4 reductions (pure, framework-free)
  src/r2.ts         R2 read helpers (getJson, getBytes, resolveEpoch)
  src/index.ts      fetch handler / router -- every /api/* endpoint
  test/samples.test.ts   vitest: §3 reference vectors + §4 reduction formulas
  wrangler.toml
  package.json
  tsconfig.json
```

## Setup

```
cd worker
npm install
```

## Tests

Pure-logic unit tests (decoder + reductions) run under plain Node/vitest --
no Workers runtime, no R2, no network required:

```
npm test          # vitest run
npm run test:watch
npm run typecheck  # tsc --noEmit
```

These assert every DEPLOY-CONTRACTS.md §3 reference vector (`uvarint(0)`,
`uvarint(1)`, `uvarint(127)`, `uvarint(128)`, `uvarint(300)`, and the worked
`n_sims=1, n_shows=1, vocab=[0,1,2], sample {0,2}` file example), plus §4
reduction formulas against a hand-built 2-night sample set (joint union vs.
per-night marginals, most-likely-night tie-breaking, chaser first-hit-index
distribution, modal/median/expected-shows, and the all-miss edge case).

## Local dev (`wrangler dev`)

```
npm run dev
```

Requires:
- `../web/dist` to exist (`npm run build` in `../web`), since `wrangler.toml`
  points the `ASSETS` binding at it. It doesn't need to exist for `npm test`.
- An R2 bucket for the `SNAPSHOTS` binding with real data (or `--local`
  with seeded objects). Without real snapshot data, every `/api/*` route
  other than routing itself will correctly 404 (no published epoch), which
  is expected without R2 credentials/data available in this environment.

To seed a local (`--local`, i.e. Miniflare-backed) R2 bucket for manual
testing, write the `publish` CLI's output tree (once `phishpred publish`
exists) under the bucket, e.g.:

```
wrangler r2 object put SNAPSHOTS/latest.json --local --file=./latest.json
wrangler r2 object put SNAPSHOTS/snapshots/<epoch>/meta.json --local --file=...
wrangler r2 object put SNAPSHOTS/snapshots/<epoch>/samples.bin --local --file=...
# ...etc for tour.json, schedule.json, show/*.json, setlist/*.json, samples_meta.json
```

Then `wrangler dev --local` serves against that seeded bucket. A full
`wrangler dev` against **remote** R2 needs a real Cloudflare account +
credentials, which aren't available in this environment -- verify via the
unit tests above instead, and `npx wrangler deploy --dry-run` to confirm the
config compiles offline.

## Deploy

```
npm run deploy   # wrangler deploy
```

### `wrangler.toml` fields the operator must fill in before deploying

| Field | Where | What to put |
|---|---|---|
| `[[r2_buckets]].bucket_name` | R2 bucket block | Real R2 bucket name, e.g. created via `wrangler r2 bucket create phish-predictor-snapshots` |
| `[[r2_buckets]].preview_bucket_name` (optional, commented out) | R2 bucket block | A separate bucket for `wrangler dev --remote` / preview, if desired |
| `account_id` (commented out) | top level | Your Cloudflare account id (`wrangler whoami`) |
| `routes` (commented out) | top level | The custom domain/zone this Worker should answer requests on, e.g. `{ pattern = "yourdomain.com/*", custom_domain = true }` |

Everything else (`name`, `main`, `compatibility_date`, the `[assets]` block,
the `SNAPSHOTS` binding name) is ready to use as-is.

### Routing notes

`[assets]` sets `run_worker_first = ["/api/*"]`: Cloudflare invokes this
Worker's `fetch()` handler for every `/api/*` request (so the API always
runs, never falls back to the SPA's `index.html`), while every other path is
served directly from the `ASSETS` binding, including the
`not_found_handling = "single-page-application"` fallback for client-side
routing in the React app.

## Known deviations / open questions from DEPLOY-CONTRACTS.md

- **`/api/chaser/{slug}`** is reduced purely from `samples.bin` +
  `samples_meta.json`, which is all the Worker has access to (no DB, no
  Python `ChaserReport` dataclass). It matches `ChaserReport`'s shape for
  everything derivable from the samples (`p_not_within_horizon`,
  `modal_show_date`, `median_show_date`, `expected_shows_until_next_play`,
  `distribution`), plus `song`, `slug`, `epoch`, `n_sims`,
  `horizon_showdates`. It **omits** `historical_play_count` and
  `low_signal_caveat` (need the `performances` table) and `model` (not
  present in `samples_meta.json`) -- these would need either a DB/D1 binding
  or a small addition to `samples_meta.json` in the publisher if wanted.
- **`/api/run` response** includes `most_likely_night_index` in addition to
  the `most_likely_night_date` shown in the DEPLOY-CONTRACTS.md §6 example
  response shape -- additive per the doc's "add, don't break" rule.
- **`GET /api/samples`** sets both the `x-samples-meta-url` header (per the
  spec's "or") and serves `/api/samples-meta` as a real sibling endpoint, so
  clients can use either.
- Float rounding to 4 decimals (DEPLOY-CONTRACTS.md preamble) is applied to
  the on-the-fly-computed `/api/run` and `/api/chaser/{slug}` responses.
  Passthrough JSON (`meta.json`, `tour.json`, `show/*.json`, etc.) is
  round-tripped as published by the Python `publish` command, which already
  rounds via `modes._round_floats` -- the Worker does not re-round it.
