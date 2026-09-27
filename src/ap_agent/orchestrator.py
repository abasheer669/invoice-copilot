"""Runs one invoice case through the state machine until it needs a human or fails.

Code owns every step: which evidence is mandatory, the checks, the outcome and each
transition. The run is saved after every tool call and state change, so a crashed run
resumes where it stopped without fetching the same evidence twice.
"""

import secrets
import time
from typing import Protocol

from ap_agent.config import Settings
from ap_agent.embeddings import Embedder
from ap_agent.result import build_result
from ap_agent.retrieval import retrieval_tool
from ap_agent.rules import Evidence, assess
from ap_agent.rules_config import RulesConfig
from ap_agent.runs import Run, RunStore, StaleRunError, State
from ap_agent.schemas import InvoiceCase
from ap_agent.tools import Tool, ToolCall, invoke

# Policy searches every run makes, so the checks always have policy to cite.
STANDARD_QUERIES = (
    "three-way matching price and quantity tolerances and missing receipts",
    "delegated financial authority approval limits and co-approval",
    "duplicate invoice detection and fraud indicators",
    "vendor bank account change verification and vendor status",
)


class KnowledgeBaseLike(Protocol):
    embedder: Embedder

    def active_version(self) -> str | None: ...

    def retrieve(self, q, index_version: str) -> dict: ...


class NoIndexError(Exception):
    pass


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        store: RunStore,
        rules: RulesConfig,
        erp_tools: dict[str, Tool],
        kb: KnowledgeBaseLike,
    ):
        self.settings = settings
        self.store = store
        self.rules = rules
        self.erp_tools = erp_tools
        self.kb = kb

    def start(self, case: InvoiceCase) -> Run:
        index_version = self.kb.active_version()
        if index_version is None:
            raise NoIndexError("No live knowledge base; run `ap ingest` first.")
        run = Run(
            run_id=f"run_{secrets.token_hex(4)}",
            case=case,
            index_version=index_version,
            rules_version=self.rules.version,
            embed_model=self.kb.embedder.model_id,
        )
        self.store.create(run)
        self.store.event(
            run.run_id,
            "run_started",
            outcome=run.state,
            payload={
                "case_id": case.case_id,
                "index_version": index_version,
                "rules_version": self.rules.version,
            },
        )
        return self.advance(run)

    def resume(self, run_id: str) -> Run:
        run = self.store.load(run_id)
        self.store.event(run_id, "run_resumed", outcome=run.state)
        return self.advance(run)

    def advance(self, run: Run) -> Run:
        """Run steps until the run waits for a human, finishes or fails."""
        steps = {
            "RECEIVED": self._receive,
            "GATHERING": self._gather,
            "CHECKING": self._check,
            "RECOMMENDING": self._recommend,
        }
        while run.state in steps:
            if run.step_count >= self.settings.max_steps:
                self._fail(run, f"step budget of {self.settings.max_steps} exceeded")
                break
            try:
                steps[run.state](run)
            except StaleRunError:
                raise  # another process owns the run now; do not overwrite it
            except Exception as e:
                self._fail(run, f"{type(e).__name__}: {e}")
        return run

    def _receive(self, run: Run) -> None:
        self._move(run, "GATHERING")  # the case was validated before the run was created

    def _gather(self, run: Run) -> None:
        tools = self._tools(run)
        for name, args in mandatory_calls(run.case):
            if not any(c.tool == name and c.args == args for c in run.evidence):
                self._call(run, tools[name], args)
        self._move(run, "CHECKING")

    def _check(self, run: Run) -> None:
        case = run.case
        evidence = Evidence(
            vendor=_data(run, "get_vendor_record", {"vendor_id": case.vendor_id}),
            purchase_order=_data(run, "get_purchase_order", {"po_ref": case.po_ref}),
            history=_data(run, "check_invoice_history", history_args(case)),
        )
        run.assessment = assess(case, evidence, self.rules)
        run.rules_version = self.rules.version
        not_passed = {c.rule_id: c.status for c in run.assessment.checks if c.status != "PASS"}
        self.store.event(
            run.run_id,
            "checks_completed",
            outcome=run.assessment.outcome,
            payload={"not_passed": not_passed, "fraud_indicators": run.assessment.fraud_indicators},
        )
        self._move(run, "RECOMMENDING")

    def _recommend(self, run: Run) -> None:
        run.result = build_result(run.case, run.evidence, run.assessment)
        self.store.event(
            run.run_id,
            "approval_requested",
            outcome=run.result.recommendation.outcome,
            payload=run.result.approval.model_dump(mode="json"),
        )
        self._move(run, "AWAITING_APPROVAL")

    def _call(self, run: Run, tool: Tool, args: dict) -> None:
        result = invoke(tool, args, self.settings)
        run.evidence.append(ToolCall(tool=tool.name, args=args, result=result))
        run.tool_call_count += 1
        self.store.save(run)
        payload = {"args": args, "attempts": result.attempts, "error": result.error}
        if result.ok and tool.name == "retrieve_finance_documents":
            payload["index_version"] = result.data["index_version"]
            payload["policy"] = [c["chunk_id"] for c in result.data["policy"]]
            payload["other_evidence"] = [c["chunk_id"] for c in result.data["other_evidence"]]
        self.store.event(
            run.run_id,
            "retrieval" if tool.name == "retrieve_finance_documents" else "tool_call",
            name=tool.name,
            outcome="ok" if result.ok else result.error,
            duration_ms=result.duration_ms,
            payload=payload,
        )

    def _move(self, run: Run, state: State) -> None:
        started = time.monotonic()
        previous = run.move_to(state)
        run.step_count += 1
        self.store.save(run)
        self.store.event(
            run.run_id,
            "state_change",
            name=f"{previous} -> {state}",
            outcome=state,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _fail(self, run: Run, reason: str) -> None:
        run.failure_reason = reason
        previous = run.move_to("FAILED")
        self.store.save(run)
        self.store.event(
            run.run_id,
            "run_failed",
            name=f"{previous} -> FAILED",
            outcome="FAILED",
            payload={"reason": reason},
        )

    def _tools(self, run: Run) -> dict[str, Tool]:
        retrieval = retrieval_tool(self.kb, run.index_version)
        return {**self.erp_tools, retrieval.name: retrieval}


def history_args(case: InvoiceCase) -> dict:
    return case.model_dump(
        mode="json", include={"vendor_id", "invoice_ref", "amount", "currency", "invoice_date"}
    )


def mandatory_calls(case: InvoiceCase) -> list[tuple[str, dict]]:
    """The lookups every case needs, whatever else is gathered (FIN-POL-001 §5)."""
    calls = [
        ("get_vendor_record", {"vendor_id": case.vendor_id}),
        ("check_invoice_history", history_args(case)),
    ]
    if case.po_ref:
        calls.append(("get_purchase_order", {"po_ref": case.po_ref}))
    calls += [("retrieve_finance_documents", {"query": q, "k": 5}) for q in STANDARD_QUERIES]
    return calls


def _data(run: Run, tool: str, args: dict) -> dict | None:
    """The latest successful result of this exact call, or None if it failed or never ran."""
    for call in reversed(run.evidence):
        if call.tool == tool and call.args == args and call.result.ok:
            return call.result.data
    return None
