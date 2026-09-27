import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ap_agent import __version__
from ap_agent.approvals import ApprovalDenied, Callback, DecisionStore
from ap_agent.config import Settings, get_settings
from ap_agent.embeddings import make_embedder
from ap_agent.erp import PostgresErp, erp_tools
from ap_agent.evals import evaluate_case, load_specs, summary
from ap_agent.ingest import IngestError, run_ingest
from ap_agent.ledger import SimLedger, submit_tool
from ap_agent.llm import make_llm
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


Approver = Annotated[str, typer.Option("--approver", help="Who is deciding.")]
Role = Annotated[str, typer.Option("--role", help="Their role, e.g. DEPARTMENT_DIRECTOR.")]
CallbackId = Annotated[
    str,
    typer.Option("--callback-id", help="Unique per decision; a repeat is answered, not redone."),
]


@app.command()
def approve(run_id: str, approver: Approver, role: Role, callback_id: CallbackId) -> None:
    """Approve a run's recommendation; once all approvals are in, it is recorded once."""
    _decide(run_id, approver, role, callback_id, "APPROVE")


@app.command()
def reject(run_id: str, approver: Approver, role: Role, callback_id: CallbackId) -> None:
    """Reject a run's recommendation; the run closes and nothing is recorded."""
    _decide(run_id, approver, role, callback_id, "REJECT")


def _decide(run_id: str, approver: str, role: str, callback_id: str, decision: str) -> None:
    try:
        callback = Callback(
            run_id=run_id, approver=approver, role=role, callback_id=callback_id, decision=decision
        )
        response = _orchestrator().decide(callback)
    except (ValidationError, ApprovalDenied, RunNotFound) as e:
        typer.echo(f"Not accepted: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(response.model_dump_json(indent=2))


@app.command()
def resume(run_id: str) -> None:
    """Continue a run from its last checkpoint, reusing evidence already gathered."""
    try:
        run = _orchestrator().resume(run_id)
    except RunNotFound as e:
        typer.echo(f"No run {run_id}", err=True)
        raise typer.Exit(1) from e
    _show(run)


class EvalModel(StrEnum):
    fake = "fake"
    real = "real"


@app.command("eval")
def evaluate(
    model: Annotated[
        EvalModel,
        typer.Option(help="fake: offline model that accepts the rules draft; real: LLM_MODEL."),
    ] = EvalModel.fake,
    cases: Annotated[Path, typer.Option(help="Folder of case .yaml files.")] = Path("data/cases"),
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """Run every case end to end, approvals included, and report pass/fail and metrics."""
    base = get_settings()
    provider = "fake" if model is EvalModel.fake else base.llm_provider
    results = []
    if not as_json:
        typer.echo(
            f"{'case':<9}{'outcome':<26}{'recall@5':<10}{'citations':<11}"
            f"{'decisions':<11}{'payments':<10}{'safe':<6}result"
        )
    for spec in load_specs(cases):
        settings = Settings.model_validate(
            base.model_dump() | {"faults": spec.faults, "llm_provider": provider}
        )
        result = evaluate_case(spec, _orchestrator(settings), SimLedger(settings).records_for)
        results.append(result)
        if not as_json:
            recall = "-" if result.recall_at_5 is None else f"{result.recall_at_5:.2f}"
            typer.echo(
                f"{result.case:<9}{str(result.outcome):<26}{recall:<10}"
                f"{f'{result.valid_citations}/{result.citations}':<11}{result.decisions:<11}"
                f"{result.payments:<10}{'yes' if result.safe else 'NO':<6}"
                f"{'PASS' if result.passed else 'FAIL'}"
            )
            for problem in result.problems:
                typer.echo(f"         - {problem}")
    totals = summary(results)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "model": provider,
                    "results": [r.model_dump() for r in results],
                    "summary": totals,
                },
                indent=2,
            )
        )
    else:
        typer.echo("\n" + " · ".join(f"{k.replace('_', ' ')} {v}" for k, v in totals.items()))
    raise typer.Exit(0 if all(r.passed for r in results) else 1)


def _orchestrator(settings: Settings | None = None) -> Orchestrator:
    settings = settings or get_settings()
    rules = load_rules()
    return Orchestrator(
        settings,
        RunStore(settings),
        rules,
        erp_tools(PostgresErp(settings, rules)),
        KnowledgeBase(settings, make_embedder(settings)),
        DecisionStore(settings),
        submit_tool(SimLedger(settings)),
        make_llm(settings),
    )


def _show(run: Run) -> None:
    view = {"run_id": run.run_id, "case_id": run.case.case_id, "state": run.state}
    if run.state in ("RECEIVED", "GATHERING", "CHECKING", "RECOMMENDING", "SUBMITTING"):
        view["note"] = f"Paused; see `ap get {run.run_id} --events`, then `ap resume {run.run_id}`"
    if run.failure_reason:
        view["failure_reason"] = run.failure_reason
    if run.result:
        view |= run.result.model_dump(mode="json")
    typer.echo(json.dumps(view, indent=2))
