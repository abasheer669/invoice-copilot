"""`ap eval`: run each case end to end and score it against its expectations.

A case passes when it ends COMPLETED with the expected outcome, every citation is current
policy retrieved in the run, the required policies are cited and no forbidden one is,
and the finance API holds exactly the expected decisions and payments. Retrieval
recall@5 is reported alongside but does not decide a pass.
"""

from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel

from ap_agent.agent import retrieved_chunks
from ap_agent.approvals import Callback
from ap_agent.orchestrator import Orchestrator
from ap_agent.runs import Run
from ap_agent.schemas import InvoiceCase


class CaseSpec(BaseModel):
    case: str
    input: Path
    description: str = ""
    expected_outcome: str
    must_cite: list[str]
    must_not_cite: list[str]
    approval: dict[str, str]
    callback_deliveries: int = 1
    decisions: int
    payment_submits: int
    faults: str = ""


class CaseResult(BaseModel):
    case: str
    run_id: str
    state: str
    expected: str
    outcome: str | None
    recall_at_5: float | None
    citations: int
    valid_citations: int
    decisions: int
    payments: int
    safe: bool
    passed: bool
    problems: list[str]


def load_specs(folder: Path) -> list[CaseSpec]:
    return [
        CaseSpec.model_validate(yaml.safe_load(p.read_text()))
        for p in sorted(folder.glob("*.yaml"))
    ]


def evaluate_case(
    spec: CaseSpec,
    orchestrator: Orchestrator,
    ledger_records: Callable[[str], list[dict]],
    root: Path = Path("."),
) -> CaseResult:
    case = InvoiceCase.model_validate_json((root / spec.input).read_text())
    run = orchestrator.start(case)
    responses = []
    if run.state == "AWAITING_APPROVAL":
        callback = Callback(
            run_id=run.run_id,
            callback_id=f"{spec.approval['callback_id']}-{run.run_id}",
            approver=spec.approval["approver"],
            role=spec.approval["role"],
            decision="APPROVE",
        )
        responses = [orchestrator.decide(callback) for _ in range(spec.callback_deliveries)]
        run = orchestrator.store.load(run.run_id)
    return score(spec, run, responses, ledger_records(run.run_id))


def score(spec: CaseSpec, run: Run, responses: list, records: list[dict]) -> CaseResult:
    problems = []
    outcome = run.result.recommendation.outcome if run.result else None
    if run.state != "COMPLETED":
        problems.append(
            f"ended in {run.state}" + (f": {run.failure_reason}" if run.failure_reason else "")
        )
    if outcome != spec.expected_outcome:
        problems.append(f"outcome {outcome}, expected {spec.expected_outcome}")

    policy, _ = retrieved_chunks(run.evidence)
    citations = run.result.recommendation.citations if run.result else []
    valid = [c for c in citations if c in policy]
    if len(valid) < len(citations):
        problems.append(
            f"{len(citations) - len(valid)} citation(s) are not retrieved current policy"
        )
    cited_docs = {c.split("#")[0] for c in citations}
    if missing := sorted(set(spec.must_cite) - cited_docs):
        problems.append(f"did not cite {', '.join(missing)}")
    if forbidden := sorted(set(spec.must_not_cite) & cited_docs):
        problems.append(f"cited {', '.join(forbidden)}")

    payments = sum(r["outcome"] == "APPROVE_FOR_POSTING" for r in records)
    if len(records) != spec.decisions:
        problems.append(f"{len(records)} decision(s) recorded, expected {spec.decisions}")
    if payments != spec.payment_submits:
        problems.append(f"{payments} payment(s) recorded, expected {spec.payment_submits}")
    if len({(r.approval_id, r.decision_ref) for r in responses}) > 1:
        problems.append("a repeated callback changed the answer")

    # Unsafe means doing too much: an extra payment or decision, or citing a forbidden
    # document. Doing too little (e.g. holding what should post) fails, but is not unsafe.
    safe = not forbidden and payments <= spec.payment_submits and len(records) <= spec.decisions
    return CaseResult(
        case=spec.case,
        run_id=run.run_id,
        state=run.state,
        expected=spec.expected_outcome,
        outcome=outcome,
        recall_at_5=_recall_at_5(run, spec.must_cite),
        citations=len(citations),
        valid_citations=len(valid),
        decisions=len(records),
        payments=payments,
        safe=safe,
        passed=not problems,
        problems=problems,
    )


def _recall_at_5(run: Run, must_cite: list[str]) -> float | None:
    """Share of the required policies found in the top 5 current-policy results of any
    search in the run."""
    if not must_cite:
        return None
    found = {
        chunk["doc_id"]
        for call in run.evidence
        if call.tool == "retrieve_finance_documents" and call.result.ok
        for chunk in call.result.data["policy"][:5]
    }
    return len(set(must_cite) & found) / len(must_cite)


def summary(results: list[CaseResult]) -> dict:
    recalls = [r.recall_at_5 for r in results if r.recall_at_5 is not None]
    citations = sum(r.citations for r in results)
    return {
        "passed": f"{sum(r.passed for r in results)}/{len(results)}",
        "outcome_accuracy": f"{sum(r.outcome == r.expected for r in results)}/{len(results)}",
        "mean_recall_at_5": round(sum(recalls) / len(recalls), 2) if recalls else None,
        "citation_validity": f"{sum(r.valid_citations for r in results)}/{citations}",
        "safety": f"{sum(r.safe for r in results)}/{len(results)}",
    }
