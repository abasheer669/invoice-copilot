"""The typed result of a run: sourced facts, calculations, policy findings, exceptions,
inferences, unknowns, the recommendation and actions taken, each kept apart.
"""

import re
from typing import Literal

from pydantic import BaseModel

from ap_agent.masking import mask_values
from ap_agent.rules import ApprovalRequirement, Assessment, Calculation, Outcome
from ap_agent.schemas import InvoiceCase
from ap_agent.tools import ToolCall

# Who resolves an exception from each check (FIN-POL-007 §3; FIN-POL-009 §1 for currency).
OWNERS = {
    "DUPLICATE": "Accounts Payable",
    "VENDOR_STATUS": "Vendor Governance",
    "BANK_DETAILS": "Vendor Governance",
    "PURCHASE_ORDER": "requester",
    "CURRENCY": "requester, with Treasury",
    "RECEIPT": "requester or receipter",
    "PRICE": "requester or receipter",
    "INVOICE_TOTAL": "Accounts Payable",
    "FRAUD_INDICATORS": "Financial Crime and Controls",
    "SEGREGATION": "Financial Control",
}

ASSUMPTIONS = [
    "Invoice fields were extracted before this run and are taken as given.",
    "Invoice line numbers correspond to purchase-order line numbers.",
]


class Fact(BaseModel):
    claim: str
    source: str


class NamedCalculation(Calculation):
    rule_id: str


class Finding(BaseModel):
    rule_id: str
    status: str
    expected: str
    observed: str
    citation: str


class ExceptionRecord(BaseModel):  # FIN-POL-007 §2
    category: str
    rule_id: str
    expected: str
    observed: str
    citation: str
    owner: str


class Recommendation(BaseModel):
    outcome: Outcome
    rationale: str
    citations: list[str]  # chunk ids of current policy retrieved in this run
    assumptions: list[str]
    confidence: Literal["low", "medium", "high"]


class RunResult(BaseModel):
    facts: list[Fact]
    calculations: list[NamedCalculation]
    policy_findings: list[Finding]
    exceptions: list[ExceptionRecord]
    inferences: list[str]
    unknowns: list[str]
    recommendation: Recommendation
    approval: ApprovalRequirement
    next_action: str
    actions_taken: list[dict]


def build_result(case: InvoiceCase, calls: list[ToolCall], assessment: Assessment) -> RunResult:
    """The recommendation here comes straight from the rules engine."""
    open_checks = [c for c in assessment.checks if c.status != "PASS"]
    unknowns = _unknowns(calls, assessment)
    return RunResult(
        facts=[_invoice_fact(case)] + [fact for call in calls for fact in _facts(call)],
        calculations=[
            NamedCalculation(rule_id=c.rule_id, **c.calculation.model_dump())
            for c in assessment.checks
            if c.calculation
        ],
        policy_findings=[
            Finding(
                **c.model_dump(include={"rule_id", "status", "expected", "observed", "citation"})
            )
            for c in assessment.checks
        ],
        exceptions=[
            ExceptionRecord(
                category=c.exception_category,
                rule_id=c.rule_id,
                expected=c.expected,
                observed=c.observed,
                citation=c.citation,
                owner=OWNERS[re.sub(r"_L\d+$", "", c.rule_id)],
            )
            for c in open_checks
        ],
        inferences=[],
        unknowns=unknowns,
        recommendation=Recommendation(
            outcome=assessment.outcome,
            rationale="; ".join(f"{c.rule_id} {c.status}: {c.observed}" for c in open_checks)
            or "All checks passed.",
            citations=_citations(calls, assessment),
            assumptions=ASSUMPTIONS,
            confidence="low" if unknowns else "high",
        ),
        approval=assessment.approval,
        next_action=next_action(assessment.outcome, assessment),
        actions_taken=[],
    )


def _unknowns(calls: list[ToolCall], assessment: Assessment) -> list[str]:
    unknowns = []
    for call in calls:
        if not call.result.ok:
            unknowns.append(
                f"{call.tool} {mask_values(call.args)} failed: {call.result.error} "
                f"after {call.result.attempts} attempt(s)"
            )
        elif call.tool == "retrieve_finance_documents" and not call.result.data["policy"]:
            unknowns.append(f"No current policy found for {call.args['query']!r}")
    unknowns += [f"{c.rule_id}: {c.observed}" for c in assessment.checks if c.status == "UNKNOWN"]
    return unknowns


def _invoice_fact(case: InvoiceCase) -> Fact:
    return Fact(
        claim=f"Invoice {case.invoice_ref} from {case.vendor_id} dated {case.invoice_date}: "
        f"{case.amount} {case.currency} including {case.tax_amount} tax; "
        f"remit to account ending {case.remit_to_last4}",
        source=f"invoice case {case.case_id} (supplier-provided)",
    )


def _facts(call: ToolCall) -> list[Fact]:
    """What a successful business-data lookup established, citing the tool as source."""
    data, tool = call.result.data, call.tool
    if not call.result.ok:
        return []
    if tool == "get_vendor_record":
        claims = [
            f"Vendor {data['vendor_id']} ({data['legal_name']}) is {data['status']}; "
            f"bank account ends {data['bank_last4']} ({data['bank_country']})"
        ]
    elif tool == "get_purchase_order":
        claims = [
            f"{data['po_ref']} for {data['vendor_id']}: total {data['total']} "
            f"{data['currency']} before tax, {data['approval_status']}"
        ]
        claims += [
            f"{data['po_ref']} line {line['line_no']}: {line['qty']} x {line['unit_price']} "
            f"({line['kind']}), tolerance {line['tolerance']}"
            for line in data["lines"]
        ]
        claims += [
            f"Receipt {r['receipt_id']}: {r['qty_received']} received for line {r['line_no']} "
            f"by {r['received_by']}"
            for r in data["receipts"]
        ]
    elif tool == "check_invoice_history":
        claims = [
            f"{m['record_id']}: {m['invoice_ref']} {m['amount']} {m['currency']} dated "
            f"{m['invoice_date']}, {m['status']} ({m['match_type']} match)"
            for m in data["matches"]
        ] or ["No matching invoices in history"]
    else:
        claims = []  # retrieved policy text is cited, not restated as fact
    return [Fact(claim=claim, source=tool) for claim in claims]


def _citations(calls: list[ToolCall], assessment: Assessment) -> list[str]:
    """For each policy behind the open checks (or every check, when all passed), the
    best retrieved current-policy chunk, preferring the exact section the rule cites."""
    checks = [c for c in assessment.checks if c.status != "PASS"] or assessment.checks
    cited = [c.citation for c in checks]
    if assessment.outcome == "APPROVE_FOR_POSTING":
        cited.append(assessment.approval.citation)
    wanted: dict[str, set[str]] = {}
    for citation in cited:
        wanted.setdefault(citation.split()[0], set()).update(_sections(citation))
    retrieved = [
        chunk
        for call in calls
        if call.tool == "retrieve_finance_documents" and call.result.ok
        for chunk in call.result.data["policy"]
    ]
    chosen: list[str] = []
    for doc, sections in wanted.items():
        in_doc = [c for c in retrieved if c["doc_id"] == doc]
        in_section = [c for c in in_doc if _sections(c["citation"]) & sections]
        best = max(in_section or in_doc, key=lambda c: c["score"], default=None)
        if best and best["chunk_id"] not in chosen:
            chosen.append(best["chunk_id"])
    return chosen


def _sections(citation: str) -> set[int]:
    """Section numbers in a citation: "FIN-POL-003 v4.0 §2-3" gives {2, 3}."""
    match = re.search(r"§(\d+)(?:-(\d+))?", citation)
    if match is None:
        return set()
    return set(range(int(match[1]), int(match[2] or match[1]) + 1))


def next_action(outcome: Outcome, assessment: Assessment) -> str:
    approval = assessment.approval
    approvers = approval.role + (
        f" and {approval.co_approval_role}" if approval.co_approval_role else ""
    )
    open_rules = ", ".join(c.rule_id for c in assessment.checks if c.status != "PASS")
    return {
        "APPROVE_FOR_POSTING": f"Awaiting {approvers} approval before posting",
        "HOLD_FOR_INFORMATION": f"Awaiting confirmation to hold; resolve {open_rules}",
        "REJECT_DUPLICATE": "Awaiting confirmation to reject as a duplicate; no payment proposed",
        "REJECT_INVALID": "Awaiting confirmation to reject as invalid; no payment proposed",
        "ESCALATE_CONTROL_REVIEW": "Awaiting Financial Control review; do not pay, and do not "
        "tell the supplier about the suspicion",
    }[outcome]
