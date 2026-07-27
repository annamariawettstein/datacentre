"""Command-line interface for the data centre pipeline."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from . import db
from .config import config

app = typer.Typer(
    add_completion=False,
    help="UK data centre pipeline — data spine, classification, and (later) the map.",
)
console = Console()


@app.command("init-db")
def init_db() -> None:
    """Create the schema (and PostGIS extension) — idempotent."""
    db.init_schema()
    console.print("[green]Schema applied.[/green]")


@app.command()
def sweep(
    incremental_days: int = typer.Option(
        None,
        "--incremental",
        help="Only pull records PlanIt changed within this many days (cron mode).",
    ),
) -> None:
    """Sweep PlanIt for data-centre candidates into the database."""
    from .ingest import sweep as run_sweep

    mode = f"incremental ({incremental_days}d)" if incremental_days else "full"
    console.print(f"Running [bold]{mode}[/bold] sweep against PlanIt...")
    result = run_sweep(incremental_days=incremental_days)
    console.print(
        f"[green]Done.[/green] {result.total_found} records — "
        f"{result.inserted} new, {result.updated} updated."
    )


@app.command()
def classify(
    limit: int = typer.Option(500, help="Max applications to classify this run."),
    force: bool = typer.Option(False, help="Re-classify already-classified rows."),
) -> None:
    """Run the LLM is_datacentre classification pass."""
    from .classify import classify as run_classify

    console.print(f"Classifying up to {limit} applications with {config.classify_model}...")
    done = run_classify(limit=limit, force=force)
    console.print(f"[green]Classified {done} applications.[/green]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
) -> None:
    """Launch the pipeline monitor UI (map + stats) on the live database."""
    import uvicorn

    console.print(f"Pipeline monitor at [bold cyan]http://{host}:{port}[/bold cyan]")
    uvicorn.run("datacentre.api:app", host=host, port=port, log_level="info")


@app.command("load-grid")
def load_grid(
    src: str = typer.Argument(..., help="GridScope backup dir or uk_primary_substations.sql.gz"),
) -> None:
    """Load GridScope primary-substation headroom and match sites to it (populates gsp)."""
    from .grid import load_grid as run

    console.print(f"Loading grid substations from [cyan]{src}[/cyan]...")
    n_subs, matched = run(src)
    console.print(
        f"[green]Loaded {n_subs:,} primary substations[/green]; matched {matched:,} "
        f"data-centre applications to their nearest one. Run db/analysis.sql for the "
        f"GSP-level headroom rollup."
    )


@app.command()
def snapshot() -> None:
    """Freeze the API responses to static web/stats.json + web/sites.geojson.

    Lets the site deploy as pure static files (e.g. Vercel) with no database in
    production — the map/stats read these instead of hitting Postgres. Re-run and
    redeploy whenever the pipeline data changes.
    """
    import json

    from . import api

    stats_body = api.stats().body
    geo_body = api.sites_geojson().body
    (api.WEB_DIR / "stats.json").write_bytes(stats_body)
    (api.WEB_DIR / "sites.geojson").write_bytes(geo_body)
    s, g = json.loads(stats_body), json.loads(geo_body)
    console.print(
        f"[green]Snapshot written[/green] → web/stats.json + web/sites.geojson  "
        f"({s['sites']} sites, {len(g['features'])} mapped · data as of {s['as_of']})"
    )


@app.command()
def dedupe() -> None:
    """Collapse applications into distinct physical sites (site_id)."""
    from .dedupe import dedupe as run_dedupe

    console.print("Linking applications into distinct sites...")
    apps, sites = run_dedupe()
    console.print(
        f"[green]Done.[/green] {apps:,} confirmed applications → "
        f"[bold]{sites:,} distinct sites[/bold]."
    )


@app.command()
def categorize(
    limit: int = typer.Option(3000, help="Max confirmed sites to categorize."),
    force: bool = typer.Option(False, help="Re-categorize already-done rows."),
) -> None:
    """Scale/type audit — flag which confirmed data centres are power-material."""
    from .categorize import categorize as run

    console.print(f"Categorizing up to {limit} confirmed data centres...")
    done = run(limit=limit, force=force)
    console.print(f"[green]Categorized {done}.[/green]")


@app.command("resolve-applicants")
def resolve_applicants(
    lapsed: bool = typer.Option(
        False, "--lapsed", help="Only the lapsed-consent sites (highest value)."
    ),
    limit: int = typer.Option(50, help="Max sites to scrape this run."),
) -> None:
    """Scrape applicant/agent from council portals (Idox) for target sites."""
    from .enrich import resolve_applicants as run

    scope = "lapsed-consent sites" if lapsed else "sites"
    console.print(f"Resolving applicants for up to {limit} {scope} (Idox portals)...")
    attempted, resolved = run(lapsed=lapsed, limit=limit)
    console.print(
        f"[green]Done.[/green] {resolved}/{attempted} resolved "
        f"({attempted - resolved} unresolved / non-Idox)."
    )


@app.command("extract-capacity")
def extract_capacity(
    limit: int = typer.Option(50, help="Max sites to extract this run."),
    force: bool = typer.Option(False, help="Re-extract already-done sites."),
    all_dc: bool = typer.Option(
        False, "--all", help="Include non-material data centres (default: material only)."
    ),
    max_docs: int = typer.Option(3, help="Top-ranked PDFs to read per site."),
) -> None:
    """Extract MW capacity + energy profile from planning PDFs (Idox portals)."""
    from .capacity import extract_capacity as run

    scope = "confirmed data centres" if all_dc else "material data centres"
    console.print(
        f"Extracting capacity for up to {limit} {scope} "
        f"(reading up to {max_docs} PDFs each with {config.classify_model})..."
    )
    attempted, extracted = run(
        limit=limit, force=force, material_only=not all_dc, max_docs=max_docs
    )
    console.print(
        f"[green]Done.[/green] {extracted}/{attempted} sites yielded a figure "
        f"({attempted - extracted} no stated capacity / no readable docs)."
    )


@app.command()
def stats() -> None:
    """Summarise what's in the database."""
    with db.connect() as conn:
        total = conn.execute("SELECT count(*) FROM application").fetchone()[0]
        classified = conn.execute(
            "SELECT count(*) FROM application WHERE is_datacentre IS NOT NULL"
        ).fetchone()[0]
        confirmed = conn.execute(
            "SELECT count(*) FROM application WHERE is_datacentre"
        ).fetchone()[0]
        with_geom = conn.execute(
            "SELECT count(*) FROM application WHERE geom IS NOT NULL"
        ).fetchone()[0]

        table = Table(title="Data centre pipeline — database")
        table.add_column("metric")
        table.add_column("count", justify="right")
        table.add_row("applications (candidates)", f"{total:,}")
        table.add_row("classified", f"{classified:,}")
        table.add_row("confirmed data centres", f"{confirmed:,}")
        table.add_row("with geometry", f"{with_geom:,}")
        console.print(table)

        console.print("\n[bold]Top authorities (confirmed data centres):[/bold]")
        rows = conn.execute(
            """SELECT area_name, count(*) c FROM application
               WHERE is_datacentre GROUP BY area_name ORDER BY c DESC LIMIT 10"""
        ).fetchall()
        for area, c in rows:
            console.print(f"  {c:>3}  {area}")


if __name__ == "__main__":
    app()
