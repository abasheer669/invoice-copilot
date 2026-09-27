"""Deterministic policy checks: code decides, the LLM only explains.

Every check is a pure function over Decimal values. It returns PASS, FAIL or UNKNOWN
(evidence missing) with the expected and observed facts, any calculation, the policy
citation, and the outcome a failure leads to. The case outcome is the most severe
consequence among the checks that did not pass.
"""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel

from ap_agent.erp import InvoiceHistory, PoLine, PurchaseOrder, VendorRecord
from ap_agent.rules_config import CENT, RulesConfig
from ap_agent.schemas import InvoiceCase, InvoiceLine

Outcome = Literal[
    "APPROVE_FOR_POSTING",
    "HOLD_FOR_INFORMATION",
    "REJECT_DUPLICATE",
    "REJECT_INVALID",
    "ESCALATE_CONTROL_REVIEW",
]
SEVERITY: list[Outcome] = [
    "REJECT_DUPLICATE",
    "REJECT_INVALID",
    "ESCALATE_CONTROL_REVIEW",
    "HOLD_FOR_INFORMATION",
    "APPROVE_FOR_POSTING",
]
Status = Literal["PASS", "FAIL", "UNKNOWN"]
ExceptionCategory = Literal[  # FIN-POL-007 §1
    "MISSING_PO",
    "MISSING_RECEIPT",
    "PRICE_VARIANCE",
    "QUANTITY_VARIANCE",
    "DUPLICATE_RISK",
    "VENDOR_BLOCK",
    "BANK_CHANGE",
    "AUTHORITY_GAP",
    "TAX_QUERY",
    "OTHER_CONTROL_RISK",
]
HOLD: Outcome = "HOLD_FOR_INFORMATION"
ESCALATE: Outcome = "ESCALATE_CONTROL_REVIEW"


class Calculation(BaseModel):
    formula: str
    inputs: dict[str, str]
    result: str
    rounding: str = "ROUND_HALF_UP to 0.01"


class CheckResult(BaseModel):
    rule_id: str
    status: Status
    expected: str
    observed: str
    citation: str
    exception_category: ExceptionCategory
    on_fail: Outcome  # an UNKNOWN always leads to a hold
    calculation: Calculation | None = None


class Evidence(BaseModel):
    """Tool results for one case; None means the lookup failed or was not possible."""

    vendor: VendorRecord | None = None
    purchase_order: PurchaseOrder | None = None
    history: InvoiceHistory | None = None


class ApprovalRequirement(BaseModel):
    role: str
    limit: Decimal | None
    co_approval_role: str | None  # set when a second approval is required
    co_approval_reasons: list[str]
    excluded_approvers: list[str]
    citation: str


class Assessment(BaseModel):
    rules_version: str
    outcome: Outcome
    checks: list[CheckResult]
    fraud_indicators: list[str]
    approval: ApprovalRequirement


def assess(case: InvoiceCase, evidence: Evidence, rules: RulesConfig) -> Assessment:
    indicators = fraud_indicators(case, evidence.vendor, rules)
    checks = [
        check_duplicate(evidence.history, rules),
        check_vendor_status(evidence.vendor, rules),
        check_bank_details(case, evidence.vendor, rules),
        check_purchase_order(case, evidence.purchase_order, rules),
        check_currency(case, evidence.purchase_order, rules),
        *[c for line in case.lines for c in check_line(line, evidence.purchase_order, rules)],
        check_total(case, rules),
        check_fraud(indicators, rules),
        check_segregation(case, evidence.purchase_order, rules),
    ]
    return Assessment(
        rules_version=rules.version,
        outcome=decide_outcome(checks),
        checks=checks,
        fraud_indicators=indicators,
        approval=approval_requirement(case, evidence, rules),
    )


def decide_outcome(checks: list[CheckResult]) -> Outcome:
    consequences = [c.on_fail if c.status == "FAIL" else HOLD for c in checks if c.status != "PASS"]
    return min(consequences, key=SEVERITY.index, default="APPROVE_FOR_POSTING")


def _status(passed: bool | None) -> Status:
    return "UNKNOWN" if passed is None else "PASS" if passed else "FAIL"


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


def check_duplicate(history: InvoiceHistory | None, rules: RulesConfig) -> CheckResult:
    if history is None:
        passed, observed, on_fail = None, "invoice history unavailable", HOLD
    else:
        matches = history.matches
        passed = not matches
        observed = ", ".join(f"{m.record_id} ({m.match_type}, {m.status})" for m in matches)
        # An exact match to a paid or posted invoice is rejected; anything else needs review.
        exact = any(m.match_type == "EXACT" and m.status in ("PAID", "POSTED") for m in matches)
        on_fail = "REJECT_DUPLICATE" if exact else HOLD
    return CheckResult(
        rule_id="DUPLICATE",
        status=_status(passed),
        expected="no matching invoice in history",
        observed=observed or "no matches",
        citation=rules.cite("FIN-POL-005", "§1-2"),
        exception_category="DUPLICATE_RISK",
        on_fail=on_fail,
    )


def check_vendor_status(vendor: VendorRecord | None, rules: RulesConfig) -> CheckResult:
    return CheckResult(
        rule_id="VENDOR_STATUS",
        status=_status(None if vendor is None else vendor.status == "ACTIVE"),
        expected="ACTIVE",
        observed=vendor.status if vendor else "vendor record unavailable",
        citation=rules.cite("FIN-POL-004", "§4"),
        exception_category="VENDOR_BLOCK",
        on_fail=HOLD,
    )


def check_bank_details(
    case: InvoiceCase, vendor: VendorRecord | None, rules: RulesConfig
) -> CheckResult:
    return CheckResult(
        rule_id="BANK_DETAILS",
        status=_status(None if vendor is None else case.remit_to_last4 == vendor.bank_last4),
        expected=f"account ending {vendor.bank_last4} (vendor master)" if vendor else "unknown",
        observed=f"invoice asks for account ending {case.remit_to_last4}",
        citation=rules.cite("FIN-POL-004", "§2"),
        exception_category="BANK_CHANGE",
        on_fail=ESCALATE,
    )


def check_purchase_order(
    case: InvoiceCase, po: PurchaseOrder | None, rules: RulesConfig
) -> CheckResult:
    if case.po_ref is None:
        passed, observed = False, "no purchase-order reference on the invoice"
    elif po is None:
        passed, observed = None, f"{case.po_ref} unavailable"
    else:
        passed = po.vendor_id == case.vendor_id and po.approval_status == "APPROVED"
        observed = f"{po.po_ref} for {po.vendor_id}, {po.approval_status}"
    return CheckResult(
        rule_id="PURCHASE_ORDER",
        status=_status(passed),
        expected=f"an approved purchase order for {case.vendor_id}",
        observed=observed,
        citation=rules.cite("FIN-POL-002", "§1"),
        exception_category="MISSING_PO",
        on_fail=HOLD,
    )


def check_currency(case: InvoiceCase, po: PurchaseOrder | None, rules: RulesConfig) -> CheckResult:
    if po is None:
        passed, section = None, "§1"
    elif case.currency != po.currency:
        passed, section = False, "§1"
    elif case.currency != rules.home_currency:
        passed, section = None, "§2"  # authority needs a verified FX rate, which we lack
    else:
        passed, section = True, "§1"
    return CheckResult(
        rule_id="CURRENCY",
        status=_status(passed),
        expected=f"invoice and purchase order both in {rules.home_currency}",
        observed=f"invoice {case.currency}, purchase order {po.currency if po else 'unavailable'}",
        citation=rules.cite("FIN-POL-009", section),
        exception_category="OTHER_CONTROL_RISK",
        on_fail=HOLD,
    )


def check_line(
    line: InvoiceLine, po: PurchaseOrder | None, rules: RulesConfig
) -> list[CheckResult]:
    """Receipt and quantity, then price tolerance, for one invoice line."""
    if po is None:
        return _unmatched_line(line, None, "purchase order unavailable", rules)
    po_line = next((pl for pl in po.lines if pl.line_no == line.line_no), None)
    if po_line is None:
        return _unmatched_line(line, False, f"line {line.line_no} is not on {po.po_ref}", rules)
    return [check_receipt(line, po, rules), check_price(line, po_line, po, rules)]


def _unmatched_line(
    line: InvoiceLine, passed: bool | None, observed: str, rules: RulesConfig
) -> list[CheckResult]:
    n = line.line_no
    common = dict(status=_status(passed), observed=observed, on_fail=HOLD)
    return [
        CheckResult(
            rule_id=f"RECEIPT_L{n}",
            expected=f"receipt for line {n}",
            citation=rules.cite("FIN-POL-002", "§4"),
            exception_category="MISSING_RECEIPT",
            **common,
        ),
        CheckResult(
            rule_id=f"PRICE_L{n}",
            expected=f"line {n} within tolerance",
            citation=rules.cite("FIN-POL-002", "§2"),
            exception_category="PRICE_VARIANCE",
            **common,
        ),
    ]


def check_receipt(line: InvoiceLine, po: PurchaseOrder, rules: RulesConfig) -> CheckResult:
    received = sum((r.qty_received for r in po.receipts if r.line_no == line.line_no), Decimal(0))
    return CheckResult(
        rule_id=f"RECEIPT_L{line.line_no}",
        status=_status(line.qty <= received),
        expected=f"invoiced quantity {line.qty} <= received",
        observed=f"received {received}",
        citation=rules.cite("FIN-POL-002", "§4" if received == 0 else "§2"),
        exception_category="MISSING_RECEIPT" if received == 0 else "QUANTITY_VARIANCE",
        on_fail=HOLD,
    )


def check_price(
    line: InvoiceLine, po_line: PoLine, po: PurchaseOrder, rules: RulesConfig
) -> CheckResult:
    """The PO line's kind sets the tolerance, so a supplier cannot relabel goods as services."""
    expected = _money(line.qty * po_line.unit_price)
    invoiced = _money(line.qty * line.unit_price)
    variance = invoiced - expected
    limit = rules.tolerances.limit_for(po_line.kind, po_line.line_value)
    freight_blocked = po_line.kind == "freight" and not po.freight_permitted
    return CheckResult(
        rule_id=f"PRICE_L{line.line_no}",
        status=_status(abs(variance) <= limit and not freight_blocked),
        expected=f"|variance| <= {limit}",
        observed="freight not permitted on this PO" if freight_blocked else f"variance {variance}",
        citation=rules.cite("FIN-POL-002", "§2"),
        exception_category="PRICE_VARIANCE",
        on_fail=HOLD,
        calculation=Calculation(
            formula="variance = qty * (invoice_unit_price - po_unit_price); "
            "limit = min(max_abs, max_pct * po_line_value)",
            inputs={
                "qty": str(line.qty),
                "invoice_unit_price": str(line.unit_price),
                "po_unit_price": str(po_line.unit_price),
                "po_line_value": str(po_line.line_value),
                "kind": po_line.kind,
            },
            result=f"variance {variance}, limit {limit}",
        ),
    )


def check_total(case: InvoiceCase, rules: RulesConfig) -> CheckResult:
    lines_total = sum((_money(line.qty * line.unit_price) for line in case.lines), Decimal(0))
    expected = lines_total + case.tax_amount
    return CheckResult(
        rule_id="INVOICE_TOTAL",
        status=_status(expected == case.amount),
        expected=f"gross {expected}",
        observed=f"gross {case.amount}",
        citation=rules.cite("FIN-POL-002", "§1"),
        exception_category="TAX_QUERY",
        on_fail=HOLD,
        calculation=Calculation(
            formula="sum(qty * unit_price) + tax",
            inputs={"lines_total": str(lines_total), "tax": str(case.tax_amount)},
            result=str(expected),
        ),
    )


def fraud_indicators(
    case: InvoiceCase, vendor: VendorRecord | None, rules: RulesConfig
) -> list[str]:
    """FIN-POL-005 §3. Instructions in untrusted text count as indicators (§4)."""
    found = []
    if vendor and (
        case.remit_to_last4 != vendor.bank_last4 or _recent_bank_change(case, vendor, rules)
    ):
        found.append("bank_details_changed")
    text = " ".join([case.notes, *(a.text for a in case.attachments)])
    if _mentions(text, rules.fraud.urgency_terms):
        found.append("urgent_or_secret_language")
    if _mentions(text, rules.fraud.bypass_terms):
        found.append("request_to_bypass_controls")
    return found


def _mentions(text: str, terms: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) for term in terms)


def _recent_bank_change(case: InvoiceCase, vendor: VendorRecord, rules: RulesConfig) -> bool:
    changed = vendor.bank_changed_at
    return (
        changed is not None
        and _days(changed.date(), case.invoice_date) <= rules.authority.recent_bank_change_days
    )


def _days(earlier: date, later: date) -> int:
    return (later - earlier).days


def check_fraud(indicators: list[str], rules: RulesConfig) -> CheckResult:
    return CheckResult(
        rule_id="FRAUD_INDICATORS",
        status=_status(len(indicators) < rules.fraud.escalate_at),
        expected=f"fewer than {rules.fraud.escalate_at} indicators",
        observed=f"{len(indicators)}: {', '.join(indicators) or 'none'}",
        citation=rules.cite("FIN-POL-005", "§3"),
        exception_category="OTHER_CONTROL_RISK",
        on_fail=ESCALATE,
    )


def check_segregation(
    case: InvoiceCase, po: PurchaseOrder | None, rules: RulesConfig
) -> CheckResult:
    threshold = rules.segregation.distinct_roles_above
    if case.amount <= threshold:
        passed, observed = True, f"not required at or below {threshold}"
    elif po is None:
        passed, observed = None, "receipters unknown: purchase order unavailable"
    else:
        receipters = {r.received_by for r in po.receipts}
        passed = case.requested_by not in receipters
        observed = (
            f"requester {case.requested_by}, receipters {', '.join(sorted(receipters)) or 'none'}"
        )
    return CheckResult(
        rule_id="SEGREGATION",
        status=_status(passed),
        expected="requester is not a receipter",
        observed=observed,
        citation=rules.cite("FIN-POL-001", "§4"),
        exception_category="AUTHORITY_GAP",
        on_fail=ESCALATE,  # any conflict goes to Financial Control
    )


def approval_requirement(
    case: InvoiceCase, evidence: Evidence, rules: RulesConfig
) -> ApprovalRequirement:
    """Who may approve (FIN-POL-003 §2-3), and who may not (FIN-POL-001 §4, FIN-POL-003 §1)."""
    role, limit = rules.authority.role_for(case.amount)
    reasons = co_approval_reasons(case, evidence.vendor, rules)
    excluded = [case.requested_by]
    po = evidence.purchase_order
    if case.amount > rules.segregation.distinct_roles_above and po:
        excluded += sorted({r.received_by for r in po.receipts} - {case.requested_by})
    return ApprovalRequirement(
        role=role,
        limit=limit,
        co_approval_role=rules.authority.co_approval_role if reasons else None,
        co_approval_reasons=reasons,
        excluded_approvers=excluded,
        citation=rules.cite("FIN-POL-003", "§2-3"),
    )


def co_approval_reasons(
    case: InvoiceCase, vendor: VendorRecord | None, rules: RulesConfig
) -> list[str]:
    """Higher-risk triggers from FIN-POL-003 §3. Manual payments are not modelled."""
    if vendor is None:
        return ["vendor_record_unavailable"]
    authority = rules.authority
    reasons = []
    if _days(vendor.created_at.date(), case.invoice_date) < authority.new_vendor_days:
        reasons.append("new_vendor")
    if case.remit_to_last4 != vendor.bank_last4 or _recent_bank_change(case, vendor, rules):
        reasons.append("bank_changed")
    if vendor.bank_country != authority.home_country:
        reasons.append("overseas_account")
    if vendor.risk_flags:
        reasons.append("fraud_flag")
    return reasons
