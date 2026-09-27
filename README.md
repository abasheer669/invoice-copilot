# invoice-copilot

A small CLI-based AI agent that helps companies process supplier invoices. It gathers evidence, checks it against policy in code, recommends an outcome with citations, and pauses for a human before any decision is recorded.

## Prerequisites

- macOS or Linux
- [uv](https://docs.astral.sh/uv/) (`brew install uv`), which installs Python 3.13 when needed
- Docker with Compose, running

## Setup

```sh
uv sync                        # create .venv from uv.lock
cp .env.example .env           # then set LLM_API_KEY
docker compose up -d --wait    # Postgres 17 + pgvector on 127.0.0.1:5433, schema and seed data loaded
uv run ap config               # effective settings, secrets masked
```

The SQL files in `db/` run once, in name order, when the database volume is first created. To rebuild from scratch:

```sh
docker compose down -v && docker compose up -d --wait
```

## Tests and lint

```sh
uv run pytest                  # database tests skip if Postgres is not running
uv run ruff check . && uv run ruff format --check .
```

## Data

All business data is synthetic. Invoice fields arrive pre-extracted in the case files (no OCR).

| Path | Contents |
| --- | --- |
| `data/corpus/` | The 15 policy documents, including superseded, irrelevant and adversarial ones |
| `data/cases/` | Acceptance cases FIN-001 to FIN-005: input (`.json`) and expected behaviour (`.yaml`) |
| `db/04_seed.sql` | Simulated vendors, purchase orders, goods receipts and invoice history |

## Database

One local Postgres database, `ap_agent`, holds two schemas:

- `mock_erp`: the simulated business systems (vendors, purchase orders, receipts, invoice history, and `sim_ledger`, the simulated finance API)
- `agent`: the agent's own state (knowledge base, runs, audit events, approvals, decisions)

Bank accounts are stored as the last four digits only.

The application logs in as `ap_app`, which has no rights of its own. Each code path switches to one least-privilege role (`src/ap_agent/db.py`):

| Role | Can | Used by |
| --- | --- | --- |
| `ap_reader` | Read `mock_erp` business data and the knowledge base | Read-only tools |
| `ap_writer` | Insert into `sim_ledger`, `approvals`, `decisions` | Approval and submit path |
| `ap_runtime` | Read and write runs, events, attachments | Orchestrator |
| `ap_ingest` | Write knowledge-base tables | `ap ingest` |

The credentials in `compose.yaml` and `db/03_roles.sql` are for local development only.

## Tools

The agent reads business data through three read-only tools. Each has a strict input and output schema (`src/ap_agent/erp.py`) and runs one fixed, parameterised query as `ap_reader`.

| Tool | Returns |
| --- | --- |
| `get_vendor_record` | Status, bank account last 4 digits, bank country, risk flags, last update |
| `get_purchase_order` | Lines, total before tax, currency, approval status, goods receipts |
| `check_invoice_history` | Past invoices from the same vendor that match exactly or probably (FIN-POL-005 §1) |

Every call goes through `invoke()` (`src/ap_agent/tools.py`):

1. Arguments are validated first; bad arguments return `invalid_args` without touching the database.
2. Each attempt has a hard deadline (`TOOL_TIMEOUT_S`, default 3 s).
3. Timeouts and transient errors are retried `TOOL_MAX_RETRIES` times (default 2) with backoff.
4. A missing record returns `not_found` and is not retried.
5. Output is validated before anyone sees it, so a full bank account number can never pass through.

The result always says what happened (`ok`, `error`, `attempts`, `duration_ms`).

To simulate failures, set `FAULTS`, for example `FAULTS=get_purchase_order:timeout` (FIN-004).

## Rules

Code decides the outcome; the LLM will only explain it. `src/ap_agent/rules.py` runs every check on every case. Each check is a pure function over exact decimals and returns:

- `PASS`, `FAIL` or `UNKNOWN` (evidence missing)
- the expected and observed facts, any calculation with its inputs and rounding
- the policy citation, and what a failure leads to

| Check | Rule | Policy |
| --- | --- | --- |
| `DUPLICATE` | No exact or probable match in invoice history | FIN-POL-005 §1-2 |
| `VENDOR_STATUS` | Vendor is `ACTIVE` | FIN-POL-004 §4 |
| `BANK_DETAILS` | Remit-to account matches the vendor master | FIN-POL-004 §2 |
| `PURCHASE_ORDER` | An approved PO for the same vendor | FIN-POL-002 §1 |
| `CURRENCY` | Invoice and PO in the same currency, and in AUD | FIN-POL-009 §1-2 |
| `RECEIPT_L{n}` | Invoiced quantity is not more than received | FIN-POL-002 §2, §4 |
| `PRICE_L{n}` | Price variance within the lower of the cap and the percentage | FIN-POL-002 §2 |
| `INVOICE_TOTAL` | Lines plus tax equal the gross amount | FIN-POL-002 §1 |
| `FRAUD_INDICATORS` | Fewer than 2 of: bank change, urgent or secret language, request to bypass controls | FIN-POL-005 §3 |
| `SEGREGATION` | Above AUD 25,000, the requester did not receive the goods | FIN-POL-001 §4 |

The outcome is the most severe consequence among the checks that did not pass:

1. `REJECT_DUPLICATE`: an exact match to a paid or posted invoice
2. `ESCALATE_CONTROL_REVIEW`: bank mismatch, fraud indicators or a segregation conflict
3. `HOLD_FOR_INFORMATION`: any other failure, or anything `UNKNOWN`
4. `APPROVE_FOR_POSTING`: everything passed

The engine also works out who must approve. That is the lowest role whose limit covers the gross amount, plus Financial Control for higher-risk payments (new vendor, changed or overseas bank account, fraud flag). The requester is always excluded from approving; above AUD 25,000, receipters are excluded too.

All numbers live in `src/ap_agent/rules_config.yaml`, each tied to the policy version it came from. Citations are built from those versions.

## Layout

| Path | Contents |
| --- | --- |
| `src/ap_agent/` | Application code: `config.py`, `cli.py`, `db.py`, `schemas.py` (invoice case), `tools.py` (tool contract), `erp.py` (ERP tools), `rules.py` + `rules_config.yaml` (rules engine) |
| `db/` | Schemas, tables, roles and seed data |
| `compose.yaml` | Local Postgres + pgvector |
| `tests/unit/` | Offline unit tests |
| `tests/contract/` | Offline tool-contract tests: schemas, timeouts, retries |
| `tests/integration/` | Tests against the local database |
