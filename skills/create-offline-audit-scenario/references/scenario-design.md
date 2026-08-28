# Scenario design contract

Create `references/scenario-manifest.json` inside every newly generated scenario Skill and validate it
against [scenario-manifest.schema.json](scenario-manifest.schema.json). The manifest is the normalized,
reviewable bridge between the representative ZIP, the user's business prompt, and code. Do not paste
the raw conversation or confidential document contents into it.

## 1. Inspect the representative package

Use `scripts/profile_scenario_zip.py` before extraction. Record only:

- source ZIP basename, byte size, and SHA-256;
- sanitized member paths, suffix counts, nested-archive shape, and role candidates;
- duplicate/case-collision, unsafe-path, encryption, size, and compression findings;
- representative document types and whether their visible contents support the proposed roles.

Extract only into a newly created temporary directory. Inspect every materially different source
family, not just filenames or the first page/photo. Clean up the directory at task end. Never commit
the production ZIP, extracted customer evidence, or personally identifying facts.

## 2. Normalize the business prompt

Resolve the following before code generation:

1. **Scope and object** — what reimbursement activity is in scope, what is explicitly excluded, and
   whether one result object represents a product, person, store, document, expense line, photo group,
   or another business unit.
2. **Evidence roles** — role ID, accepted file types, minimum/maximum count, nested source, filename
   hints, required/optional status, unique-binding rule, and excluded-file policy.
3. **Authority graph** — the business baseline, every allowed directional comparison, and every
   forbidden cross-link. A matching total never creates a missing identity or execution fact.
4. **AI stages** — which visual files each stage may see, which schema it returns, whether bounded
   repository reference images are allowed, how stages merge, and how every supplied source basename
   is accounted for. Spreadsheets and final decisions stay deterministic.
5. **Controls** — stable control ID, source/object, expected visible or deterministic fact, exact or
   unique-fuzzy comparison policy, severity/blocking behavior, confidence, error grouping, and exact
   resubmission action.
6. **Amounts and outcomes** — claim source, supported-amount formula, unit basis, caps, rounding,
   behavior when a control is blocked, and permitted conclusion values. Never infer a unit price by
   dividing a total unless the user explicitly defines that business rule.
7. **Output semantics** — Chinese label, sheet name, tab behavior, object count unit, full-result or
   error-only mode, summary/context rows, required human facts, prohibited engineering text, and
   mobile behavior.
8. **Dependencies** — deterministic parsers, shared product ledger, reference-image scope, image
   fingerprinting, or other project-owned assets. State why each dependency is needed.

If a required authority, evidence role, amount rule, or blocking condition cannot be established from
the user prompt and visible sample, do not invent it. Ask one concise business question before
activation.

## 3. Generate the scenario Skill

Minimum files:

```text
skills/audit-<scenario>/
|-- SKILL.md
|-- agents/openai.yaml
`-- references/
    |-- audit-rules.md
    |-- evidence.schema.json
    `-- scenario-manifest.json
```

`SKILL.md` contains the evidence chain, trust boundary, deterministic responsibilities, output
contract, and parent command. `audit-rules.md` contains the detailed authority, matching, amount,
decision, grouping, and resubmission rules. The evidence schema contains visible facts only, uses a
top-level exact `scenario` constant, closes objects with `additionalProperties: false`, and makes
source basenames explicit enough for post-validation.

Add one schema per additional model stage only when required. For every stage, implement a source-
coverage validator that rejects invented basenames, duplicate role bindings, and silently ignored
required sources.

Write `agents/openai.yaml` as strict UTF-8, quote all strings, and make `default_prompt` mention the
new Skill as `$audit-<scenario>`. Re-read every generated text/JSON/YAML file as strict UTF-8 before
activation.

## 4. Prevent sample overfitting

Search generated instructions, handlers, tests, and UI copy for sample-only customer names, archive
IDs, amounts, dates, product names, item counts, store counts, and filenames. A test fixture may carry
fixture values; reusable code and default copy must derive them from the case, evidence, or manifest.
Keep the source ZIP SHA-256 only as provenance, never as a runtime allowlist.
