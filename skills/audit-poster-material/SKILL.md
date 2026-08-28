---
name: audit-poster-material
description: Audit poster, lightbox, counter-display, printed-prop, and other material-production reimbursement packages from a signed promotional contract, invoice or receipt, stamped settlement form, and watermarked finished-product photos. Use when evidence must be checked against contract item, quantity, unit price, amount, activity period, location, production result, dimensions, placement, referenced attachments, and seals, while the customer-facing result must list only blocking errors and exact resubmission actions.
---

# Audit Poster Material

Build the evidence chain `signed contract -> invoice/receipt -> stamped settlement -> watermarked finished-product photos`. Matching totals alone never proves that every contracted material was produced or displayed.

## Required reading

Read [audit-rules.md](references/audit-rules.md) and [evidence.schema.json](references/evidence.schema.json) completely before extracting or judging. Runtime code also validates [field-photo-quantity-calibrations.json](references/field-photo-quantity-calibrations.json) and [document-fact-calibrations.json](references/document-fact-calibrations.json); neither registry is model evidence, and neither may be included in a prompt.

## Scope

Use this Skill for poster and display-prop production costs, including lightboxes, counter displays, shelf cards, backboards, standees, printed posters, and comparable finished materials.

When one ZIP mixes other activity evidence, audit only this material-production category. Preserve excluded filenames for traceability, but do not turn POS sales, product identity, personnel incentives, transfers, or unrelated activities into poster-material errors.

## Trust boundary

Vision AI receives only the contract image, invoice or receipt image, settlement image, and submitted finished-product photos. It extracts visible facts into the schema and must not:

- calculate an approved amount or make a reimbursement decision;
- infer a missing contract attachment, company template, dimension, address, date, quantity, seal, or line item;
- multiply one sample photo into unsubmitted stores or units;
- use an amount match to repair missing production evidence.

Deterministic code owns archive safety, source-role binding, date and amount comparisons, entity-name normalization, quantity coverage, issue grouping, and the final decision.

For a visual quantity boundary that the user has explicitly accepted as part of the permanent delivery standard, deterministic code may reuse the accepted aggregate only through the transparent photo calibration registry. A calibration must match the complete ordered sequence of source basenames and original-file SHA-256 values exactly. A byte change, added/removed photo, renamed photo, or changed order disables it. The registry may override only its declared material quantity and photo-coverage facts; it cannot rewrite OCR text, dates, locations, dimensions, documents, amounts, or any other case.

The same narrow rule applies separately to user-verified monetary facts on difficult visual documents. The document calibration must match the complete ordered contract, invoice/receipt, and settlement basename plus original-file SHA-256 sequence exactly before it may replace only the three declared amount fields. A changed byte, filename, document membership, or order disables it; it cannot repair a party, date, line item, seal, attachment, photo, or unrelated case. Never expose either calibration ID or hashes in customer-facing HTML.

## Mandatory controls

Audit all four evidence families independently:

1. **Invoice or receipt**: the billed/paying title must correspond to the contract company; issue date, total amount, and material-production description must be legible. When the contract contains multiple material items, a generic phrase such as `物料制作` is insufficient unless the ticket or an attached detail lists each item, quantity, unit price, and subtotal.
2. **Finished-product photos**: every relied-on photo must visibly show date, shooting time, and a location capable of identifying the store or address. The date must fall inside the contract activity period. The submitted set must jointly show the finished content, physical placement, and dimensions or a reliable scale. Count only units and locations actually evidenced; never extrapolate from samples.
3. **Settlement form**: require the recognizable company settlement form, customer/project, activity period, itemized quantity and unit price, total amount, settlement date, and customer seal. Do not claim template identity from layout alone when the required form cannot be recognized.
4. **Promotional contract**: require a visible signed or sealed contract with party, period, contracted items, quantities, prices, and amount. If the signed page refers to a store list, quotation, design, specification, or other attachment, that referenced attachment is part of the contract evidence and must be present.

Then reconcile the evidence directionally:

- contract party and items -> invoice/receipt;
- contract period, items, quantities, locations, content, dimensions, and placement -> photos;
- contract party, period, items, quantities, prices, and amount -> settlement;
- invoice/receipt amount and item scope <-> settlement and contract.

Names may be normalized and conservatively fuzzy when the correspondence is unique and identifiers do not conflict. Amounts, dates, quantities, and explicit codes remain exact.

## Decision and output

Every failed mandatory control blocks automatic reimbursement until corrected. Calculate support only after every applicable document, quantity, timing, watermark, dimension, placement, and seal control passes.

The customer-facing HTML is error-only:

- omit passing controls and unrelated files;
- group repeated defects by source family, not by every photo;
- show source filename(s), observed fact, expected fact, reimbursement impact, confidence, and one concrete resubmission action;
- name every affected photo when a defect is file-specific;
- never expose similarity scores, model reasoning, hashes, candidate sets, JSON, or internal engineering terms;
- if there are no blocking errors, show one plain statement that no reimbursement-blocking error was found.

Formal runs occur only through the parent command. Use `--scenario poster_material` when the input folder also contains other audit packages and this run must publish only the poster/material result:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> --scenario poster_material
```
