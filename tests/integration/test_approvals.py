"""Approval gate, replay-safe callbacks and exactly-once submission (see conftest.py)."""

import dataclasses
from pathlib import Path

import pytest
import yaml
from psycopg.errors import UniqueViolation

from ap_agent.approvals import ApprovalDenied, Callback
from ap_agent.erp import PostgresErp, erp_tools
from ap_agent.ledger import NotApproved, SubmitArgs, submit_tool
from ap_agent.rules_config import load_rules
from ap_agent.schemas import InvoiceCase

ROOT = Path(__file__).parents[2]
SPECS = {p.stem: yaml.safe_load(p.read_text()) for p in (ROOT / "data/cases").glob("*.yaml")}


def case(case_id: str) -> InvoiceCase:
    return InvoiceCase.model_validate_json((ROOT / SPECS[case_id]["input"]).read_text())


def callback(run, approver="j.smith", role="DEPARTMENT_DIRECTOR", decision="APPROVE", cid="cb"):
    return Callback(
        run_id=run.run_id,
        callback_id=f"{cid}-{run.run_id}",
        approver=approver,
        role=role,
        decision=decision,
    )


@pytest.fixture
def ledger_rows(query):
    return lambda run_id: query(
        "ap_writer",
        "select outcome, decision_ref from mock_erp.sim_ledger where run_id = %s",
        [run_id],
    )


@pytest.mark.parametrize("case_id", sorted(SPECS))
def test_each_case_is_recorded_once_and_only_fin_001_and_005_pay(
    make_orchestrator, ledger_rows, case_id
):
    spec = SPECS[case_id]
    faults = dict([spec["faults"].split(":")]) if "faults" in spec else {}
    orchestrator = make_orchestrator(faults=faults)
    run = orchestrator.start(case(case_id))
    approval = spec["approval"]
    cb = callback(run, approval["approver"], approval["role"], cid=approval["callback_id"])

    responses = [orchestrator.decide(cb) for _ in range(spec.get("callback_deliveries", 1))]

    assert {r.state for r in responses} == {"COMPLETED"}
    assert len({(r.approval_id, r.decision_ref) for r in responses}) == 1  # a stable answer
    assert [r.replayed for r in responses] == [False] + [True] * (len(responses) - 1)
    rows = ledger_rows(run.run_id)
    assert len(rows) == spec["decisions"]
    assert sum(row["outcome"] == "APPROVE_FOR_POSTING" for row in rows) == spec["payment_submits"]


@pytest.mark.parametrize(
    "approver, role, reason",
    [
        ("a.nguyen", "DEPARTMENT_DIRECTOR", "requested or received this purchase"),
        ("p.lee", "COST_CENTRE_MANAGER", "cannot give the approval still needed"),
        ("x.nobody", "INTERN", "unknown role INTERN"),
    ],
)
def test_ineligible_approvers_are_refused_and_nothing_is_recorded(
    make_orchestrator, store, ledger_rows, approver, role, reason
):
    orchestrator = make_orchestrator()
    run = orchestrator.start(case("FIN-001"))
    with pytest.raises(ApprovalDenied, match=reason):
        orchestrator.decide(callback(run, approver, role))
    assert store.load(run.run_id).state == "AWAITING_APPROVAL"
    assert ledger_rows(run.run_id) == []
    assert store.events(run.run_id)[-1]["event_type"] == "approval_denied"


def test_a_higher_risk_payment_needs_financial_control_as_a_second_approver(
    settings, make_orchestrator, ledger_rows
):
    vendor_tool = erp_tools(PostgresErp(settings, load_rules()))["get_vendor_record"]
    flagged = dataclasses.replace(
        vendor_tool, call=lambda args: vendor_tool.call(args) | {"risk_flags": ["watchlist"]}
    )
    erp = erp_tools(PostgresErp(settings, load_rules())) | {"get_vendor_record": flagged}
    orchestrator = make_orchestrator(erp=erp)
    run = orchestrator.start(case("FIN-001"))

    first = orchestrator.decide(callback(run, cid="cb-1"))
    assert first.state == "AWAITING_APPROVAL"
    assert first.next_action == "Awaiting FINANCIAL_CONTROL approval as well"
    with pytest.raises(ApprovalDenied, match="already decided this run"):
        orchestrator.decide(callback(run, role="FINANCIAL_CONTROL", cid="cb-2"))
    assert ledger_rows(run.run_id) == []

    second = orchestrator.decide(callback(run, "f.control", "FINANCIAL_CONTROL", cid="cb-3"))
    assert second.state == "COMPLETED"
    assert len(ledger_rows(run.run_id)) == 1


def test_escalations_are_confirmed_by_financial_control_only(make_orchestrator):
    orchestrator = make_orchestrator()
    run = orchestrator.start(case("FIN-003"))
    with pytest.raises(ApprovalDenied, match="still needed: FINANCIAL_CONTROL"):
        orchestrator.decide(callback(run))
    assert orchestrator.decide(callback(run, "f.control", "FINANCIAL_CONTROL")).decision_ref


def test_rejecting_closes_the_run_without_recording_anything(make_orchestrator, ledger_rows):
    orchestrator = make_orchestrator()
    run = orchestrator.start(case("FIN-001"))
    response = orchestrator.decide(callback(run, decision="REJECT"))
    assert (response.state, response.decision_ref) == ("CLOSED", None)
    assert ledger_rows(run.run_id) == []
    with pytest.raises(ApprovalDenied, match="is CLOSED, not awaiting approval"):
        orchestrator.decide(callback(run, "k.wu", "CFO", cid="cb-later"))


def test_a_callback_id_cannot_be_reused_for_a_different_decision(make_orchestrator):
    orchestrator = make_orchestrator()
    run = orchestrator.start(case("FIN-001"))
    orchestrator.decide(callback(run))
    with pytest.raises(ApprovalDenied, match="used for another decision"):
        orchestrator.decide(callback(run, "k.wu", "CFO"))


def test_approval_is_refused_when_the_rules_changed_after_the_recommendation(make_orchestrator):
    run = make_orchestrator().start(case("FIN-001"))
    newer = load_rules().model_copy(update={"version": "rules-2"})
    with pytest.raises(ApprovalDenied, match="rules or policy index changed"):
        make_orchestrator(rules=newer).decide(callback(run))


def test_two_deliveries_racing_still_record_one_decision(
    make_orchestrator, decisions, ledger_rows, monkeypatch
):
    orchestrator = make_orchestrator()
    run = orchestrator.start(case("FIN-001"))
    record = decisions.record

    def lose_the_race(cb):
        record(cb)  # the other delivery stores the approval first
        raise UniqueViolation("duplicate key value violates unique constraint")

    monkeypatch.setattr(decisions, "record", lose_the_race)
    response = orchestrator.decide(callback(run))
    assert (response.state, response.replayed) == ("COMPLETED", True)
    assert len(ledger_rows(run.run_id)) == 1


def test_crash_after_the_api_call_is_recovered_by_its_idempotency_key(
    make_orchestrator, ledger, ledger_rows, store
):
    tool = submit_tool(ledger)
    crash = {"pending": True}

    def submit_then_die(args):
        receipt = tool.call(args)
        if crash["pending"]:
            crash["pending"] = False
            raise KeyboardInterrupt  # the process dies before recording the receipt
        return receipt

    orchestrator = make_orchestrator(submit=dataclasses.replace(tool, call=submit_then_die))
    run = orchestrator.start(case("FIN-001"))
    with pytest.raises(KeyboardInterrupt):
        orchestrator.decide(callback(run))
    assert store.load(run.run_id).state == "SUBMITTING"

    resumed = orchestrator.resume(run.run_id)
    assert resumed.state == "COMPLETED"
    assert resumed.result.actions_taken[0]["replayed"] is True
    assert len(ledger_rows(run.run_id)) == 1


def test_a_resumed_run_does_not_call_the_api_again_once_the_decision_is_recorded(
    make_orchestrator, ledger, decisions, store, monkeypatch
):
    tool = submit_tool(ledger)
    calls = []
    counted = dataclasses.replace(tool, call=lambda args: calls.append(1) or tool.call(args))
    record_decision = decisions.record_decision
    crash = {"pending": True}

    def record_then_die(*args):
        record_decision(*args)
        if crash["pending"]:
            crash["pending"] = False
            raise KeyboardInterrupt  # dies after recording, before the run is saved

    monkeypatch.setattr(decisions, "record_decision", record_then_die)
    orchestrator = make_orchestrator(submit=counted)
    run = orchestrator.start(case("FIN-001"))
    with pytest.raises(KeyboardInterrupt):
        orchestrator.decide(callback(run))

    assert orchestrator.resume(run.run_id).state == "COMPLETED"
    assert len(calls) == 1


def test_a_transient_api_failure_is_retried_safely(make_orchestrator, store, ledger_rows):
    orchestrator = make_orchestrator(faults={"submit_finance_decision": "transient"})
    run = orchestrator.start(case("FIN-001"))
    assert orchestrator.decide(callback(run)).state == "COMPLETED"
    submit = [e for e in store.events(run.run_id) if e["name"] == "submit_finance_decision"]
    assert submit[0]["payload"]["attempts"] == 2
    assert len(ledger_rows(run.run_id)) == 1


def test_the_finance_api_refuses_a_run_without_a_stored_approval(make_orchestrator, ledger):
    run = make_orchestrator().start(case("FIN-001"))
    args = SubmitArgs(
        idempotency_key=f"{run.run_id}:APPROVE_FOR_POSTING",
        run_id=run.run_id,
        outcome="APPROVE_FOR_POSTING",
        amount="11000.00",
        currency="AUD",
        approval_id="apr_00000000",
    )
    with pytest.raises(NotApproved):
        ledger.submit(args)
