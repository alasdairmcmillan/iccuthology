/* HAND-WRITTEN fixtures for the accuracy-scorecard (past-prediction) views.
   generated.ts is AUTO-GENERATED from build/snapshots and has no scorecard
   data, so these live in a sibling file. Shapes mirror DEPLOY-CONTRACTS §8
   (scoreboard.json + scorecards/{showdate}.json). Two played shows, multiple
   sources incl. mcp:* with rationale, a missed_by_all list, and both
   best_call/biggest_whiff nulls exercised so past mode is fully developable
   offline. */
import type { Scoreboard, Scorecard } from "../types";

export const genScoreboard: Scoreboard = {
  updated_at: "2026-07-09T06:12:00Z",
  shows: [
    {
      showdate: "2026-07-08",
      venue_name: "Xfinity Center",
      city: "Mansfield",
      state: "MA",
      n_played: 17,
      source_keys: ["heuristic", "mcp:claude-fable"],
    },
    {
      showdate: "2026-07-06",
      venue_name: "Bethel Woods Center for the Arts",
      city: "Bethel",
      state: "NY",
      n_played: 12,
      source_keys: ["heuristic", "mcp:gemini-3.5-flash"],
    },
  ],
  models: {
    heuristic: {
      kind: "statistical",
      n_shows: 2,
      hit_rate_top10: 0.9,
      recall: 0.5441,
      brier: 0.149,
      log_loss: 0.451,
    },
    "mcp:claude-fable": {
      kind: "mcp",
      n_shows: 1,
      hit_rate_top10: 0.625,
      recall: 0.2941,
      brier: 0.221,
      log_loss: 0.612,
    },
    "mcp:gemini-3.5-flash": {
      kind: "mcp",
      n_shows: 1,
      hit_rate_top10: 0.0,
      recall: 0.0,
      brier: 0.352,
      log_loss: 0.98,
    },
  },
};

export const genScorecards: Record<string, Scorecard> = {
  "2026-07-08": {
    showdate: "2026-07-08",
    venue_name: "Xfinity Center",
    city: "Mansfield",
    state: "MA",
    frozen_epoch: "228c7eb3a0e9",
    scored_at: "2026-07-09T06:11:00Z",
    phishnet_url: "https://phish.net/setlists/?d=2026-07-08",
    n_played: 17,
    played: [
      { slug: "wilson", song: "Wilson" },
      { slug: "chalk-dust-torture", song: "Chalk Dust Torture" },
      { slug: "sand", song: "Sand" },
      { slug: "ghost", song: "Ghost" },
      { slug: "bathtub-gin", song: "Bathtub Gin" },
      { slug: "wolfmans-brother", song: "Wolfman's Brother" },
      { slug: "free", song: "Free" },
      { slug: "blaze-on", song: "Blaze On" },
      { slug: "tweezer", song: "Tweezer" },
      { slug: "harry-hood", song: "Harry Hood" },
      { slug: "slave-to-the-traffic-light", song: "Slave to the Traffic Light" },
      { slug: "backwards-down-the-number-line", song: "Backwards Down the Number Line" },
      { slug: "carini", song: "Carini" },
      { slug: "weekapaug-groove", song: "Weekapaug Groove" },
      { slug: "simple", song: "Simple" },
      { slug: "down-with-disease", song: "Down with Disease" },
      { slug: "tweezer-reprise", song: "Tweezer Reprise" },
    ],
    sources: {
      heuristic: {
        model: "heuristic",
        kind: "statistical",
        n_rows: 40,
        metrics: {
          hits_top10: 8,
          hit_rate_top10: 0.8,
          recall: 0.5882,
          brier: 0.187,
          log_loss: 0.542,
        },
        best_call: { song: "Wilson", slug: "wilson", prob: 0.16 },
        biggest_whiff: { song: "Fluffhead", slug: "fluffhead", prob: 0.38 },
        rows: [
          { song: "Tweezer", slug: "tweezer", prob: 0.62, hit: true },
          { song: "Chalk Dust Torture", slug: "chalk-dust-torture", prob: 0.58, hit: true },
          { song: "Harry Hood", slug: "harry-hood", prob: 0.54, hit: true },
          { song: "Sand", slug: "sand", prob: 0.49, hit: true },
          { song: "Blaze On", slug: "blaze-on", prob: 0.44, hit: true },
          { song: "Ghost", slug: "ghost", prob: 0.41, hit: true },
          { song: "Fluffhead", slug: "fluffhead", prob: 0.38, hit: false },
          { song: "Bathtub Gin", slug: "bathtub-gin", prob: 0.35, hit: true },
          { song: "Down with Disease", slug: "down-with-disease", prob: 0.33, hit: true },
          { song: "Everything's Right", slug: "everythings-right", prob: 0.31, hit: false },
          { song: "Possum", slug: "possum", prob: 0.24, hit: false },
          { song: "Carini", slug: "carini", prob: 0.19, hit: true },
          { song: "Wilson", slug: "wilson", prob: 0.16, hit: true },
          { song: "Mike's Song", slug: "mikes-song", prob: 0.12, hit: false },
        ],
      },
      "mcp:claude-fable": {
        model: "mcp:claude-fable",
        kind: "mcp",
        n_rows: 8,
        submitted_at: "2026-07-08T14:30:00Z",
        rationale:
          "Leaning into a Tweezer/Hood anchor with Fluffhead overdue (12-show gap). " +
          "Carini as the dark-horse jam vehicle for the mid-second-set slot.",
        metrics: {
          hits_top10: 5,
          hit_rate_top10: 0.625,
          recall: 0.2941,
          brier: 0.221,
          log_loss: 0.612,
        },
        best_call: { song: "Sand", slug: "sand", prob: 0.33 },
        biggest_whiff: { song: "Fluffhead", slug: "fluffhead", prob: 0.52 },
        rows: [
          { song: "Harry Hood", slug: "harry-hood", prob: 0.66, hit: true },
          { song: "Tweezer", slug: "tweezer", prob: 0.6, hit: true },
          { song: "Fluffhead", slug: "fluffhead", prob: 0.52, hit: false },
          { song: "Ghost", slug: "ghost", prob: 0.45, hit: true },
          { song: "Carini", slug: "carini", prob: 0.4, hit: true },
          { song: "Sand", slug: "sand", prob: 0.33, hit: true },
          { song: "Mike's Song", slug: "mikes-song", prob: 0.28, hit: false },
          { song: "Reba", slug: "reba", prob: 0.2, hit: false },
        ],
      },
    },
    missed_by_all: [
      { slug: "wolfmans-brother", song: "Wolfman's Brother" },
      { slug: "slave-to-the-traffic-light", song: "Slave to the Traffic Light" },
      { slug: "weekapaug-groove", song: "Weekapaug Groove" },
    ],
  },

  "2026-07-06": {
    showdate: "2026-07-06",
    venue_name: "Bethel Woods Center for the Arts",
    city: "Bethel",
    state: "NY",
    frozen_epoch: "17a04f9c8b21",
    scored_at: "2026-07-07T06:09:00Z",
    phishnet_url: "https://phish.net/setlists/?d=2026-07-06",
    n_played: 12,
    played: [
      { slug: "free", song: "Free" },
      { slug: "sample-in-a-jar", song: "Sample in a Jar" },
      { slug: "rift", song: "Rift" },
      { slug: "bathtub-gin", song: "Bathtub Gin" },
      { slug: "reba", song: "Reba" },
      { slug: "twist", song: "Twist" },
      { slug: "fluffhead", song: "Fluffhead" },
      { slug: "slave-to-the-traffic-light", song: "Slave to the Traffic Light" },
      { slug: "julius", song: "Julius" },
      { slug: "ghost", song: "Ghost" },
      { slug: "character-zero", song: "Character Zero" },
      { slug: "suzy-greenberg", song: "Suzy Greenberg" },
    ],
    sources: {
      heuristic: {
        model: "heuristic",
        kind: "statistical",
        n_rows: 6,
        metrics: {
          hits_top10: 6,
          hit_rate_top10: 1.0,
          recall: 0.5,
          brier: 0.111,
          log_loss: 0.36,
        },
        // Sparse shortlist that every listed row hit -> no miss -> null whiff.
        best_call: { song: "Julius", slug: "julius", prob: 0.22 },
        biggest_whiff: null,
        rows: [
          { song: "Bathtub Gin", slug: "bathtub-gin", prob: 0.55, hit: true },
          { song: "Reba", slug: "reba", prob: 0.48, hit: true },
          { song: "Ghost", slug: "ghost", prob: 0.42, hit: true },
          { song: "Fluffhead", slug: "fluffhead", prob: 0.36, hit: true },
          { song: "Twist", slug: "twist", prob: 0.3, hit: true },
          { song: "Julius", slug: "julius", prob: 0.22, hit: true },
        ],
      },
      "mcp:gemini-3.5-flash": {
        model: "mcp:gemini-3.5-flash",
        kind: "mcp",
        n_rows: 5,
        submitted_at: "2026-07-06T15:05:00Z",
        rationale:
          "Betting on a rock-forward opener set — Blaze On into Wilson — with Possum " +
          "closing. A cold night for the model: none of these landed.",
        metrics: {
          hits_top10: 0,
          hit_rate_top10: 0.0,
          recall: 0.0,
          brier: 0.352,
          log_loss: 0.98,
        },
        // Zero hits -> no best_call.
        best_call: null,
        biggest_whiff: { song: "Blaze On", slug: "blaze-on", prob: 0.5 },
        rows: [
          { song: "Blaze On", slug: "blaze-on", prob: 0.5, hit: false },
          { song: "Wilson", slug: "wilson", prob: 0.44, hit: false },
          { song: "Carini", slug: "carini", prob: 0.38, hit: false },
          { song: "Possum", slug: "possum", prob: 0.3, hit: false },
          { song: "Mike's Song", slug: "mikes-song", prob: 0.24, hit: false },
        ],
      },
    },
    missed_by_all: [
      { slug: "sample-in-a-jar", song: "Sample in a Jar" },
      { slug: "character-zero", song: "Character Zero" },
      { slug: "suzy-greenberg", song: "Suzy Greenberg" },
    ],
  },
};
