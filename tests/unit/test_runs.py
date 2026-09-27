import json
from pathlib import Path

import pytest

from ap_agent.runs import TRANSITIONS, IllegalTransition, Run
from ap_agent.schemas import InvoiceCase

CASE = InvoiceCase.model_validate(
    json.loads((Path(__file__).parents[2] / "data/cases/FIN-001.json").read_text())
)


def run_in(state: str) -> Run:
    return Run(run_id="run_test", case=CASE, state=state)


def test_the_happy_path_is_allowed():
    run = run_in("RECEIVED")
    for state in ["GATHERING", "CHECKING", "RECOMMENDING", "AWAITING_APPROVAL", "SUBMITTING"]:
        run.move_to(state)
    assert run.move_to("COMPLETED") == "SUBMITTING"


@pytest.mark.parametrize(
    "start, target",
    [
        ("RECEIVED", "SUBMITTING"),  # no skipping straight to a decision
        ("RECOMMENDING", "SUBMITTING"),  # never submit without an approval
        ("AWAITING_APPROVAL", "FAILED"),  # a waiting run only moves on a human decision
        ("COMPLETED", "SUBMITTING"),  # finished runs stay finished
        ("FAILED", "GATHERING"),
    ],
)
def test_illegal_transitions_raise(start, target):
    run = run_in(start)
    with pytest.raises(IllegalTransition, match=f"{start} -> {target} is not allowed"):
        run.move_to(target)
    assert run.state == start


def test_only_awaiting_approval_leads_to_submitting():
    assert [s for s, targets in TRANSITIONS.items() if "SUBMITTING" in targets] == [
        "AWAITING_APPROVAL"
    ]
