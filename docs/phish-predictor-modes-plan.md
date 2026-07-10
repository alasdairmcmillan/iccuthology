# Phish Predictor — Prediction Modes & LLM Integration Plan

Companion to `phish-predictor-plan.md`. Covers the next pre-UI phase: five
prediction modes (tour / run / show / chaser / setlist) plus an LLM-driven
prediction path. Show-level (#3) is already built; the rest layer on top of it.

Guiding realization: **modes 1, 2, and 4 are all views over a single forward
Monte-Carlo simulator**; mode 5 needs a new position/sequence model; the LLM
path is an alternative *predictor* that plugs into the existing backtest as
just another model.

---

## 0. Current state (what we build on)

- Per-(song, show) calibrated probabilities via `features.py` +
  `models/{heuristic,ml}.py`, renormalized so Σp ≈ K.
- `features.py` runs a chronological sweep maintaining running state
  (`_State`: last-played index, decayed numerators, per-venue/-tour/-era
  counts, gap medians). **This state object is the seed of the simulator.**
- `backtest.py` scores any per-(song, show) probability source: Brier, log
  loss, Hit@K, calibration. Model-agnostic — this is what makes the LLM path
  measurable.
- Data confirmed clean for the new work: `set_label` ∈ {1,2,3,e,e2,e3},
  `position` ordered per show, `trans_mark` distinguishes `>` (segue) vs `->`
  (hard segue) vs `,`, era-4 structure ≈ Set 1: 9.2 songs, Set 2: 7.3,
  encore: 2.1.

---

## 1. Core new component: forward Monte-Carlo simulator (`simulate.py`)

Everything except setlist-ordering rides on this. Build it first.

### 1a. Make `_State` drivable step-by-step
Refactor `features.py` so the sweep's per-show state can be (a) built up to
"now" from real history, then (b) advanced by *hypothetical* setlists. Extract
the existing `apply_show(setlist)` so the simulator can feed it *sampled*
setlists instead of actual ones. `features_for_future_show` becomes a special
case: one step, no sampling.

### 1b. Sample a setlist for one future show
Given calibrated per-candidate probabilities `p_i` (already Σ ≈ K):
- **Song set:** draw each candidate as Bernoulli(`p_i`). Expected count = Σp_i
  ≈ K by construction, giving variable-length setlists centered on K. (Optional
  refinement: condition on a target length sampled from the era's
  show-length distribution, then take the top-K by a Gumbel-perturbed score —
  a Plackett-Luce draw — for tighter length control.)
- **No same-night duplicates:** inherent (Bernoulli over distinct candidates).

### 1c. Advance state and repeat
After sampling night T's setlist: fold it into a *copy* of `_State`
(gap resets to 0 for played songs, `played_in_run` will fire next night,
decayed numerators update, venue/tour counts increment), recompute
probabilities for T+1, sample again. Walk to end of the horizon (run or tour).

### 1d. Run M simulations
Default M = 2,000 (tune for stable tails). Each simulation yields a full
sampled sequence of setlists for the horizon. All aggregate modes are
reductions over these M samples. Fixed seed for reproducibility (pass seed
in; **note** `Math.random`-style nondeterminism is fine here since this is
Python, but seed it for repeatable reports).

**Key property:** the no-repeat-across-a-run behavior you want is *emergent* —
we don't hard-code it; the `played_in_run` feature (already a massive
down-weight in both heuristic and LR) suppresses re-selection once a song is
sampled earlier in the run. Optionally add a hard mask (P→0 if already played
this run) as a `--strict-no-repeat` flag to match the band's near-absolute
practice.

### Acceptance
- Simulated single shows reproduce the marginal per-song probabilities from
  `predict` (average inclusion rate over M sims ≈ calibrated p_i, within MC
  error). This is the correctness anchor.
- Simulated run reproduces "played night 1 ⇒ ~0 nights 2-3" for a
  high-probability song (e.g. Harry Hood).

---

## 2. Mode 1 — Tour-level

**Question:** across the remaining tour, which songs, and how many times each?

- Simulate all remaining tour shows M times.
- Per song, report: **expected plays** (mean count), **P(≥1 play)**,
  play-count distribution (P(exactly n)), and a "lock / likely / longshot /
  bustout-watch" bucket.
- Fast analytic sanity check: Σ over remaining shows of marginal P(song, show).
  Will *overestimate* frequent songs (ignores rotation cooldown) — ship it as a
  labeled approximation, use MC as the headline.

**CLI:** `phishpred tour 2026-summer` (resolve tour by name/id; horizon = its
future shows). Output: table sorted by expected plays.

**Effort:** small once the simulator exists (pure aggregation).

---

## 3. Mode 2 — Run-level

**Question:** across a multi-night run (e.g. Deer Creek 7/10–12), what will I
most likely hear at least once?

- Simulate just the run's shows with no-repeat enforced (soft via feature,
  or `--strict-no-repeat` hard mask).
- Per song: **P(hear at least once in the run)**, **most-likely night**, and
  the conditional "if not night 1, then night 2/3" shift.
- This is the mode that most *needs* joint simulation: independent per-show
  marginals would triple-count Harry Hood; the simulator gives the correct
  "≈certain across 3 nights, but mutually exclusive across them."

**CLI:** `phishpred run --venue ruoff --nights 3` (or explicit dates).
Output: song | P(≥1 in run) | most-likely night | per-night probs.

**Effort:** small — same simulator, run-scoped reduction.

---

## 4. Mode 3 — Show-level ✅ built

Already shipped (`predict`). Two refinements worth folding in later:
- Feed run-context automatically when predicting night 2+ (currently needs a
  manual `refresh`); the simulator makes this natural.
- Report simulator-based marginals as an alternative to the renormalized
  point estimate (they'll agree; MC gives free uncertainty bands).

---

## 5. Mode 4 — Chaser

**Question:** for song X, what's the most likely *next* show they play it?

- Simulate forward from now over a horizon (e.g. next 30 shows / rest of year).
- Per simulation, record the index of the **first** future show containing X.
- Report the distribution: P(next play is show T+1, T+2, …), the modal /
  median next show, and P(not within horizon).
- Naturally captures "overdue for a bustout": a large `gap_ratio` lifts near-term
  probability. Great for the "I'm chasing Fluffhead / Icculus" use case.

**CLI:** `phishpred chaser fluffhead --horizon 30` → ranked upcoming shows with
probabilities + "expected shows until next play."

**Effort:** small — reduction over the same sims (record first-hit index).

**Note:** accuracy for rare bustouts is inherently limited (few training
signals); surface a confidence caveat.

---

## 6. Mode 5 — Setlist (exact ordered)

**Question:** a full, ordered, plausible setlist for one show. The hard one.
Needs new modeling beyond song-inclusion.

### 6a. Slot / position model (new features — ties into the earlier
opener/closer/encore request)
Compute per-song **slot propensities** from `performances.position` +
`set_label`:
- Slot taxonomy: `set1-open`, `set1-mid`, `set1-close`, `set2-open`,
  `set2-mid`, `set2-close`, `encore` (extend for set 3 / e2).
- For each song, P(slot | played) from history (era-weighted). Data confirms
  strong signal: AC/DC Bag / Buried Alive / Moma Dance open; Tweezer Reprise /
  Loving Cup / Slave / First Tube encore; Hood / Slave close sets.

### 6b. Set-structure model
Sample the show's skeleton from era distributions: number of sets (≈always
2 + encore in era 4), per-set length (Set 1 ≈ 9, Set 2 ≈ 7, encore ≈ 2), and
their variance.

### 6c. Sequence assembly
Two candidate implementations — build (i), keep (ii) in reserve:

**(i) Structured sampler.** For each slot in the skeleton, draw a song weighted
by `P(song) × P(slot | song)`, without replacement, honoring hard constraints:
- Pairings/segues from `trans_mark`: Tweezer Reprise only after Tweezer;
  Mike's Song → (Hydrogen/other) → Weekapaug; Fluffhead as a unit; etc.
  Mine these as high-lift bigrams from consecutive `>`/`->` performances.
- Encore usually a "clean" (non-jam) song; set-2 heavy on jam vehicles.

**(ii) LLM assembler.** Give the model the top ~40 candidates with their
per-song probs and slot propensities, plus the set skeleton, and have it emit an
ordered setlist. LLMs already encode segue conventions and "flow" that are
painful to hand-code (see §7). Best-of-both: sampler for calibrated song choice,
LLM for ordering/segues.

### 6d. Scoring
100% ordered-hit is effectively impossible; don't pretend. Report:
- Song-set overlap (Hit@K, already have it), **plus** sequence metrics:
  Kendall-τ / longest-common-subsequence vs actual, and slot-accuracy
  (did the opener/encore match?).

**CLI:** `phishpred setlist 2026-07-10 [--llm]` → formatted Set 1 / Set 2 /
Encore with segue marks.

**Effort:** medium-large. Slot features + set-structure = small; sequence
assembly with constraints = the real work.

---

## 7. LLM-driven prediction path (`models/llm.py`)

Answers Q1: let a model drive the DB / features to produce predictions, and —
crucially — **benchmark it against LR/GBM/heuristic on the same backtest.**

### 7a. LLM-as-model (benchmarkable) — build first
- Input: the pre-computed candidate feature table for a show (the same frame
  `predict` builds) rendered compactly, plus optional context (recent setlists,
  tour position, debut-this-tour flags).
- Output: **structured** per-song probabilities (JSON, schema-validated), which
  `ml_predict`-style renormalize to K.
- Wrap as a `CalibratedSongModel` so `backtest.py` scores it unchanged. Then we
  *know* whether the LLM adds signal over LR, per Brier/log-loss/calibration.
- Cost control: batch a whole show's candidates in one call; cache by
  (showid, model, prompt-version).

### 7b. LLM-as-analyst (agentic) — build second
- Expose the SQLite DB read-only to the model: either a small **MCP server**
  with typed query tools (`songs_like`, `song_history`, `venue_history`,
  `recent_setlists`, `run_context`) or a sandboxed read-only SQL runner.
- Use for one-off deep dives, setlist-mode ordering (§6c-ii), and narrative
  explanations ("why Fluffhead is due"). Harder to batch-score, so keep it for
  qualitative/interactive use, not the leaderboard.

### 7c. Why this is worth it
The statistical models see only the engineered features. An LLM can inject:
tour storylines, song teases, cover/debut patterns, anniversary/venue lore,
band statements — soft signals with real predictive value at Phish shows. The
backtest tells us empirically if that pays off. If it beats LR, it becomes a
first-class model; if not, we've learned the features already capture the signal.

---

## 8. Build order

1. **`simulate.py`** — refactor `_State` to be step-drivable; setlist sampler;
   M-run driver; acceptance test vs marginal probs. (Unlocks modes 1/2/4.)
2. **Modes 1, 2, 4** — thin reductions + CLI commands. Ship together.
3. **`models/llm.py` §7a** — LLM-as-model + backtest bake-off vs LR. Small,
   high-learning-value.
4. **Slot/set-structure model** (§6a-b) — new features; also feeds a better
   show-level model. (Standalone-useful even before full setlist mode.)
5. **Setlist mode §6c** — structured sampler first, LLM assembler behind
   `--llm`. Sequence scoring.
6. **MCP / agentic path §7b** — after the above, for interactive/qualitative use.

## 9. Open questions for the user

- **Simulation horizon defaults:** rest-of-calendar-year vs named-tour-only for
  tour/chaser modes? 
  - **Answer**: default to rest-of-calendar-year, option to select named tour
- **Strict vs soft no-repeat** in run mode: hard mask (P→0) or trust the learned
  penalty? (Band practice is near-absolute → I lean hard mask as default, soft
  as a flag.)
  - **Answer**: hard mask as default, soft as flag. If we ever expand to include side projects like TAB, they do repeat songs.
- **LLM cost/latency budget** for §7 — is per-show one-call batching acceptable,
  and which model tier for the benchmark run?
  - **Answer**: yep this makes sense, and I'd like the MCP to be model agnostic so I can compare claude, gemini, GPT, open models, etc.
- **Setlist mode:** prioritize the deterministic sampler or the LLM assembler
  first? (I lean sampler-first so there's a calibrated, cheap baseline the LLM
  must beat.)
  - **Answer**: yes prioritize deterministic sampler but I'm guessing there will be a pretty hard ceiling on potential accuracy there.
