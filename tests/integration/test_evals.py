"""The evaluation itself, run with the canned knowledge base (see conftest.py)."""

import json
from pathlib import Path

import pytest

from ap_agent.evals import evaluate_case, load_specs, summary
from ap_agent.llm import EchoLLM

ROOT = Path(__file__).parents[2]
SPECS = {spec.case: spec for spec in load_specs(ROOT / "data/cases")}


class CautiousLLM(EchoLLM):
    """Turns every recommendation into a hold."""

    def generate_json(self, system, prompt, schema):
        draft = json.loads(super().generate_json(system, prompt, schema))
        return json.dumps(draft | {"outcome": "HOLD_FOR_INFORMATION"})


def run_case(make_orchestrator, ledger, case_id, **overrides):
    spec = SPECS[case_id]
    faults = dict([spec.faults.split(":")]) if spec.faults else {}
    orchestrator = make_orchestrator(faults=faults, **overrides)
    return evaluate_case(spec, orchestrator, ledger.records_for, root=ROOT)


def test_all_five_cases_load():
    assert sorted(SPECS) == ["FIN-001", "FIN-002", "FIN-003", "FIN-004", "FIN-005"]
    assert SPECS["FIN-005"].callback_deliveries == 2


@pytest.mark.parametrize("case_id", sorted(SPECS))
def test_every_case_passes_with_the_rules_draft(make_orchestrator, ledger, case_id):
    result = run_case(make_orchestrator, ledger, case_id)
    assert result.problems == []
    assert (result.passed, result.safe, result.state) == (True, True, "COMPLETED")
    assert result.valid_citations == result.citations > 0


def test_holding_an_invoice_that_should_post_fails_but_is_not_unsafe(make_orchestrator, ledger):
    result = run_case(make_orchestrator, ledger, "FIN-001", llm=CautiousLLM())
    assert not result.passed
    assert result.safe
    assert result.problems == [
        "outcome HOLD_FOR_INFORMATION, expected APPROVE_FOR_POSTING",
        "0 payment(s) recorded, expected 1",
    ]


def test_a_run_that_never_finishes_fails_with_its_reason(make_orchestrator, ledger):
    result = run_case(make_orchestrator, ledger, "FIN-002", max_steps=2)
    assert not result.passed
    assert result.problems[0] == "ended in FAILED: step budget of 2 exceeded"


def test_summary_counts(make_orchestrator, ledger):
    results = [run_case(make_orchestrator, ledger, c) for c in ("FIN-001", "FIN-003")]
    results.append(run_case(make_orchestrator, ledger, "FIN-001", llm=CautiousLLM()))
    citations = sum(r.citations for r in results)
    assert summary(results) == {
        "passed": "2/3",
        "outcome_accuracy": "2/3",
        "mean_recall_at_5": 1.0,
        "citation_validity": f"{citations}/{citations}",
        "safety": "3/3",
    }
