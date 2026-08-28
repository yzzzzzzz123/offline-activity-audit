# Maintenance-fee audit rules

## Scope and audit object

The audit object is one maintenance-fee claim package. `maintenance_fee` is an independent registered
scenario. It is not `直营`, `personnel_incentive`, or the special-approval `other_expense` channel.
The archive name may route a structurally compatible package, but the fee nature must also be visible
in the signed promotional contract, fee-specific support, or settlement.

## Required evidence

All of the following are blocking unless the rule explicitly states a conditional requirement:

1. **Stamped POS data**: at least one legible visual POS statement with the dealer seal. It must show
   enough product/detail and total fields to compare with the electronic POS sheet.
2. **POS spreadsheet**: exactly one readable `.xlsx` or `.xlsm` containing product-level POS rows,
   quantities, sales amounts, and a reproducible total. AI must not see this file.
3. **Signed promotional contract**: exactly one visual contract showing both parties' execution,
   activity period, maintenance-fee scope, eligible POS/product scope, calculation method, rate, and
   any amount ceiling that governs the claim.
4. **Stamped settlement**: exactly one company-template settlement, dealer-stamped, that identifies
   the dealer, fee item, activity period, POS basis, calculation method, and claimed amount.
5. **Fee-specific support**: at least one agreement, related document, or execution record that
   proves the particular maintenance activity. When the contract or agreement requires on-site
   execution, activity photos are required and must visibly fall within the activity period.

An absent role creates a grouped issue in the result. Duplicate candidates for a singleton role are
ambiguous and fail intake before AI extraction.

## Visible-fact extraction

Return every supplied visual source once with the role assigned by deterministic intake. Do not use
the filename as visible content. Preserve document titles, parties, dealer/customer name, period,
fee wording, calculation method, rate, quantities, sales amounts, claimed amounts, seals/signatures,
and every useful POS detail row independently. Use `null`, `unclear`, or a limitation instead of
copying a value from another source.

For the POS image, a complete product row needs a visible product name plus its own quantity and sales
amount. Preserve printed totals separately. A dealer seal is `visible` only when the seal itself can
be seen. For the settlement, `company_template_visible` requires recognizable company-template
branding or structure; a generic table titled `结算单` is not enough by itself. A promotional contract
is signed only when execution marks for the contracting parties are visible.

## Deterministic controls

Evaluate and group the following controls. Later evidence cannot silently repair an earlier missing
authority source.

- `required_materials`: every mandatory role is present and singleton roles are unique.
- `fee_nature`: the authoritative documents describe maintenance fees. Wording that expressly
  describes personnel incentives, displays, material production, other expenses, or direct-operation
  fees is a conflict and blocks this scenario.
- `pos_visual_seal`: the submitted visual POS data carries a visible dealer seal.
- `pos_spreadsheet`: the electronic POS file is readable, contains item rows, and has no external
  formula dependency. Derived totals come only from its own cells.
- `pos_correspondence`: POS visual and spreadsheet totals match exactly for quantity and to RMB 0.01
  for sales amount. When both contain detail rows, each visual row must map uniquely to one spreadsheet
  product and its quantity and amount must match. A total match never repairs a row conflict.
- `promotional_contract`: the contract is signed and states parties, period, maintenance-fee scope,
  eligible POS scope, calculation method, rate, and any applicable ceiling.
- `settlement`: the company-template settlement is dealer-stamped and itemizes the fee, calculation
  method, POS basis, and claimed amount.
- `party_alignment`: dealer/customer identity is uniquely compatible across POS, contract, settlement,
  and spreadsheet when those fields are present. A generic chain name is insufficient when it could
  refer to several legal parties.
- `period_alignment`: POS and execution evidence fall within the contract period, and the settlement
  period does not exceed it.
- `type_specific_support`: the concrete maintenance activity has at least one independent supporting
  agreement, related document, or execution record. Required activity photos must be in-period.
- `amount_recalculation`: apply only the contract's explicit eligible POS scope and explicit formula.
  The recalculated amount, after contract ceiling if any, must equal the stamped settlement claim to
  RMB 0.01. Never infer a rate or unit price by division.

## Amount and decision

The claimed amount comes from the dealer-stamped settlement. Use `ROUND_HALF_UP` to two decimal places.
If any blocking control fails or the contractual amount cannot be deterministically recomputed, the
suggested approved amount is `0.00`, the full claim is temporarily held, and the conclusion is
`human_review`. If all controls pass and the recomputed amount equals the claim, the suggested approved
amount equals the claim, the held amount is `0.00`, and the conclusion is `pass`.

## Error grouping and resubmission

Group one issue per failed control rather than one issue per missing field. Name every affected source,
state the visible or deterministic observation, explain the blocking effect, and request the exact
replacement or missing file. Do not expose model, prompt, Schema, temporary path, confidence internals,
or implementation terminology in customer-facing facts; the six-column result may show only the
business confidence label `高/中/低`.
