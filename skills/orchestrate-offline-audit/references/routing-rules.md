# ZIP routing rules

## Accepted combinations

Read one to ten ZIPs directly from `input/`:

- personnel only;
- display only;
- poster/material only;
- maintenance fee only;
- extra giveaway only;
- price difference only;
- POS target incentive only;
- entry fee only;
- self-procured gift material only;
- any combination containing at most one ZIP of each supported scenario.

Reject no ZIP, more than ten ZIPs, or a repeated scenario. Classification uses material shape first; a suggestive filename never repairs an ambiguous material class. The registered maintenance-fee scene is the narrow exception for incomplete mandatory roles: an explicit maintenance marker plus at least one POS/settlement visual routes the package so missing roles can be reported as blocking issues. The registered extra-giveaway scene similarly accepts its marked all-visual package so generic camera filenames can be classified from visible content and missing roles can become report issues. A formal run may select one submitted scenario with `--scenario`; all ZIPs are still checked for safe, unambiguous classification before the selected package is processed.

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

## Extra giveaway

Route an outer ZIP explicitly marked `额外搭赠` or `搭赠` as `giveaway_promotion` when it has no Excel or nested ZIP and contains at least one visual document. This shape is distinct from maintenance and other expense by its outer marker, distinct from personnel and promotional display by the absence of Excel/PDF pairing, and distinct from poster/material by the absence of a nested field-photo ZIP.

Bind every image/PDF exactly once to the neutral `visual_document` role because camera-export basenames may contain no business role. The visual stage classifies each source from its visible title/content as signed promotional contract, dealer-stamped settlement, system sales/delivery statement, store receipt, activity photo, support, or other. Deterministic source validation requires exact source coverage and rejects more than one contract, settlement, or system sales/delivery statement. Missing mandatory roles remain blocking report issues so an incomplete but unambiguous extra-giveaway package can still produce a precise resubmission list. Preserve nonvisual files as excluded; do not invent an Excel requirement or route the claim to `other_expense`, `maintenance_fee`, or `直营`.

## Price difference support

Route an outer ZIP explicitly marked `补差` or `价格补差` as `price_difference_support` when it contains exactly one visual `促销协议`/`促销合同`, exactly one visual settlement, at least one stamped POS/sales visual, zero or one Excel, and exactly one RAR named as photos/returns/field evidence. Missing Excel remains a blocking report issue; duplicate singleton candidates stop intake.

List and validate the RAR before extraction with the same path, link, member-count, member-size and total-expansion boundaries used for ZIP input. Bind every extracted image exactly once as an activity photo and reject non-image RAR members or duplicate basenames. Deterministic audit requires all activity stores—not a sample—to have an in-period image with visible date/address/time watermarks and the contract activity price. Use only the signed contract's explicit support unit amount; never substitute retail original-price minus activity-price.

## POS target incentive

Route an outer ZIP marked `POS激励达标`, `POS达标激励`, or `POS激励` as `pos_target_incentive` when it has no nested archive, at most one Excel, and at least one visual file. Bind at most one promotional contract and one settlement by basename; duplicate singleton candidates stop intake. Bind POS/sales/data visuals as stamped POS, and bind remaining visuals as photos, receipts, or other activity proof. Missing contract, settlement, Excel, POS, or activity proof remains a blocking report issue.

The recipient is the dealer. The signed contract—not the settlement—must establish approved strategic/special-channel eligibility, activity period and mechanic, eligible POS scope, target tiers, rates and cap. Deterministic code reads the unique period/store/sales summary sheet, reconciles every store and total against stamped POS, requires activity-period proof for the full-reduction activity, selects the highest reached contract tier, applies its rate and cap, and compares the dealer-stamped settlement claim.

## Entry fee / barcode fee

Route an outer ZIP marked `进场费` or `条码费` as `entry_fee` when it has no nested ZIP or Excel, exactly one RAR named as entry/shelf photos, and exactly one visual contract candidate named `产品推广协议`, `进场费合同`, `条码费合同`, or `进场合同`. Duplicate contract/RAR candidates stop intake.

List and validate the RAR before extraction, require image-only members and unique basenames, and bind each image as `shelf_photo`. Preserve the RAR parent directory as a routing-only `store_hint`; it never proves the photo's store. Bind outer visual files explicitly named as system deduction/entry proof to `system_deduction_proof`; missing proof remains a report issue. Deterministic audit uses the signed contract as authority for parties, channel, product rows, contracted stores, barcode fee and total, matches only visible photo watermarks to contract stores, screens duplicate bytes, and requires the contract's system deduction proof. A product-row barcode fee is summed once per product and is not multiplied by the printed store count unless the contract explicitly says it is per store.

## Safe extraction

## Customer self-procured gift material

Route an outer ZIP marked `自采赠品物料`, `自采赠品`, or `自采物料` as `self_procured_gift_material`. Require no nested ZIP/RAR, exactly one legacy `.xls` activity-return workbook, at most one modern `.xlsx`/`.xlsm` POS spreadsheet, exactly one named promotional contract, one settlement, one purchase invoice/receipt, at least one named stamped POS visual, and any payment records or additional visual representations.

Convert the legacy `.xls` through hidden read-only Microsoft Excel COM into the run-scoped temporary directory, resolve WPS `DISPIMG` relationships, and extract every embedded activity photo. Preserve each workbook row, store, period, and customer-code value only as routing hints; never treat those cells as visible photo evidence. Bind each extracted photo as `activity_photo` and require the model to read its own watermark and activity content. The activity-return workbook is never the POS electronic spreadsheet. Deterministic audit separately reads a modern POS spreadsheet when present, reconciles it to the stamped visual POS without double-counting alternate visual representations, checks all-store photo coverage, gift-rule quantity sufficiency, purchase/receipt/payment support, contract/settlement alignment, and amount arithmetic. A missing POS spreadsheet or any failed mandatory control blocks the entire claim.

## Safe extraction

Before writing any member, reject absolute paths, `..`, drive-qualified paths, NUL bytes, Windows-invalid names, symbolic links, encrypted members, duplicate/case-colliding destinations, more than 2,000 members, a member above 250 MiB, total expansion above 1 GiB, or a suspicious compression ratio. Extract into a new run-scoped temporary directory and remove it automatically at the end of the run.
