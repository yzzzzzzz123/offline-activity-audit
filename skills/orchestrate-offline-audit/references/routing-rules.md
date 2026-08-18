# ZIP routing rules

## Accepted combinations

Read one or two ZIPs directly from `input/`:

- personnel only;
- display only;
- one personnel plus one display.

Reject no ZIP, more than two ZIPs, or a repeated scenario. Classification uses material shape first; a suggestive filename never repairs a missing material class.

## Personnel incentive

Require exactly one `.xlsx`/`.xlsm`, no PDF, at least two images, exactly one image whose basename identifies it as `结算单` or `结算表`, and at least one image whose basename identifies transfer/red-packet/payment evidence. Every non-settlement image is routed as a transfer screenshot.

If the package has personnel-like shape but the settlement or transfer role is not unique, report the candidate counts and stop.

## Promotional display

Require exactly one `.pdf`, exactly one `.xlsx`/`.xlsm`, and at least one image. Route the PDF as the contract, the Excel as sales support, and every image as a field photo. Nested directories are allowed, but image basenames must remain unique because evidence JSON binds by basename.

## Safe extraction

Before writing any member, reject absolute paths, `..`, drive-qualified paths, NUL bytes, Windows-invalid names, symbolic links, encrypted members, duplicate/case-colliding destinations, more than 2,000 members, a member above 250 MiB, total expansion above 1 GiB, or a suspicious compression ratio. Extract into a new run-scoped temporary directory and remove it automatically at the end of the run.
