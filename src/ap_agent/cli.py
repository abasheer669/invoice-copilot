import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ap_agent import __version__
from ap_agent.config import get_settings
from ap_agent.embeddings import make_embedder
from ap_agent.erp import PostgresErp, erp_tools
from ap_agent.ingest import IngestError, run_ingest
from ap_agent.orchestrator import NoIndexError, Orchestrator
from ap_agent.retrieval import KnowledgeBase
from ap_agent.rules_config import load_rules
from ap_agent.runs import Run, RunNotFound, RunStore
from ap_agent.schemas import InvoiceCase
from ap_agent.tools import TransientError

app = typer.Typer(no_args_is_help=True)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_show_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Accounts-payable agent: gathers evidence, checks policy in code, pauses for approval."""


@app.command()
def config() -> None:
    """Print the effective configuration with secrets masked."""
    typer.echo(get_settings().model_dump_json(indent=2))


@app.command()
def ingest() -> None:
    """Build the policy knowledge base and make it live if it passes validation."""
    settings = get_settings()
    embedder = make_embedder(settings)
    try:
        report = run_ingest(settings, embedder, load_rules())
    except (IngestError, TransientError) as e:
        typer.echo(f"Ingest failed; the live index is unchanged.\n{e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"{report.index_version} is live ({embedder.model_id}, {embedder.dim}-d)")
    typer.echo(f"{report.documents} documents, {report.chunks} chunks")
    typer.echo(f"Golden queries: {len(report.golden)} of {len(report.golden)} found")
    for query, rank in report.golden:
        found = f"#{rank}  {query.expect}" if rank else "--  no policy"
        typer.echo(f"  {found:<16} {query.query}")


@app.command()
def start(
    case: Annotated[Path, typer.Option("--case", exists=True, dir_okay=False, help="Case JSON.")],
) -> None:
    """Start a run and continue until it needs approval, completes or fails."""
    try:
        invoice = InvoiceCase.model_validate_json(case.read_text())
    except ValidationError as e:
        typer.echo(f"Invalid case file {case}:\n{e}", err=True)
        raise typer.Exit(1) from e
    try:
        run = _orchestrator().start(invoice)
    except NoIndexError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    _show(run)


@app.command("get")
def get_run(
    run_id: str,
    events: Annotated[bool, typer.Option("--events", help="Also print the audit trail.")] = False,
) -> None:
    """Show a run's state and result, and optionally its audit events."""
    store = RunStore(get_settings())
    try:
        run = store.load(run_id)
    except RunNotFound as e:
        typer.echo(f"No run {run_id}", err=True)
        raise typer.Exit(1) from e
    _show(run)
    if events:
        typer.echo("\nAudit trail:")
        for e in store.events(run_id):
            took = f"{e['duration_ms']} ms" if e["duration_ms"] is not None else ""
            typer.echo(
                f"  {e['ts']:%H:%M:%S.%f}"[:-3]
                + f"  {e['event_type']:<18} {e['name'] or '':<36} {e['outcome'] or '':<24} {took}"
            )


@app.command()
def resume(run_id: str) -> None:
    """Continue a run from its last checkpoint, reusing evidence already gathered."""
    try:
        run = _orchestrator().resume(run_id)
    except RunNotFound as e:
        typer.echo(f"No run {run_id}", err=True)
        raise typer.Exit(1) from e
    _show(run)


def _orchestrator() -> Orchestrator:
    settings = get_settings()
    rules = load_rules()
    return Orchestrator(
        settings,
        RunStore(settings),
        rules,
        erp_tools(PostgresErp(settings, rules)),
        KnowledgeBase(settings, make_embedder(settings)),
    )


def _show(run: Run) -> None:
    view = {"run_id": run.run_id, "case_id": run.case.case_id, "state": run.state}
    if run.failure_reason:
        view["failure_reason"] = run.failure_reason
    if run.result:
        view |= run.result.model_dump(mode="json")
    typer.echo(json.dumps(view, indent=2))
