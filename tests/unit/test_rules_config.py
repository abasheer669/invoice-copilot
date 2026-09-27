from decimal import Decimal

import pytest

from ap_agent.rules_config import load_rules

RULES = load_rules()


@pytest.mark.parametrize(
    "kind, po_line_value, expected",
    [
        ("goods", "10000.00", "50.00"),  # 1% is 100, the 50 cap is lower
        ("goods", "1000.00", "10.00"),  # 1% is lower than the cap
        ("services", "4000.00", "80.00"),
        ("services", "10000.00", "100.00"),
        ("freight", "5.00", "75.00"),  # absolute only
    ],
)
def test_tolerance_is_the_lower_of_cap_and_percentage(kind, po_line_value, expected):
    assert RULES.tolerances.limit_for(kind, Decimal(po_line_value)) == Decimal(expected)


@pytest.mark.parametrize(
    "amount, role",
    [
        ("10000.00", "COST_CENTRE_MANAGER"),
        ("10000.01", "DEPARTMENT_DIRECTOR"),
        ("50000.00", "DEPARTMENT_DIRECTOR"),
        ("250000.01", "CFO"),
        ("1000000.01", "CEO"),
    ],
)
def test_approval_role_is_the_lowest_limit_that_covers_the_amount(amount, role):
    assert RULES.authority.role_for(Decimal(amount))[0] == role


def test_citations_carry_the_source_policy_version():
    assert RULES.cite("FIN-POL-002", "§2") == "FIN-POL-002 v2.4 §2"
    assert RULES.cite("FIN-POL-003", "§2-3") == "FIN-POL-003 v4.0 §2-3"
