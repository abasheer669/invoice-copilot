-- Simulated business systems: vendor master, purchasing, AP history and the finance API.
-- All data is synthetic, and nothing here can move money.

create schema mock_erp;

-- Only the last four digits of a bank account are ever stored (FIN-POL-004 §2).
create table mock_erp.vendors (
  vendor_id        text primary key,
  legal_name       text not null,
  status           text not null
                   check (status in ('ACTIVE', 'BLOCKED', 'DORMANT', 'SANCTIONS_REVIEW', 'PENDING_VERIFICATION')),
  bank_last4       text not null check (bank_last4 ~ '^[0-9]{4}$'),
  bank_country     text not null check (length(bank_country) = 2),
  bank_changed_at  timestamptz,
  risk_flags       text[] not null default '{}',
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create table mock_erp.purchase_orders (
  po_ref             text primary key,
  vendor_id          text not null references mock_erp.vendors,
  currency           text not null check (length(currency) = 3),
  approval_status    text not null check (approval_status in ('APPROVED', 'PENDING', 'CANCELLED')),
  freight_permitted  boolean not null default false,
  created_at         timestamptz not null default now()
);

create table mock_erp.po_lines (
  po_ref       text not null references mock_erp.purchase_orders,
  line_no      int not null,
  description  text not null,
  kind         text not null check (kind in ('goods', 'services', 'freight')),
  qty          numeric(12, 2) not null check (qty > 0),
  unit_price   numeric(12, 2) not null check (unit_price >= 0),
  primary key (po_ref, line_no)
);

create table mock_erp.goods_receipts (
  receipt_id    text primary key,
  po_ref        text not null,
  line_no       int not null,
  qty_received  numeric(12, 2) not null check (qty_received > 0),
  received_at   timestamptz not null,
  received_by   text not null,
  foreign key (po_ref, line_no) references mock_erp.po_lines
);

create table mock_erp.invoice_history (
  record_id         text primary key,
  vendor_id         text not null references mock_erp.vendors,
  invoice_ref       text not null,
  invoice_ref_norm  text generated always as (upper(btrim(invoice_ref))) stored,
  invoice_date      date not null,
  amount            numeric(14, 2) not null,
  currency          text not null check (length(currency) = 3),
  po_ref            text,
  status            text not null check (status in ('PAID', 'POSTED', 'HELD', 'REJECTED'))
);

create index invoice_history_lookup on mock_erp.invoice_history (vendor_id, invoice_ref_norm);

-- The simulated finance API. A retried submit with the same idempotency key
-- finds the original row instead of recording a second decision.
create table mock_erp.sim_ledger (
  idempotency_key  text primary key,
  decision_ref     text not null unique,
  run_id           text not null,
  outcome          text not null
                   check (outcome in ('APPROVE_FOR_POSTING', 'HOLD_FOR_INFORMATION', 'REJECT_DUPLICATE',
                                      'REJECT_INVALID', 'ESCALATE_CONTROL_REVIEW')),
  amount           numeric(14, 2) not null,
  currency         text not null check (length(currency) = 3),
  approval_id      text not null,
  created_at       timestamptz not null default now()
);
