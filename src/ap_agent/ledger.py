"""submit_finance_decision: the simulated finance API that records a decided outcome.

It is the only write tool. The model is never given it; the orchestrator calls it once
the required approvals are stored. It refuses a run with no stored approval, and its
idempotency key makes a retried or repeated submit return the original record. It can
record a posting, hold, rejection or escalation, but it cannot move money.
"""

import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, StringConstraints

from ap_agent.config import Settings
from ap_agent.db import connect
from ap_agent.rules import Outcome
from ap_agent.schemas import Currency, Money
from ap_agent.tools import Tool

RunId = Annotated[str, StringConstraints(pattern=r"^run_[0-9a-f]{8}$")]


class NotApproved(Exception):
    pass


class SubmitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: Annotated[str, StringConstraints(pattern=r"^run_[0-9a-f]{8}:[A-Z_]+$")]
    run_id: RunId
    outcome: Outcome
    amount: Money
    currency: Currency
    approval_id: Annotated[str, StringConstraints(pattern=r"^apr_[0-9a-f]{8}$")]


class SubmitReceipt(BaseModel):
    decision_ref: str
    outcome: Outcome
    status: Literal["RECORDED"]
    replayed: bool  # true when this key was already recorded; nothing new was written


class SimLedger:
    def __init__(
        self,
        settings: Settings,
        connect_fn: Callable[[], AbstractContextManager[psycopg.Connection]] | None = None,
    ):
        self._connect = connect_fn or (lambda: connect("ap_writer", settings))

    def submit(self, args: SubmitArgs) -> dict:
        with self._connect() as conn:
            approved = conn.execute(
                """select 1 from agent.approvals
                   where approval_id = %s and run_id = %s and decision = 'APPROVE'""",
                [args.approval_id, args.run_id],
            ).fetchone()
            if approved is None:
                raise NotApproved(f"no stored approval {args.approval_id} for {args.run_id}")
            row = conn.execute(
                """insert into mock_erp.sim_ledger
                     (idempotency_key, decision_ref, run_id, outcome, amount, currency, approval_id)
                   values (%s, %s, %s, %s, %s, %s, %s)
                   on conflict (idempotency_key) do nothing
                   returning decision_ref, outcome""",
                [
                    args.idempotency_key,
                    f"DEC-{secrets.token_hex(4).upper()}",
                    args.run_id,
                    args.outcome,
                    args.amount,
                    args.currency,
                    args.approval_id,
                ],
            ).fetchone()
            replayed = row is None
            if replayed:
                row = conn.execute(
                    """select decision_ref, outcome from mock_erp.sim_ledger
                       where idempotency_key = %s""",
                    [args.idempotency_key],
                ).fetchone()
        return {**row, "status": "RECORDED", "replayed": replayed}


def submit_tool(ledger: SimLedger) -> Tool:
    return Tool(
        "submit_finance_decision",
        "Record an approved posting, hold, rejection or escalation. Never offered to the model.",
        SubmitArgs,
        SubmitReceipt,
        ledger.submit,
    )
