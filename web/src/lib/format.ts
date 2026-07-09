// Small pure formatting helpers shared across screens.

/** 0.3415 -> "34%" (matches the mock's pct()). */
export function pct(x: number): string {
  return Math.round(x * 100) + "%";
}

/** 0.337 -> "33.7%" — one decimal, for run/setlist values that carry precision. */
export function pct1(x: number): string {
  return (Math.round(x * 1000) / 10).toFixed(1) + "%";
}

/** Lowercase, drop apostrophes/periods/slashes, hyphenate whitespace.
 *  Matches the canonical slugs in the sample data (e.g. "AC/DC Bag" -> "acdc-bag"). */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/['.]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** "2026-07-10" -> "Fri · Jul 10, 2026" (parsed as a local calendar date). */
export function dateLabel(showdate: string): string {
  const [y, m, d] = showdate.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  return `${WEEKDAYS[dt.getDay()]} · ${MONTHS[m - 1]} ${d}, ${y}`;
}

/** "2026-07-10" -> "Jul 10, 2026" (no weekday) for the multiselect summary. */
export function dateLabelShort(showdate: string): string {
  const [y, m, d] = showdate.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}

/** "2026-07" -> "July, 2026" for schedule month headers. */
export function monthLabel(yyyymm: string): string {
  const [y, m] = yyyymm.split("-").map(Number);
  const full = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  return `${full[m - 1]}, ${y}`;
}
