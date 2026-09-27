import pytest

from ap_agent import tools
from ap_agent.erp import PostgresErp, erp_tools
from ap_agent.rules_config import load_rules
from ap_agent.tools import invoke


@pytest.fixture
def call(settings):
    registry = erp_tools(PostgresErp(settings, load_rules()))
    return lambda name, **args: invoke(registry[name], args, settings)


def test_vendor_record(call):
    result = call("get_vendor_record", vendor_id="V-1001")
    assert result.ok
    assert result.data["status"] == "ACTIVE"
    assert result.data["bank_last4"] == "4471"


def test_unknown_vendor_is_not_found(call):
    result = call("get_vendor_record", vendor_id="V-9999")
    assert (result.ok, result.error) == (False, "not_found")


def test_purchase_order_with_lines_total_and_receipt(call):
    result = call("get_purchase_order", po_ref="PO-7788")
    assert result.ok
    po = result.data
    assert [(line["qty"], line["line_value"], line["tolerance"]) for line in po["lines"]] == [
        ("100.00", "10000.00", "50.00")  # goods: min(50, 1% of 10,000)
    ]
    assert po["total"] == "10000.00"
    assert [(r["receipt_id"], r["qty_received"]) for r in po["receipts"]] == [("GR-3341", "100.00")]


def test_purchase_order_without_receipt(call):
    result = call("get_purchase_order", po_ref="PO-9100")
    assert result.ok
    assert result.data["receipts"] == []
    assert result.data["lines"][0]["tolerance"] == "80.00"  # services: min(100, 2% of 4,000)


@pytest.mark.parametrize(
    "vendor_id, invoice_ref, amount, invoice_date, expected",
    [
        ("V-1001", "INV-3310", "5500.00", "2026-08-15", [("IH-0042", "EXACT")]),
        ("V-1001", " inv-3310 ", "5500.00", "2026-08-15", [("IH-0042", "EXACT")]),
        ("V-1001", "INV 3310", "5500.00", "2026-08-15", [("IH-0042", "FUZZY")]),
        ("V-1001", "INV-3310", "5600.00", "2026-08-15", [("IH-0042", "FUZZY")]),
        ("V-1001", "INV-9999", "5510.00", "2026-08-20", [("IH-0042", "FUZZY")]),
        ("V-1001", "INV-5521", "11000.00", "2026-09-20", []),
        ("V-3003", "MFS-2209", "4400.00", "2026-09-20", []),
    ],
    ids=[
        "exact",
        "exact-after-normalising",
        "punctuation-differs",
        "same-number-new-amount",
        "near-date-and-amount",
        "no-history",
        "monthly-repeat-is-not-a-duplicate",
    ],
)
def test_invoice_history_matching(call, vendor_id, invoice_ref, amount, invoice_date, expected):
    result = call(
        "check_invoice_history",
        vendor_id=vendor_id,
        invoice_ref=invoice_ref,
        amount=amount,
        currency="AUD",
        invoice_date=invoice_date,
    )
    assert result.ok
    assert [(m["record_id"], m["match_type"]) for m in result.data["matches"]] == expected


def test_purchase_order_timeout_fault_gives_up_after_retries(settings, monkeypatch):
    """FIN-004: the purchase-order lookup times out on every attempt."""
    monkeypatch.setattr(tools, "BACKOFF_S", 0)
    faulty = settings.model_copy(
        update={"faults": {"get_purchase_order": "timeout"}, "tool_timeout_s": 0.1}
    )
    registry = erp_tools(PostgresErp(faulty, load_rules()))
    result = invoke(registry["get_purchase_order"], {"po_ref": "PO-9100"}, faulty)
    assert (result.ok, result.error, result.attempts) == (False, "timeout", 3)
