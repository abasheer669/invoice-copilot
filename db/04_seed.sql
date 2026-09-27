-- Synthetic business data for the acceptance cases FIN-001 to FIN-005.
-- Each case trips only the control it is meant to test; everything else matches.

insert into mock_erp.vendors
  (vendor_id, legal_name, status, bank_last4, bank_country, bank_changed_at, created_at, updated_at)
values
  ('V-1001', 'Northwind Office Furniture Pty Ltd', 'ACTIVE', '4471', 'AU', null,
   '2023-03-01T09:00:00+10', '2026-06-01T09:00:00+10'),
  ('V-2002', 'Harbour IT Supplies Pty Ltd',        'ACTIVE', '1190', 'AU', '2025-02-10T09:00:00+10',
   '2022-11-15T09:00:00+10', '2025-02-10T09:00:00+10'),
  ('V-3003', 'Metro Facilities Services Pty Ltd',  'ACTIVE', '5520', 'AU', null,
   '2024-01-20T09:00:00+10', '2026-01-20T09:00:00+10');

insert into mock_erp.purchase_orders (po_ref, vendor_id, currency, approval_status, created_at)
values
  ('PO-7788', 'V-1001', 'AUD', 'APPROVED', '2026-09-01T09:00:00+10'),  -- FIN-001, FIN-005
  ('PO-7701', 'V-1001', 'AUD', 'APPROVED', '2026-08-01T09:00:00+10'),  -- FIN-002
  ('PO-8200', 'V-2002', 'AUD', 'APPROVED', '2026-09-05T09:00:00+10'),  -- FIN-003
  ('PO-9100', 'V-3003', 'AUD', 'APPROVED', '2026-09-01T09:00:00+10');  -- FIN-004

insert into mock_erp.po_lines (po_ref, line_no, description, kind, qty, unit_price)
values
  ('PO-7788', 1, 'Office chair',               'goods',    100, 100.00),
  ('PO-7701', 1, 'Desk lamp',                  'goods',     50, 100.00),
  ('PO-8200', 1, 'Network switch',             'goods',     40, 200.00),
  ('PO-9100', 1, 'Office cleaning, September', 'services',   1, 4000.00);

-- PO-9100 has no receipt on purpose (FIN-004: missing evidence).
insert into mock_erp.goods_receipts (receipt_id, po_ref, line_no, qty_received, received_at, received_by)
values
  ('GR-3341', 'PO-7788', 1, 100, '2026-09-18T10:30:00+10', 'm.chen'),
  ('GR-3302', 'PO-7701', 1,  50, '2026-08-12T14:00:00+10', 'm.chen'),
  ('GR-3410', 'PO-8200', 1,  40, '2026-09-10T11:15:00+10', 'k.ali');

-- IH-0042 is the paid original that FIN-002 resubmits. The rest are ordinary history
-- that must not match any case exactly or fuzzily.
insert into mock_erp.invoice_history
  (record_id, vendor_id, invoice_ref, invoice_date, amount, currency, po_ref, status)
values
  ('IH-0040', 'V-1001', 'INV-3102', '2026-07-10', 2200.00, 'AUD', 'PO-7610', 'PAID'),
  ('IH-0042', 'V-1001', 'INV-3310', '2026-08-15', 5500.00, 'AUD', 'PO-7701', 'PAID'),
  ('IH-0043', 'V-2002', 'HIT-9870', '2026-08-02', 3300.00, 'AUD', 'PO-8150', 'PAID'),
  ('IH-0044', 'V-3003', 'MFS-2208', '2026-08-20', 4400.00, 'AUD', 'PO-9050', 'PAID');
