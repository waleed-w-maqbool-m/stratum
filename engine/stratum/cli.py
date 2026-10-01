"""Stratum's command line.

Everything the web app does, without the web app — which is what makes the engine
testable and what the eval harness drives.

    stratum excavate sqlite:///gauntlet/build/chinook_legacy.sqlite
    stratum model chinook_legacy
    stratum review chinook_legacy
    stratum ask chinook_legacy "how many customers do we have?"
    stratum serve
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table as RichTable

from .config import load_env, settings

# Read .env before any command runs, so provider keys are visible to the LLM client.
load_env()

app = typer.Typer(
    add_completion=False,
    help="Natural language to SQL for legacy databases.",
    no_args_is_help=True,
)
console = Console()


def _store():
    from .semantic.store import ModelStore

    return ModelStore(settings().models_dir)


@app.command()
def excavate(
    url: str = typer.Argument(..., help="database URL, e.g. sqlite:///path/to.db"),
    name: str = typer.Option("", help="name to store the model under"),
    profile: str = typer.Option("", help="capability profile override"),
    offline: bool = typer.Option(False, help="skip LLM enrichment"),
    sample: int = typer.Option(2000, help="rows sampled per table"),
) -> None:
    """Excavate a database and compile its semantic model."""
    from .connectors.registry import build
    from .excavate.compile_model import compile_model
    from .excavate.pipeline import excavate as run_excavation
    from .llm.client import LLMClient

    connector = build(url, profile or None)
    connector.connect()
    info = connector.server_info()
    database = name or info.database.replace(".sqlite", "")
    console.print(
        f"[bold]{database}[/bold]  {info.dialect} {info.version}  "
        f"profile [cyan]{info.profile.name}[/cyan]"
    )
    for note in info.privileges.notes:
        # A privileges note is only a warning when the login can actually write.
        label = "[yellow]warning[/yellow]" if info.privileges.can_write else "[dim]note[/dim]"
        console.print(f"  {label} {note}")

    store = _store()
    llm = None if offline else LLMClient(cache_dir=store.cache_dir(database))

    def show(event) -> None:
        detail = ", ".join(f"{k}={v}" for k, v in list(event.detail.items())[:4])
        tokens = f"  {event.tokens} tok" if event.tokens else ""
        console.print(f"  [dim]{event.stage:12}[/dim] {event.ms:>6} ms{tokens}  {detail}")

    result = run_excavation(connector, sample_size=sample, emit=show, llm=llm)
    model = compile_model(result, database=database, profile_name=info.profile.name)
    version = store.save(model)
    store.save_findings(database, [f.to_dict() for f in result.findings])

    console.print()
    console.print(f"[green]model v{version}[/green]  {json.dumps(model.stats())}")
    console.print(f"stored in {settings().models_dir / database}")


@app.command()
def model(
    database: str,
    entity: str = typer.Option("", help="show one entity in detail"),
    yaml_out: bool = typer.Option(False, "--yaml", help="print the whole model as YAML"),
) -> None:
    """Show a compiled semantic model."""
    stored = _store().load(database)
    if stored is None:
        console.print(f"[red]no model for {database}[/red] — run `stratum excavate` first")
        raise typer.Exit(1)

    if yaml_out:
        console.print(Syntax(stored.to_yaml(), "yaml", theme="ansi_dark"))
        return

    if entity:
        found = stored.entity(entity)
        if found is None:
            console.print(f"[red]no entity {entity!r}[/red]")
            raise typer.Exit(1)
        console.print(f"[bold]{found.name}[/bold]  <- {found.source}")
        if found.discriminator:
            console.print(f"  discriminator: {found.discriminator}")
        console.print(f"  key: {', '.join(found.key) or '(none)'}")
        for filt in found.active_filters():
            console.print(f"  [yellow]filter[/yellow] {filt.expr}  — {filt.reason}")
        table = RichTable(show_header=True, header_style="bold")
        table.add_column("logical")
        table.add_column("type")
        table.add_column("expression")
        table.add_column("status")
        for column in found.columns.values():
            table.add_row(
                column.name, column.data_type, column.expr[:70],
                column.status,
            )
        console.print(table)
        return

    table = RichTable(show_header=True, header_style="bold")
    table.add_column("entity")
    table.add_column("source")
    table.add_column("rows", justify="right")
    table.add_column("cols", justify="right")
    table.add_column("filters", justify="right")
    for e in sorted(stored.entities.values(), key=lambda x: x.name):
        table.add_row(
            e.name, e.source, f"{e.row_estimate:,}", str(len(e.columns)),
            str(len(e.active_filters())),
        )
    console.print(table)
    console.print(f"\n{len(stored.relationships)} relationships, "
                  f"{len(stored.enums)} decoded enums")


@app.command()
def review(database: str, limit: int = 20) -> None:
    """The review queue, most impactful hypothesis first."""
    from .semantic.store import build_review_queue

    store = _store()
    stored = store.load(database)
    if stored is None:
        console.print(f"[red]no model for {database}[/red]")
        raise typer.Exit(1)
    items = build_review_queue(store.load_findings(database), stored, limit)
    for item in items:
        console.print(
            f"[bold]{item.kind}[/bold] {item.target}  "
            f"[dim]impact {item.impact:.2f} · confidence {item.confidence:.2f}[/dim]"
        )
        reason = item.evidence.get("reason") or (item.evidence.get("reasons") or [""])[0]
        if reason:
            console.print(f"   {reason}")
        console.print(f"   [cyan]{json.dumps(item.value, default=str)[:150]}[/cyan]")
        console.print()


@app.command()
def ask(
    database: str,
    question: str,
    url: str = typer.Option("", help="database URL, if not already connected"),
    show_sql: bool = typer.Option(True, help="print the compiled SQL"),
) -> None:
    """Ask a question."""
    from .ask.pipeline import AskContext
    from .ask.pipeline import ask as run_ask
    from .connectors.costguard import CostGuard
    from .connectors.executor import ReadOnlyExecutor
    from .connectors.registry import build
    from .llm.client import LLMClient
    from .semantic.verified import VerifiedLibrary

    store = _store()
    stored = store.load(database)
    if stored is None:
        console.print(f"[red]no model for {database}[/red] — run `stratum excavate` first")
        raise typer.Exit(1)

    target = url or f"sqlite:///{Path.cwd() / 'gauntlet' / 'build' / (database + '.sqlite')}"
    connector = build(target)
    connector.connect()

    context = AskContext(
        model=stored,
        executor=ReadOnlyExecutor(connector, connector.profile),
        llm=LLMClient(cache_dir=store.cache_dir(database)),
        cost_guard=CostGuard(connector),
        verified=VerifiedLibrary(store.verified_path(database)),
    )

    def show(event) -> None:
        tokens = f" {event.tokens:>5} tok" if event.tokens else "          "
        console.print(f"  [dim]{event.stage:12} {event.ms:>5} ms{tokens}[/dim]")

    answer = run_ask(context, question, emit=show)
    console.print()

    if answer.status != "ok":
        console.print(f"[yellow]{answer.status}[/yellow]: {answer.message}")
        if answer.cost_warning and answer.cost_warning.get("cheaper_variant"):
            console.print("\n[dim]a cheaper version of this query:[/dim]")
            console.print(Syntax(answer.cost_warning["cheaper_variant"], "sql"))
        raise typer.Exit(0)

    if answer.rows:
        table = RichTable(show_header=True, header_style="bold")
        for column in answer.columns:
            table.add_column(str(column))
        for row in answer.rows[:30]:
            table.add_row(*[str(v) for v in row])
        console.print(table)
        if answer.row_count > 30:
            console.print(f"[dim]… {answer.row_count - 30} more rows[/dim]")

    if answer.confidence:
        colour = {
            "show": "green", "assumptions": "yellow", "abstain": "red"
        }[answer.confidence.band]
        console.print(
            f"\nconfidence [{colour}]{answer.confidence.score:.2f}[/{colour}]"
            + ("  [green]verified[/green]" if answer.verified_hit else "")
        )
    if answer.explanation:
        console.print()
        console.print(answer.explanation)
    if show_sql and answer.physical_sql:
        console.print("\n[dim]compiled SQL[/dim]")
        console.print(Syntax(answer.physical_sql, "sql", theme="ansi_dark"))
    console.print(f"\n[dim]{answer.tokens} tokens · {answer.elapsed_ms} ms[/dim]")


@app.command()
def sql(
    database: str,
    logical: str,
    url: str = typer.Option("", help="database URL"),
) -> None:
    """Compile logical SQL to physical SQL without running it."""
    from .ask.compiler import compile_logical
    from .connectors.registry import build

    stored = _store().load(database)
    if stored is None:
        console.print(f"[red]no model for {database}[/red]")
        raise typer.Exit(1)
    target = url or f"sqlite:///{Path.cwd() / 'gauntlet' / 'build' / (database + '.sqlite')}"
    connector = build(target)
    connector.connect()

    compiled = compile_logical(logical, stored, connector.profile)
    console.print(Syntax(compiled.physical_sql, "sql", theme="ansi_dark"))
    if compiled.assumptions:
        console.print("\n[yellow]assumptions[/yellow]")
        for assumption in compiled.assumptions:
            console.print(f"  • {assumption.text}")
    if compiled.downgrades:
        console.print(f"\n[dim]dialect rewrites: {', '.join(compiled.downgrades)}[/dim]")
    if compiled.unresolved:
        console.print(f"\n[red]unresolvable: {'; '.join(compiled.unresolved)}[/red]")


@app.command()
def calibrate(
    suite: str = typer.Option("chinook_v1", help="which suite's run to fit on"),
    reports: Path = typer.Option(
        Path("../gauntlet/reports"), help="where the eval harness wrote its reports"
    ),
    mode: str = typer.Option("full", help="which run: full, thorough, naive"),
) -> None:
    """Fit the confidence weights on an eval run, and say whether the score is honest.

    A confidence number that does not track accuracy is worse than none, because the
    gate built on it shows wrong answers with a green badge. This prints the reliability
    diagram so that can be checked rather than assumed.
    """
    import json as json_module

    from .ask.calibration import fit, observations_from_run, render_reliability

    answers = reports / f"{suite}.{mode}.answers.jsonl"
    report_path = reports / f"{suite}.{mode}.json"
    if not answers.exists() or not report_path.exists():
        console.print(
            f"[red]no run found[/red] — expected {answers.name} and {report_path.name} "
            f"in {reports}. Run `make eval` first."
        )
        raise typer.Exit(1)

    report = json_module.loads(report_path.read_text("utf-8"))
    # The scorer already decided which answers were correct; calibration never looks at
    # gold SQL itself.
    failed = {f["id"] for f in report.get("failures", [])}
    scored_ids = set()
    for line in answers.read_text("utf-8").splitlines():
        if line.strip():
            scored_ids.add(json_module.loads(line)["id"])
    outcomes = {qid: qid not in failed for qid in scored_ids}

    observations = observations_from_run(answers, outcomes)
    calibration = fit(observations)
    console.print(render_reliability(calibration))
    if calibration.weights:
        console.print()
        console.print("fitted weights:")
        for name, weight in sorted(
            calibration.weights.items(), key=lambda kv: -abs(kv[1])
        ):
            console.print(f"  {name:22} {weight:+.3f}")
    target = reports / f"{suite}.{mode}.calibration.json"
    calibration.save(target)
    console.print()
    console.print(f"wrote {target}")


@app.command()
def mcp(
    database: str = typer.Option(..., help="the excavated database to expose"),
    url: str = typer.Option(..., help="the database URL to connect to"),
) -> None:
    """Run the MCP server, so Claude Desktop can drive Stratum with no API key."""
    from .mcp.server import main as run_mcp

    run_mcp(database, url)


@app.command()
def serve(
    host: str = typer.Option("", help="bind address"),
    port: int = typer.Option(0, help="port"),
    reload: bool = typer.Option(False, help="reload on code changes"),
) -> None:
    """Run the API."""
    import uvicorn

    config = settings()
    uvicorn.run(
        "stratum.api.app:app",
        host=host or config.host,
        port=port or config.port,
        reload=reload,
    )


if __name__ == "__main__":
    app()
