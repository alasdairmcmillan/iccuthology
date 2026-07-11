# Rotation-stats deep-dive: due-ness reference, rate-floor window, era-4 rotation

Analysis-only study (no `src/` changes). Feature frame from
`phishpred.features.build_features(conn, half_life=50)` over the full DB
(616,916 candidate rows, 1984-2026). Predictive evaluations restricted to
`showdate` year >= 2009 (training era); era-4 = 2021+ via `config.era_for_year`.
All end-to-end numbers reimplement the **currently committed** heuristic formula
(`score = base * m_prev_show * m_in_run * m_venue * m_due`, no `m_cooldown` — that
term is being added concurrently and is orthogonal to everything here).

---

## TL;DR — recommendations

### Question A (due-ness reference gap): **KEEP the career-median `gap_ratio` in `m_due`. Do NOT switch to a recent-window reference.**
- The tempting single-feature signal is a **trap**. As a raw ordering score,
  career `gap_ratio` has ROC-AUC **0.412** (below 0.5 — it *anti*-orders) while
  `recent_ratio_50` (= `gap * plays_last_50 / 50`) scores **0.784**. That looks
  like a slam-dunk for switching.
- **But `m_due` is a multiplier on top of a rate `base`, not a standalone score.**
  Judged end-to-end on per-show ranking (the real task), the current career
  reference **wins on every metric**, and by the widest margin in era-4:
  - era-4 Hit@20/show: **5.22 (career, CURRENT)** vs 4.92 (recent-50) vs 4.79 (no `m_due`).
  - 2009+ Hit@20/show: **5.42 (career)** vs 5.33 (recent-50) vs 5.24 (no `m_due`).
  - The recent-window ratio is collinear with `base` (it *is* a rate × gap), so it
    re-counts information already in `base`; career `gap_ratio` is rate-independent,
    which is exactly why it adds orthogonal within-show lift.
- The current coefficient/clip is already at the optimum. A strength sweep
  (`m_due = 1 + coef*clip(gap_ratio-1, 0, cap)`) peaks at **coef 0.3, cap 2** — the
  shipped values. coef 0.5/0.7/1.0 and cap 3 all *reduce* Hit@K.
- **No formula change. No new parameters.** If anything, this study is positive
  evidence the shipped `m_due` is well-calibrated; leave it.

### Question B (rate-floor window): **KEEP the 150-show floor. Do NOT swap to or add a 50-show floor.**
- Swapping `plays_last_150/150` → `plays_last_50/50` inside the floor is a **wash
  overall** (base AUC 0.8437 → 0.8437) and **marginally hurts the exact cohorts it
  was meant to help**: the accelerating cohort (0.7924 → 0.7775) and the
  recently-debuted cohort (0.8450 → 0.8382). Adding it as `max(r50, r150)` also hurts.
- Root cause: `decayed_rate` (half-life 50 shows) is *already* an
  exponentially-weighted recent-rate estimate with a ~50-show center of mass, so it
  captures acceleration on its own. `decayed_rate` alone is the best single ordering
  feature (0.8451). A 50-window floor is a **noisier duplicate** (1 play in 50 = a
  0.02 spike) that, because it binds more often (32.3% of played rows vs 16.7% for
  r150), *displaces* the better `decayed_rate` in the `max`.
- The 150-floor's job is magnitude preservation for steady-but-rare songs, not
  ranking; even for that cohort it only binds on **6%** of their played rows and
  lifts `base` a median **1.16×** — a minor mechanism working as intended. Keep it.

### Broader story (headline numbers)
- **Shows/calendar-year, era-4: ~46, not ~62.** Full recent years: 2022=46,
  2023=49, 2024=41, 2025=47. **Flag:** `models/notebook.py`'s comment "`plays_last_50`
  (~62 shows/yr in era 4)" over-states the rate; the true figure makes
  `plays_last_50` ≈ **1.08 calendar years** — still a good trailing-year stand-in,
  but the "62" annotation is wrong and should read ~46.
- **Within-run repeats are essentially zero** (repeat rate ≤ 0.001 at every gap when
  `played_in_run=1`) — strong support for the hard/`0.05` in-run mask.
- **Across-run repeat rate rises with gap** and peaks around gap 5, giving a clean
  empirical basis for cooldown (see §Broader story for the gap-1..3 table the
  cooldown calibration wants).
- **phish.net's hard 3-show exclusion would discard 11.3% of real era-4 plays**
  (19.8% for 2009+). Our soft cooldown keeps them — the right call.

---

## Question A — due-ness reference gap (evidence)

### A1. How often do career-median and recent-window expected gaps diverge?
Rows: 2009+, `cum_plays>=2`, `plays_last_150>0` (n=198,452). `div = recent_exp_gap / career_median_gap`.

| comparison | median div | recent >1.5× slower | recent <0.67× faster | material (either) |
|---|---|---|---|---|
| recent-150 / career | 2.78 | 74.8% | 3.1% | 77.9% |
| recent-50 / career | 2.38 | 69.9% | 3.8% | 73.7% |

The raw divergence is large — but it is **overwhelmingly one-directional** (recent
"slower"), which is an **artifact of the estimator, not staleness**. `recent_exp_gap
= window / plays_last_window`; for the vast majority of candidate rows
`plays_last_150` is a handful, so the window estimate is biased high and noisy.
The genuine "accelerating" divergences (recent faster) are only ~3%.

**Named-song reality check** (latest era-4 candidate snapshot) shows career median is
*not* stale for the songs that matter — including the newer songs the hypothesis
named (Evolve 30 plays, Ether Edge 14 plays):

| song | cum_plays | career_median_gap | recent_exp_gap_50 | recent_exp_gap_150 | gap_ratio |
|---|---|---|---|---|---|
| Ghost | 233 | 4 | 5.0 | 4.4 | 0.50 |
| Sand | 163 | 4 | 5.0 | 4.7 | 0.50 |
| Fluffhead | 291 | 5 | 7.1 | 7.9 | 0.60 |
| Tweezer | 415 | 4 | 6.3 | 5.6 | 2.25 |
| Evolve | 30 | 5 | 7.1 | 5.8 | 2.20 |
| Ether Edge | 14 | 9 | 12.5 | 10.7 | 0.33 |

For every one, career median and the recent estimates agree to within a couple of
shows — career median is the *lower-variance* cadence measure.

### A2. Which ratio better orders P(play)? (2009+, `cum_plays>=2`, n=246,379)
Single-feature ROC-AUC (higher ratio → predicts play):

| score | AUC |
|---|---|
| career `gap_ratio` | **0.412** |
| `recent_ratio_150` (gap·plays150/150) | 0.692 |
| `recent_ratio_50` (gap·plays50/50) | **0.784** |
| `recent_ratio_10` | 0.735 |

Empirical play-rate by ratio bucket (monotonicity):

| bucket | career gap_ratio | recent_ratio_50 |
|---|---|---|
| <0.5 | 0.026 | 0.020 |
| 0.5-0.8 | 0.076 | 0.092 |
| 0.8-1.0 | 0.119 | 0.111 |
| 1.0-1.25 | 0.103 | 0.176 |
| 1.25-1.6 | 0.116 | 0.182 |
| 1.6-2 | 0.115 | 0.166 |
| 2-3 | 0.082 | 0.156 |
| 3-5 | 0.053 | 0.126 |
| 5+ | 0.016 | — |

Career `gap_ratio` is an **inverted-U that then falls off a cliff**: play-rate peaks
near ratio≈1 and *declines* as the ratio climbs — yet `m_due` *increases* with the
ratio. Taken in isolation this looks broken. `recent_ratio_50` is far cleaner (rises
to a plateau near 1.25-1.6). **This is the evidence that argues for switching.**

### A3. End-to-end: it reverses. (per-show Hit@K — the metric that matters)
Full committed formula, only `m_due`'s reference varied. n_shows: 679 (2009+), 235 (era-4).

| m_due config | 2009+ Hit@20 | 2009+ Hit@25 | era-4 Hit@20 | era-4 Hit@25 |
|---|---|---|---|---|
| none | 5.24 | 6.14 | 4.79 | 5.60 |
| **career coef0.3 (CURRENT)** | **5.42** | **6.27** | **5.22** | 5.95 |
| career coef0.5 | 5.32 | 6.18 | 5.11 | 5.98 |
| career coef0.7 | 5.19 | 6.02 | 5.06 | 5.84 |
| career coef1.0 | 4.93 | 5.85 | 4.86 | 5.65 |
| recent50 coef0.3 | 5.33 | 6.24 | 4.92 | 5.78 |
| recent50 coef0.5 | 5.34 | 6.25 | 4.97 | 5.80 |
| recent150 coef0.5 | 5.23 | 6.12 | 4.93 | 5.69 |

**Why the reversal:** global AUC rewards cross-show separation, and `recent_ratio_50`
gets its 0.784 almost entirely from the `plays_last_N` rate term — information that
`base` *already* contains. Within a single show (where `base` sets the level and
`m_due` only re-ranks), the rate-independent career `gap_ratio` supplies genuinely
new lift; the recent ratio is redundant with `base` and slightly distorts it. The
lesson: **judge a multiplier by its marginal end-to-end effect, never by its own
AUC.** No blend of the two beat career-alone either (blend .5/.5: 5.33 / 5.01).

---

## Question B — rate-floor window length (evidence)

Base-rate ordering AUC by definition, 2009+ and cohorts. `base = max(decayed_rate,
w_recent * rate)`; `w_recent = clip((4-gap_ratio)/3, 0, 1)`.

| feature / base variant | ALL 2009+ | era-4 | debuted (age≤100) | accelerating (r50>1.5·r150) | steady-rare (1-3/150) |
|---|---|---|---|---|---|
| n rows | 301,446 | 106,416 | 31,394 | 38,225 | 97,678 |
| `decayed_rate` alone | 0.8451 | 0.8376 | 0.8450 | 0.7924 | **0.7257** |
| `plays_last_150/150` | 0.8392 | 0.8356 | 0.8045 | 0.7734 | 0.6459 |
| `plays_last_50/50` | 0.8351 | 0.8239 | 0.8385 | 0.7831 | 0.6591 |
| `plays_last_10/10` | 0.7321 | 0.6990 | 0.7842 | 0.7272 | 0.5882 |
| **base=max(decayed, wr·r150) [CURRENT]** | **0.8437** | **0.8364** | **0.8450** | **0.7924** | 0.7128 |
| base=max(decayed, wr·r50) | 0.8437 | 0.8347 | 0.8382 | 0.7775 | 0.6991 |
| base=max(decayed, wr·max(r50,r150)) | 0.8425 | 0.8337 | 0.8382 | 0.6876 | 0.6876 |
| base=max(decayed, wr·max(r10,r150)) | 0.8247 | 0.8087 | 0.8312 | 0.7543 | 0.7145 |

Reading:
- **Overall / era-4:** swapping r150→r50 is a dead heat overall and slightly worse in
  era-4. No case to change.
- **Recently-debuted cohort:** current (r150) floor **matches** `decayed_rate` (0.8450);
  the r50 floor is *worse* (0.8382). `decayed_rate` already handles young songs — the
  concern that "150 dilutes recently-heavy songs" doesn't show up, because the max
  with `decayed_rate` covers it.
- **Accelerating cohort:** current floor never binds below `decayed_rate` here
  (0.7924 = decayed-alone), so it's harmless; the r50 floor *displaces* `decayed_rate`
  and drops to 0.7775. This is the clearest risk of a 50-window floor.
- **Steady-but-rare (the 150-floor's raison d'être):** `decayed_rate` alone (0.7257)
  actually edges the floored base (0.7128) on *ranking* — but ranking is the wrong
  lens here. The floor's value is magnitude: on this cohort's **played** rows it binds
  only 6.0% of the time and lifts `base` a median 1.16×. A 50-window floor would bind
  far more often (noisily) without a ranking payoff.

**Verdict:** the 150-window floor is doing a small, well-targeted job; no window change
improves the model and r50 introduces a real regression risk in the accelerating and
debuted cohorts.

---

## Broader story — era-4 rotation, gap repeat rates, 3-show rule

### Shows per calendar year (indexed, non-excluded)
| year | 2021 | 2022 | 2023 | 2024 | 2025 | 2026(partial) |
|---|---|---|---|---|---|---|
| shows | 36 | 46 | 49 | 41 | 47 | 16 |

Full-year era-4 mean ≈ **46** (2020 = 4 pandemic shows excluded from era-3 mean).
`plays_last_50` ≈ 1.08 calendar years — a fine trailing-year proxy, but update the
"~62" annotation in `models/notebook.py`.

### Song career-median-gap distribution (era-4-active songs, ≥2 plays, n=438)
median **8** shows; p25=5, p75=18.75, p90=97.6. Binned:

| median gap (shows) | ≤3 | 4-6 | 7-12 | 13-25 | 26-50 | 51-100 | >100 |
|---|---|---|---|---|---|---|---|
| songs | 54 | 121 | 117 | 57 | 29 | 17 | 43 |

Most active songs cycle every 4-12 shows; a long tail (>100) are the rarities the
floor protects.

### Empirical repeat rate by gap — across-run vs within-run
**2009+:**

| gap | overall rate | across-run rate (n) | within-run rate (n) |
|---|---|---|---|
| 1 | 0.0213 | 0.0427 (6,794) | 0.0007 (7,045) |
| 2 | 0.0640 | 0.0817 (10,572) | 0.0010 (2,979) |
| 3 | 0.1184 | 0.1278 (11,744) | 0.0000 (935) |
| 4 | 0.1529 | 0.1571 (10,881) | 0.0000 |
| 5 | 0.1603 | 0.1645 (9,215) | 0.0000 |
| 6 | 0.1441 | 0.1477 (7,748) | 0.0000 |

**era-4:**

| gap | overall | across-run (n) | within-run (n) |
|---|---|---|---|
| 1 | 0.0147 | 0.0353 (1,757) | 0.0004 (2,541) |
| 2 | 0.0406 | 0.0563 (3,036) | 0.0008 (1,205) |
| 3 | 0.0597 | 0.0672 (3,617) | 0.0000 (452) |
| 4 | 0.1081 | 0.1121 (3,692) | 0.0000 |
| 5 | 0.1385 | 0.1428 (3,312) | 0.0000 |
| 6 | 0.1344 | 0.1375 (2,873) | 0.0000 |

**Directly useful for cooldown calibration** (across-run, gap≥4 = "no-cooldown baseline"):

| | across-run gap-2 | across-run gap-3 | gap-4..6 baseline | gap2 / base | gap3 / base |
|---|---|---|---|---|---|
| 2009+ | 0.0817 | 0.1278 | ≈0.156 | **0.52×** | **0.82×** |
| era-4 | 0.0563 | 0.0672 | ≈0.131 | **0.43×** | **0.51×** |

Two takeaways for the concurrent cooldown work: (1) within-run repeats are ~0, so the
cooldown correction is purely an **across-run** phenomenon; (2) era-4's cooldown is
**stronger and extends further into gap-3** (0.51×) than the 2009+ average (0.82×) —
if cooldown constants are meant to serve era-4, `COOLDOWN_GAP3` should be well below 1,
not near it.

### phish.net 3-show hard-exclusion vs our data
phish.net's "Trey's Notebook" hard-excludes anything played in the last 3 shows
(`gap ≤ 3`). Against actual plays:

| | plays at gap≤1 | gap≤2 | gap≤3 |
|---|---|---|---|
| 2009+ | 2.2% | 8.6% | **19.8%** |
| era-4 | 1.5% | 5.6% | **11.3%** |

A hard `gap≤3` exclusion throws away **11.3% of real era-4 plays** (mostly gap-3,
whose across-run rate is a healthy 0.067). Our soft multiplicative cooldown retains
these songs at reduced weight — strictly better than the notebook's hard rule, and
the reason the notebook stays a backtest-only baseline.

---

## Methodology notes
- Feature frame: `build_features(conn, half_life=50)`, unmodified. `year` and `era`
  derived from `showdate`. All `y`/`gap`/`gap_ratio`/`plays_last_*`/`decayed_rate`/
  `played_in_run`/`played_prev_show` are the leakage-free walk-forward columns.
- `career_median_gap` and `cum_plays` reconstructed exactly per candidate row from a
  per-song play-index sweep over `performances ⋈ shows` (indexed, non-excluded). Sanity
  check: reconstructed `gap/career_median_gap` matches the frame's `gap_ratio` to
  max abs diff **0.0** over all `cum_plays>=2` rows.
- `recent_exp_gap_W = W / plays_last_W`; `recent_ratio_W = gap · plays_last_W / W`
  (0 when no recent plays; the divide-by-zero `inf` only appears in the *expected-gap*
  column and is excluded from divergence stats).
- Predictive evaluation restricted to `year >= 2009`; era-4 = 2021+.
- End-to-end scores reimplement the committed `heuristic_scores` formula locally
  (not imported — `heuristic.py` is under concurrent edit). `m_cooldown` is omitted
  because it is not in the committed formula and is orthogonal to A/B. Per-show
  metrics group by `showid`; Hit@K counts a show's actual songs landing in the top-K
  by score; `renormalize_to_k` is monotone within a show so it does not affect Hit@K.
- Single-feature ROC-AUC via `sklearn.metrics.roc_auc_score` on the pooled 2009+ rows
  (non-finite scores dropped). Cohort AUCs use the same definition on the cohort slice.
