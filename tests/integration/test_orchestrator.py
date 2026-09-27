"""Runs against the seed data with a canned knowledge base (see conftest.py)."""

import dataclasses
from collections import Counter
from pathlib import Path

import pytest
import yaml

from ap_agent.erp import PostgresErp, erp_tools
from ap_agent.orchestrator import NoIndexError
from ap_agent.retrieval import ModelMismatchError
from ap_agent.rules_config import load_rules
from ap_agent.runs import StaleRunError
from ap_agent.schemas import InvoiceCase

ROOT = Path(__file__).parents[2]
SPECS = {p.stem: yaml.safe_load(p.read_text()) for p in (ROOT / "data/cases").glob("*.yaml")}


def case(case_id: str) -> InvoiceCase:
    return InvoiceCase.model_validate_json((ROOT / SPECS[case_id]["input"]).read_text())


def faults(case_id: str) -> dict:
    spec = SPECS[case_id]
    return dict([spec["faults"].split(":")]) if "faults" in spec else {}


@pytest.fixture
def run_ids(query):
    return lambda: {row["run_id"] for row in query("ap_runtime", "select run_id from agent.runs")}


@pytest.mark.parametrize("case_id", sorted(SPECS))
def test_case_pauses_for_approval_with_the_expected_outcome(make_orchestrator, case_id):
    spec = SPECS[case_id]
    run = make_orchestrator(faults=faults(case_id)).start(case(case_id))

    assert run.state == "AWAITING_APPROVAL"
    recommendation = run.result.recommendation
    assert recommendation.outcome == spec["expected_outcome"]
    cited_docs = {chunk_id.split("#")[0] for chunk_id in recommendation.citations}
    assert set(spec["must_cite"]) <= cited_docs
    assert not set(spec["must_not_cite"]) & cited_docs


def test_missing_purchase_order_is_an_unknown_not_a_guess(make_orchestrator):
    run = make_orchestrator(faults=faults("FIN-004")).start(case("FIN-004"))
    assert run.result.unknowns[0] == (
        "get_purchase_order {'po_ref': 'PO-9100'} failed: timeout after 3 attempt(s)"
    )
    assert run.result.recommendation.confidence == "low"


def test_every_step_and_tool_call_is_an_audit_event(make_orchestrator, store):
    run = make_orchestrator().start(case("FIN-001"))
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


def test_a_saved_run_reloads_identically(make_orchestrator, store):
    run = make_orchestrator().start(case("FIN-003"))
    assert store.load(run.run_id) == run


def test_crash_mid_gathering_resumes_without_fetching_evidence_twice(
    settings, make_orchestrator, store, run_ids
):
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
    before = run_ids()
    with pytest.raises(KeyboardInterrupt):
        make_orchestrator(erp=erp).start(case("FIN-001"))

    (run_id,) = run_ids() - before
    crashed = store.load(run_id)
    assert crashed.state == "GATHERING"
    assert [c.tool for c in crashed.evidence] == ["get_vendor_record"]

    run = make_orchestrator(erp=erp).resume(run_id)
    assert run.state == "AWAITING_APPROVAL"
    assert calls == {"get_vendor_record": 1, "check_invoice_history": 2, "get_purchase_order": 1}


def test_a_stale_copy_of_a_run_cannot_overwrite_a_newer_one(make_orchestrator, store):
    run = make_orchestrator().start(case("FIN-001"))
    first, second = store.load(run.run_id), store.load(run.run_id)
    store.save(first)
    with pytest.raises(StaleRunError):
        store.save(second)


def test_an_unexpected_error_fails_the_run_with_its_reason(make_orchestrator, store, fake_kb):
    kb = fake_kb(error=ModelMismatchError("index built with another model"))
    run = make_orchestrator(kb=kb).start(case("FIN-001"))
    assert run.state == "FAILED"
    assert run.failure_reason == "ModelMismatchError: index built with another model"
    assert store.events(run.run_id)[-1]["event_type"] == "run_failed"


def test_the_step_budget_stops_a_run(make_orchestrator):
    run = make_orchestrator(max_steps=2).start(case("FIN-001"))
    assert (run.state, run.failure_reason) == ("FAILED", "step budget of 2 exceeded")


def test_no_run_starts_without_a_live_knowledge_base(make_orchestrator, fake_kb, run_ids):
    before = run_ids()
    with pytest.raises(NoIndexError, match="run `ap ingest` first"):
        make_orchestrator(kb=fake_kb(active=None)).start(case("FIN-001"))
    assert run_ids() == before
