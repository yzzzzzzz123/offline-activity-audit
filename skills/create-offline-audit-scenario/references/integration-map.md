# Main-flow integration map

A new Skill directory is not a registered scenario. Activation is complete only when the same
scenario ID is supported end to end by the one formal command.

## Required activation points

1. **Safe classification and role binding — `audit_core/archive_input.py`**
   - add a structural classifier that cannot overlap an existing scene;
   - bind every required role exactly once and preserve excluded filenames;
   - validate nested archives and unique image basenames with the same safety limits;
   - update supported scene order, duplicate labels, accepted ZIP maximum, and selected-scene errors.

2. **Visual extraction — `audit_core/codex_runner.py`**
   - register the Skill directory and evidence schema;
   - implement the minimum justified single- or multi-stage prompt graph;
   - copy only allowed visual sources into the isolated model workspace;
   - validate JSON plus complete source coverage and reject invented or silently omitted basenames;
   - keep spreadsheets, prior output, caches, final decisions, and unrelated sources out of prompts.

3. **Deterministic handler — `audit_core/<scenario>.py` and `audit_core/orchestrator.py`**
   - implement comparisons, normalization, arithmetic, grouping, supported amount, conclusion, and
     resubmission from evidence and deterministic sources;
   - register evidence schema, order, CLI choice, and handler dispatch;
   - keep sample values out of reusable strings and formulas.

4. **Result contract — `contracts/audit-result.schema.json`**
   - add the scenario enum and a closed conditional result branch;
   - require scene-specific audit data and forbid another scene's payload;
   - add positive and negative Schema tests.

5. **Six-column intermediate — `audit_core/report.py`**
   - create one renderer and one exact sheet name;
   - preserve source order and original values, use no formulas, and emit deterministic confidence,
     conclusion, impact, and resubmission text;
   - register sheet ordering and verification.

6. **Canonical HTML — `audit_core/html_report.py` and
   `skills/orchestrate-offline-audit/assets/canban-audit-shell.html`**
   - register sheet/scenario metadata, row sections, object counts, and full-result or error-only mode;
   - add a real scenario entry and subinterface to the existing single HTML;
   - deliberately bump the template version and expected static fingerprint, retain all fixed controls,
     and add desktop/mobile and no-horizontal-scroll coverage;
   - never bypass the shell fingerprint or rebuild a second page.

7. **Project contracts and discovery**
   - update `skills/orchestrate-offline-audit/SKILL.md`, its routing rules and UI metadata;
   - update root `AGENTS.md`, `README.md`, and `input/README.md` with the new registered scene,
     material shape, selection ID, evidence boundary, and output semantics;
   - update this creator Skill's `current-scenarios.md` so the next onboarding checks collisions against
     the newly registered scene.

8. **Tests and real fixture**
   - routing: valid, missing role, duplicate role, ambiguous scene, duplicate scene, unsafe/nested ZIP;
   - extraction: evidence-schema validation, allowed inputs, invented basename, and complete coverage;
   - deterministic rules: every blocking control, exact/fuzzy boundary, amount cap/rounding, grouping;
   - output: result schema, renderer order, selected-only run, combined run, HTML entry, counts,
     full/error-only behavior, responsive layout, and self-containment;
   - run the formal command with `--scenario <id>` against the representative ZIP after isolating the
     intended runtime input set. Do not publish or commit customer evidence.

## Activation gate

Run all of the following from the project root:

```powershell
py -3 skills/create-offline-audit-scenario/scripts/check_scenario_integration.py --scenario <id> --skill <skill> --audit-module <module.py> --sheet-name <sheet>
py -3 C:\Users\EDY\.codex\skills\.system\skill-creator\scripts\quick_validate.py skills/<skill>
py -3 -B -m unittest discover -s tests -v
py -3 -B -m compileall -q audit_core skills
git diff --check
git status --short
```

Also strict-decode every Skill text file as UTF-8 and parse every JSON file. Run the formal fixture
only when the execution path is fully implemented; a failed run must publish nothing and must remove
temporary extraction, model, workbook, and page files.

The integration checker is a completeness guard, not proof of business correctness. Do not satisfy it
with dead strings or documentation-only mentions; inspect and test the actual dispatch and behavior.
