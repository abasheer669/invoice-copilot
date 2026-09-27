"""Runs against the seed data, with a canned knowledge base. Every run is written inside
a transaction that is rolled back, so the tests leave no runs behind."""

import dataclasses
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml

from ap_agent import tools
from ap_agent.db import connect
from ap_agent.embeddings import FakeEmbedder
from ap_agent.erp import PostgresErp, erp_tools
from ap_agent.orchestrator import NoIndexError, Orchestrator
from ap_agent.retrieval import ModelMismatchError
from ap_agent.rules_config import load_rules
from ap_agent.runs import RunStore, StaleRunError
from ap_agent.schemas import InvoiceCase

ROOT = Path(__file__).parents[2]
SPECS = {p.stem: yaml.safe_load(p.read_text()) for p in (ROOT / "data/cases").glob("*.yaml")}


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
def conn(settings):
    with connect("ap_runtime", settings) as conn, conn.transaction(force_rollback=True):
        yield conn


@pytest.fixture
def store(settings, conn):
    return RunStore(settings, connect_fn=lambda: nullcontext(conn))


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(tools, "BACKOFF_S", 0)


def make(settings, store, kb=None, erp=None, **overrides) -> Orchestrator:
    settings = settings.model_copy(update={"tool_timeout_s": 0.2, **overrides})
    rules = load_rules()
    erp = erp or erp_tools(PostgresErp(settings, rules))
    return Orchestrator(settings, store, rules, erp, kb or FakeKB())


def run_ids(conn) -> set[str]:
    return {row["run_id"] for row in conn.execute("select run_id from agent.runs")}


def case(case_id: str) -> InvoiceCase:
    return InvoiceCase.model_validate_json((ROOT / SPECS[case_id]["input"]).read_text())


@pytest.mark.parametrize("case_id", sorted(SPECS))
def test_case_pauses_for_approval_with_the_expected_outcome(settings, store, case_id):
    spec = SPECS[case_id]
    faults = dict([spec["faults"].split(":")]) if "faults" in spec else {}
    run = make(settings, store, faults=faults).start(case(case_id))

    assert run.state == "AWAITING_APPROVAL"
    recommendation = run.result.recommendation
    assert recommendation.outcome == spec["expected_outcome"]
    cited_docs = {chunk_id.split("#")[0] for chunk_id in recommendation.citations}
    assert set(spec["must_cite"]) <= cited_docs
    assert not set(spec["must_not_cite"]) & cited_docs


def test_missing_purchase_order_is_an_unknown_not_a_guess(settings, store):
    run = make(settings, store, faults={"get_purchase_order": "timeout"}).start(case("FIN-004"))
    assert run.result.unknowns[0] == (
        "get_purchase_order {'po_ref': 'PO-9100'} failed: timeout after 3 attempt(s)"
    )
    assert run.result.recommendation.confidence == "low"


def test_every_step_and_tool_call_is_an_audit_event(settings, store):
    run = make(settings, store).start(case("FIN-001"))
    events = store.events(run.run_id)
    assert [e["name"] for e in events if e["event_type"] == "state_change"] == [
        "RECEIVED -> GATHERING",
        "GATHERING -> CHECKING",
        "CHECKING -> RECOMMENDING",
        "RECOMMENDING -> AWAITING_APPROVAL",
    ]
    types = Counter(e["event_type"] for e in events)
    assert types["tool_call"] + types["retrieval"] == run.tool_call_count == 7
    assert types["checks_completed"] == types["approval_requested"] == 1
    assert all(e["ts"] and e["outcome"] for e in events)


def test_a_saved_run_reloads_identically(settings, store):
    run = make(settings, store).start(case("FIN-003"))
    assert store.load(run.run_id) == run


def test_crash_mid_gathering_resumes_without_fetching_evidence_twice(settings, store, conn):
    calls = Counter()
    crash = {"pending": True}

    def counted(tool):
        def call(args):
            calls[tool.name] += 1
            if tool.name == "check_invoice_history" and crash["pending"]:
                crash["pending"] = False
                raise KeyboardInterrupt  # the process dies half-way through gathering
            return tool.call(args)

        return dataclasses.replace(tool, call=call)

    real = erp_tools(PostgresErp(settings, load_rules()))
    erp = {name: counted(tool) for name, tool in real.items()}
    before = run_ids(conn)
    with pytest.raises(KeyboardInterrupt):
        make(settings, store, erp=erp).start(case("FIN-001"))

    (run_id,) = run_ids(conn) - before
    crashed = store.load(run_id)
    assert crashed.state == "GATHERING"
    assert [c.tool for c in crashed.evidence] == ["get_vendor_record"]

    run = make(settings, store, erp=erp).resume(run_id)
    assert run.state == "AWAITING_APPROVAL"
    assert calls == {"get_vendor_record": 1, "check_invoice_history": 2, "get_purchase_order": 1}


def test_a_stale_copy_of_a_run_cannot_overwrite_a_newer_one(settings, store):
    run = make(settings, store).start(case("FIN-001"))
    first, second = store.load(run.run_id), store.load(run.run_id)
    store.save(first)
    with pytest.raises(StaleRunError):
        store.save(second)


def test_an_unexpected_error_fails_the_run_with_its_reason(settings, store):
    kb = FakeKB(error=ModelMismatchError("index built with another model"))
    run = make(settings, store, kb=kb).start(case("FIN-001"))
    assert run.state == "FAILED"
    assert run.failure_reason == "ModelMismatchError: index built with another model"
    assert store.events(run.run_id)[-1]["event_type"] == "run_failed"


def test_the_step_budget_stops_a_run(settings, store):
    run = make(settings, store, max_steps=2).start(case("FIN-001"))
    assert (run.state, run.failure_reason) == ("FAILED", "step budget of 2 exceeded")


def test_no_run_starts_without_a_live_knowledge_base(settings, store, conn):
    before = run_ids(conn)
    with pytest.raises(NoIndexError, match="run `ap ingest` first"):
        make(settings, store, kb=FakeKB(active=None)).start(case("FIN-001"))
    assert run_ids(conn) == before
