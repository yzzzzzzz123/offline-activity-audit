# Promotional display deterministic rules

## Mandatory store controls

Support the per-store fee only when all mandatory controls pass:

- at least one referenced source photo exists;
- a complete visible date is inside the contract period;
- deterministic visible-location comparison is `exact` or `compatible` and includes visible location evidence;
- when the contract names a numbered branch (for example `二店`), the visible location must show the same branch number; a shared chain name alone cannot pass;
- a filename remains insufficient by itself, but may bridge a `compatible` mapping when an independently visible location and the contract name are both strongly corroborated by the same source filename and no numbered-branch conflict exists;
- display observation is `meets`, with `matched_standard` proving `stack_1sqm`,
  `four_vertical`, or `both`, and is therefore code-translated to `pass`;
- no exact or unresolved cross-store near duplicate applies;
- contract-required product/promotion evidence, if any, is visible.

Missing, filename-only, mismatch, unclear, failed, exact-duplicate, and possible-duplicate controls support zero until supplemented.

## Display standard versus photo reuse

- The display rule is an OR condition: a store passes this control when the photo clearly
  proves either a roughly one-square-metre stack display or at least four vertical
  facings/columns. Proving both is allowed but not required.
- `standard_evidence=meets` pairs only with `matched_standard=stack_1sqm`,
  `four_vertical`, or `both`; `does_not_meet` pairs only with `none`; `unclear` pairs only
  with `unclear`.
- A generic statement such as `陈列符合` is not evidence. State the visible area/arrangement
  or the countable vertical facings. When neither can be established from the submitted
  view, use `unclear` and request a full-view photo.
- Photo reuse is a separate cross-store anti-fraud control. SHA-256 identifies byte-exact
  reuse; perceptual hashes flag visually near-identical cross-store candidates. It must be
  shown as `照片复用检查`, never as `陈列重复`, and cannot prove display compliance.

## Contract interpretation

- Read the explicit fee/calculation wording; never infer a per-store fee from the total claim alone.
- A whole-brand phrase such as `参半所有系列` means no narrow mandatory-product subset.
- A later product or sales table is supporting material, not an implicit discount, gift, multi-buy, or other promotion requirement.
- Product or promotion evidence becomes mandatory only when the core contract explicitly makes it a condition.

## Excel and products

- Read every sales name from the raw source cell. AI JSON contains no Excel name field.
- Resolve a `product_reference_hits` ID only through the validated visual-RAG catalog. The catalog owns product name, product code, and 69 code; model text never overrides them.
- For an exact catalog hit, compare sales rows by 69 code first and product code second. A candidate catalog hit stays candidate even if its catalog identifiers exist in Excel.
- A complete 13-digit 69 code must pass EAN-13 validation. Brand, red/silver color, box shape, QR code, batch/date printing, or one generic claim cannot uniquely identify a SKU.
- Preserve code-read names byte-for-text; never invent a normalized display name.
- Use exact only for a unique high-signal package/SKU/specification correspondence.
- Use candidate for maintained alias/series rules or several plausible source names.
- Use unmatched when no source name is supported.
- Distributor-level sales supports only customer/period/product movement and cannot prove a contract store. Preserve the warning in every row.

## Maintained campaign aliases

These aliases narrow candidates but do not by themselves prove exact identity:

- visible SP-1/`减少软垢` → Excel SP1 series and/or `倍养护牙膏100g`;
- visible SE-1/`双重因子` → `专研清新美白牙膏100g`;
- visible SP-2 terms → code-read SP2 names, narrowed by `氨基酸/温和护龈` or `植萃/益清新`;
- visible SP-4 terms → code-read SP4 names, narrowed by `清新/薄荷` or `美白`;
- visible green-tea/极光白 combination → code-read `极光白酵素` name as a candidate;
- visible `3+2` → the code-read `3+2` bundle; it is exact only when unique and no other candidate rule is needed.

## Promotion

- Explicit signal kinds are special price, old/new price, discount, gift, multi-buy, 1+1, 3+2, and value pack.
- Normal prices stay in `visible_prices`. Without an explicit signal, output `未识别到明确促销...`, not `有促销`.
- Code may derive a bundle signal from AI-extracted visible packaging text containing 1+1, 3+2, 超值装, 特享装, or 量贩装.

## Outcome

- A failed store receives zero supported amount and a deterministic supplement list.
- `pass` requires all contract stores to pass; `partial_pass` requires at least one pass and one supplement; no supported store or an unreadable contract is `human_review`.
