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
    model: str = typer.Option("heuristic", help="heuristic | lr | gbm | llm:<provider>[:<model-id>]"),
    half_life: int = typer.Option(50),
    top: int = typer.Option(30, help="Rows to display"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    """Predict a setlist for one show date, or the next N shows at a venue."""
    from .db import get_connection
    from .models.llm import LLMError
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
        try:
            pred = predict_show(conn, d, model=model, half_life=half_life, top=top)
        except LLMError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1)
        typer.echo(render_prediction(pred, json_out=json_out))


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
        from .models.llm import DEFAULT_MODELS, LLMError, get_client

        model_id = model or DEFAULT_MODELS.get(provider)
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
    from .models.llm import (
        DEFAULT_MODELS,
        LLMSongModel,
        get_client,
        llm_backtest,
        render_llm_backtest,
    )

    model_id = model or DEFAULT_MODELS.get(provider)
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


@app.command()
def epoch(
    emit_github_output: bool = typer.Option(
        False, "--emit-github-output", help="Append epoch=/changed= to $GITHUB_OUTPUT for CI gating"
    ),
    submitted: str = typer.Option(None, help="Submissions inbox dir (folded into the epoch)"),
    model: str = typer.Option("heuristic", help="Publishing model (part of the epoch)"),
    n_sims: int = typer.Option(2000, "--n-sims"),
    seed: int = typer.Option(0),
    half_life: int = typer.Option(50),
    compare_models: str = typer.Option(None, "--compare-models", help="Extra per-show columns folded into the epoch, comma-separated (e.g. lr,gbm,llm:anthropic)"),
) -> None:
    """Print the current epoch and whether it differs from the last published one.

    Cheap: reads DB state + the submissions manifest, no simulation. Gates the
    publish workflow (deploy plan §6).
    """
    from .config import DATA_DIR
    from .db import get_connection
    from .epoch import emit_github_output as _emit
    from .epoch import epoch_status

    compare = [m.strip() for m in compare_models.split(",") if m.strip()] if compare_models else None
    pointer = DATA_DIR / "predictions" / "latest.json"
    status = epoch_status(
        get_connection(), pointer_path=pointer, model=model, n_sims=n_sims,
        seed=seed, half_life=half_life, compare_models=compare, submitted_dir=submitted,
    )
    typer.echo(f"epoch={status['epoch']} changed={'true' if status['changed'] else 'false'}")
    if emit_github_output:
        _emit(status["epoch"], status["changed"])


@app.command()
def publish(
    out: str = typer.Option("build/snapshots", help="Output directory for the snapshot tree"),
    n_sims: int = typer.Option(2000, "--n-sims", help="Monte-Carlo simulations"),
    model: str = typer.Option("heuristic", help="Headline model: heuristic | lr | gbm"),
    seed: int = typer.Option(0),
    half_life: int = typer.Option(50),
    with_samples: bool = typer.Option(False, "--with-samples", help="Also emit samples.bin + samples_meta.json"),
    sample_sims: int = typer.Option(None, "--sample-sims", help="Ship a downsampled samples.bin of this many sims (tables keep --n-sims accuracy)"),
    with_catalog: bool = typer.Option(False, "--with-catalog", help="Emit catalog.json (history for the personalized 'due to see' view)"),
    compare_models: str = typer.Option(None, "--compare-models", help="Extra per-show columns, comma-separated (e.g. lr,gbm,llm:anthropic)"),
    submitted: str = typer.Option(None, help="Submissions inbox dir to fold in as mcp:<label> sources"),
    frozen: str = typer.Option(None, "--frozen", help="Local mirror of R2's frozen/ prefix (e.g. data/frozen): frozen/tour/{id}.json predictions become authoritative, tour docs gain a plays-so-far tracker"),
) -> None:
    """Compute every publishable artifact for the current epoch -> JSON (+ samples).

    ONE simulation per epoch feeds the tour table and the raw samples; per-show
    predictions + setlists reuse the existing library paths (deploy plan §3).
    """
    from .db import get_connection
    from .publish import publish as _publish

    compare = [m.strip() for m in compare_models.split(",") if m.strip()] if compare_models else None
    meta = _publish(
        get_connection(), out, n_sims=n_sims, model=model, seed=seed, half_life=half_life,
        with_samples=with_samples, sample_sims=sample_sims, with_catalog=with_catalog,
        compare_models=compare, submitted_dir=submitted, frozen_dir=frozen,
    )
    typer.echo(f"published epoch {meta['epoch']} -> {out} ({len(meta['horizon_showdates'])} shows)")


@app.command("backcast-tour")
def backcast_tour_cmd(
    tour_id: str = typer.Argument(..., help="Tour id to freeze (e.g. summer-2026; see meta.json tours[].id)"),
    out: str = typer.Option("data/frozen", "--out", help="Frozen dir root; writes {out}/tour/{tour_id}.json"),
    db: str = typer.Option(None, "--db", help="Source DB (default: the configured data/phish.db); it is copied, never mutated"),
    n_sims: int = typer.Option(2000, "--n-sims", help="Monte-Carlo simulations"),
    model: str = typer.Option("heuristic", help="heuristic | lr | gbm"),
    seed: int = typer.Option(0),
    half_life: int = typer.Option(50),
) -> None:
    """Back-compute the FROZEN pre-tour heuristic prediction for one tour and
    write it to the frozen staging location (DEPLOY-CONTRACTS §3).

    Copies the DB, scrubs every show on/after the tour opener to look un-played,
    then runs the normal tour simulation over the whole tour horizon — i.e. what
    today's model would have predicted the day before the opener, blind to the
    tour. Deterministic given seed. Seeds ``frozen/tour/{tour_id}.json`` so
    subsequent publishes serve these frozen rows + a live plays-so-far tracker.
    """
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path as _Path

    from .config import DB_PATH
    from .db import get_connection
    from .modes import _round_floats
    from .publish import backcast_tour

    src_db = _Path(db) if db else DB_PATH
    if not src_db.exists():
        typer.echo(f"DB not found: {src_db}")
        raise typer.Exit(1)

    with tempfile.TemporaryDirectory() as tmp:
        copy_db = _Path(tmp) / "backcast.db"
        shutil.copyfile(src_db, copy_db)
        conn = get_connection(copy_db)
        try:
            doc = backcast_tour(conn, tour_id, n_sims=n_sims, model=model, seed=seed, half_life=half_life)
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1)
        finally:
            conn.close()

    dest = _Path(out) / "tour" / f"{tour_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_json.dumps(_round_floats(doc), ensure_ascii=False), encoding="utf-8")
    typer.echo(
        f"backcast {tour_id}: {len(doc['horizon_showdates'])}-show horizon "
        f"as_of={doc['as_of_showdate']} -> {dest}"
    )


@app.command()
def score(
    frozen: str = typer.Option(..., "--frozen", help="Frozen show-doc dir (frozen/show — one {showdate}.json per show)"),
    out: str = typer.Option(..., "--out", help="Scorecards output dir (writes {showdate}.json + scoreboard.json)"),
    rescore_days: int = typer.Option(7, "--rescore-days", help="Rewrite scorecards for shows within this many days of UTC today"),
    force: bool = typer.Option(False, "--force", help="Rescore EVERY eligible frozen show, ignoring the rescore window — for metric-definition changes (e.g. a top-N cutover) that must propagate to old scorecards"),
) -> None:
    """Score frozen past predictions against actual setlists (deploy plan §8).

    Scans each frozen show doc whose show is played + indexed in the DB, writes a
    per-show scorecard (skipping already-scored shows outside the rescore window),
    then always rebuilds scoreboard.json. ``--force`` bypasses the skip so a metric
    redefinition reaches every old card in one pass.
    """
    from .db import get_connection
    from .score import score_all

    written = score_all(get_connection(), frozen, out, rescore_days=rescore_days, force=force)
    typer.echo(f"scored {len(written)} show(s) -> {out}; scoreboard.json rebuilt")


@app.command()
def personal(
    user: str = typer.Option(None, help="phish.net username — fetches your public seedfile"),
    seedfile: str = typer.Option(None, help="Full seedfile URL (overrides --user)"),
    dates: str = typer.Option(None, help="Explicit attended dates yyyy-mm-dd,... (offline, no fetch)"),
    tour_name: str = typer.Option(None, "--tour", help="Restrict the horizon to a named tour"),
    year: int = typer.Option(None, help="Horizon calendar year (default: current)"),
    top: int = typer.Option(20, help="How many unseen songs to show"),
    min_plays: int = typer.Option(20, help="Ignore songs with fewer historical plays"),
    model: str = typer.Option("heuristic", help="heuristic | lr | gbm"),
    n_sims: int = typer.Option(2000, "--n-sims"),
    seed: int = typer.Option(0),
    half_life: int = typer.Option(50),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    """Songs you're due to finally see: the most common songs you've never caught
    live, with the odds you'll hear each over the upcoming horizon and the show
    most likely to play it. A forward-looking complement to phish.net's stats.
    """
    from .db import get_connection
    from .modes import resolve_tour_horizon
    from .personal import fetch_seedfile, unlikely_unseen
    from .simulate import SimConfig

    if dates:
        attended = [d.strip() for d in dates.split(",") if d.strip()]
    elif seedfile or user:
        try:
            attended = fetch_seedfile(seedfile or user)
        except Exception as exc:
            typer.echo(f"Could not load seedfile: {exc}")
            raise typer.Exit(1)
    else:
        typer.echo("Provide --user, --seedfile, or --dates")
        raise typer.Exit(2)

    conn = get_connection()
    horizon = resolve_tour_horizon(conn, tour=tour_name, year=year)
    if not horizon:
        typer.echo("No future shows in the resolved horizon.")
        raise typer.Exit(1)
    cfg = SimConfig(n_sims=n_sims, seed=seed, model=model, half_life=half_life)
    report = unlikely_unseen(conn, attended, horizon, cfg, top=top, min_plays=min_plays)
    typer.echo(report.render(json_out=json_out))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
