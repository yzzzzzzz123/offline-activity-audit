"""Explicit model doubles for testing the content-routed workflow, never production inputs."""
from copy import deepcopy
import io
from pathlib import Path
import sys
from zipfile import ZipFile

from PIL import Image

from audit_core.pdf_policy import FLAGS, MATERIALS, applicability, requirements, rules


def fixture_cli_command(arguments, *, candidates=None):
    """Exercise the real bundled CLI in a child with only its model replaced."""
    root = Path(__file__).resolve().parents[1]
    script = (
        "import runpy,sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        f"sys.path.insert(0, {str(root / 'skills/orchestrate-offline-audit/scripts')!r})\n"
        "from unittest.mock import patch\n"
        "from tests.pdf_test_support import PolicyProvider\n"
        f"with patch('audit_core.orchestrator._default_provider', return_value=PolicyProvider(candidates={candidates!r})):\n"
        f"    runpy.run_path({str(root / 'skills/orchestrate-offline-audit/scripts/run.py')!r}, run_name='__main__')\n"
    )
    return [sys.executable, "-B", "-c", script, *arguments]


def image_bytes(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(output, format="PNG")
    return output.getvalue()


def archive_bytes(files=None):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for name, value in (files or {"材料.png": image_bytes()}).items():
            archive.writestr(name, value)
    return output.getvalue()


def bundle(root, name="资料.zip", files=None):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(archive_bytes(files))
    return path


def flags_for(scenario, **overrides):
    flags = dict.fromkeys(FLAGS, False)
    flags["uses_pos"] = scenario in {
        "personnel_incentive", "giveaway_promotion", "price_difference_support", "self_procured_gift_material", "pos_target_incentive"}
    flags.update(overrides)
    return flags


def classification(scenario, ids, *, missing=(), flags=None, candidates=None, extra=()):
    flags = flags if flags is not None else flags_for(scenario)
    candidates = [scenario] if candidates is None else candidates
    matches = []
    for compared in MATERIALS:
        compared_flags = flags if compared == scenario else flags_for(compared)
        missing_ids = set(missing) if compared in candidates else {r.id for r in requirements(compared)}
        materials = [{"id": r.id, "state": "not_applicable" if applicability(r.when, compared, compared_flags) is False
                      else "unclear" if applicability(r.when, compared, compared_flags) is None
                      else "missing" if r.id in missing_ids else "present",
                      "reason": f"未提供{r.id}的对应资料" if r.id in missing_ids else "本次原始资料可见对应事实",
                      "source_ids": ids if r.id not in missing_ids and applicability(r.when, compared, compared_flags) is not False else []}
                     for r in requirements(compared)]
        extras = deepcopy(list(extra)) if compared in candidates else []
        covered = {uid for m in materials if m["state"] in {"present", "unclear"} for uid in m["source_ids"]}
        covered.update(uid for item in extras for uid in item["source_ids"])
        remaining = [uid for uid in ids if uid not in covered]
        if remaining:
            extras.append({"description": "其他已提交资料", "reason": "本次资料不能归入此类清单", "source_ids": remaining})
        exact = not extras and all(m["state"] in {"present", "not_applicable"} for m in materials)
        matches.append({"scenario": compared, "supported": exact,
                        "reason": "本次资料组合支持此类型" if compared in candidates else "本次资料组合不支持此类型",
                        "source_ids": ids if compared in candidates else [],
                        "flags": compared_flags, "materials": materials, "extra_materials": extras})
    candidates = [m["scenario"] for m in matches if m["supported"]]
    selected = next((m for m in matches if m["scenario"] == candidates[0]), None) if len(candidates) == 1 else None
    return {"candidate_scenarios": candidates, "reason": "按PDF逐类比对后，原始资料组合支持候选类型。" if candidates else "逐类比对后，本次资料组合不能对应PDF八类。",
            "source_ids": ids, "flags": selected["flags"] if selected else flags,
            "materials": selected["materials"] if selected else [], "material_matches": matches}


def unknown_checks(scenario, flags):
    return {"checks": [{"rule_id": r.id,
                        "status": "not_applicable" if applicability(r.when, scenario, flags) is False else "unknown",
                        "reason": "不适用PDF条件" if applicability(r.when, scenario, flags) is False else f"未提供{r.text.split('。')[0]}的核验依据",
                        "source_ids": [], "comparisons": []} for r in rules(scenario)]}


class PolicyProvider:
    def __init__(self, scenarios=("poster_material",), *, missing=None, flags=None, candidates=None, extra=None, mutate=None):
        self.scenarios = scenarios
        self.missing = missing or {}
        self.flags = flags or {}
        self.candidates = candidates or {}
        self.extra = extra or {}
        self.mutate = mutate
        self.calls = []
        self.temporary_roots = []

    def __call__(self, case, temporary):
        self.calls.append(deepcopy(case))
        self.temporary_roots.append(Path(temporary))
        index = int(case["archive_id"][1:]) - 1
        scenario = self.scenarios[index % len(self.scenarios)]
        if case["kind"] == "pdf_material_classification":
            documents = [{"unit_id": u["unit_id"], "roles": ["submitted"],
                          "facts": ["本次资料记载费用用途和统一结算单，客户印章可见"] if u["image"] else u["native_facts"],
                          "limitations": u["limitations"]} for u in case["units"]]
            value = {"documents": documents,
                     "classification": classification(scenario, [d["unit_id"] for d in documents],
                         missing=self.missing.get(index, ()), flags=self.flags.get(index), candidates=self.candidates.get(index),
                         extra=self.extra.get(index, ()))}
        else:
            value = unknown_checks(scenario, case["flags"])
            for check in value["checks"]:
                if check["rule_id"] in {"settlement_template_seal", "entry_agreement"}:
                    check.update(status="pass", reason="统一模板和客户签章可见", source_ids=[case["documents"][0]["unit_id"]])
        if self.mutate:
            self.mutate(case, value)
        return value
