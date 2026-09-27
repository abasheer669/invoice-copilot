from typing import Annotated

import typer

from ap_agent import __version__
from ap_agent.config import get_settings
from ap_agent.embeddings import make_embedder
from ap_agent.ingest import IngestError, run_ingest
from ap_agent.rules_config import load_rules
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
