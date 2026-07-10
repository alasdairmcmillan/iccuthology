/**
 * phish.net seedfile parsing for the /api/seedfile/{user} proxy route.
 *
 * phish.net serves seedfiles without CORS headers, so the browser can't fetch
 * them directly; the Worker proxies the fetch and returns parsed attended
 * showdates. The date parsing mirrors `src/phishpred/personal.py
 * parse_seedfile` exactly: one M/D/YY or M/D/YYYY date per line, two-digit
 * years mapping to 2000-2069 / 1970-1999.
 */

const DATE_RE = /\b(\d{1,2})\/(\d{1,2})\/(\d{2,4})\b/g;

export function parseSeedfile(text: string): string[] {
  const out = new Set<string>();
  for (const m of text.matchAll(DATE_RE)) {
    const month = Number(m[1]);
    const day = Number(m[2]);
    let year = Number(m[3]);
    if (year <= 99) {
      year = year < 70 ? 2000 + year : 1900 + year;
    }
    out.add(
      `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`,
    );
  }
  return [...out].sort();
}

/** phish.net usernames: conservative allowlist to keep the proxy un-abusable. */
export const SEEDFILE_USER_RE = /^[A-Za-z0-9_.-]{1,64}$/;

export function seedfileUrl(user: string): string {
  return `https://phish.net/seedfile/user/${user}`;
}
