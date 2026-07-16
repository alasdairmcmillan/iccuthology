// Centralized metric-definition copy (§8), shared by the Shows scores band,
// the past-scorecards standings board, and the Tours standings panel — one
// place to keep the explainers consistent wherever a metric is surfaced.

export const METRIC_TIPS = {
  recall:
    "Of the songs actually played, the share that appeared anywhere in the model's full shortlist. Longer shortlists (20–40 allowed) make this easier — read it next to the list length.",
  brier:
    "Mean squared error between each shortlist probability and the outcome (1 = played, 0 = not). Rewards calibration: a confident miss costs far more than a hedged one. 0 is perfect; always guessing 50% scores 0.25.",
  vsHeuristic:
    "This model's top-20 hit rate minus the statistical baseline's on the same shows — positive means it beat the baseline.",
  list: "Average shortlist length submitted (20–40 allowed). Context for recall: longer lists cover more.",
  setlistWeighted:
    "Each called song: 1 point for playing at all, +1 for the right set, +1 for the exact slot. 100% = every called song in its exact slot.",
  setlistHitRate: "Share of songs in the called setlist that played anywhere in the show.",
  placedRate: "Of the called songs that played, the share the model put in the correct set.",
  exactCalls: "Right song, right set, right slot.",
  marqueeCalls:
    "Named-slot calls that landed: opener, set closers, set 2 opener, encore.",
  refreshGain:
    "Mean change in top-20 hit rate from a model's first take to its final take, over shows with multiple takes.",
  shows: "Scored shows this model has a frozen, scored take for.",
  sharp: "Shows where the model landed 2+ exact calls (right song, right set, right slot).",
} as const;

export function hitRateTip(topN: number): string {
  return `Of the model's ${topN} highest-probability songs, the share that actually played — anywhere in the show, any set. With ~18 songs in a typical show, a perfect 20-song list tops out near 90%.`;
}

/** 0.15 -> "+15%", -0.05 -> "-5%" — signed whole-percent for delta metrics. */
export function formatSignedPct(x: number): string {
  return (x >= 0 ? "+" : "") + Math.round(x * 100) + "%";
}
