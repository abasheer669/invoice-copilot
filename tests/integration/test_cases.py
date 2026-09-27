"""FIN-001 to FIN-005: real tool results against the seed data, checked by the rules engine."""

from pathlib import Path

import pytest
import yaml

from ap_agent import tools
from ap_agent.config import Settings
from ap_agent.erp import PostgresErp, erp_tools
from ap_agent.rules import Evidence, assess
from ap_agent.rules_config import load_rules
from ap_agent.schemas import InvoiceCase
from ap_agent.tools import invoke

ROOT = Path(__file__).parents[2]
SPECS = sorted((ROOT / "data" / "cases").glob("FIN-*.yaml"))


def gather(case: InvoiceCase, settings) -> Evidence:
    registry = erp_tools(PostgresErp(settings, load_rules()))
    history_args = case.model_dump(
        mode="json", include={"vendor_id", "invoice_ref", "amount", "currency", "invoice_date"}
    )
    results = {
        "vendor": invoke(registry["get_vendor_record"], {"vendor_id": case.vendor_id}, settings),
        "purchase_order": invoke(registry["get_purchase_order"], {"po_ref": case.po_ref}, settings),
        "history": invoke(registry["check_invoice_history"], history_args, settings),
    }
    return Evidence(**{name: r.data for name, r in results.items()})


@pytest.mark.parametrize("spec_path", SPECS, ids=lambda p: p.stem)
def test_case_reaches_expected_outcome(settings, monkeypatch, spec_path):
    spec = yaml.safe_load(spec_path.read_text())
    monkeypatch.setattr(tools, "BACKOFF_S", 0)
    run_settings = Settings(
        database_url=settings.database_url, faults=spec.get("faults", ""), tool_timeout_s=0.2
    )
    case = InvoiceCase.model_validate_json((ROOT / spec["input"]).read_text())
    assessment = assess(case, gather(case, run_settings), load_rules())
    assert assessment.outcome == spec["expected_outcome"]
