import pytest
from pydantic import ValidationError

from ap_agent.config import Settings
from ap_agent.erp import (
    InvoiceHistoryQuery,
    PostgresErp,
    PurchaseOrderQuery,
    VendorQuery,
    VendorRecord,
    erp_tools,
)
from ap_agent.tools import Tool, invoke

HISTORY_ARGS = {
    "vendor_id": "V-1001",
    "invoice_ref": "INV-3310",
    "amount": "5500.00",
    "currency": "AUD",
    "invoice_date": "2026-08-15",
}


def test_registry_holds_exactly_the_three_read_only_tools():
    tools = erp_tools(PostgresErp(Settings(_env_file=None)))
    assert set(tools) == {"get_vendor_record", "get_purchase_order", "check_invoice_history"}


@pytest.mark.parametrize(
    "model, raw_args",
    [
        (VendorQuery, {"vendor_id": "V-1001'; drop table vendors; --"}),
        (VendorQuery, {"vendor_id": ""}),
        (PurchaseOrderQuery, {"po_ref": "PO 7788"}),
        (InvoiceHistoryQuery, {**HISTORY_ARGS, "amount": "-5.00"}),
        (InvoiceHistoryQuery, {**HISTORY_ARGS, "amount": "5500.001"}),
        (InvoiceHistoryQuery, {**HISTORY_ARGS, "currency": "aud"}),
        (InvoiceHistoryQuery, {**HISTORY_ARGS, "invoice_date": "next week"}),
    ],
)
def test_input_schemas_reject_malformed_arguments(model, raw_args):
    with pytest.raises(ValidationError):
        model.model_validate(raw_args)


def test_vendor_record_with_a_full_bank_account_is_rejected():
    leaky = {
        "vendor_id": "V-1001",
        "legal_name": "Northwind Office Furniture Pty Ltd",
        "status": "ACTIVE",
        "bank_last4": "062000123444471",
        "bank_country": "AU",
        "bank_changed_at": None,
        "risk_flags": [],
        "updated_at": "2026-06-01T09:00:00+10:00",
    }
    tool = Tool("get_vendor_record", "", VendorQuery, VendorRecord, lambda args: leaky)
    result = invoke(tool, {"vendor_id": "V-1001"}, Settings(_env_file=None))
    assert (result.ok, result.error, result.data) == (False, "invalid_output", None)
