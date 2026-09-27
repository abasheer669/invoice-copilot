-- Least-privilege roles. The application logs in as ap_app, which holds no rights itself,
-- and switches role per code path with SET ROLE (src/ap_agent/db.py), so each path holds
-- only the rights it needs.

create role ap_reader nologin;   -- the four read-only tools
create role ap_writer nologin;   -- the approval and submit path
create role ap_runtime nologin;  -- the orchestrator's own state and audit events
create role ap_ingest nologin;   -- knowledge-base builds

create role ap_app login noinherit password 'ap_app';  -- local development only
grant ap_reader, ap_writer, ap_runtime, ap_ingest to ap_app;

grant usage on schema mock_erp, agent to ap_reader, ap_writer, ap_runtime, ap_ingest;

grant select on mock_erp.vendors, mock_erp.purchase_orders, mock_erp.po_lines,
                mock_erp.goods_receipts, mock_erp.invoice_history,
                agent.kb_index_versions, agent.kb_documents, agent.kb_chunks, agent.run_attachments
  to ap_reader;

grant select, insert on mock_erp.sim_ledger, agent.approvals, agent.decisions to ap_writer;

grant select, insert, update on agent.runs to ap_runtime;
grant select, insert on agent.events, agent.run_attachments to ap_runtime;
grant select on agent.approvals, agent.decisions to ap_runtime;

-- Builds are never deleted: retired and failed versions stay for audit.
grant select, insert, update on agent.kb_index_versions, agent.kb_documents, agent.kb_chunks
  to ap_ingest;
