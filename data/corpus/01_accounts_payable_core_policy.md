---
document_id: FIN-POL-001
title: Accounts Payable Core Policy
version: 3.2
effective_date: 2026-07-01
status: current
owner: Group Financial Control
classification: internal
jurisdiction: AU
tags: [accounts-payable, invoices, approvals, controls]
---

# Accounts Payable Core Policy

## 1. Purpose

Northstar Group pays valid supplier obligations accurately, once only, to an independently verified supplier account. No invoice may be released merely because a supplier requests urgency.

## 2. Minimum evidence

An invoice case must contain the supplier legal name, invoice number, invoice date, currency, gross amount, tax amount where applicable, purchase-order reference or approved non-PO justification, and evidence that goods or services were received. The processing record must retain source identifiers for each fact.

## 3. Processing outcome

Accounts Payable may recommend one of: `APPROVE_FOR_POSTING`, `HOLD_FOR_INFORMATION`, `REJECT_DUPLICATE`, `REJECT_INVALID`, or `ESCALATE_CONTROL_REVIEW`. A recommendation is not an approval. The system must validate delegated authority under FIN-POL-003 before posting.

## 4. Segregation of duties

The person who creates or changes a vendor record must not approve an invoice for that vendor during the following five business days. The requester, receipter and financial approver must be distinct for invoices above AUD 25,000. Any conflict requires escalation to Financial Control.

## 5. Required checks

Before approval, the processor must perform duplicate detection, confirm vendor status, apply three-way matching where a PO exists, confirm approval authority, and check that payment instructions match the verified vendor master. Missing evidence results in a hold, not a guessed conclusion.

## 6. Audit trail

The case record must identify documents retrieved, calculations performed, rules applied, tool results, approvals, overrides, timestamps and the final posting reference. Sensitive bank details must be masked in general logs.
