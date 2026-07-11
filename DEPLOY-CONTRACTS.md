# Deploy contracts — publish artifacts, API, samples.bin, MCP

Single source of truth for the deployment tier (deploy plan §2–§7). Companion to
`CONTRACTS.md` (library interfaces). If an implementation must deviate, keep these
shapes working (add, don't break). All JSON is UTF-8, keys as written, floats
rounded to 4 decimals unless noted.

Reference source: `src/phishpred/modes.py` (`TourReport`, `RunReport`,
`ChaserReport`), `src/phishpred/predict.py` (`ShowPrediction`),
`src/phishpred/setlist.py` (`SetlistPrediction`), `src/phishpred/simulate.py`
(`SimResult.samples`).

---

## 1. Epoch

The epoch is the identity of a prediction state (deploy plan §6). Recompute only
when it changes.

```
epoch_key = sha256(canonical_json({
  "max_played_show_index": <int>,        # max shows.show_index over indexed shows
  "schedule_hash": <hex12>,              # sha256 of sorted [[showdate,venueid], ...] of future shows
  "code_version": <str>,                 # `git rev-parse --short HEAD`, else "nogit"
  "model": <str>, "n_sims": <int>, "seed": <int>, "half_life": <int>,
  "compare_models": [<str>, ...],        # sorted; extra per-show columns are part of the identity
  "submitted_manifest_hash": <hex12>,    # sha256 of sorted submitted/{label}/{showdate}.json paths+mtimes-or-content-hash
}))[:12]
```

`canonical_json` = `json.dumps(obj, sort_keys=True, separators=(",", ":"))`.

`phishpred epoch [--emit-github-output] [--compare-models m1,m2]`:
- Prints `epoch=<hex12>` and `changed=<true|false>` (changed vs the last published
  epoch, read from `data/predictions/latest.json` — the publish workflow pulls R2
  `latest.json` down to that path at the start of every run). Cheap: no simulation.
- With `--emit-github-output`, appends `epoch=...` and `changed=...` lines to the
  file named by `$GITHUB_OUTPUT`.

---

## 2. Publish artifacts

`phishpred publish --out DIR [--n-sims N=2000] [--model M=heuristic] [--seed S=0]
[--half-life H=50] [--with-samples] [--compare-models m1,m2] [--submitted DIR]`

Writes this tree under `DIR` (default `build/snapshots`). ONE `simulate_horizon`
run per epoch feeds tour + chaser + every run subset (they are reductions over the
same samples). Deterministic given seed.

```
meta.json
tour.json                     # all future shows (the "all" tour pill)
tour/{tour_id}.json           # one per tour — same shape as tour.json, that tour's nights
show/{showdate}.json          # one per future show
setlist/{showdate}.json       # one per future show (deterministic sampler)
samples.bin                   # if --with-samples
samples_meta.json             # if --with-samples
schedule.json                 # full future schedule for the UI
```

`tour/{tour_id}.json` is the identical shape to `tour.json`, reduced over only
that tour's horizon positions from the SAME single simulation (deploy plan §3).
`--sample-sims N` ships a downsampled `samples.bin` of N sims (smaller client
download) while the reduced tables keep full `--n-sims` accuracy.

### meta.json
```json
{
  "epoch": "a1b2c3d4e5f6",
  "created_at": "2026-07-09T14:00:00Z",
  "as_of_showdate": "2026-06-22",         // date of the last indexed (played) show
  "as_of_show_index": 2043,
  "code_version": "482c767",
  "models": ["heuristic"],                // headline model first; extra compare cols appended
  "headline_model": "heuristic",
  "n_sims": 2000, "seed": 0, "half_life": 50,
  "horizon_showdates": ["2026-07-10", "..."],
  "tours": [                              // distinct tour_name values over future shows
    {"id": "summer-2026", "tour_name": "2026 Summer Tour", "has_data": true}
  ]
}
```
`tours[].id` = slug of `tour_name` minus the generic "tour"/"run" words, with the
4-digit year (when present) appended as a suffix so same-season tours in different
years stay distinct (e.g. "summer-2026", "fall-2026", "new-years-2026"); the UI's
tour pills map to these. `has_data` = whether the horizon for that tour is
non-empty and was simulated.

### tour.json  (mirrors `TourReport`, deploy plan headline reduction)
```json
{
  "epoch": "a1b2c3d4e5f6",
  "horizon_showdates": ["2026-07-10", "..."],
  "model": "heuristic", "n_sims": 2000, "half_life": 50,
  "rows": [
    {
      "song": "Harry Hood", "slug": "harry-hood",
      "expected_plays": 2.34, "p_at_least_one": 0.978,
      "dist": {"0": 0.02, "1": 0.20, "2": 0.42, "3": 0.24, "4+": 0.12},
      "bucket": "lock",                    // lock | likely | bustout-watch | longshot
      "gap_ratio": 1.9, "analytic_p": 2.24
    }
  ]
}
```
Rows sorted by `expected_plays` desc. Buckets per `modes._bucket_for`.

### show/{showdate}.json  (multi-source; deploy plan §3)
```json
{
  "showdate": "2026-07-10",
  "venue_name": "Ruoff Music Center", "city": "Noblesville", "state": "IN",
  "epoch": "a1b2c3d4e5f6", "k": 22.4,
  "sources": {
    "heuristic": {
      "model": "heuristic", "kind": "statistical",
      "rows": [
        {"song": "Harry Hood", "slug": "harry-hood", "prob": 0.61, "gap": 7,
         "drivers": ["rate=0.310", "due x1.4"]}
      ]
    }
    // "lr", "gbm": same shape when --compare-models includes them.
    // "llm:anthropic": {"model": "llm:anthropic:claude-sonnet-5", "kind": "llm", "rows": [...]}
    //   when --compare-models includes an llm:<provider>[:<model-id>] spec.
    // "mcp:<label>": {"model": "...", "kind": "mcp", "rationale": "...", "submitted_at": "...", "rows": [...],
    //                 "setlist": {"sets": {"1": [{"slug","song"},...], "2": [...], "e": [...]}},  // §5; absent if not submitted
    //                 "versions": [{"submitted_at","rationale","rows":[...],"setlist"?:{...}}, ...]}  // §5; PRIOR takes, oldest first; absent if none
  }
}
```
Fold policy for mcp extras: each PRIOR version's rows are validated, clamped,
renormalized, and capped exactly like the latest rows; a version that fails
validation is dropped individually with a warning. A submitted `setlist` whose
slugs aren't all known is dropped whole (warning) — a partially-resolved
setlist would corrupt the placement benchmark. Setlist slugs are resolved to
`{"slug","song"}` objects at fold time.
Each `sources[*].rows` sorted by `prob` desc. Statistical/LLM rows come from
`predict_show` / `models.llm`; both are floored and renormalized to K the same
way. The source key is the `--compare-models` string as passed; the `model`
field is the resolved name (a defaulted model id filled in). An `llm:*` compare
source whose call fails (missing provider API key, network, malformed response)
is dropped for the rest of the batch with a stderr warning — publish never
crashes on it, and `meta.json`'s `models` still lists it as declared. `mcp:*`
sources are folded in from the submissions inbox (§5).

### setlist/{showdate}.json  (mirrors `SetlistPrediction`)
```json
{
  "showdate": "2026-07-10", "venue_name": "Ruoff Music Center",
  "era": "4.0", "model": "sampler", "seed": 1286289815,
  "skeleton": {"1": 8, "2": 7, "e": 2},
  "sets": {
    "1": [{"song_name": "Sample in a Jar", "slug": "sample-in-a-jar",
           "songid": 123, "slot": "set1-open", "prob": 0.42, "segue_mark": " > "}],
    "2": [ ... ], "e": [ ... ]
  }
}
```
Set labels are the raw `set` keys ("1","2","3","e","e2",...). `segue_mark` is the
mark AFTER this song ("", " > ", " -> "). `seed` is the per-show sampler seed
`zlib.crc32(f"{global_seed}:{showdate}")` — not the global publish seed — so
consecutive nights draw decorrelated setlists; within a multi-night same-venue
run, later nights exclude songs placed on earlier nights (predicted or actually
played), and a previous night at a different venue is discouraged (×0.02).

### schedule.json  (full future schedule for the UI's schedule sidebar + multiselect)
```json
{
  "shows": [
    {"showdate": "2026-07-10", "venue_name": "Ruoff Music Center",
     "city": "Noblesville", "state": "IN", "tour_id": "summer-2026",
     "tour_name": "2026 Summer Tour", "has_data": true}
  ]
}
```
`has_data` = a `show/{showdate}.json` was published for it (true for all simulated
future shows). Ordered by showdate.

### samples_meta.json  (only with --with-samples)
```json
{
  "epoch": "a1b2c3d4e5f6", "n_sims": 2000, "seed": 0,
  "horizon_showdates": ["2026-07-10", "..."],
  "horizon_showids": [1697, 1698, 1699, "..."],
  "horizon_venueids": [1497, 1497, 1497, "..."],
  "vocab": [
    {"i": 0, "songid": 123, "slug": "harry-hood", "name": "Harry Hood"}
  ]
}
```
`vocab[i].i` == array index i (redundant but explicit). The bin references vocab
index `i`. `horizon_showids` is ordered like `horizon_showdates` (readers must
tolerate its absence in snapshots published before it was added).

### 2a. catalog.json  (only with --with-catalog)

History for the client-side personalized "due to see" view: the browser fetches a
user's phish.net seedfile (attended showdates), computes their seen-songs from
`by_show`, then reduces `samples.bin` locally for the unseen songs (no per-user
server compute). Epoch-pinned; ~one file, CDN-cached.
```json
{
  "epoch": "a1b2c3d4e5f6",
  "songs": [{"songid": 123, "slug": "harry-hood", "name": "Harry Hood",
             "plays": 421, "last": "2026-07-07"}],   // sorted by plays desc — the ranking axis
  "by_show": {"2024-08-06": [12, 45, 88, "..."]}      // each PAST show -> songids played
}
```

---

## 3. samples.bin — binary format (CROSS-LANGUAGE: Python writer, JS reader)

Little-endian. Encodes `SimResult.samples[m][t]` = set of vocab indices.

```
Header (17 bytes):
  bytes 0..3   magic  = ASCII "PSMP"
  byte  4      version = 0x01
  bytes 5..8   n_sims   (uint32 LE)
  bytes 9..12  n_shows  (uint32 LE)   # == len(horizon_showdates)
  bytes 13..16 n_vocab  (uint32 LE)

Body: for m in range(n_sims):          # outer loop sims
        for t in range(n_shows):       # inner loop horizon position
          count       : uvarint        # number of songs sampled in sim m, show t
          idx[0..count): uvarint each  # vocab indices, ascending sorted
```

- **uvarint** = unsigned LEB128: 7 bits/byte, little-endian groups, high bit =
  continuation. (Same as protobuf varints.)
- Indices within one (m,t) MUST be written ascending sorted (lets readers stop
  early / use them directly; readers must not assume more than "a set").
- No delta encoding (keep the codec trivially identical across languages).

The whole file is served gzip'd by the CDN; do not gzip inside the format.

**Reference vectors** (writer and reader unit tests MUST both pass these):
- `uvarint(0)   = [0x00]`
- `uvarint(1)   = [0x01]`
- `uvarint(127) = [0x7F]`
- `uvarint(128) = [0x80, 0x01]`
- `uvarint(300) = [0xAC, 0x02]`
- A file with n_sims=1, n_shows=1, vocab=[0,1,2], sample {0,2}:
  header + `[0x02, 0x00, 0x02]` (count=2, idx 0, idx 2).

---

## 4. Sample reductions (shared by Worker `/api/run|chaser` and the browser)

Given decoded `samples[m][t]` (sets of vocab indices), `vocab`, and
`horizon_showdates`:

```
# selected = subset S of horizon indices (positions t)
per_night_prob[t][i] = mean_m [ i in samples[m][t] ]
p_union_over_S[i]    = mean_m [ i in UNION_{t in S} samples[m][t] ]
expected_plays_S[i]  = mean_m [ count of t in S with i in samples[m][t] ]
most_likely_night(i) = argmax_{t in S} per_night_prob[t][i]
# chaser (song i, horizon S in order):
first_hit_index(m)   = min t in S with i in samples[m][t], else miss
P(next play at t)    = mean_m [ first_hit_index(m) == t ]
p_not_within         = mean_m [ miss ]
```
These MUST match `modes.run_mode` / `modes.chaser_mode` numerically (same samples →
same result). Run mode's headline `p_at_least_one` is `p_union_over_S`, NOT
`1-Π(1-p_i)`.

---

## 5. Submissions inbox (MCP → publish)

`submitted/{model_label}/{showdate}.json` (local dir passed via `--submitted`, and
the R2 `submitted/` prefix). Written by the MCP `submit_prediction` tool, read by
`publish` and folded into `show/{showdate}.json` under `sources["mcp:"+label]`.

```json
{
  "model_label": "claude-desktop",       // becomes source key "mcp:claude-desktop"
  "showdate": "2026-07-10",
  "epoch": "a1b2c3d4e5f6",               // epoch the agent saw (pinned)
  "submitted_at": "2026-07-09T13:00:00Z",
  "rationale": "Fluffhead is due; ...",  // optional
  "predictions": [{"slug": "harry-hood", "prob": 0.55}, ...],
  "setlist": {                           // OPTIONAL structured setlist call (2nd benchmark, §8)
    "sets": {"1": ["slug", "..."], "2": ["..."], "e": ["..."]}
  },
  "versions": [ ... ]                    // OPTIONAL prior submissions, oldest first (see below)
}
```
`publish` validates: known slugs, probs in (0,1] (booleans rejected), no duplicate
slugs, and a filesystem-safe label directory; it resolves slug→song_name and emits
rows sorted by prob. Probs are published AS SUBMITTED (each clamped to <=0.99) and
scaled DOWN via `probs.renormalize_to_k` only when their sum exceeds the era's
expected setlist size K — never scaled up, so a sparse shortlist keeps its stated
probabilities. `rationale` is truncated to 4000 chars and rows capped at the publish
top-N at fold time. Malformed/unknown/invalid submissions are skipped with a logged
warning (never crash publish).

**Structured setlist (optional).** `setlist.sets` keys match `^(\d+|e\d*)$` (same
raw set labels as §2's setlist doc); values are non-empty ordered lists of known
slugs. No slug may appear twice anywhere in the setlist; total songs <= 40. The
setlist is INDEPENDENT of `predictions` — two different benchmarks (§8); a
submission may carry either or both. `submit_prediction` raises on an invalid
setlist; the publish fold skips-with-warning (§2 fold policy). Submissions
without a setlist (including all pre-existing "v0" files) simply sit out the
setlist benchmark.

**Versioning.** A resubmission for the same `{label}/{showdate}` must not lose
history — the improvement arc across takes IS a product feature (§8). On
rewrite, `submit_prediction` appends the prior file's content (minus its own
`versions` key) to the new file's `versions` array (prior versions carried
over first, oldest first). At most the 10 most recent prior versions are kept;
older ones are dropped. Legacy files without `versions` remain valid. The
whole file (latest + versions) is one R2 object, so the epoch's
`submitted_manifest_hash` picks up resubmissions with no mechanics changes.

---

## 6. Worker API (deploy plan §7)  — read-only JSON over R2

Base: same Worker serves static assets (`/*`) and the API (`/api/*`). All responses
`application/json`, `Cache-Control: public, max-age=300` (except `/api/run` which
may be `no-store` or short). CORS: `Access-Control-Allow-Origin: *` (public data).

```
GET  /api/latest                     -> meta.json (current epoch)
GET  /api/scoreboard                 -> scorecards/scoreboard.json (§8; NOT epoch-scoped)
GET  /api/scorecard/{showdate}       -> scorecards/{showdate}.json (§8; NOT epoch-scoped)
GET  /api/schedule                   -> schedule.json
GET  /api/tour                       -> tour.json (all future shows)
GET  /api/tour/{tour_id}             -> tour/{tour_id}.json (one tour)
GET  /api/show/{showdate}            -> show/{showdate}.json  (all sources)
GET  /api/setlist/{showdate}         -> setlist/{showdate}.json
GET  /api/samples                    -> raw samples.bin (Content-Type application/octet-stream)
                                        + header "x-samples-meta-url" or a sibling /api/samples-meta
GET  /api/samples-meta               -> samples_meta.json
POST /api/run   { "showdates": ["2026-07-10","2026-07-12"] }
     -> { "showdates": [...], "rows": [
          {"song": "...", "slug": "...", "p_at_least_one": 0.9,
           "per_night_probs": [0.6,0.5], "most_likely_night_date": "2026-07-10"} ],
          "missing": ["2026-07-14"] }   // selected dates lacking horizon coverage
GET  /api/chaser/{slug}              -> ChaserReport-shaped JSON reduced from samples,
                                        minus the DB-derived fields (`model`,
                                        `historical_play_count`, `low_signal_caveat`
                                        are omitted, not fabricated), plus `songid`
                                        and `epoch`. Keys match `modes.ChaserReport`:
                                        `horizon_dates`, `p_not_within_horizon`,
                                        `modal_show_date`, `median_show_date`,
                                        `expected_shows_until_next_play`,
                                        `distribution: [{showid, showdate, probability}]`.
                                        `horizon_showids` (and per-entry `showid`) are
                                        null/omitted for snapshots whose samples_meta
                                        predates the `horizon_showids` field.
```

The Worker resolves the current epoch via `latest.json`, then reads
`snapshots/{epoch}/...` from the R2 binding. `/api/run` and `/api/chaser` decode
`samples.bin` (§3) and reduce (§4) — no Python, no simulator in the request path.
Endpoints requiring D1 (`/api/history/...`) are deferred (deploy plan step 6).

---

## 7. Frontend (deploy plan §7, design handoff)

React + Vite, built to static assets served by the Worker. Recreate the design in
`scratchpad/design/.../Iccuthologist UI.dc.html` faithfully (tokens in its README).
Data via `fetch(import.meta.env.VITE_API_BASE + "/api/...")`. `VITE_API_BASE`
defaults to "" (same origin) in prod; a dev fallback may serve bundled sample
fixtures matching the shapes above. Show multiselect → `POST /api/run` (exact joint
reduction), NOT the mock's `1-Π(1-p)`. Tours table ← `/api/tour`; per-show ←
`/api/show/{showdate}`; setlist ← `/api/setlist/{showdate}`.

---

## 8. Accuracy scorecards (frozen predictions → post-show scoring)

Past-prediction accuracy for shows we published predictions for. Two
epoch-INDEPENDENT R2 prefixes (append-only; never under `snapshots/{epoch}/`):

```
frozen/show/{showdate}.json      # the FROZEN pre-show prediction (§2 show shape, all sources)
scorecards/{showdate}.json       # per-show scorecard, written once the show is played
scorecards/scoreboard.json       # rolling index + per-model aggregates
```

**Freeze rule.** Every publish run pushes `build/snapshots/show/` →
`frozen/show/` (overwrite). The horizon only contains future shows, so once a
show is played it drops out of publish and its frozen file stops changing —
the last pre-play publish IS the frozen prediction. A scorecard may only ever
be computed from `frozen/show/{showdate}.json`, never from a current-epoch
artifact. The frozen file's own `epoch` field records provenance. Shows played
before `frozen/` existed have no frozen file and are simply unscoreable.

**Scoring.** `phishpred score --frozen DIR --out DIR [--rescore-days 7]`
(new `src/phishpred/score.py`). For each `frozen/show/{showdate}.json` whose
show is indexed in the DB (played, `show_index IS NOT NULL`) and
`showdate < UTC today`: compute the scorecard and write
`{out}/{showdate}.json`. If a scorecard already exists it is skipped, UNLESS
`showdate >= UTC today - rescore_days` — inside that window scoring is
idempotent-rewrite, so late setlist corrections and partially-ingested
west-coast shows self-heal on the next run. Afterwards ALWAYS rebuild
`{out}/scoreboard.json` from every scorecard present (empty shows/models
lists are valid). The played set is the show's DISTINCT performed slugs.

### scorecards/{showdate}.json
```json
{
  "showdate": "2026-07-10",
  "venue_name": "Ruoff Music Center", "city": "Noblesville", "state": "IN",
  "frozen_epoch": "228c7eb3a0e9",
  "scored_at": "2026-07-11T06:10:00Z",
  "phishnet_url": "https://phish.net/setlists/?d=2026-07-10",
  "n_played": 21,
  "played": [{"slug": "harry-hood", "song": "Harry Hood"}],   // distinct, setlist order
  "played_sets": {                                            // per raw set label, position order,
    "1": [{"slug": "...", "song": "..."}], "2": [...], "e": [...]   // distinct within each set
  },
  "sources": {
    "heuristic": {
      "model": "heuristic", "kind": "statistical", "n_rows": 40,
      "metrics": {
        "hits_top10": 6,          // hits among the first min(10, n_rows) rows (rows are prob desc)
        "hit_rate_top10": 0.6,    // hits_top10 / min(10, n_rows)
        "recall": 0.4286,         // |played ∩ shortlist| / n_played
        "brier": 0.081,           // mean over rows of (prob - hit)^2
        "log_loss": 0.31          // mean over rows of -(y·ln p + (1-y)·ln(1-p)), p clamped to [0.001, 0.999]
      },
      "best_call": {"song": "...", "slug": "...", "prob": 0.12},     // hit with the LOWEST prob; null if no hits
      "biggest_whiff": {"song": "...", "slug": "...", "prob": 0.61}, // miss with the HIGHEST prob; null if no misses
      "rows": [{"song": "...", "slug": "...", "prob": 0.61, "hit": true}],  // frozen rows, prob desc
      "setlist_score": null,      // see below; null when the frozen source has no setlist (it sits out)
      "versions": [ ... ]         // see below; absent/empty when only one take exists
    }
    // "mcp:claude-fable", "mcp:gemini-3.5-flash-high", ...: same shape; mcp
    // sources keep their frozen "rationale"/"submitted_at" fields verbatim.
  },
  "missed_by_all": [{"slug": "...", "song": "..."}]  // played songs in NO source's shortlist
}
```
Metrics are computed over each source's own shortlist rows only — shortlists
differ in length across sources (`n_rows` is published so the UI can caveat).
The scorecard embeds everything the UI needs; readers never re-fetch frozen
artifacts. Boundary: this tier stores OUR predictions and a flat played-song
list for hit/miss context — full setlists (sets, segues, jamcharts) remain
phish.net's domain; `phishnet_url` links out.

**Setlist benchmark** (`setlist_score`, non-null only when the frozen source
carries a `setlist` — sources without one, including legacy v0 submissions,
sit out and are excluded from scoreboard setlist aggregates):
```json
{
  "n_songs": 18,
  "sets": {"1": [{"slug","song","hit","placed"},...], "2": [...], "e": [...]},  // predicted setlist, annotated
  "hits": 9, "hit_rate": 0.5,        // predicted songs played ANYWHERE / n_songs
  "placed": 6, "placed_rate": 0.6667,// of hits: played in the PREDICTED set (denominator = hits; 0/0 -> 0)
  "marquee": {                       // positional bragging rights, vs played_sets
    "opener": true,                  //   predicted sets["1"][0] == played_sets["1"][0]
    "set1_closer": false,            //   predicted last of set 1 == actual last of set 1
    "set2_opener": false, "set2_closer": true,
    "encore": true                   //   predicted "e" overlaps actual "e" (any hit; encores are 1–2 songs)
  },
  "marquee_calls": 3,                // count of true marquee flags
  "exact_calls": 2,                  // positions where predicted sets[s][i] == played_sets[s][i]
  "sharpshooter": true               // exact_calls >= 2 — the badge (rare by design)
}
```
Marquee flags compare only set keys present on BOTH sides; a predicted set the
band never played (e.g. a called "3") contributes misses to hit/placed but no
marquee slots. `exact_calls` subsumes opener/closer positions — the badge is
about ordering, the marquee flags are the story stats.

**Version scoring** (`versions`): every PRIOR frozen take is scored with the
same machinery, oldest first — the top-level source entry IS the final take
(the official benchmark; scoreboard aggregates use only it):
```json
[{"submitted_at": "...", "after_showdate": "2026-07-11" | null,  // latest played showdate this take could know
  "metrics": { ...same shape... }, "setlist_score": { ...same shape... } | null,
  "rows": [{"song","slug","prob","hit"}, ...]}]
```
`after_showdate` is a UI labeling heuristic, not a metric: the latest played
showdate S with S < min(UTC date of submitted_at, this showdate) and S within
the 10 days before this show (runs never gap longer); null -> "pre-run". A
mid-show submission (UTC date already past S) counts S as known — acceptable
slop, documented here so nobody litigates it later.

### scorecards/scoreboard.json
```json
{
  "updated_at": "2026-07-11T06:10:00Z",
  "shows": [                                 // every scored show, showdate DESC
    {"showdate": "2026-07-10", "venue_name": "Ruoff Music Center",
     "city": "Noblesville", "state": "IN", "n_played": 21,
     "source_keys": ["heuristic", "mcp:claude-fable"]}
  ],
  "models": {                                // unweighted means over scored shows (FINAL takes only)
    "heuristic": {"kind": "statistical", "n_shows": 3, "hit_rate_top10": 0.55,
                  "recall": 0.41, "brier": 0.09, "log_loss": 0.29,
                  "setlist": {                       // absent when no setlist-scored shows
                    "n_shows": 2, "hit_rate": 0.44, "placed_rate": 0.61,
                    "marquee_calls": 5, "exact_calls": 3, "sharpshooters": 1
                  },
                  "refresh_gain": {                  // absent when no multi-take shows; the Monty Hall dividend
                    "n_shows": 2,                    //   shows with >= 1 prior version
                    "mean_hit_rate_top10_delta": 0.15,   // mean(final - first take)
                    "mean_recall_delta": 0.08
                  }}
  }
}
```
`setlist.marquee_calls`/`exact_calls`/`sharpshooters` are TOTALS over scored
shows (they're counting stats); `hit_rate`/`placed_rate` are unweighted means.
`refresh_gain` compares each show's FIRST take against its final one.

**Workflow wiring** (`.github/workflows/publish.yml`): the restore step also
pulls `frozen/` → `data/frozen/` and `scorecards/` → `data/scorecards/`; every
run then executes `phishpred score --frozen data/frozen/show --out
data/scorecards` and pushes `data/scorecards` → `scorecards` UNGATED, because
the epoch does NOT change when an already-indexed show's setlist merely gains
rows (partial mid-show ingest completing later) — the rescore window needs
every run to fire for partial scorecards to self-heal. The publish itself and
the `build/snapshots/show` → `frozen/show` freeze stay gated on
`changed == 'true'`.

---
