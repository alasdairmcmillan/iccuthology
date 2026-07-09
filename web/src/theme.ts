// Design tokens from the handoff README, with one product override:
// the accent defaults to VIOLET (#9d8cf5) rather than the mock's cyan.
// The accent is exposed as a single CSS variable (--accent, set in styles.css)
// so it stays swappable; JS reads it here for inline/computed colors.
import type { Bucket } from "./types";

export const ACCENT = "#9d8cf5"; // violet — product default (mock lists it as an option)
export const ON_ACCENT = "#04191c"; // "on-accent" text, fixed across all accents

export interface BucketStyle {
  bg: string;
  fg: string;
  border: string;
}

// Mirrors the mock's bucketColor(): lock/likely derive from the accent;
// bustout-watch and longshot are fixed semantic colors regardless of accent.
export function bucketColor(bucket: Bucket, accent: string = ACCENT): BucketStyle {
  if (bucket === "lock") return { bg: accent, fg: ON_ACCENT, border: accent };
  if (bucket === "likely") return { bg: accent + "22", fg: accent, border: accent + "55" };
  if (bucket === "bustout-watch") return { bg: "#2a2013", fg: "#e3bb52", border: "#4a3a1a" };
  return { bg: "#121a20", fg: "#5c6a72", border: "#1e2932" }; // longshot
}
