from contextlib import contextmanager

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from ap_agent import tools
from ap_agent.approvals import DecisionStore
from ap_agent.config import Settings
from ap_agent.embeddings import FakeEmbedder
from ap_agent.erp import PostgresErp, erp_tools
from ap_agent.ledger import SimLedger, submit_tool
from ap_agent.llm import EchoLLM
from ap_agent.orchestrator import Orchestrator
from ap_agent.rules_config import load_rules
from ap_agent.runs import RunStore


@pytest.fixture(scope="session")
def settings():
    s = Settings()
    try:
        psycopg.connect(s.database_url.get_secret_value(), connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("database not running; start it with `docker compose up -d --wait`")
    return s


@pytest.fixture
def conn(settings):
    """One connection whose transaction is rolled back after the test, so tests leave no
    rows behind. Each store switches to its own role on it."""
    url = settings.database_url.get_secret_value()
    with psycopg.connect(url, row_factory=dict_row) as conn, conn.transaction(force_rollback=True):
        yield conn


def as_role(conn, role):
    @contextmanager
    def use():
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        yield conn

    return use


@pytest.fixture
def query(conn):
    def run(role, statement, params=()):
        with as_role(conn, role)() as c:
            return c.execute(statement, params).fetchall()

    return run


@pytest.fixture
def store(settings, conn):
    return RunStore(settings, connect_fn=as_role(conn, "ap_runtime"))


@pytest.fixture
def decisions(settings, conn):
    return DecisionStore(settings, connect_fn=as_role(conn, "ap_writer"))


@pytest.fixture
def ledger(settings, conn):
    return SimLedger(settings, connect_fn=as_role(conn, "ap_writer"))


def chunk(doc: str, slug: str, section: int, score: float) -> dict:
    return {
        "chunk_id": f"{doc}#{slug}",
        "doc_id": doc,
        "doc_type": "policy",
        "status": "current",
        "version": "1",
        "title": doc,
        "section": f"§{section}",
        "citation": f"{doc} v1 §{section}",
        "score": score,
        "text": "...",
    }


POLICY = [
    chunk("FIN-POL-001", "segregation-of-duties", 4, 0.70),
    chunk("FIN-POL-002", "tolerances", 2, 0.80),
    chunk("FIN-POL-002", "missing-receipt", 4, 0.75),
    chunk("FIN-POL-003", "standard-operating-expenditure", 2, 0.78),
    chunk("FIN-POL-004", "bank-account-changes", 2, 0.77),
    chunk("FIN-POL-005", "duplicate-detection", 1, 0.76),
    chunk("FIN-POL-005", "fraud-indicators", 3, 0.74),
    chunk("FIN-POL-009", "currency-agreement", 1, 0.72),
]


class FakeKB:
    """A knowledge base that returns the same current-policy chunks for every query."""

    embedder = FakeEmbedder()

    def __init__(self, active="kb-test", error: Exception | None = None):
        self.active, self.error = active, error

    def active_version(self):
        return self.active

    def retrieve(self, q, index_version):
        if self.error:
            raise self.error
        return {"index_version": index_version, "policy": POLICY, "other_evidence": []}


@pytest.fixture
def fake_kb():
    return FakeKB


@pytest.fixture
def make_orchestrator(settings, store, decisions, ledger, monkeypatch):
    monkeypatch.setattr(tools, "BACKOFF_S", 0)

    def make(kb=None, erp=None, submit=None, rules=None, llm=None, **overrides) -> Orchestrator:
        run_settings = settings.model_copy(update={"tool_timeout_s": 0.2, **overrides})
        rules = rules or load_rules()
        return Orchestrator(
            run_settings,
            store,
            rules,
            erp or erp_tools(PostgresErp(run_settings, rules)),
            kb or FakeKB(),
            decisions,
            submit or submit_tool(ledger),
            llm or EchoLLM(),
        )

    return make
