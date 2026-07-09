"""Typer CLI — thin wrappers over the library modules.

Owned by the orchestrator. Implementation modules must match CONTRACTS.md.
"""
from __future__ import annotations

import typer

app = typer.Typer(help="Phish setlist predictor", no_args_is_help=True)


@app.command()
def ingest(
    start_year: int = typer.Option(1983, help="First year to backfill"),
    end_year: int = typer.Option(None, help="Last year (default: current)"),
    force: bool = typer.Option(False, "--force", help="Bypass raw JSON cache"),
) -> None:
    """Full backfill of shows/setlists/songs/venues into data/phish.db."""
    from .api import PhishNetClient
    from .db import get_connection, init_db
    from .ingest import full_ingest

    conn = get_connection()
    init_db(conn)
    stats = full_ingest(conn, PhishNetClient(), start_year=start_year, end_year=end_year, force=force)
    typer.echo(str(stats))


@app.command()
def refresh() -> None:
    """Incremental refresh: current year + anything since last refresh."""
    from .api import PhishNetClient
    from .db import get_connection, init_db
    from .ingest import refresh as _refresh

    conn = get_connection()
    init_db(conn)
    stats = _refresh(conn, PhishNetClient())
    typer.echo(str(stats))


@app.command("build-features")
def build_features_cmd(
    half_life: int = typer.Option(50, help="Decay half-life in shows"),
    out: str = typer.Option("data/features.parquet", help="Output parquet path"),
) -> None:
    """Run the chronological feature sweep and save to parquet."""
    from .db import get_connection
    from .features import build_features

    df = build_features(get_connection(), half_life=half_life)
    df.to_parquet(out, index=False)
    typer.echo(f"wrote {len(df):,} rows -> {out}")


@app.command()
def backtest(
    holdout_tours: int = typer.Option(2, help="Number of most recent tours to hold out"),
    seed: int = typer.Option(42),
) -> None:
    """Walk-forward backtest: heuristic vs LR vs GBM, H sweep, calibration table."""
    from .backtest import run_backtest
    from .db import get_connection

    report = run_backtest(get_connection(), holdout_tours=holdout_tours, seed=seed)
    typer.echo(str(report))


@app.command()
def predict(
    showdate: str = typer.Argument(None, help="Show date yyyy-mm-dd (omit with --venue)"),
    venue: str = typer.Option(None, help="Predict upcoming shows matching this venue/city substring"),
    next_n: int = typer.Option(3, "--next", help="With --venue: how many upcoming shows"),
    model: str = typer.Option("heuristic", help="heuristic | lr | gbm"),
    half_life: int = typer.Option(50),
    top: int = typer.Option(30, help="Rows to display"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    """Predict a setlist for one show date, or the next N shows at a venue."""
    from .db import get_connection
    from .predict import predict_show, render_prediction, upcoming_shows

    conn = get_connection()
    if venue:
        shows = upcoming_shows(conn, venue_query=venue, limit=next_n)
        if not shows:
            typer.echo(f"No upcoming shows matching {venue!r}")
            raise typer.Exit(1)
        dates = [s["showdate"] for s in shows]
    elif showdate:
        dates = [showdate]
    else:
        typer.echo("Provide a SHOWDATE or --venue")
        raise typer.Exit(2)

    for d in dates:
        pred = predict_show(conn, d, model=model, half_life=half_life, top=top)
        typer.echo(render_prediction(pred, json_out=json_out))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
