# ZIP routing rules

## Accepted combinations

Read one to five ZIPs directly from `input/`:

- personnel only;
- display only;
- poster/material only;
- maintenance fee only;
- any combination containing at most one ZIP of each supported scenario.

Reject no ZIP, more than five ZIPs, or a repeated scenario. Classification uses material shape first; a suggestive filename never repairs an ambiguous material class. The registered maintenance-fee scene is the narrow exception for incomplete mandatory roles: an explicit maintenance marker plus at least one POS/settlement visual routes the package so missing roles can be reported as blocking issues. A formal run may select one submitted scenario with `--scenario`; all ZIPs are still checked for safe, unambiguous classification before the selected package is processed.

## Personnel incentive

Require exactly one `.xlsx`/`.xlsm`, no PDF, at least two images, exactly one image whose basename identifies it as `结算单` or `结算表`, and at least one image whose basename identifies transfer/red-packet/payment evidence. Every non-settlement image is routed as a transfer screenshot.

If the package has personnel-like shape but the settlement or transfer role is not unique, report the candidate counts and stop.

## Promotional display

Require exactly one `.pdf`, exactly one `.xlsx`/`.xlsm`, and at least one image. Route the PDF as the contract, the Excel as sales support, and every image as a field photo. Nested directories are allowed, but image basenames must remain unique because evidence JSON binds by basename.

## Poster/material production

Require no sales Excel, exactly one image basename identifying a promotional contract, exactly one image basename identifying an invoice or receipt, exactly one image basename identifying a settlement form, and exactly one nested ZIP basename identifying field returns, field photos, or watermarked photos. The outer ZIP basename must identify poster, material, or display-prop production so this image-led shape is not confused with another scenario.

Safely extract the nested photo ZIP with the same limits as the outer package. Route every nested image as a finished-product field photo and require unique basenames. Route explicitly named contract attachments, store lists, quotations, or specifications as contract attachments. Preserve remaining files such as POS evidence as excluded filenames, but do not send them to the poster/material visual pass and do not create audit errors from them.

## Other expense

Require an outer ZIP marker `其他`, no sales Excel, no nested ZIP, exactly one visual document named as a promotional contract, exactly one visual document named as a settlement form, and at least one independent agreement, contract, invoice, receipt, or related file. Require at least four visual documents in total. This shape remains distinct from poster/material because it has no nested field-photo ZIP and may contain a PDF ticket or multiple support agreements.

Bind every image/PDF basename exactly once as promotional contract, settlement, supporting document, invoice/receipt, activity photo, POS data, or special-approval candidate. Preserve non-visual files as excluded filenames. The outer marker only routes the package: deterministic audit must reject `其他费用` when visible expense descriptions belong to an established category, and must require independent special approval when the fee is genuinely new.

## Maintenance fee

Route this registered scene as `maintenance_fee`. Require an outer ZIP marker `维护费用` or `维护费`, no nested ZIP, at least one visual document, and at least one basename identifying POS data or a settlement. This marker may distinguish the scene from a structurally incomplete personnel package, but it never proves that the document body describes maintenance fees.

Bind every visual file exactly once. POS/销售数据 candidates become stamped-POS sources; a maximum of one `促销合同` candidate becomes the signed promotional contract; a maximum of one `结算单`/`结算表` candidate becomes the settlement; remaining agreements, related documents, maintenance contracts, and activity/field photos become fee-specific support. Accept zero or one Excel as the POS spreadsheet so a missing electronic sheet becomes a reportable audit issue; reject two or more Excel files because the authority source cannot be uniquely bound. Preserve other files as explicitly accounted sources or excluded filenames. Do not route a maintenance-fee package to `other_expense`, `personnel_incentive`, or a `直营` scenario.

## Safe extraction

Before writing any member, reject absolute paths, `..`, drive-qualified paths, NUL bytes, Windows-invalid names, symbolic links, encrypted members, duplicate/case-colliding destinations, more than 2,000 members, a member above 250 MiB, total expansion above 1 GiB, or a suspicious compression ratio. Extract into a new run-scoped temporary directory and remove it automatically at the end of the run.
