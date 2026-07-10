/** Rows per page for the long song tables (tour table, run view, personal
 * lookahead). 25 on small screens; on desktop, enough rows to fill the
 * viewport (~41px per row after ~350px of header/pills/card chrome), never
 * fewer than 25. Computed once per mount — a resize doesn't rebalance pages
 * mid-read. */
export function songPageSize(): number {
  if (typeof window === "undefined") return 25;
  if (window.innerWidth <= 720) return 25;
  return Math.max(25, Math.floor((window.innerHeight - 350) / 41));
}
