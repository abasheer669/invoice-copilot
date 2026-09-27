---
document_id: FIN-POL-007
title: Invoice Exceptions and Escalations
version: 1.9
effective_date: 2026-06-01
status: current
owner: Accounts Payable Operations
classification: internal
jurisdiction: global
tags: [exceptions, escalation, hold, sla]
---

# Invoice Exceptions and Escalations

## 1. Exception categories

Use one primary category: `MISSING_PO`, `MISSING_RECEIPT`, `PRICE_VARIANCE`, `QUANTITY_VARIANCE`, `DUPLICATE_RISK`, `VENDOR_BLOCK`, `BANK_CHANGE`, `AUTHORITY_GAP`, `TAX_QUERY`, or `OTHER_CONTROL_RISK`.

## 2. Required exception record

The record must describe the failed rule, expected and observed facts, cited sources, responsible owner and next review date. Generic notes such as "does not match" are insufficient.

## 3. Escalation targets

PO and receipt issues go to the requester or receipter. Vendor and bank issues go to Vendor Governance. Approval issues go to Financial Control. Suspected fraud goes to Financial Crime and Controls without notifying the supplier of the suspicion.

## 4. Service levels

Standard exceptions are reviewed within three business days. Invoices due within two business days may be prioritised, but urgency does not relax controls. A case held longer than ten business days must be escalated to the Accounts Payable Manager.

## 5. Resumption

When new evidence arrives, the case must resume from the failed control, revalidate any time-sensitive vendor or delegation information, and retain the previous result in the audit history.
