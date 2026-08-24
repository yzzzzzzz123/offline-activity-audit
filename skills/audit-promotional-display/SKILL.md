---
name: audit-promotional-display
description: Audit promotional/stack-display claims from a contract PDF, distributor sales Excel, and field photos. Use whenever every contract store must receive a deterministic date/location/display/duplicate decision, visible packaging must be conservatively matched to exact source-cell Excel product names, ordinary prices must be separated from real promotion evidence, and a fixed store-by-store display worksheet must be produced.
---

# Audit Promotional Display

Build one evidence row per contract store. A filename, distributor total, or product-family resemblance never proves that a particular store performed the contracted display.

## AI extraction

Read [evidence.schema.json](references/evidence.schema.json) and [audit-rules.md](references/audit-rules.md) completely. For field-photo product identity, also read [product-rag.md](references/product-rag.md); the runtime validates [product-rag.json](references/product-rag.json) and attaches its indexed multi-view images separately from field evidence.

Give the vision AI only the contract PDF, field photos, and repository-owned product-reference views. Product-reference views may establish product identity only; they cannot establish a field store, date, display, promotion, price, or photo uniqueness and must never be returned as `photo_files`. For a scanned PDF, expose every page as a lossless full-resolution page image. Read and validate the contract first; then use that frozen contract result to review the field photos. It must return schema version `2.2` with:

- contract customer, dates, display standard, per-store fee, claimed amount, explicit product/promotion requirements, and the complete ordered store list;
- one review per contract store, with an empty `photo_files` array when that store lacks a photo;
- visible complete date/location with its visual basis;
- a structured display observation that separately records whether the photo proves
  `1平米堆头`, `4纵陈列`, both, neither, or cannot be judged, plus a left-to-right count and
  description of every visible vertical facing and an independent one-square-metre basis;
- target-brand products supported by visible packaging only;
- grounded `product_reference_hits` using only catalog product/view IDs, with `exact` reserved for a valid visible 69 code or multiple independent identity anchors and `candidate` used for non-unique partial packaging;
- explicit promotion signals separated from ordinary visible prices;
- source basenames, risks, and suggested supplemental evidence.

Only explicit core contract terms create mandatory controls. A generic whole-brand scope such as `参半所有系列` is not a narrow required-product subset. Product/sales tables appended after the contract do not create a promotion requirement unless the contract text explicitly says so.

The AI must not receive/read sales Excel, copy an Excel name into a photo product, calculate supported amount, hash photos, or make the final pass decision. Field filenames are leads only. Deterministic code resolves a returned reference ID to the catalog product name, product code, and 69 code; the model does not invent or rewrite those identity fields.

## Deterministic audit

Python must:

1. Read the sales Excel directly, preserving every original `product_name` cell, customer, period, quantity, amount, and source row.
2. Validate all AI-returned photo basenames against the extracted source set.
3. Determine activity-period result from the visible ISO date and contract dates.
4. Calculate the store relation from the contract name, independently visible location, original source filename, and numbered-branch conflicts; the AI does not decide `exact`, `compatible`, or `mismatch`.
5. Translate the display observation into the mandatory display control. `meets` is valid only when `matched_standard` is `stack_1sqm`, `four_vertical`, or `both`; `does_not_meet` pairs only with `none`; `unclear` pairs only with `unclear`.
6. Independently calculate SHA-256, dHash, and pHash for cross-store photo-reuse screening. Exact reuse fails; cross-store near-duplicate candidates remain unresolved until reviewed. This is not a display-standard judgment.
7. Resolve each visual-RAG hit from the validated catalog, display its product name/product code/69 code, and match it to code-read sales rows by 69 code first and product code second. Fall back to conservative visible-text matching for products outside the catalog. Output only exact source-cell Excel strings and classify `exact`, `candidate`, or `unmatched`; translate them in Excel to `明确对应`, `候选对应`, or `未匹配`.
8. Build promotion text deterministically. An ordinary price list without an explicit signal must never produce `有促销`.
9. Award the per-store fee only when photo, full date, location, display, and photo-reuse controls pass. Apply product/promotion controls only if the contract explicitly makes them mandatory.
10. Sum supported amounts and write values, never formulas.

## Display standard and photo reuse

The contracted display standard is an OR condition: clearly prove at least one of
`1平米堆头` or `4纵陈列`. Do not output only `陈列符合`. The visual description must say
which branch is met and what is visible. If area cannot be established and four vertical
facings/columns cannot be counted, return `unclear` and request a wider or clearer photo.
For `4纵陈列`, record the exact count and list the same number of distinct, simultaneously
visible columns from left to right; never add boxes stacked vertically or columns from different
shelf levels/angles. For `1平米堆头`, provide visible scale, dimensions, or complete-footprint
evidence rather than inferring area from a close-up.

Photo reuse is a separate anti-fraud control across contract stores. Never call it
`陈列重复`, and never use a no-reuse result as evidence that the display itself is compliant.

## Product correspondence

The maintained multi-view catalog is an identity reference, not field evidence. A complete valid 69 code is the strongest single identifier. Otherwise an exact catalog hit needs a visible product code or legal product name plus an independent compatible anchor, or at least two independent anchors that uniquely converge on one catalog item. Brand, color, box shape, generic claims, QR codes, variable batch/date printing, and backgrounds are never sufficient alone.

- `exact`: visible packaging/bundle/specification uniquely supports one code-read Excel row. In the maintained campaign mapping, a legible `3+2` bundle that selects the unique `3+2` Excel item qualifies.
- `candidate`: visible series/packaging narrows the code-read names but cannot establish one exact SKU, including maintained SP-1/SE-1/SP-2/SP-4 aliases.
- `unmatched`: no source-cell Excel name is supported.

Do not silently rewrite spelling, brand, size, flavor, or bundle notation. Candidate output may list several original Excel names, in source-row order.

## Promotion

`有促销` requires visible special-price wording, old/new price, discount, gift, multi-buy, `1+1`, `3+2`, or `超值装/特享装/量贩装`. A lone `19.90元` or other ordinary tag is only a price. When quality prevents a decision, write `无法判断` with the limitation.

Product and promotion fields are auxiliary by default. They must not override date, location, display, or duplicate failures unless the contract explicitly requires the product/promotion.

## Worksheet contract

For every contract store write:

- A: store, per-store fee, display standard;
- B: code-read customer/period, exact Excel names, correspondence label, and `无门店明细，不能单独证明该店` when applicable;
- C in strict order: `文件`, `识别日期`, `识别地点`, `陈列标准核验`, `视觉依据`, `照片复用检查`, `识别产品`, `促销信息`;
- D: date/location/display-standard/photo-reuse comparison with the contract, keeping the last two controls on separate lines;
- E: deterministic supported amount;
- F: `通过` or specific supplemental evidence.

Formal runs occur only through:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
```
