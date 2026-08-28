---
name: new-product-onboarding-rag-workflow
description: Onboard new packaged-product reference-image folders into the offline-activity-audit visual RAG catalog by extracting visible product names and valid EAN-13 barcodes, resolving Canban API candidates by exact barcode plus unique fuzzy name evidence, importing immutable image copies, and reconciling the result to an inventory workbook. Use for new product samples; do not use to identify field-audit evidence or to repair an existing catalog identity by guesswork.
---

# New Product Onboarding RAG Workflow

Use the repository project root as the working directory. Read its `AGENTS.md` before acting.

The formal catalog, schemas, maintenance scripts, controlled product directories, and reference
images live together at `shared/canban-product-multimodal-knowledge-base`. This project-level RAG is
shared by every audit scenario that needs product identity; do not create a scenario-local copy.

## Identity gate

1. Inspect every direct sample folder and its relevant views. Record only a product name, specification or variant, and complete barcode that are actually visible. The barcode must start with `69` and pass EAN-13 validation.
2. Finish the visual observation manifest before opening an authority workbook. Excel is an independent reconciliation source, not a way to fill unreadable packaging.
3. Query the Canban mapping API with `barcode` only. Discard every returned row whose `barcode` is not exactly the observed barcode or whose three identity fields are empty or invalid.
4. Within the exact-barcode set, require one fuzzy-compatible product-name candidate; character-for-character name equality is not required. Visible variant or fragrance text may disambiguate. Unseen modifiers such as `套盒专用`, `箱规`, `升级配方`, `代言人`, international labels, or customer-specific versions cannot win a tie.
5. Fail closed when the barcode is missing or invalid, the visible name conflicts, or multiple candidates remain. Keep the folder outside formal RAG and request clearer name/barcode evidence; never take the API's first row.

Read [manifest-and-selection.md](references/manifest-and-selection.md) when creating or reviewing an observation manifest. Validate it against [observation-manifest.schema.json](references/observation-manifest.schema.json).

## Deterministic workflow

Create the manifest, then generate a credential-free plan. API credentials are read only from `CANBAN_API_ACCESS_KEY` and `CANBAN_API_SECRET_KEY` in the process or Windows user environment.

```powershell
py -3 -B skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py match `
  --source-root '<直接包含逐商品文件夹的目录>' `
  --observations '<视觉观察清单.json>' `
  --output '<接入计划.json>'
```

Review `selection_status`, `selection_basis`, every exact-barcode candidate, source hashes, and final three fields. An unresolved plan is not applicable.

Run an apply dry-run first. It checks catalog freshness, source immutability, collisions, directory names, schema, and the full proposed catalog without writing.

```powershell
py -3 -B skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py apply `
  --plan-file '<接入计划.json>'
```

Only after the product selections and write scope are authorized, repeat with `--confirm`. The confirmed path copies each product independently into `products/<69码>__<接口产品名称>__<产品编码>/`, preserves the source folders, atomically replaces the catalog, and rolls back script-created directories if validation fails.

```powershell
py -3 -B skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py apply `
  --plan-file '<接入计划.json>' `
  --confirm
```

## Workbook reconciliation

After import, compare the validated catalog to the stock-eligible rows of the authority workbook. Product code and 69 code are strict; the product name only needs to be fuzzy-compatible for that same strict product and never requires character-for-character equality. The default exclusion marker is the exact text `无库存，未发出` in the tracking-number column.

```powershell
py -3 -B skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py reconcile `
  --workbook '<权威清单.xlsx>' `
  --output '<对账结果.json>'
```

Do not declare completion unless `passed=true`, the expected stock count equals the catalog count, `missing_products`, `catalog_extra_products`, `field_mismatches`, and duplicate-code lists are empty, and `load_product_rag` verifies every image path and SHA-256. Update any maintained README counts, then run relevant tests, JSON/Skill validation, `git diff --check`, and `git status --short`.
