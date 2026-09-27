-- The agent's own state: knowledge base, runs, audit events, approvals and decisions.

create extension if not exists vector;

create schema agent;

-- Knowledge base. Every `ap ingest` creates a new index version; only one is live.
create table agent.kb_index_versions (
  index_version  text primary key,
  embed_model    text not null,
  embed_dim      int not null check (embed_dim > 0),
  status         text not null check (status in ('BUILDING', 'ACTIVE', 'RETIRED', 'FAILED')),
  created_at     timestamptz not null default now(),
  activated_at   timestamptz
);

create unique index kb_one_active_index on agent.kb_index_versions (status) where status = 'ACTIVE';

create table agent.kb_documents (
  index_version   text not null references agent.kb_index_versions,
  doc_id          text not null,
  version         text not null,
  title           text not null,
  doc_type        text not null check (doc_type in ('policy', 'supplier_document', 'reference_extract')),
  status          text not null check (status in ('current', 'superseded', 'untrusted')),
  effective_date  date not null,
  classification  text not null,
  content_hash    text not null,
  primary key (index_version, doc_id, version)
);

-- The vector dimension is left open so the embedding model can change between builds;
-- an exact scan is fine at this corpus size.
create table agent.kb_chunks (
  index_version  text not null,
  chunk_id       text not null,
  doc_id         text not null,
  doc_version    text not null,
  section        text not null,
  citation       text not null,
  text           text not null,
  content_hash   text not null,
  embedding      vector not null,
  primary key (index_version, chunk_id),
  foreign key (index_version, doc_id, doc_version) references agent.kb_documents
);

create table agent.runs (
  run_id           text primary key,
  case_id          text not null,
  case_input       jsonb not null,
  state            text not null
                   check (state in ('RECEIVED', 'GATHERING', 'CHECKING', 'RECOMMENDING', 'AWAITING_APPROVAL',
                                    'SUBMITTING', 'COMPLETED', 'CLOSED', 'FAILED')),
  evidence         jsonb not null default '{}',
  checks           jsonb not null default '[]',
  result           jsonb,
  failure_reason   text,
  index_version    text,
  rules_version    text,
  llm_model        text,
  embed_model      text,
  step_count       int not null default 0,
  tool_call_count  int not null default 0,
  version          int not null default 0,  -- optimistic locking
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create table agent.events (
  event_id     bigint generated always as identity primary key,
  run_id       text not null references agent.runs,
  ts           timestamptz not null default clock_timestamp(),
  event_type   text not null,  -- state_change, tool_call, tool_denied, retrieval, llm_call, validation_error, approval
  name         text,
  outcome      text,
  duration_ms  int,
  payload      jsonb not null default '{}'
);

create index events_by_run on agent.events (run_id, event_id);

-- Case attachments, chunked and embedded per run. Always untrusted.
create table agent.run_attachments (
  run_id     text not null references agent.runs,
  chunk_id   text not null,
  name       text not null,
  text       text not null,
  embedding  vector,
  primary key (run_id, chunk_id)
);

-- A repeated approval callback hits the unique callback_id and gets the stored response.
create table agent.approvals (
  approval_id  text primary key,
  run_id       text not null references agent.runs,
  callback_id  text not null unique,
  approver     text not null,
  role         text not null,
  decision     text not null check (decision in ('APPROVE', 'REJECT')),
  response     jsonb not null,
  created_at   timestamptz not null default now()
);

-- idempotency_key is "{run_id}:{outcome}", so a resumed run never submits twice.
create table agent.decisions (
  idempotency_key  text primary key,
  run_id           text not null references agent.runs,
  outcome          text not null
                   check (outcome in ('APPROVE_FOR_POSTING', 'HOLD_FOR_INFORMATION', 'REJECT_DUPLICATE',
                                      'REJECT_INVALID', 'ESCALATE_CONTROL_REVIEW')),
  decision_ref     text not null,
  response         jsonb not null,
  created_at       timestamptz not null default now()
);
