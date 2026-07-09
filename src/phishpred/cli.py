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


# Per-provider default model for the LLM path (§7a). Kept here so the CLI can
# offer `--provider` without forcing `--model`.
_LLM_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4.1",
    "google": "gemini-2.5-flash",
    "openai-compat": None,  # open-model endpoints must name their model explicitly
}


@app.command()
def tour(
    tour_name: str = typer.Option(None, "--tour", help="Restrict horizon to shows whose tour name matches this substring"),
    year: int = typer.Option(None, help="Calendar year for the rest-of-year horizon (default: current year)"),
    model: str = typer.Option("heuristic", help="heuristic | lr | gbm"),
    n_sims: int = typer.Option(2000, "--n-sims", help="Monte-Carlo simulations"),
    seed: int = typer.Option(0),
    half_life: int = typer.Option(50),
    top: int = typer.Option(30, help="Rows to display"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    """Mode 1 — tour-level: expected plays / P(>=1) per song over the horizon.

    Default horizon is the rest of the calendar year; use --tour to restrict to a
    named tour.
    """
    from .db import get_connection
    from .modes import resolve_tour_horizon, tour_mode
    from .simulate import SimConfig

    conn = get_connection()
    horizon = resolve_tour_horizon(conn, tour=tour_name, year=year)
    if not horizon:
        typer.echo("No future shows in the resolved horizon.")
        raise typer.Exit(1)
    cfg = SimConfig(n_sims=n_sims, seed=seed, model=model, half_life=half_life)
    report = tour_mode(conn, horizon, cfg)
    report.rows = report.rows[:top]
    typer.echo(report.render(json_out=json_out))


@app.command()
def run(
    venue: str = typer.Option(None, help="Match the run's venue by name/city substring"),
    nights: int = typer.Option(3, help="With --venue: number of consecutive future shows"),
    dates: str = typer.Option(None, help="Explicit comma-separated show dates (yyyy-mm-dd,...)"),
    soft_no_repeat: bool = typer.Option(False, "--soft-no-repeat", help="Trust the learned penalty instead of the hard no-repeat mask"),
    model: str = typer.Option("heuristic", help="heuristic | lr | gbm"),
    n_sims: int = typer.Option(2000, "--n-sims"),
    seed: int = typer.Option(0),
    half_life: int = typer.Option(50),
    top: int = typer.Option(30, help="Rows to display"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    """Mode 2 — run-level: P(hear >=1 across the run) + most-likely night."""
    from .db import get_connection
    from .modes import resolve_run, run_mode
    from .simulate import SimConfig

    if not venue and not dates:
        typer.echo("Provide --venue (with --nights) or --dates")
        raise typer.Exit(2)

    conn = get_connection()
    date_list = [d.strip() for d in dates.split(",")] if dates else None
    horizon = resolve_run(conn, venue=venue, nights=nights, dates=date_list)
    if not horizon:
        typer.echo("No future shows matched the requested run.")
        raise typer.Exit(1)
    cfg = SimConfig(
        n_sims=n_sims, seed=seed, model=model, half_life=half_life,
        strict_no_repeat=not soft_no_repeat,
    )
    report = run_mode(conn, horizon, cfg)
    report.rows = report.rows[:top]
    typer.echo(report.render(json_out=json_out))


@app.command()
def chaser(
    song: str = typer.Argument(..., help="Song to chase (slug, or name/slug substring)"),
    tour_name: str = typer.Option(None, "--tour", help="Restrict horizon to a named tour"),
    year: int = typer.Option(None, help="Calendar year for the horizon (default: current year)"),
    model: str = typer.Option("heuristic", help="heuristic | lr | gbm"),
    n_sims: int = typer.Option(2000, "--n-sims"),
    seed: int = typer.Option(0),
    half_life: int = typer.Option(50),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    """Mode 4 — chaser: distribution of the next show that plays a given song."""
    from .db import get_connection
    from .modes import chaser_mode, resolve_tour_horizon
    from .simulate import SimConfig

    conn = get_connection()
    horizon = resolve_tour_horizon(conn, tour=tour_name, year=year)
    if not horizon:
        typer.echo("No future shows in the resolved horizon.")
        raise typer.Exit(1)
    cfg = SimConfig(n_sims=n_sims, seed=seed, model=model, half_life=half_life)
    try:
        report = chaser_mode(conn, song, horizon, cfg)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    typer.echo(report.render(json_out=json_out))


@app.command()
def setlist(
    showdate: str = typer.Argument(..., help="Show date yyyy-mm-dd"),
    llm: bool = typer.Option(False, "--llm", help="Use the LLM assembler instead of the deterministic sampler"),
    provider: str = typer.Option("anthropic", help="With --llm: anthropic | openai | google | openai-compat"),
    model: str = typer.Option(None, help="With --llm: model id (default: provider's default)"),
    seed: int = typer.Option(0),
    half_life: int = typer.Option(50),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    """Mode 5 — setlist: a full ordered setlist for one show.

    Deterministic structured sampler by default; --llm routes ordering through a
    model-agnostic LLM assembler.
    """
    from .db import get_connection
    from .setlist import assemble_setlist_llm, sample_setlist

    conn = get_connection()
    if llm:
        from .models.llm import LLMError, get_client

        model_id = model or _LLM_DEFAULT_MODEL.get(provider)
        if model_id is None:
            typer.echo(f"--model is required for provider {provider!r}")
            raise typer.Exit(2)
        try:
            client = get_client(provider, model_id)
            pred = assemble_setlist_llm(conn, showdate, client, half_life=half_life)
        except LLMError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1)
    else:
        pred = sample_setlist(conn, showdate, half_life=half_life, seed=seed)
    typer.echo(pred.render(json_out=json_out))


@app.command("llm-backtest")
def llm_backtest_cmd(
    provider: str = typer.Option("anthropic", help="anthropic | openai | google | openai-compat"),
    model: str = typer.Option(None, help="Model id (default: provider's default)"),
    half_life: int = typer.Option(50),
    holdout_tours: int = typer.Option(2, help="Most-recent tours to hold out"),
) -> None:
    """Benchmark the LLM-as-model (§7a) on the same holdout as heuristic/LR/GBM."""
    from .db import get_connection
    from .models.llm import LLMSongModel, get_client, llm_backtest, render_llm_backtest

    model_id = model or _LLM_DEFAULT_MODEL.get(provider)
    if model_id is None:
        typer.echo(f"--model is required for provider {provider!r}")
        raise typer.Exit(2)
    conn = get_connection()
    from .models.llm import LLMError

    try:
        client = get_client(provider, model_id)
        song_model = LLMSongModel(client, provider=provider)
        result = llm_backtest(conn, song_model, half_life=half_life, holdout_tours=holdout_tours)
    except LLMError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    typer.echo(render_llm_backtest(result, song_model.name))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
