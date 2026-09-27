"""Read-only tools over the simulated business systems (the mock_erp schema).

Each tool runs one fixed, parameterised query as ap_reader. PostgresErp is the only code
that knows where the data lives, so a real ERP API can replace it without changing the
tool contracts below.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ap_agent.config import Settings
from ap_agent.db import connect
from ap_agent.tools import NotFoundError, Tool, TransientError

# Duplicate-matching limits from FIN-POL-005 §1.
FUZZY_DAYS = 14
FUZZY_AMOUNT_PCT = Decimal("0.005")

Ref = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# get_vendor_record


class VendorQuery(_Args):
    vendor_id: Ref


class VendorRecord(BaseModel):
    vendor_id: str
    legal_name: str
    status: Literal["ACTIVE", "BLOCKED", "DORMANT", "SANCTIONS_REVIEW", "PENDING_VERIFICATION"]
    bank_last4: Annotated[str, StringConstraints(pattern=r"^\d{4}$")]  # never a full account
    bank_country: str
    bank_changed_at: datetime | None
    risk_flags: list[str]
    updated_at: datetime


# get_purchase_order


class PurchaseOrderQuery(_Args):
    po_ref: Ref


class PoLine(BaseModel):
    line_no: int
    description: str
    kind: Literal["goods", "services", "freight"]
    qty: Decimal
    unit_price: Decimal
    line_value: Decimal


class GoodsReceipt(BaseModel):
    receipt_id: str
    line_no: int
    qty_received: Decimal
    received_at: datetime
    received_by: str


class PurchaseOrder(BaseModel):
    po_ref: str
    vendor_id: str
    currency: str
    approval_status: Literal["APPROVED", "PENDING", "CANCELLED"]
    freight_permitted: bool
    lines: list[PoLine]
    total: Decimal  # sum of line values, before tax
    receipts: list[GoodsReceipt]


# check_invoice_history


class InvoiceHistoryQuery(_Args):
    vendor_id: Ref
    invoice_ref: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    amount: Annotated[Decimal, Field(gt=0, decimal_places=2)]
    currency: Currency
    invoice_date: date


class HistoryMatch(BaseModel):
    record_id: str
    invoice_ref: str
    invoice_date: date
    amount: Decimal
    currency: str
    status: Literal["PAID", "POSTED", "HELD", "REJECTED"]
    match_type: Literal["EXACT", "FUZZY"]


class InvoiceHistory(BaseModel):
    matches: list[HistoryMatch]


class PostgresErp:
    def __init__(self, settings: Settings):
        self.settings = settings

    def get_vendor(self, q: VendorQuery) -> dict:
        with self._reader() as conn:
            row = conn.execute(
                """select vendor_id, legal_name, status, bank_last4, bank_country,
                          bank_changed_at, risk_flags, updated_at
                   from mock_erp.vendors where vendor_id = %s""",
                [q.vendor_id],
            ).fetchone()
        if row is None:
            raise NotFoundError(q.vendor_id)
        return row

    def get_purchase_order(self, q: PurchaseOrderQuery) -> dict:
        with self._reader() as conn:
            po = conn.execute(
                """select po_ref, vendor_id, currency, approval_status, freight_permitted
                   from mock_erp.purchase_orders where po_ref = %s""",
                [q.po_ref],
            ).fetchone()
            if po is None:
                raise NotFoundError(q.po_ref)
            lines = conn.execute(
                """select line_no, description, kind, qty, unit_price,
                          round(qty * unit_price, 2) as line_value
                   from mock_erp.po_lines where po_ref = %s order by line_no""",
                [q.po_ref],
            ).fetchall()
            receipts = conn.execute(
                """select receipt_id, line_no, qty_received, received_at, received_by
                   from mock_erp.goods_receipts where po_ref = %s order by receipt_id""",
                [q.po_ref],
            ).fetchall()
        total = sum((line["line_value"] for line in lines), Decimal("0.00"))
        return {**po, "lines": lines, "total": total, "receipts": receipts}

    def check_invoice_history(self, q: InvoiceHistoryQuery) -> dict:
        """EXACT: same vendor, normalised invoice number, currency and amount.
        FUZZY: same vendor and either the same punctuation-stripped invoice number, or an
        invoice date within FUZZY_DAYS and an amount within FUZZY_AMOUNT_PCT."""
        ref_norm = q.invoice_ref.strip().upper()
        with self._reader() as conn:
            rows = conn.execute(
                """select record_id, invoice_ref, invoice_date, amount, currency, status,
                          case when invoice_ref_norm = %(ref_norm)s and currency = %(currency)s
                                    and amount = %(amount)s
                               then 'EXACT' else 'FUZZY' end as match_type
                   from mock_erp.invoice_history
                   where vendor_id = %(vendor_id)s
                     and (regexp_replace(invoice_ref_norm, '[^A-Z0-9]', '', 'g') = %(ref_key)s
                          or (abs(invoice_date - %(invoice_date)s) <= %(days)s
                              and abs(amount - %(amount)s) < %(amount)s * %(pct)s))
                   order by record_id""",
                {
                    "vendor_id": q.vendor_id,
                    "ref_norm": ref_norm,
                    "ref_key": re.sub(r"[^A-Z0-9]", "", ref_norm),
                    "currency": q.currency,
                    "amount": q.amount,
                    "invoice_date": q.invoice_date,
                    "days": FUZZY_DAYS,
                    "pct": FUZZY_AMOUNT_PCT,
                },
            ).fetchall()
        return {"matches": rows}

    @contextmanager
    def _reader(self) -> Iterator[psycopg.Connection]:
        try:
            with connect("ap_reader", self.settings) as conn:
                yield conn
        except psycopg.OperationalError as e:
            raise TransientError("database unavailable") from e


def erp_tools(erp: PostgresErp) -> dict[str, Tool]:
    tools = [
        Tool(
            "get_vendor_record",
            "Vendor master data: status, bank account last 4 digits, risk flags, last update.",
            VendorQuery,
            VendorRecord,
            erp.get_vendor,
        ),
        Tool(
            "get_purchase_order",
            "Purchase order lines, total before tax, currency, approval status and receipts.",
            PurchaseOrderQuery,
            PurchaseOrder,
            erp.get_purchase_order,
        ),
        Tool(
            "check_invoice_history",
            "Past invoices from the same vendor that exactly or probably match this one.",
            InvoiceHistoryQuery,
            InvoiceHistory,
            erp.check_invoice_history,
        ),
    ]
    return {tool.name: tool for tool in tools}
