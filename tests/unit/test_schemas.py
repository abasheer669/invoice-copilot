import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ap_agent.schemas import InvoiceCase

CASES = sorted((Path(__file__).parents[2] / "data" / "cases").glob("FIN-*.json"))
FIN_001 = json.loads(CASES[0].read_text())


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_case_files_are_valid(path):
    assert InvoiceCase.model_validate_json(path.read_text()).case_id == path.stem


@pytest.mark.parametrize(
    "change",
    [
        {"approved": True},  # unknown field
        {"remit_to_last4": "062000123444471"},  # full account number
        {"amount": "11000.005"},  # sub-cent amount
        {"currency": "aud"},
        {"lines": []},
    ],
)
def test_malformed_cases_are_rejected(change):
    with pytest.raises(ValidationError):
        InvoiceCase.model_validate(FIN_001 | change)
