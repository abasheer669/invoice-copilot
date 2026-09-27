"""Run state: the state machine, persistence with optimistic locking, and audit events.

A run is saved after every state change and every tool call, so `ap resume` continues
from the last checkpoint with the evidence already gathered.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from ap_agent.config import Settings
from ap_agent.db import connect
from ap_agent.result import RunResult
from ap_agent.rules import Assessment
from ap_agent.schemas import InvoiceCase
from ap_agent.tools import ToolCall

State = Literal[
    "RECEIVED",
    "GATHERING",
    "CHECKING",
    "RECOMMENDING",
    "AWAITING_APPROVAL",
    "SUBMITTING",
    "COMPLETED",
    "CLOSED",
    "FAILED",
]

TRANSITIONS: dict[State, set[State]] = {
    "RECEIVED": {"GATHERING", "FAILED"},
    "GATHERING": {"CHECKING", "FAILED"},
    "CHECKING": {"RECOMMENDING", "FAILED"},
    "RECOMMENDING": {"AWAITING_APPROVAL", "FAILED"},
    "AWAITING_APPROVAL": {"SUBMITTING", "CLOSED"},  # only a stored human decision moves it on
    "SUBMITTING": {"COMPLETED", "FAILED"},
    "COMPLETED": set(),
    "CLOSED": set(),
    "FAILED": set(),
}


class IllegalTransition(Exception):
    pass


class RunNotFound(Exception):
    pass


class StaleRunError(Exception):
    """Another process saved this run first; reload it before continuing."""


class Run(BaseModel):
    run_id: str
    case: InvoiceCase
    state: State = "RECEIVED"
    evidence: list[ToolCall] = []
    assessment: Assessment | None = None
    result: RunResult | None = None
    failure_reason: str | None = None
    index_version: str | None = None
    rules_version: str | None = None
    embed_model: str | None = None
    llm_model: str | None = None
    step_count: int = 0
    tool_call_count: int = 0
    version: int = 0

    def move_to(self, state: State) -> State:
        """Change state if the transition is allowed; returns the previous state."""
        if state not in TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.run_id}: {self.state} -> {state} is not allowed")
        previous, self.state = self.state, state
        return previous


_INSERT = """
insert into agent.runs (run_id, case_id, case_input, state, evidence, checks, result,
  failure_reason, index_version, rules_version, embed_model, llm_model, step_count, tool_call_count)
values (%(run_id)s, %(case_id)s, %(case)s, %(state)s, %(evidence)s, %(assessment)s, %(result)s,
  %(failure_reason)s, %(index_version)s, %(rules_version)s, %(embed_model)s, %(llm_model)s,
  %(step_count)s, %(tool_call_count)s)
"""

_UPDATE = """
update agent.runs set state = %(state)s, evidence = %(evidence)s, checks = %(assessment)s,
  result = %(result)s, failure_reason = %(failure_reason)s, index_version = %(index_version)s,
  rules_version = %(rules_version)s, embed_model = %(embed_model)s, llm_model = %(llm_model)s,
  step_count = %(step_count)s, tool_call_count = %(tool_call_count)s,
  version = version + 1, updated_at = now()
where run_id = %(run_id)s and version = %(version)s
"""


class RunStore:
    def __init__(
        self,
        settings: Settings,
        connect_fn: Callable[[], AbstractContextManager[psycopg.Connection]] | None = None,
    ):
        self._connect = connect_fn or (lambda: connect("ap_runtime", settings))

    def create(self, run: Run) -> None:
        with self._connect() as conn:
            conn.execute(_INSERT, self._params(run))

    def load(self, run_id: str) -> Run:
        with self._connect() as conn:
            row = conn.execute("select * from agent.runs where run_id = %s", [run_id]).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return Run(
            **{k: v for k, v in row.items() if k in Run.model_fields},
            case=row["case_input"],
            assessment=row["checks"],
        )

    def save(self, run: Run) -> None:
        """Write the run only if nobody else has since this process last loaded or saved it."""
        with self._connect() as conn:
            if conn.execute(_UPDATE, self._params(run)).rowcount != 1:
                raise StaleRunError(f"{run.run_id} changed since version {run.version}")
        run.version += 1

    def event(
        self,
        run_id: str,
        event_type: str,
        name: str | None = None,
        outcome: str | None = None,
        duration_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """insert into agent.events
                     (run_id, event_type, name, outcome, duration_ms, payload)
                   values (%s, %s, %s, %s, %s, %s)""",
                [run_id, event_type, name, outcome, duration_ms, Jsonb(payload or {})],
            )

    def events(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            return conn.execute(
                """select ts, event_type, name, outcome, duration_ms, payload
                   from agent.events where run_id = %s order by event_id""",
                [run_id],
            ).fetchall()

    @staticmethod
    def _params(run: Run) -> dict[str, Any]:
        data = run.model_dump(mode="json")
        for field in ("case", "evidence", "assessment", "result"):
            data[field] = Jsonb(data[field])
        return data | {"case_id": run.case.case_id}
