import json
from pathlib import Path

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege, UniqueViolation

from ap_agent.db import connect

CASES = sorted((Path(__file__).parents[2] / "data" / "cases").glob("FIN-*.json"))

SIM_LEDGER_INSERT = """
    insert into mock_erp.sim_ledger
      (idempotency_key, decision_ref, run_id, outcome, amount, currency, approval_id)
    values ('run_t:APPROVE_FOR_POSTING', 'DEC-T', 'run_t', 'APPROVE_FOR_POSTING', 1, 'AUD', 'apr_t')
"""
RUN_INSERT = """
    insert into agent.runs (run_id, case_id, case_input, state)
    values ('run_t', 'FIN-T', '{}', 'RECEIVED')
"""
KB_INSERT = """
    insert into agent.kb_index_versions (index_version, embed_model, embed_dim, status)
    values (%s, 'fake', 8, 'ACTIVE')
"""


def test_seed_is_loaded(settings):
    with connect("ap_reader", settings) as conn:
        vendors = conn.execute("select vendor_id from mock_erp.vendors order by 1").fetchall()
        paid = conn.execute(
            "select status from mock_erp.invoice_history where record_id = 'IH-0042'"
        ).fetchone()
    assert [v["vendor_id"] for v in vendors] == ["V-1001", "V-2002", "V-3003"]
    assert paid["status"] == "PAID"


@pytest.mark.parametrize(
    "role, statement",
    [
        ("ap_reader", "select * from mock_erp.vendors"),
        ("ap_reader", "select * from agent.kb_chunks"),
        ("ap_writer", SIM_LEDGER_INSERT),
        ("ap_runtime", RUN_INSERT),
        (
            "ap_ingest",
            "insert into agent.kb_index_versions (index_version, embed_model, embed_dim, status) "
            "values ('kb-test', 'fake', 8, 'BUILDING')",
        ),
    ],
)
def test_role_can_do_its_job(settings, role, statement):
    with connect(role, settings) as conn, conn.transaction(force_rollback=True):
        conn.execute(statement)


@pytest.mark.parametrize(
    "role, statement",
    [
        ("ap_reader", SIM_LEDGER_INSERT),
        ("ap_reader", "update mock_erp.vendors set bank_last4 = '0000'"),
        ("ap_reader", "select * from agent.runs"),
        ("ap_writer", "update mock_erp.vendors set bank_last4 = '0000'"),
        ("ap_writer", "update agent.runs set state = 'COMPLETED'"),
        ("ap_runtime", SIM_LEDGER_INSERT),
        ("ap_runtime", "delete from agent.events"),
        ("ap_ingest", "select * from mock_erp.vendors"),
        ("ap_ingest", "delete from agent.kb_chunks"),
    ],
)
def test_role_is_denied_everything_else(settings, role, statement):
    with connect(role, settings) as conn, pytest.raises(InsufficientPrivilege):
        conn.execute(statement)


def test_login_role_has_no_rights_until_it_switches_role(settings):
    url = settings.database_url.get_secret_value()
    with psycopg.connect(url) as conn, pytest.raises(InsufficientPrivilege):
        conn.execute("select * from mock_erp.vendors")


def test_sim_ledger_rejects_a_repeated_idempotency_key(settings):
    with connect("ap_writer", settings) as conn, conn.transaction(force_rollback=True):
        conn.execute(SIM_LEDGER_INSERT)
        with pytest.raises(UniqueViolation), conn.transaction():
            conn.execute(SIM_LEDGER_INSERT)


def test_only_one_kb_index_can_be_active(settings):
    with connect("ap_ingest", settings) as conn, conn.transaction(force_rollback=True):
        conn.execute(KB_INSERT, ["kb-a"])
        with pytest.raises(UniqueViolation), conn.transaction():
            conn.execute(KB_INSERT, ["kb-b"])


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_case_fixture_matches_seed(settings, path):
    case = json.loads(path.read_text())
    assert case["case_id"] == path.stem
    assert path.with_suffix(".yaml").exists()
    with connect("ap_reader", settings) as conn:
        po = conn.execute(
            "select vendor_id, currency from mock_erp.purchase_orders where po_ref = %s",
            [case["po_ref"]],
        ).fetchone()
    assert po == {"vendor_id": case["vendor_id"], "currency": case["currency"]}
