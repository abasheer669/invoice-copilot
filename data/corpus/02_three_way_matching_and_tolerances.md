---
document_id: FIN-POL-002
title: Three-Way Matching and Tolerances
version: 2.4
effective_date: 2026-04-01
status: current
owner: Procure-to-Pay Operations
classification: internal
jurisdiction: AU
tags: [purchase-order, invoice, receipt, tolerance, matching]
---

# Three-Way Matching and Tolerances

## 1. Matching basis

PO-backed invoices must be compared against the approved purchase order and recorded goods or service receipt. Matching is performed per line and for the document total. Currency must match unless FIN-POL-009 authorises conversion.

## 2. Tolerances

For goods, a line is within tolerance when invoiced quantity does not exceed received quantity and the lower of these limits is met: AUD 50 absolute variance or 1% of the PO line value. For services, the limit is AUD 100 or 2%, whichever is lower, provided the service owner confirms completion.

Freight may vary by up to AUD 75 when the PO explicitly permits freight. Tax, rounding and foreign-exchange differences are assessed separately and must not be hidden inside a price variance.

## 3. Outcomes

All lines within tolerance may proceed to delegated approval. A variance outside tolerance produces `HOLD_FOR_INFORMATION` and must identify the failed line, expected value, actual value, difference and applicable threshold. Splitting a variance across multiple invoices does not make it acceptable.

## 4. Missing receipt

An invoice without a required receipt cannot be approved for payment. The agent may request confirmation from the designated receipter but must not infer receipt from delivery language on the invoice.

## 5. Calculation rule

Tolerance calculations must use decimal arithmetic and the invoice currency. Model-generated arithmetic is not authoritative. The calculation record must store input values, formula, result and rounding method.
