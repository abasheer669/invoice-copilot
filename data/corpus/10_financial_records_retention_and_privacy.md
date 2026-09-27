---
document_id: FIN-POL-010
title: Financial Records Retention and Privacy
version: 4.3
effective_date: 2026-01-01
status: current
owner: Records Management and Privacy
classification: internal
jurisdiction: AU
tags: [records, retention, privacy, logging, data]
---

# Financial Records Retention and Privacy

## 1. Retention

Invoice, approval, purchase-order, receipt, payment and exception records are retained for seven years after the end of the relevant financial year. Temporary model prompts, retrieval caches and debug logs are not the system of record.

## 2. Minimum necessary data

Prompts and logs must contain only the data necessary for the task. Full bank-account numbers, tax identifiers, signatures and personal contact details must be masked unless a restricted tool specifically requires them.

## 3. Access

Access must follow legal entity, business unit and job role. Retrieval systems must enforce source permissions before returning chunks; telling the model not to reveal a document is not an access control.

## 4. Model providers

Production financial data may be sent only to approved model endpoints configured for no training and the required data region. Test corpora must be synthetic or de-identified. Provider, model, region and request ID must be auditable.

## 5. Deletion and legal hold

Normal deletion schedules pause when a legal hold applies. Deleting a source document must also remove or expire derived index entries and caches according to the platform's deletion process.
