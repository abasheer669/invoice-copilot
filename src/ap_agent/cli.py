from typing import Annotated

import typer

from ap_agent import __version__
from ap_agent.config import get_settings

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
