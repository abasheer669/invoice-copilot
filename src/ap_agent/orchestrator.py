"""Runs one invoice case through the state machine, pausing for a human decision.

The model gathers and explains; code decides and writes; a human approves. Code owns
which evidence is mandatory, the checks, the outcome, who may approve and every
transition. The run is saved after every tool call and state change, so a crashed run
resumes where it stopped without fetching evidence or submitting twice.
"""

import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Protocol, TypeVar

from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ValidationError

from ap_agent.agent import (
    EVIDENCE_SYSTEM,
    READ_ONLY,
    RECOMMEND_SYSTEM,
    LLMRecommendation,
    case_brief,
    recommendation_prompt,
    repair_prompt,
    tool_result_for_model,
    validate_recommendation,
)
from ap_agent.approvals import (
    Approval,
    ApprovalDenied,
    Callback,
    Decision,
    DecisionStore,
    approval_problem,
    approvals_needed,
    outstanding,
)
from ap_agent.config import Settings
from ap_agent.embeddings import Embedder
from ap_agent.llm import LLM, LLMError, ToolRequest, ToolSpec
from ap_agent.masking import mask_values
from ap_agent.result import ASSUMPTIONS, Recommendation, build_result, next_action
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


class SubmitFailed(Exception):
    pass


class RecommendationInvalid(Exception):
    """The model's recommendation failed validation twice."""


class Paused(Exception):
    """The step cannot finish now, e.g. the model is unavailable; `ap resume` retries it."""


T = TypeVar("T")
CONFIDENCE = ["low", "medium", "high"]


class DecisionResponse(BaseModel):
    """The answer to an approval callback; a repeated callback gets the same answer."""

    run_id: str
    callback_id: str
    approval_id: str
    decision: Decision
    state: State
    decision_ref: str | None
    replayed: bool
    next_action: str


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        store: RunStore,
        rules: RulesConfig,
        erp_tools: dict[str, Tool],
        kb: KnowledgeBaseLike,
        decisions: DecisionStore,
        submit: Tool,
        llm: LLM,
    ):
        self.settings = settings
        self.store = store
        self.rules = rules
        self.erp_tools = erp_tools
        self.kb = kb
        self.decisions = decisions
        self.submit = submit  # the only write tool; never part of a run's read-only toolset
        self.llm = llm

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
            llm_model=self.llm.model_id,
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
        return self._settle(run)

    def decide(self, callback: Callback) -> DecisionResponse:
        """Handle an approval callback. A repeat of a stored callback changes nothing and
        returns the same answer; the run is then taken as far as its approvals allow."""
        approval = self.decisions.by_callback(callback.callback_id)
        replayed = approval is not None
        if approval is None:
            try:
                approval = self._record_approval(callback)
            except UniqueViolation:  # the same callback arrived twice at the same moment
                approval, replayed = self.decisions.by_callback(callback.callback_id), True
        if not approval.answers(callback):
            raise ApprovalDenied(f"callback {callback.callback_id} was used for another decision")
        if replayed:
            self.store.event(
                approval.run_id,
                "callback_replayed",
                name=callback.callback_id,
                outcome=approval.decision,
            )
        run = self._settle(self.store.load(approval.run_id))
        receipt = self.decisions.decision(_idempotency_key(run))
        return DecisionResponse(
            run_id=run.run_id,
            callback_id=approval.callback_id,
            approval_id=approval.approval_id,
            decision=approval.decision,
            state=run.state,
            decision_ref=receipt["decision_ref"] if receipt else None,
            replayed=replayed,
            next_action=run.result.next_action,
        )

    def advance(self, run: Run) -> Run:
        """Run steps until the run waits for a human, finishes or fails."""
        steps = {
            "RECEIVED": self._receive,
            "GATHERING": self._gather,
            "CHECKING": self._check,
            "RECOMMENDING": self._recommend,
            "SUBMITTING": self._submit,
        }
        while run.state in steps:
            if run.step_count >= self.settings.max_steps:
                self._fail(run, f"step budget of {self.settings.max_steps} exceeded")
                break
            try:
                steps[run.state](run)
            except StaleRunError:
                raise  # another process owns the run now; do not overwrite it
            except Paused as e:
                self.store.event(
                    run.run_id,
                    "run_paused",
                    name=run.state,
                    outcome="paused",
                    payload={"reason": str(e)},
                )
                break
            except Exception as e:
                self._fail(run, f"{type(e).__name__}: {e}")
        return run

    def _receive(self, run: Run) -> None:
        self._move(run, "GATHERING")  # the case was validated before the run was created

    def _gather(self, run: Run) -> None:
        """The model chooses lookups first; code then runs any mandatory one it skipped,
        so the model's choices can never remove a control."""
        tools = self._tools(run)
        self._explore(run, tools)
        for name, args in mandatory_calls(run.case):
            if _find(run, tools[name], args) is None:
                self._call(run, tools[name], args)
        self._move(run, "CHECKING")

    def _explore(self, run: Run, tools: dict[str, Tool]) -> None:
        """A bounded tool loop: at most MAX_TOOL_CALLS requests, read-only tools only."""
        budget = self.settings.max_tool_calls
        used = sum(c.by == "llm" for c in run.evidence)  # counts calls made before a crash
        specs = [
            ToolSpec(t.name, t.description, t.input_model.model_json_schema())
            for t in tools.values()
        ]
        brief = case_brief(run.case)
        try:
            session = self.llm.tool_session(EVIDENCE_SYSTEM.format(budget=budget), specs)
            reply = self._ask(run, "evidence", brief, lambda: session.send(brief))
            while reply.tool_requests and used < budget:
                results = []
                for request in reply.tool_requests:  # every request gets an answer
                    if used < budget:
                        results.append((request, self._model_tool_call(run, tools, request)))
                        used += 1
                    else:
                        results.append((request, tool_result_for_model(None, "tool budget used")))
                sent = str([(r.name, r.args) for r, _ in results])
                reply = self._ask(
                    run, "evidence", sent, lambda r=results: session.send_tool_results(r)
                )
            if reply.tool_requests:
                self.store.event(run.run_id, "tool_budget_exhausted", outcome=f"{used} calls")
        except LLMError as e:
            # The mandatory lookups below still run, so the checks lose nothing.
            self.store.event(run.run_id, "llm_unavailable", name="evidence", outcome=str(e))

    def _model_tool_call(self, run: Run, tools: dict[str, Tool], request: ToolRequest) -> dict:
        if request.name not in READ_ONLY or request.name not in tools:
            self.store.event(
                run.run_id,
                "tool_denied",
                name=request.name,
                outcome="forbidden",
                payload={"args": mask_values(request.args)},
            )
            return tool_result_for_model(None, f"{request.name} is not an available tool")
        earlier = _find(run, tools[request.name], request.args)
        return tool_result_for_model(
            earlier or self._call(run, tools[request.name], request.args, by="llm")
        )

    def _check(self, run: Run) -> None:
        case, tools = run.case, self._tools(run)
        evidence = Evidence(
            vendor=_data(run, tools["get_vendor_record"], {"vendor_id": case.vendor_id}),
            purchase_order=_data(run, tools["get_purchase_order"], {"po_ref": case.po_ref}),
            history=_data(run, tools["check_invoice_history"], history_args(case)),
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
        """The rules engine drafts; the model rewrites the draft; code validates the result."""
        result = build_result(run.case, run.evidence, run.assessment)
        rec = self._model_recommendation(run, result)
        ceiling = CONFIDENCE.index(result.recommendation.confidence)  # low if anything is unknown
        result.recommendation = Recommendation(
            outcome=rec.outcome,
            rationale=rec.rationale,
            citations=rec.citations,
            assumptions=ASSUMPTIONS + [a for a in rec.assumptions if a not in ASSUMPTIONS],
            confidence=CONFIDENCE[min(CONFIDENCE.index(rec.confidence), ceiling)],
        )
        result.inferences = rec.inferences
        result.unknowns += [u for u in rec.unknowns if u not in result.unknowns]
        result.next_action = next_action(rec.outcome, run.assessment)
        run.result = result
        self.store.event(
            run.run_id,
            "approval_requested",
            outcome=run.result.recommendation.outcome,
            payload=run.result.approval.model_dump(mode="json"),
        )
        self._move(run, "AWAITING_APPROVAL")

    def _model_recommendation(self, run: Run, draft) -> LLMRecommendation:
        prompt = recommendation_prompt(run.case, run.evidence, run.assessment, draft)
        schema = LLMRecommendation.model_json_schema()
        answer, errors = "", []
        for attempt in (1, 2):  # one repair, then fail explicitly
            text = repair_prompt(prompt, answer, errors) if errors else prompt
            try:
                answer = self._ask(
                    run,
                    "recommendation",
                    text,
                    lambda text=text: self.llm.generate_json(RECOMMEND_SYSTEM, text, schema),
                )
            except LLMError as e:
                raise Paused(f"model unavailable: {e}") from e
            rec, errors = validate_recommendation(answer, run.evidence, run.assessment.outcome)
            if rec:
                return rec
            self.store.event(
                run.run_id,
                "validation_error",
                name="recommendation",
                outcome="rejected",
                payload={"attempt": attempt, "errors": errors},
            )
        raise RecommendationInvalid("; ".join(errors))

    def _ask(self, run: Run, purpose: str, sent: str, send: Callable[[], T]) -> T:
        """Call the model and log it with a hash of what was sent, never the text itself."""
        started = time.monotonic()
        payload = {"model": self.llm.model_id, "sent_sha256": _sha(sent)}
        try:
            reply = send()
        except LLMError as e:
            self.store.event(
                run.run_id,
                "llm_call",
                name=purpose,
                outcome="error",
                duration_ms=_ms(started),
                payload=payload | {"error": str(e)},
            )
            raise
        if hasattr(reply, "tool_requests"):
            payload["tool_requests"] = [r.name for r in reply.tool_requests]
        self.store.event(
            run.run_id,
            "llm_call",
            name=purpose,
            outcome="ok",
            duration_ms=_ms(started),
            payload=payload,
        )
        return reply

    def _record_approval(self, callback: Callback) -> Approval:
        run = self.store.load(callback.run_id)
        if run.state != "AWAITING_APPROVAL":
            raise ApprovalDenied(f"{run.run_id} is {run.state}, not awaiting approval")
        prior = self.decisions.for_run(run.run_id)
        problem = approval_problem(run, callback, prior, self.rules, self.kb.active_version())
        who = {"approver": callback.approver, "role": callback.role}
        if problem:
            self.store.event(
                run.run_id,
                "approval_denied",
                name=callback.callback_id,
                outcome="denied",
                payload=who | {"decision": callback.decision, "reason": problem},
            )
            raise ApprovalDenied(problem)
        approval = self.decisions.record(callback)
        self.store.event(
            run.run_id,
            "approval_recorded",
            name=callback.callback_id,
            outcome=callback.decision,
            payload=who | {"approval_id": approval.approval_id},
        )
        return approval

    def _settle(self, run: Run) -> Run:
        """Move a waiting run on if its stored decisions allow it, then continue."""
        if run.state == "AWAITING_APPROVAL":
            approvals = self.decisions.for_run(run.run_id)
            rejection = next((a for a in approvals if a.decision == "REJECT"), None)
            remaining = outstanding(approvals_needed(run, self.rules), approvals)
            if rejection:
                run.result.next_action = (
                    f"Closed: {rejection.approver} ({rejection.role}) rejected the "
                    "recommendation; nothing was recorded"
                )
                self._move(run, "CLOSED")
            elif not remaining:
                self._move(run, "SUBMITTING")
            elif approvals:
                run.result.next_action = (
                    f"Awaiting {', '.join(n.label for n in remaining)} approval as well"
                )
                self.store.save(run)
        return self.advance(run)

    def _submit(self, run: Run) -> None:
        """Record the approved outcome exactly once. The decisions table stops a resumed run
        calling the API again; the API's idempotency key covers a crash in between."""
        key = _idempotency_key(run)
        receipt = self.decisions.decision(key)
        if receipt is None:
            approval = [a for a in self.decisions.for_run(run.run_id) if a.decision == "APPROVE"]
            args = {
                "idempotency_key": key,
                "run_id": run.run_id,
                "outcome": run.result.recommendation.outcome,
                "amount": str(run.case.amount),
                "currency": run.case.currency,
                "approval_id": approval[-1].approval_id,
            }
            result = invoke(self.submit, args, self.settings)
            run.tool_call_count += 1
            self.store.event(
                run.run_id,
                "tool_call",
                name=self.submit.name,
                outcome="ok" if result.ok else result.error,
                duration_ms=result.duration_ms,
                payload={
                    "idempotency_key": key,
                    "attempts": result.attempts,
                    "error": result.error,
                    "data": result.data,
                },
            )
            if not result.ok:
                raise SubmitFailed(f"{self.submit.name} failed: {result.error}")
            receipt = result.data
            self.decisions.record_decision(key, run.run_id, receipt)
        run.result.actions_taken.append(
            {"action": self.submit.name, "idempotency_key": key} | receipt
        )
        run.result.next_action = f"Done: {receipt['outcome']} recorded as {receipt['decision_ref']}"
        self._move(run, "COMPLETED")

    def _call(self, run: Run, tool: Tool, args: dict, by: str = "code") -> ToolCall:
        result = invoke(tool, args, self.settings)
        call = ToolCall(tool=tool.name, args=args, result=result, by=by)
        if result.error != "invalid_args":  # a malformed request is logged, not evidence
            run.evidence.append(call)
        run.tool_call_count += 1
        self.store.save(run)
        payload = {"args": mask_values(args), "attempts": result.attempts, "error": result.error}
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
            payload=payload | {"by": by},
        )
        return call

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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _idempotency_key(run: Run) -> str:
    return f"{run.run_id}:{run.result.recommendation.outcome}" if run.result else ""


def _same_args(tool: Tool, a: dict, b: dict) -> bool:
    """Compare arguments as the tool reads them, so 8800 and "8800.00" are the same call."""
    try:
        return tool.input_model.model_validate(a) == tool.input_model.model_validate(b)
    except ValidationError:
        return a == b


def _find(run: Run, tool: Tool, args: dict, ok_only: bool = False) -> ToolCall | None:
    """The latest recorded call of this tool with these arguments."""
    for call in reversed(run.evidence):
        if call.tool == tool.name and (call.result.ok or not ok_only):
            if _same_args(tool, call.args, args):
                return call
    return None


def _data(run: Run, tool: Tool, args: dict) -> dict | None:
    """The latest successful result of this call, or None if it failed or never ran."""
    call = _find(run, tool, args, ok_only=True)
    return call.result.data if call else None
