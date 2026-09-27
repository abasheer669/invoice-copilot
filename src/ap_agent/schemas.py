"""The invoice case a run starts from, plus field types shared with the tools.

Invoice fields arrive pre-extracted. Notes and attachment text come from the supplier
or requester and are untrusted: they are evidence, never instructions.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ap_agent.rules_config import LineKind

Ref = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Money = Annotated[Decimal, Field(ge=0, decimal_places=2)]


class InvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int = Field(gt=0)  # matches the purchase-order line number
    description: str
    qty: Annotated[Decimal, Field(gt=0)]
    unit_price: Money
    kind: LineKind


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    text: str


class InvoiceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: Ref
    invoice_ref: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    vendor_id: Ref
    po_ref: Ref | None = None
    invoice_date: date
    amount: Money  # gross, including tax
    tax_amount: Money
    currency: Currency
    lines: list[InvoiceLine] = Field(min_length=1)
    remit_to_last4: Annotated[str, StringConstraints(pattern=r"^\d{4}$")]
    requested_by: str
    notes: str = ""
    attachments: list[Attachment] = []
