# ZIP routing rules

## Input contract

- Read only `.zip` files directly inside the selected input directory.
- Require exactly two archives.
- Require one `personnel_incentive` result and one `promotional_display` result.
- Reject encrypted members, symbolic links, absolute paths, `..` traversal, drive-qualified paths, Windows-invalid names, case-colliding targets, oversized members, excessive file counts, excessive expansion, and suspicious compression ratios.

## Personnel incentive route

Prefer explicit signals in the archive filename or member names:

- `人员激励` is the strongest signal.
- `结算单` and `红包`/`转账` reinforce the route.
- A typical package contains exactly one Excel workbook, no PDF, one settlement image, and one or more transfer images.

After extraction, require exactly one Excel workbook and exactly one image whose name identifies it as the final settlement sheet. Treat every remaining image as transfer evidence. Route the generated case to `audit-personnel-incentive`.

## Promotional display route

Prefer explicit signals in the archive filename or member names:

- `堆头` or `陈列` is the strongest signal.
- `合同` reinforces the route.
- A typical package contains exactly one PDF contract, exactly one Excel workbook, and multiple display photos.

After extraction, require exactly one PDF, exactly one Excel workbook, and at least one image. When photos do not share one directory, copy them into a normalized flat photo directory without changing the originals. Route the generated case to `audit-promotional-display`.

## Ambiguity

Fail closed when scores tie, neither score reaches the minimum, both archives resolve to the same scenario, or a required file class has zero or multiple candidates. Report filenames and counts; never select the closest-looking file silently.
