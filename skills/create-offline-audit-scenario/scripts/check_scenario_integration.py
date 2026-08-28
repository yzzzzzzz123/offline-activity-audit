from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCENARIO_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
SKILL_PATTERN = re.compile(r"^audit-[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class Check:
    name: str
    ok: bool
    path: str
    detail: str


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"不是严格 UTF-8：{path}（{exc}）") from exc


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_utf8(path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 无效：{path}（{exc}）") from exc


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开始")
    try:
        header, _body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter 没有闭合") from exc
    values: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"无法解析 frontmatter 行：{line!r}")
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def _mention_check(
    checks: list[Check],
    *,
    name: str,
    path: Path,
    needles: tuple[str, ...],
) -> None:
    if not path.is_file():
        checks.append(Check(name, False, str(path), "文件不存在"))
        return
    try:
        text = _read_utf8(path)
    except ValueError as exc:
        checks.append(Check(name, False, str(path), str(exc)))
        return
    missing = [needle for needle in needles if needle not in text]
    checks.append(
        Check(
            name,
            not missing,
            str(path),
            "已登记" if not missing else "缺少：" + "、".join(missing),
        )
    )


def run_checks(
    project_root: str | Path,
    *,
    scenario: str,
    skill_name: str,
    audit_module: str,
    sheet_name: str,
) -> list[Check]:
    root = Path(project_root).resolve()
    checks: list[Check] = []
    if not root.is_dir():
        return [Check("project_root", False, str(root), "项目根目录不存在")]
    if not SCENARIO_PATTERN.fullmatch(scenario):
        checks.append(Check("scenario_id", False, scenario, "必须是 snake_case"))
    else:
        checks.append(Check("scenario_id", True, scenario, "格式正确"))
    if not SKILL_PATTERN.fullmatch(skill_name):
        checks.append(Check("skill_name", False, skill_name, "必须是 audit-kebab-case"))
    else:
        checks.append(Check("skill_name", True, skill_name, "格式正确"))
    if not sheet_name or len(sheet_name) > 31:
        checks.append(Check("sheet_name", False, sheet_name, "工作表名必须为 1～31 个字符"))
    else:
        checks.append(Check("sheet_name", True, sheet_name, "格式正确"))

    module_path = (root / "audit_core" / audit_module).resolve()
    if not _inside(module_path, root / "audit_core") or module_path.suffix != ".py":
        checks.append(
            Check("audit_module_path", False, str(module_path), "模块必须位于 audit_core/ 内")
        )

    skill_dir = (root / "skills" / skill_name).resolve()
    required_skill_files = {
        "skill_entry": skill_dir / "SKILL.md",
        "skill_rules": skill_dir / "references" / "audit-rules.md",
        "evidence_schema": skill_dir / "references" / "evidence.schema.json",
        "scenario_manifest": skill_dir / "references" / "scenario-manifest.json",
        "agent_metadata": skill_dir / "agents" / "openai.yaml",
        "audit_module": module_path,
    }
    for name, path in required_skill_files.items():
        checks.append(
            Check(name, path.is_file(), str(path), "存在" if path.is_file() else "文件不存在")
        )

    skill_path = required_skill_files["skill_entry"]
    if skill_path.is_file():
        try:
            text = _read_utf8(skill_path)
            metadata = _frontmatter(text)
            problems = []
            if metadata.get("name") != skill_name:
                problems.append(f"name={metadata.get('name')!r}")
            if not metadata.get("description"):
                problems.append("description 为空")
            checks.append(
                Check(
                    "skill_frontmatter",
                    not problems,
                    str(skill_path),
                    "正确" if not problems else "；".join(problems),
                )
            )
        except ValueError as exc:
            checks.append(Check("skill_frontmatter", False, str(skill_path), str(exc)))

    evidence_path = required_skill_files["evidence_schema"]
    if evidence_path.is_file():
        try:
            evidence_schema = _read_json(evidence_path)
            actual = (
                evidence_schema.get("properties", {})
                .get("scenario", {})
                .get("const")
            )
            checks.append(
                Check(
                    "evidence_scenario_const",
                    actual == scenario,
                    str(evidence_path),
                    f"const={actual!r}",
                )
            )
            Draft202012Validator.check_schema(evidence_schema)
            checks.append(Check("evidence_schema_meta", True, str(evidence_path), "Schema 有效"))
        except Exception as exc:  # jsonschema reports several precise exception types
            checks.append(Check("evidence_schema_meta", False, str(evidence_path), str(exc)))

    manifest_path = required_skill_files["scenario_manifest"]
    manifest_schema_path = (
        root
        / "skills"
        / "create-offline-audit-scenario"
        / "references"
        / "scenario-manifest.schema.json"
    )
    if manifest_path.is_file() and manifest_schema_path.is_file():
        try:
            manifest = _read_json(manifest_path)
            manifest_schema = _read_json(manifest_schema_path)
            validator = Draft202012Validator(manifest_schema)
            errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
            semantic = []
            if manifest.get("scenario_id") != scenario:
                semantic.append("scenario_id 不一致")
            if manifest.get("skill_name") != skill_name:
                semantic.append("skill_name 不一致")
            if manifest.get("output", {}).get("sheet_name") != sheet_name:
                semantic.append("sheet_name 不一致")
            detail = [error.message for error in errors[:8]] + semantic
            checks.append(
                Check(
                    "scenario_manifest_schema",
                    not detail,
                    str(manifest_path),
                    "有效" if not detail else "；".join(detail),
                )
            )
        except ValueError as exc:
            checks.append(Check("scenario_manifest_schema", False, str(manifest_path), str(exc)))
    elif manifest_path.is_file():
        checks.append(
            Check(
                "scenario_manifest_schema",
                False,
                str(manifest_schema_path),
                "生成器 manifest Schema 不存在",
            )
        )

    agent_path = required_skill_files["agent_metadata"]
    if agent_path.is_file():
        try:
            agent_text = _read_utf8(agent_path)
            checks.append(
                Check(
                    "agent_default_prompt",
                    f"${skill_name}" in agent_text,
                    str(agent_path),
                    "包含 Skill 调用" if f"${skill_name}" in agent_text else "default_prompt 未引用 Skill",
                )
            )
        except ValueError as exc:
            checks.append(Check("agent_default_prompt", False, str(agent_path), str(exc)))

    for text_name in ("skill_rules", "skill_entry", "agent_metadata"):
        path = required_skill_files[text_name]
        if not path.is_file():
            continue
        try:
            _read_utf8(path)
            checks.append(Check(f"utf8_{text_name}", True, str(path), "严格 UTF-8"))
        except ValueError as exc:
            checks.append(Check(f"utf8_{text_name}", False, str(path), str(exc)))

    if module_path.is_file():
        _mention_check(
            checks,
            name="audit_handler",
            path=module_path,
            needles=(scenario, "def audit_"),
        )

    static_mentions = (
        ("archive_routing", root / "audit_core" / "archive_input.py", (scenario,)),
        ("model_extraction", root / "audit_core" / "codex_runner.py", (scenario, skill_name)),
        ("orchestrator_dispatch", root / "audit_core" / "orchestrator.py", (scenario, module_path.stem)),
        ("result_contract", root / "contracts" / "audit-result.schema.json", (scenario,)),
        ("workbook_renderer", root / "audit_core" / "report.py", (scenario, sheet_name)),
        ("html_projection", root / "audit_core" / "html_report.py", (scenario, sheet_name)),
        (
            "canonical_shell",
            root / "skills" / "orchestrate-offline-audit" / "assets" / "canban-audit-shell.html",
            (scenario,),
        ),
        (
            "parent_skill",
            root / "skills" / "orchestrate-offline-audit" / "SKILL.md",
            (scenario, skill_name),
        ),
        (
            "routing_rules",
            root / "skills" / "orchestrate-offline-audit" / "references" / "routing-rules.md",
            (scenario,),
        ),
        ("project_agents", root / "AGENTS.md", (scenario, skill_name)),
        ("project_readme", root / "README.md", (scenario,)),
        ("input_readme", root / "input" / "README.md", (scenario,)),
        (
            "creator_collision_baseline",
            root
            / "skills"
            / "create-offline-audit-scenario"
            / "references"
            / "current-scenarios.md",
            (scenario, skill_name),
        ),
    )
    for name, path, needles in static_mentions:
        _mention_check(checks, name=name, path=path, needles=needles)

    test_files = sorted((root / "tests").glob("test_*.py"))
    mentioning_tests: list[str] = []
    for path in test_files:
        try:
            if scenario in _read_utf8(path):
                mentioning_tests.append(path.name)
        except ValueError:
            continue
    checks.append(
        Check(
            "scenario_tests",
            bool(mentioning_tests),
            str(root / "tests"),
            "、".join(mentioning_tests) if mentioning_tests else "没有测试引用该场景 ID",
        )
    )
    return checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查新核销场景是否已从 Skill 到唯一 HTML 完整接入主流程。"
    )
    parser.add_argument("--scenario", required=True, help="snake_case 场景 ID")
    parser.add_argument("--skill", required=True, help="audit-kebab-case Skill 目录名")
    parser.add_argument("--audit-module", required=True, help="audit_core 下的确定性处理模块文件")
    parser.add_argument("--sheet-name", required=True, help="六列表中该场景的工作表名")
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parents[3]),
        help="项目根目录；默认从本脚本位置推导",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    checks = run_checks(
        args.project_root,
        scenario=args.scenario,
        skill_name=args.skill,
        audit_module=args.audit_module,
        sheet_name=args.sheet_name,
    )
    failures = [check for check in checks if not check.ok]
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failures,
                    "scenario": args.scenario,
                    "skill": args.skill,
                    "checks": [asdict(check) for check in checks],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for check in checks:
            marker = "通过" if check.ok else "失败"
            print(f"[{marker}] {check.name}: {check.detail} ({check.path})")
        print(f"\n检查 {len(checks)} 项，失败 {len(failures)} 项。")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
