# Observation manifest and selection rules

Use one manifest item per direct product folder. Inspect the images before writing the item. The source directory is immutable input; neither `match` nor `apply` renames, moves, deletes, or edits it.

## Required fields

- `source_folder`: one direct child of `--source-root`; no nested or absolute path.
- `source_id`: stable lowercase identifier unique across the catalog.
- `brand`: visible brand used in the catalog.
- `observed_product_name`: packaging text, not the API or Excel wording.
- `observed_specification`: visible net content or pack size.
- `observed_variant`: visible fragrance, color, flavor, or other variant; use `null` when absent.
- `barcode_69`: the complete 13 digits printed with the barcode.
- `name_evidence_files`: one or more files where the observed name is readable.
- `barcode_evidence_files`: one or more files where the complete barcode digits are readable.

The optional `approved_product_code` is only for an already human-confirmed same-barcode candidate. It cannot introduce a code absent from the exact-barcode API response and it cannot override a name/category/specification conflict.

## Example shape

```json
{
  "schema_version": "1.0",
  "observations": [
    {
      "source_folder": "1",
      "source_id": "sample-001",
      "brand": "Example Brand",
      "observed_product_name": "Example hydrating body wash",
      "observed_specification": "500ml",
      "observed_variant": "Pear blossom",
      "barcode_69": "69xxxxxxxxxxx",
      "name_evidence_files": ["front.jpg"],
      "barcode_evidence_files": ["back.jpg"]
    }
  ]
}
```

Replace the placeholder barcode with an actually visible valid EAN-13 before execution.

## Candidate precedence

The script applies these rules in order:

1. exact returned barcode and valid non-empty three fields;
2. compatible category and visible measurement;
3. a uniquely present visible variant/fragrance;
4. the only candidate without an unobserved special-purpose, logistics, upgrade, channel, or spokesperson modifier;
5. a strong fuzzy-name lead with a safe margin;
6. otherwise unresolved.

Absence of a modifier is used only to keep a standard retail package from being silently mapped to a special candidate. It is not permission to infer packaging facts that are not visible.
