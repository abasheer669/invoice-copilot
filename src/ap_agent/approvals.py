"""Human decisions: who may decide a run, and replay-safe storage of each decision.

Each approval is stored with its callback id, which is unique, so a repeated callback
finds the stored approval instead of creating a second decision.
"""

import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, StringConstraints

from ap_agent.config import Settings
from ap_agent.db import connect
from ap_agent.rules_config import RulesConfig
from ap_agent.runs import Run

Decision = Literal["APPROVE", "REJECT"]
Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")]


class Callback(BaseModel):
    """What an approver sends."""

    model_config = ConfigDict(extra="forbid")

    run_id: Name
    callback_id: Name
    approver: Name
    role: Name
    decision: Decision


class Approval(BaseModel):
    approval_id: str
    run_id: str
    callback_id: str
    approver: str
    role: str
    decision: Decision

    def answers(self, callback: Callback) -> bool:
        """Whether a callback with this id is a true repeat, not a different decision."""
        mine = (self.run_id, self.approver, self.role, self.decision)
        return mine == (callback.run_id, callback.approver, callback.role, callback.decision)


class ApprovalDenied(Exception):
    pass


@dataclass(frozen=True)
class Need:
    label: str
    roles: frozenset[str]


def approvals_needed(run: Run, rules: RulesConfig) -> list[Need]:
    """The approvals a run's outcome needs before anything is recorded (FIN-POL-003 §2-3)."""
    outcome = run.result.recommendation.outcome
    requirement = run.result.approval
    co_role = rules.authority.co_approval_role
    if outcome == "APPROVE_FOR_POSTING":
        amount = run.case.amount
        covering = frozenset(
            role
            for role, limit in rules.authority.limits.items()
            if limit is None or amount <= limit
        )
        needs = [Need(f"{requirement.role} or above", covering)]
        if requirement.co_approval_role:
            needs.append(Need(co_role, frozenset({co_role})))
        return needs
    if outcome == "ESCALATE_CONTROL_REVIEW":
        return [Need(co_role, frozenset({co_role}))]
    return [Need("any approver", frozenset(rules.authority.limits) | {co_role})]


def outstanding(needs: list[Need], approvals: list[Approval]) -> list[Need]:
    remaining = list(needs)
    for approval in approvals:
        if approval.decision == "APPROVE":
            met = next((n for n in remaining if approval.role in n.roles), None)
            if met:
                remaining.remove(met)
    return remaining


def approval_problem(
    run: Run,
    callback: Callback,
    prior: list[Approval],
    rules: RulesConfig,
    active_index: str | None,
) -> str | None:
    """Why this approver may not make this decision now, or None if they may."""
    known_roles = set(rules.authority.limits) | {rules.authority.co_approval_role}
    if callback.role not in known_roles:
        return f"unknown role {callback.role}"
    if callback.approver in run.result.approval.excluded_approvers:
        return f"{callback.approver} requested or received this purchase and may not decide it"
    if callback.approver in {a.approver for a in prior}:
        return f"{callback.approver} has already decided this run; the next must be someone else"
    if (run.rules_version, run.index_version) != (rules.version, active_index):
        return "the rules or policy index changed after this recommendation; start a new run"
    if callback.decision == "APPROVE":
        remaining = outstanding(approvals_needed(run, rules), prior)
        if not any(callback.role in need.roles for need in remaining):
            still = ", ".join(need.label for need in remaining)
            return f"{callback.role} cannot give the approval still needed: {still}"
    return None


class DecisionStore:
    """Approvals and recorded decisions, written as ap_writer."""

    def __init__(
        self,
        settings: Settings,
        connect_fn: Callable[[], AbstractContextManager[psycopg.Connection]] | None = None,
    ):
        self._connect = connect_fn or (lambda: connect("ap_writer", settings))

    def by_callback(self, callback_id: str) -> Approval | None:
        with self._connect() as conn:
            row = conn.execute(
                "select response from agent.approvals where callback_id = %s", [callback_id]
            ).fetchone()
        return Approval.model_validate(row["response"]) if row else None

    def for_run(self, run_id: str) -> list[Approval]:
        with self._connect() as conn:
            rows = conn.execute(
                "select response from agent.approvals where run_id = %s order by created_at",
                [run_id],
            ).fetchall()
        return [Approval.model_validate(row["response"]) for row in rows]

    def record(self, callback: Callback) -> Approval:
        """Store a new approval. A callback id that is already stored raises UniqueViolation."""
        approval = Approval(approval_id=f"apr_{secrets.token_hex(4)}", **callback.model_dump())
        with self._connect() as conn, conn.transaction():
            conn.execute(
                """insert into agent.approvals
                     (approval_id, run_id, callback_id, approver, role, decision, response)
                   values (%s, %s, %s, %s, %s, %s, %s)""",
                [
                    approval.approval_id,
                    approval.run_id,
                    approval.callback_id,
                    approval.approver,
                    approval.role,
                    approval.decision,
                    Jsonb(approval.model_dump()),
                ],
            )
        return approval

    def decision(self, idempotency_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "select response from agent.decisions where idempotency_key = %s",
                [idempotency_key],
            ).fetchone()
        return row["response"] if row else None

    def record_decision(self, idempotency_key: str, run_id: str, receipt: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """insert into agent.decisions
                     (idempotency_key, run_id, outcome, decision_ref, response)
                   values (%s, %s, %s, %s, %s)
                   on conflict (idempotency_key) do nothing""",
                [
                    idempotency_key,
                    run_id,
                    receipt["outcome"],
                    receipt["decision_ref"],
                    Jsonb(receipt),
                ],
            )
