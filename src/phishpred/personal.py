"""Personalized 'songs you're due to finally see' — a forward-looking
complement to phish.net's account stats.

phish.net tells you the *most common songs you have NOT seen* and the odds they
stay unseen across your next N shows (backward/among a chosen set). This flips it
forward: given the shows you've attended (a phish.net seedfile, or explicit
dates), for each common song you've never caught live, use the Monte-Carlo
simulator over the *upcoming* horizon to say how likely you are to finally hear
it and which upcoming show is most likely to deliver it (a personalized chaser).

Everything reduces the same forward simulation the tour/run/chaser modes use.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from rich import box
from rich.table import Table

from . import features
from .modes import _new_console, _round_floats
from .simulate import SimConfig, SimResult, simulate_horizon

SEEDFILE_URL = "https://phish.net/seedfile/user/{user}"


def parse_seedfile(text: str) -> list[str]:
    """Extract attended showdates (yyyy-mm-dd) from a phish.net seedfile body.

    The seedfile lists one M/D/YY (or M/D/YYYY) date per line. Two-digit years
    map to 2000-2069 / 1970-1999."""
    out: set[str] = set()
    for m, d, y in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text):
        month, day, year = int(m), int(d), int(y)
        if year <= 99:
            year = 2000 + year if year < 70 else 1900 + year
        out.add(f"{year:04d}-{month:02d}-{day:02d}")
    return sorted(out)


def fetch_seedfile(source: str) -> list[str]:
    """`source` = a phish.net username or a full seedfile URL. Fetches and
    parses the attended showdates. Requires network (httpx)."""
    import httpx

    url = source if source.startswith("http") else SEEDFILE_URL.format(user=source)
    resp = httpx.get(url, headers={"User-Agent": "phishpred/0.1"}, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    dates = parse_seedfile(resp.text)
    if not dates:
        raise ValueError(f"No attended showdates found in seedfile at {url!r}")
    return dates


def seen_songids(conn: sqlite3.Connection, attended_dates: list[str]) -> set[int]:
    """Distinct songids the user has heard live across their attended shows."""
    if not attended_dates:
        return set()
    ph = ",".join("?" for _ in attended_dates)
    rows = conn.execute(
        f"SELECT DISTINCT p.songid AS songid FROM performances p "
        f"JOIN shows s ON s.showid = p.showid "
        f"WHERE s.showdate IN ({ph}) AND s.exclude = 0",
        list(attended_dates),
    ).fetchall()
    return {int(r["songid"]) for r in rows}


@dataclass
class UnseenSong:
    song: str
    slug: str
    songid: int
    times_played: int             # overall historical play count (the "surprise" metric)
    last_played: str | None       # last date the band played it (anyone's show)
    p_see_in_horizon: float       # P(you finally hear it >=1 time across the horizon)
    modal_next_show: str | None   # upcoming show most likely to be the FIRST to play it
    modal_prob: float             # P(that show is the first to play it)


@dataclass
class PersonalReport:
    attended_dates: list[str]
    n_attended: int
    n_seen_songs: int
    horizon_showdates: list[str]
    model: str
    n_sims: int
    rows: list[UnseenSong] = field(default_factory=list)

    def render(self, json_out: bool = False) -> str:
        if json_out:
            return json.dumps(_round_floats(asdict(self)))

        console = _new_console()
        start = self.horizon_showdates[0] if self.horizon_showdates else "?"
        end = self.horizon_showdates[-1] if self.horizon_showdates else "?"
        console.print(
            f"DUE TO SEE | {self.n_attended} shows attended, {self.n_seen_songs} distinct "
            f"songs seen | horizon {start}..{end} ({len(self.horizon_showdates)} shows) | "
            f"model={self.model} n_sims={self.n_sims}"
        )
        console.print(
            "Most common songs you've never caught live, and your odds of finally hearing "
            "each over the upcoming horizon."
        )
        table = Table(box=box.ASCII)
        table.add_column("Song")
        table.add_column("Plays", justify="right")
        table.add_column("Last played")
        table.add_column("P(see in horizon)", justify="right")
        table.add_column("Most likely show")
        for r in self.rows:
            table.add_row(
                r.song, str(r.times_played), r.last_played or "-",
                f"{r.p_see_in_horizon * 100:.1f}%",
                f"{r.modal_next_show} ({r.modal_prob * 100:.0f}%)" if r.modal_next_show else "-",
            )
        console.print(table)
        return console.export_text()


def _horizon_reductions(result: SimResult):
    """Per-songid: (P(>=1 over horizon), modal first-hit showdate, modal prob)."""
    n = len(result.samples)
    ndates = len(result.horizon_dates)
    union: dict[int, int] = defaultdict(int)
    first_hit: dict[int, list[int]] = defaultdict(lambda: [0] * ndates)
    for sim in result.samples:
        seen_sim: set[int] = set()
        hit_at: dict[int, int] = {}
        for t, step in enumerate(sim):
            for sid in step:
                seen_sim.add(sid)
                if sid not in hit_at:
                    hit_at[sid] = t
        for sid in seen_sim:
            union[sid] += 1
        for sid, t in hit_at.items():
            first_hit[sid][t] += 1

    p_see: dict[int, float] = {}
    modal: dict[int, tuple[str | None, float]] = {}
    for sid, counts in first_hit.items():
        p_see[sid] = union[sid] / n if n else 0.0
        best_t = max(range(ndates), key=lambda t: counts[t])
        modal[sid] = (result.horizon_dates[best_t], counts[best_t] / n if n else 0.0)
    return p_see, modal


def unlikely_unseen(
    conn: sqlite3.Connection,
    attended_dates: list[str],
    horizon_showids: list[int],
    config: SimConfig | None = None,
    *,
    top: int = 20,
    min_plays: int = 20,
) -> PersonalReport:
    """Rank the common songs the user has never seen (by historical play count),
    annotated with the forward odds of finally hearing each over the horizon and
    the show most likely to deliver it. `min_plays` drops obscure songs."""
    config = config or SimConfig()
    seen = seen_songids(conn, attended_dates)

    catalog = features.song_play_catalog(conn)

    result = simulate_horizon(conn, horizon_showids, config) if horizon_showids else \
        SimResult([], [], [], {}, [[] for _ in range(config.n_sims)], config)
    p_see, modal = _horizon_reductions(result)

    rows: list[UnseenSong] = []
    for r in catalog:
        sid = int(r["songid"])
        if sid in seen or r["plays"] < min_plays:
            continue
        modal_show, modal_prob = modal.get(sid, (None, 0.0))
        rows.append(UnseenSong(
            song=r["name"], slug=r["slug"], songid=sid,
            times_played=int(r["plays"]), last_played=r["last_played"],
            p_see_in_horizon=p_see.get(sid, 0.0),
            modal_next_show=modal_show, modal_prob=modal_prob,
        ))
        if len(rows) >= top:
            break

    return PersonalReport(
        attended_dates=attended_dates, n_attended=len(attended_dates),
        n_seen_songs=len(seen), horizon_showdates=result.horizon_dates,
        model=config.model, n_sims=config.n_sims, rows=rows,
    )
