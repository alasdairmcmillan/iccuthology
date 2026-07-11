"""Predict CLI support — see CONTRACTS.md `predict.py` section.

Owned module. `features.py` / `models/heuristic.py` / `models/ml.py` are written
in parallel elsewhere in the project, so they are imported lazily (inside
functions) rather than at module import time. This keeps `predict.py` importable
even while those modules are stubs, and lets tests monkeypatch them cleanly.
"""
from __future__ import annotations

import io
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date

import pandas as pd
from rich import box
from rich.console import Console
from rich.table import Table

from phishpred.config import era_for_year

# Trained ML models are expensive to fit; cache per (model, half_life) so that
# repeated predict_show() calls in the same process train at most once.
_MODEL_CACHE: dict[tuple[str, int], object] = {}


@dataclass
class PredictionRow:
    song: str
    slug: str
    prob: float
    gap: int | None
    drivers: list[str] = field(default_factory=list)


@dataclass
class ShowPrediction:
    showdate: str
    venue_name: str
    city: str | None
    state: str | None
    model: str
    k: float
    half_life: int
    rows: list[PredictionRow] = field(default_factory=list)


def upcoming_shows(
    conn: sqlite3.Connection, venue_query: str | None = None, limit: int = 10
) -> list[sqlite3.Row]:
    """Future, non-excluded shows (showdate >= today), optionally filtered by a
    case-insensitive substring match against venue name OR city. Ordered by
    showdate ascending, capped at `limit`.
    """
    today = date.today().isoformat()
    query = [
        "SELECT shows.*, venues.name AS venue_name, venues.city AS city,",
        "       venues.state AS state",
        "FROM shows",
        "LEFT JOIN venues ON shows.venueid = venues.venueid",
        "WHERE shows.showdate >= ? AND shows.exclude = 0",
    ]
    params: list[object] = [today]

    if venue_query:
        query.append("AND (LOWER(venues.name) LIKE ? OR LOWER(venues.city) LIKE ?)")
        like = f"%{venue_query.lower()}%"
        params.extend([like, like])

    query.append("ORDER BY shows.showdate")
    query.append("LIMIT ?")
    params.append(limit)

    return conn.execute("\n".join(query), params).fetchall()


def _phish_artistid(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'phish_artistid'"
        ).fetchone()
    except sqlite3.Error:
        return 1
    if row is None:
        return 1
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 1


def _resolve_show(conn: sqlite3.Connection, showdate: str) -> sqlite3.Row:
    rows = conn.execute("SELECT * FROM shows WHERE showdate = ?", (showdate,)).fetchall()
    if not rows:
        raise ValueError(
            f"No show found for {showdate!r}. Check the date is yyyy-mm-dd and that "
            "it has been ingested (run `phishpred ingest` / `phishpred refresh`)."
        )
    if len(rows) == 1:
        return rows[0]

    phish_id = _phish_artistid(conn)
    for row in rows:
        try:
            if row["artistid"] == phish_id:
                return row
        except (IndexError, KeyError):
            continue
    return rows[0]


def _resolve_venue(conn: sqlite3.Connection, venueid: int | None) -> sqlite3.Row | None:
    if venueid is None:
        return None
    return conn.execute("SELECT * FROM venues WHERE venueid = ?", (venueid,)).fetchone()


def _fmt_mult(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _heuristic_drivers(row: pd.Series) -> list[str]:
    drivers = [f"rate={float(row['decayed_rate']):.3f}"]
    for col, label in (
        ("m_prev_show", "prev-show"),
        ("m_in_run", "in-run"),
        ("m_cooldown", "cooldown"),
        ("m_venue", "venue"),
        ("m_due", "due"),
    ):
        val = row.get(col)
        if val is None or pd.isna(val):
            continue
        val = float(val)
        if not math.isclose(val, 1.0, abs_tol=1e-9):
            drivers.append(f"{label} x{_fmt_mult(val)}")
    return drivers


def _ml_drivers(model: object, row: pd.Series, feature_columns: list[str]) -> list[str]:
    """Best-effort explanation for ML models. Keep simple per CONTRACTS.md:
    top-3 |coef * value| feature names for LR when coefficients are exposed,
    otherwise an empty list (also the default for GBM for now).
    """
    coefficients = getattr(model, "coefficients", None)
    if not coefficients:
        return []

    contributions = []
    for feat in feature_columns:
        coef = coefficients.get(feat) if hasattr(coefficients, "get") else None
        if coef is None or feat not in row:
            continue
        try:
            value = float(row[feat])
        except (TypeError, ValueError):
            continue
        if pd.isna(value):
            continue
        contributions.append((feat, abs(coef * value)))

    contributions.sort(key=lambda item: item[1], reverse=True)
    return [feat for feat, _ in contributions[:3]]


def _train_test_split_by_show_index(hist: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    show_indexes = sorted(hist["show_index"].dropna().unique())
    if not show_indexes:
        return hist, hist.iloc[0:0]
    n_valid = max(1, int(round(len(show_indexes) * 0.15)))
    valid_indexes = set(show_indexes[-n_valid:])
    valid_mask = hist["show_index"].isin(valid_indexes)
    return hist[~valid_mask], hist[valid_mask]


def _get_or_train_model(conn: sqlite3.Connection, model: str, half_life: int):
    key = (model, half_life)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]

    import phishpred.features as features
    import phishpred.models.ml as ml_mod

    hist = features.build_features(conn, half_life=half_life)
    hist = hist[hist["showdate"].astype(str).str.slice(0, 4).astype(int) >= 2009]
    train_df, valid_df = _train_test_split_by_show_index(hist)

    if model == "lr":
        trained = ml_mod.train_lr(train_df, valid_df)
    else:
        trained = ml_mod.train_gbm(train_df, valid_df)

    _MODEL_CACHE[key] = trained
    return trained


def predict_show(
    conn: sqlite3.Connection,
    showdate: str,
    model: str = "heuristic",
    half_life: int = 50,
    top: int = 30,
    llm_cache: object | None = None,
) -> ShowPrediction:
    """Resolve a show by date and return ranked per-song probabilities.

    ``model`` is ``heuristic``/``lr``/``gbm``, or ``llm:<provider>[:<model-id>]``
    (e.g. ``llm:anthropic`` or ``llm:anthropic:claude-sonnet-5``) to score the
    show with ``models.llm.LLMSongModel``. LLM failures (missing API key,
    network, malformed response, bad spec) raise ``models.llm.LLMError``.
    ``llm_cache`` overrides the LLM path's disk ``PredictionCache`` (tests);
    the default cache means repeated calls for the same show never re-bill.
    """
    if model not in ("heuristic", "lr", "gbm") and not model.startswith("llm:"):
        raise ValueError(
            f"Unknown model {model!r}; expected 'heuristic', 'lr', 'gbm', "
            "or 'llm:<provider>[:<model-id>]'."
        )

    show = _resolve_show(conn, showdate)
    venue = _resolve_venue(conn, show["venueid"])
    venue_name = venue["name"] if venue is not None else "Unknown venue"
    city = venue["city"] if venue is not None else None
    state = venue["state"] if venue is not None else None

    import phishpred.features as features

    feat_df = features.features_for_future_show(conn, show["showid"], half_life)

    year = int(str(show["showdate"])[:4])
    k = features.mean_setlist_size(conn, era_for_year(year))

    model_label = model
    if model == "heuristic":
        import phishpred.models.heuristic as heuristic_mod

        pred_df = heuristic_mod.heuristic_predict(feat_df, k)
        driver_fn = _heuristic_drivers
        trained = None
    elif model.startswith("llm:"):
        import phishpred.models.llm as llm_mod
        import phishpred.models.ml as ml_mod

        provider, model_id = llm_mod.parse_model_spec(model)
        client = llm_mod.get_client(provider, model_id)
        song_model = llm_mod.LLMSongModel(
            client, provider=provider, cache=llm_cache,
            k_hint_fn=lambda _df, _k=float(k): _k,
        )
        # Resolved name ("llm:<provider>:<model-id>") so a defaulted model id
        # still shows up in the output/publish artifacts.
        model_label = song_model.name
        # Same floor (LLMSongModel.floor_prob) + per-show renormalization to K
        # (ml_predict) as every other calibrated source.
        pred_df = ml_mod.ml_predict(song_model, feat_df, k)

        def driver_fn(_row: pd.Series) -> list[str]:
            return []  # no per-song explanation from the LLM path (as with GBM)
    else:
        trained = _get_or_train_model(conn, model, half_life)
        import phishpred.models.ml as ml_mod

        pred_df = ml_mod.ml_predict(trained, feat_df, k)

        def driver_fn(row: pd.Series, _trained=trained) -> list[str]:
            return _ml_drivers(_trained, row, features.FEATURE_COLUMNS)

    pred_df = pred_df.sort_values("prob", ascending=False).head(top)

    rows: list[PredictionRow] = []
    for _, row in pred_df.iterrows():
        gap = row.get("gap")
        gap_val = None if gap is None or pd.isna(gap) else int(gap)
        rows.append(
            PredictionRow(
                song=row["song_name"],
                slug=row["slug"],
                prob=float(row["prob"]),
                gap=gap_val,
                drivers=driver_fn(row),
            )
        )

    return ShowPrediction(
        showdate=str(show["showdate"]),
        venue_name=venue_name,
        city=city,
        state=state,
        model=model_label,
        k=float(k),
        half_life=half_life,
        rows=rows,
    )


def render_prediction(pred: ShowPrediction, json_out: bool = False) -> str:
    """Render a ShowPrediction as JSON or a rich table (returned as text)."""
    if json_out:
        payload = asdict(pred)
        payload["k"] = round(payload["k"], 4)
        for row in payload["rows"]:
            row["prob"] = round(row["prob"], 4)
        return json.dumps(payload)

    # Render into a buffer only — the caller decides where the text goes.
    console = Console(record=True, width=120, file=io.StringIO())

    location = ", ".join(part for part in (pred.city, pred.state) if part)
    header = f"{pred.showdate} - {pred.venue_name}"
    if location:
        header += f" ({location})"
    header += f" | model={pred.model}  K={pred.k:.1f}  half_life={pred.half_life}"
    console.print(header)

    # ASCII box so output survives cp1252 stdout on Windows when redirected.
    table = Table(box=box.ASCII)
    table.add_column("Song")
    table.add_column("Prob", justify="right")
    table.add_column("Gap", justify="right")
    table.add_column("Drivers")
    for row in pred.rows:
        gap_str = "" if row.gap is None else str(row.gap)
        table.add_row(row.song, f"{row.prob * 100:.1f}%", gap_str, ", ".join(row.drivers))
    console.print(table)

    return console.export_text()
