// Offline run reduction — the labeled fallback used when there is no backend
// (samples.bin) to POST /api/run against.
//
// DESIGN-DECISION: production uses POST /api/run, whose headline p_at_least_one is
// the exact joint union over Monte-Carlo samples (DEPLOY-CONTRACTS §4). Without
// samples we can only approximate with independent-events union 1 - Π(1-p_i),
// exactly the mock's math. This is flagged in the UI (an "offline estimate" note)
// and on the returned report via `approximate: true`.
import type { RunReport, RunRow, ShowReport } from "../types";

export function computeRunFromShows(
  showdates: string[],
  showsByDate: Record<string, ShowReport | undefined>,
  sourceKey: string,
): RunReport {
  const selected = [...showdates].sort();
  const missing: string[] = [];
  const dataDates: string[] = [];
  for (const d of selected) {
    const show = showsByDate[d];
    if (show && show.sources[sourceKey]) dataDates.push(d);
    else missing.push(d);
  }

  interface Agg {
    song: string;
    slug: string;
    perNight: { date: string; prob: number }[];
  }
  const map = new Map<string, Agg>();
  for (const d of dataDates) {
    const rows = showsByDate[d]!.sources[sourceKey].rows;
    for (const r of rows) {
      let e = map.get(r.slug);
      if (!e) {
        e = { song: r.song, slug: r.slug, perNight: [] };
        map.set(r.slug, e);
      }
      e.perNight.push({ date: d, prob: r.prob });
    }
  }

  const rows: RunRow[] = [];
  for (const e of map.values()) {
    const perNightProbs = dataDates.map(
      (d) => e.perNight.find((n) => n.date === d)?.prob ?? 0,
    );
    const pUnion = 1 - perNightProbs.reduce((acc, p) => acc * (1 - p), 1);
    let best = e.perNight[0];
    for (const n of e.perNight) if (n.prob > best.prob) best = n;
    rows.push({
      song: e.song,
      slug: e.slug,
      p_at_least_one: pUnion,
      per_night_probs: perNightProbs,
      most_likely_night_date: best.date,
    });
  }
  rows.sort((a, b) => b.p_at_least_one - a.p_at_least_one);

  return { showdates: selected, rows, missing, approximate: true };
}
