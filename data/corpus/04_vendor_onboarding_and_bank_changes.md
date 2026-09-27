---
document_id: FIN-POL-004
title: Vendor Onboarding and Bank Detail Changes
version: 5.1
effective_date: 2026-05-15
status: current
owner: Vendor Governance
classification: restricted
jurisdiction: global
tags: [vendor, onboarding, bank-account, verification, fraud]
---

# Vendor Onboarding and Bank Detail Changes

## 1. New vendors

New vendors require legal-name validation, registration identifier, tax status, business address, sanctions screening, conflict-of-interest declaration and independently verified bank details. The requester cannot complete verification.

## 2. Bank-account changes

Bank changes are high risk. Instructions contained in an invoice, email attachment or chat message are not sufficient. Vendor Governance must verify the change using a known contact method already stored in the vendor master, not contact details supplied in the change request.

After a verified bank change, the vendor is placed on payment hold for two business days. The first subsequent payment requires Financial Control co-approval regardless of amount. General application logs must display only the last four digits of the account.

## 3. Prohibited automation

No agent, workflow or processor may directly update bank details based solely on retrieved documents or model output. Automated systems may collect evidence, identify mismatches and open a verification task. Only the vendor-governance workflow may commit the change.

## 4. Vendor status

Invoices for `BLOCKED`, `DORMANT`, `SANCTIONS_REVIEW` or `PENDING_VERIFICATION` vendors must be held. A vendor marked `ACTIVE` may still require additional controls under FIN-POL-003.
