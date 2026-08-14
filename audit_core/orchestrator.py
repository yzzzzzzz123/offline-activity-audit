from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .archive_input import DEFAULT_INPUT_DIR, prepare_archive_batch
from .codex_runner import extract_with_codex
from .common import (
    AuditError,
    clean_identifier,
    evidence_manifest,
    load_json,
    now_utc,
    resolve_root,
    write_json,
)
from .report import create_combined_report, workbook_formula_inventory
from .worktree import (
    BRANCH_PREFIX,
    PROJECT_ROOT as SUPERVISOR_ROOT,
    checkpoint,
    create_worktree,
    export_output,
    list_worktrees,
    normalize_run_id,
    preflight,
    remove_worktree,
    worktree_status,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_RUNNERS = {
    "personnel_incentive": PROJECT_ROOT
    / "skills"
    / "audit-personnel-incentive"
    / "scripts"
    / "run_audit.py",
    "promotional_display": PROJECT_ROOT
    / "skills"
    / "audit-promotional-display"
    / "scripts"
    / "run_audit.py",
}


def _resolve_reference(base_file: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_file.parent / path
    return path.resolve()


def _source_paths(case_path: Path, case: dict[str, Any]) -> list[Path]:
    files = case.get("files")
    if not isinstance(files, dict):
        raise AuditError("Case config must contain a files object")
    root = resolve_root(case_path, case)
    result: list[Path] = []
    for value in files.values():
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if raw in (None, ""):
                continue
            path = Path(str(raw))
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            if not path.exists():
                raise AuditError(f"Case source file does not exist: {path}")
            if path.is_dir():
                result.extend(item for item in path.rglob("*") if item.is_file())
            else:
                result.append(path)
    return sorted(set(result), key=lambda item: str(item).lower())


def _resolved_case(case_path: Path, case: dict[str, Any]) -> dict[str, Any]:
    value = dict(case)
    value["case_root"] = str(resolve_root(case_path, case))
    return value


def _run_skill_process(
    scenario: str,
    case_path: Path,
    evidence_path: Path,
    result_path: Path,
    log_dir: Path,
) -> None:
    runner = SKILL_RUNNERS.get(scenario)
    if runner is None:
        raise AuditError(f"Unsupported scenario: {scenario}")
    command = [
        sys.executable,
        str(runner),
        "--case",
        str(case_path),
        "--evidence",
        str(evidence_path),
        "--output",
        str(result_path),
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "skill-stdout.log").write_text(completed.stdout, encoding="utf-8")
    (log_dir / "skill-stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-3000:]
        raise AuditError(f"Skill runner failed with exit {completed.returncode}: {detail}")
    if not result_path.exists():
        raise AuditError("Skill runner completed without writing result.json")


def run_case(
    case_path: str | Path,
    evidence_path: str | Path | None,
    run_dir: str | Path,
    *,
    agent: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    case_file = Path(case_path).resolve()
    case = load_json(case_file)
    scenario = str(case.get("scenario") or "")
    if scenario not in SKILL_RUNNERS:
        raise AuditError(f"Unsupported case scenario: {scenario!r}")
    root = Path(run_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise AuditError(f"Run directory already exists and is not empty: {root}")
    input_dir = root / "input"
    context_dir = root / "context"
    result_dir = root / "result"
    log_dir = root / "logs"
    for directory in (input_dir, context_dir, result_dir, log_dir):
        directory.mkdir(parents=True, exist_ok=True)
    state_path = root / "run-state.json"
    state: dict[str, Any] = {
        "schema_version": "1.0",
        "case_id": str(case.get("case_id") or case_file.stem),
        "scenario": scenario,
        "status": "running",
        "started_at": now_utc(),
    }
    write_json(state_path, state)
    frozen_case = input_dir / "case.json"
    frozen_evidence = input_dir / "evidence.json"
    result_path = result_dir / "audit-result.json"
    try:
        write_json(frozen_case, _resolved_case(case_file, case))
        sources = _source_paths(case_file, case)
        manifest = evidence_manifest(sources, resolve_root(case_file, case))
        write_json(input_dir / "source-manifest.json", {"files": manifest})
        if agent:
            extract_with_codex(frozen_case, frozen_evidence, root, model=model)
        else:
            if evidence_path is None:
                raise AuditError("Provide --evidence or enable --agent extraction")
            supplied = load_json(Path(evidence_path).resolve())
            write_json(frozen_evidence, supplied)
        _run_skill_process(scenario, frozen_case, frozen_evidence, result_path, log_dir)
        result = load_json(result_path)
        post_manifest = evidence_manifest(sources, resolve_root(case_file, case))
        if manifest != post_manifest:
            raise AuditError("Source evidence changed while the audit was running")
        state.update(
            {
                "status": "completed",
                "completed_at": now_utc(),
                "result_path": str(result_path),
                "source_file_count": len(manifest),
            }
        )
        write_json(state_path, state)
        return result
    except Exception as exc:
        state.update(
            {
                "status": "failed",
                "failed_at": now_utc(),
                "error": str(exc),
            }
        )
        write_json(state_path, state)
        if isinstance(exc, AuditError):
            raise
        raise AuditError(str(exc)) from exc


def _timestamp_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def run_single_command(args: argparse.Namespace) -> dict[str, Any]:
    case_path = Path(args.case).resolve()
    case = load_json(case_path)
    case_id = clean_identifier(str(case.get("case_id") or case_path.stem))
    output_dir = Path(args.output_dir).resolve()
    run_id = clean_identifier(args.run_id or _timestamp_id())
    run_dir = output_dir / "runs" / case_id / run_id
    result = run_case(
        case_path,
        Path(args.evidence).resolve() if args.evidence else None,
        run_dir,
        agent=args.agent,
        model=args.model,
    )
    excel_name = args.excel_name or f"{case_id}-核销结果.xlsx"
    excel_path = create_combined_report([result], output_dir / excel_name)
    verification = workbook_formula_inventory(excel_path)
    write_json(run_dir / "result" / "workbook-verification.json", verification)
    delivery = {
        "run_dir": str(run_dir),
        "result_json": str(run_dir / "result" / "audit-result.json"),
        "excel_report": str(excel_path),
        "workbook_verification": verification,
    }
    write_json(run_dir / "delivery.json", delivery)
    return delivery


def run_batch_command(args: argparse.Namespace) -> dict[str, Any]:
    batch_path = Path(args.batch).resolve()
    batch = load_json(batch_path)
    cases = batch.get("cases")
    if not isinstance(cases, list) or not cases:
        raise AuditError("Batch config must contain a non-empty cases array")
    output_dir = Path(args.output_dir).resolve()
    batch_id = clean_identifier(str(batch.get("batch_id") or batch_path.stem))
    run_id = clean_identifier(args.run_id or _timestamp_id())
    batch_run = output_dir / "runs" / batch_id / run_id
    if batch_run.exists() and any(batch_run.iterdir()):
        raise AuditError(f"Batch run directory already exists and is not empty: {batch_run}")
    results: list[dict[str, Any]] = []
    case_deliveries: list[dict[str, Any]] = []
    for item in cases:
        if not isinstance(item, dict) or not item.get("case"):
            raise AuditError("Each batch case must contain a case path")
        case_path = _resolve_reference(batch_path, item["case"])
        evidence_path = (
            _resolve_reference(batch_path, item["evidence"])
            if item.get("evidence")
            else None
        )
        case = load_json(case_path)
        case_id = clean_identifier(str(case.get("case_id") or case_path.stem))
        case_run = batch_run / "cases" / case_id
        result = run_case(
            case_path,
            evidence_path,
            case_run,
            agent=args.agent and evidence_path is None,
            model=args.model,
        )
        results.append(result)
        case_deliveries.append(
            {
                "case_id": case_id,
                "run_dir": str(case_run),
                "result_json": str(case_run / "result" / "audit-result.json"),
            }
        )
    excel_name = str(batch.get("excel_name") or f"{batch_id}-核销结果.xlsx")
    excel_path = create_combined_report(results, output_dir / excel_name)
    verification = workbook_formula_inventory(excel_path)
    write_json(batch_run / "workbook-verification.json", verification)
    delivery = {
        "batch_id": batch_id,
        "run_id": run_id,
        "run_dir": str(batch_run),
        "excel_report": str(excel_path),
        "cases": case_deliveries,
        "workbook_verification": verification,
    }
    write_json(batch_run / "delivery.json", delivery)
    return delivery


def _inside_path(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_export_target(output_dir: Path, run_id: str) -> None:
    if _inside_path(output_dir, SUPERVISOR_ROOT.resolve()):
        raise AuditError("--output-dir must be outside the project Git repository")
    target = output_dir / run_id
    if target.exists() or target.is_symlink():
        raise AuditError(f"Export target already exists: {target}")


def _write_supervisor_log(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _internal_command(
    *,
    worktree: Path,
    mode: str,
    config_path: Path,
    evidence_path: Path | None,
    run_id: str,
    agent: bool,
    model: str | None,
    excel_name: str | None,
) -> list[str]:
    command = [
        sys.executable,
        str(worktree / "main.py"),
        "_execute",
        "--mode",
        mode,
        "--config",
        str(config_path),
        "--worktree",
        str(worktree),
        "--run-id",
        run_id,
    ]
    if evidence_path is not None:
        command.extend(["--evidence", str(evidence_path)])
    if agent:
        command.append("--agent")
    if model:
        command.extend(["--model", model])
    if excel_name:
        command.extend(["--excel-name", excel_name])
    return command


def _launch_in_worktree(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    config_path = Path(args.case if mode == "run" else args.batch).resolve()
    if not config_path.is_file():
        raise AuditError(f"Run config does not exist: {config_path}")
    evidence_path = (
        Path(args.evidence).resolve()
        if mode == "run" and getattr(args, "evidence", None)
        else None
    )
    if evidence_path is not None and not evidence_path.is_file():
        raise AuditError(f"Evidence JSON does not exist: {evidence_path}")
    run_id = normalize_run_id(args.run_id or _timestamp_id())
    export_root = Path(args.output_dir).resolve()
    _assert_export_target(export_root, run_id)

    preflight_receipt = preflight(run_id)
    allocation = create_worktree(
        run_id=run_id,
        expected_preflight_sha256=str(preflight_receipt["preflight_sha256"]),
    )
    worktree = Path(str(allocation["path"])).resolve()
    output_root = worktree / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "worktree-preflight.json", preflight_receipt)
    write_json(output_root / "worktree-allocation.json", allocation)
    input_manifest_path = getattr(args, "input_manifest", None)
    if input_manifest_path:
        archive_manifest = load_json(Path(input_manifest_path).resolve())
        write_json(output_root / "input-archive-manifest.json", archive_manifest)
        write_json(
            output_root / "skill-routing.json",
            {
                "schema_version": "1.0",
                "entry_skill": "orchestrate-offline-audit",
                "routes": archive_manifest.get("routing", {}),
            },
        )
    state_path = output_root / "worktree-run-state.json"
    state: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "mode": mode,
        "status": "running",
        "started_at": now_utc(),
        "snapshot_commit": allocation["snapshot_commit"],
    }
    write_json(state_path, state)

    command = _internal_command(
        worktree=worktree,
        mode=mode,
        config_path=config_path,
        evidence_path=evidence_path,
        run_id=run_id,
        agent=bool(args.agent),
        model=args.model,
        excel_name=getattr(args, "excel_name", None),
    )
    completed = subprocess.run(
        command,
        cwd=worktree,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    _write_supervisor_log(output_root / "supervisor" / "stdout.log", completed.stdout)
    _write_supervisor_log(output_root / "supervisor" / "stderr.log", completed.stderr)

    if completed.returncode != 0:
        state.update(
            {
                "status": "failed",
                "failed_at": now_utc(),
                "exit_code": completed.returncode,
                "error": (completed.stderr or completed.stdout).strip()[-3000:],
            }
        )
        write_json(state_path, state)
        failed_checkpoint = checkpoint(
            worktree,
            run_id,
            f"checkpoint failed offline audit {run_id}",
        )
        raise AuditError(
            "Audit worker failed; the evidence and logs were checkpointed at "
            f"{failed_checkpoint['checkpoint_commit']} in {worktree}"
        )

    delivery_path = output_root / "delivery.json"
    if not delivery_path.is_file():
        raise AuditError("Audit worker completed without output/delivery.json")
    internal_delivery = load_json(delivery_path)
    state.update(
        {
            "status": "completed",
            "completed_at": now_utc(),
            "delivery": str(delivery_path),
        }
    )
    write_json(state_path, state)
    checkpoint_receipt = checkpoint(
        worktree,
        run_id,
        f"checkpoint completed offline audit {run_id}",
    )
    export_receipt = export_output(
        worktree=worktree,
        run_id=run_id,
        output_dir=export_root,
        checkpoint_commit=str(checkpoint_receipt["checkpoint_commit"]),
    )
    return {
        "schema_version": "1.0",
        "status": "completed",
        "run_id": run_id,
        "branch": allocation["branch"],
        "worktree": str(worktree),
        "snapshot_commit": allocation["snapshot_commit"],
        "checkpoint_commit": checkpoint_receipt["checkpoint_commit"],
        "authoritative_output": str(output_root),
        "export_output": export_receipt["export_dir"],
        "delivery": internal_delivery,
    }


def launch_single_worktree(args: argparse.Namespace) -> dict[str, Any]:
    return _launch_in_worktree(args, "run")


def launch_batch_worktree(args: argparse.Namespace) -> dict[str, Any]:
    return _launch_in_worktree(args, "batch")


def launch_input_worktree(args: argparse.Namespace) -> dict[str, Any]:
    run_id = normalize_run_id(args.run_id or _timestamp_id())
    prepared = prepare_archive_batch(
        input_dir=Path(args.input_dir).resolve(),
        run_id=run_id,
    )
    if args.prepare_only:
        return {
            "schema_version": "1.0",
            "status": "prepared",
            **prepared,
        }
    launch_args = argparse.Namespace(
        batch=prepared["batch_path"],
        output_dir=args.output_dir,
        run_id=run_id,
        agent=True,
        model=args.model,
        input_manifest=prepared["manifest_path"],
    )
    delivery = _launch_in_worktree(launch_args, "batch")
    delivery["input"] = prepared
    return delivery


def _assert_internal_identity(worktree: Path, run_id: str) -> None:
    expected = worktree.resolve()
    if PROJECT_ROOT.resolve() != expected:
        raise AuditError("Internal worker project root differs from the allocated worktree")
    if not (PROJECT_ROOT / ".git").is_file():
        raise AuditError("Internal execution is restricted to a linked Git worktree")
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    expected_branch = f"{BRANCH_PREFIX}{run_id}"
    if branch.returncode != 0 or branch.stdout.strip() != expected_branch:
        raise AuditError("Internal worker branch differs from the allocated run identity")


def execute_internal_command(args: argparse.Namespace) -> dict[str, Any]:
    run_id = normalize_run_id(args.run_id)
    worktree = Path(args.worktree).resolve()
    _assert_internal_identity(worktree, run_id)
    output_dir = PROJECT_ROOT / "output"
    if args.mode == "run":
        command_args = argparse.Namespace(
            case=args.config,
            evidence=args.evidence,
            output_dir=str(output_dir),
            excel_name=args.excel_name,
            run_id=run_id,
            agent=args.agent,
            model=args.model,
        )
        delivery = run_single_command(command_args)
    else:
        command_args = argparse.Namespace(
            batch=args.config,
            output_dir=str(output_dir),
            run_id=run_id,
            agent=args.agent,
            model=args.model,
        )
        delivery = run_batch_command(command_args)
    write_json(output_dir / "delivery.json", delivery)
    return delivery


def list_worktree_command(_: argparse.Namespace) -> dict[str, Any]:
    return {"worktrees": list_worktrees()}


def status_worktree_command(args: argparse.Namespace) -> dict[str, Any]:
    return worktree_status(args.run_id)


def remove_worktree_command(args: argparse.Namespace) -> dict[str, Any]:
    return remove_worktree(args.run_id, args.confirm_run_id)


def build_parser(*, include_internal: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="offline-activity-audit",
        description="Route an offline activity case to the relevant audit skill",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    single = subparsers.add_parser("run", help="Run one audit case")
    single.add_argument("--case", required=True)
    single.add_argument("--evidence")
    single.add_argument("--output-dir", required=True)
    single.add_argument("--excel-name")
    single.add_argument("--run-id")
    single.add_argument("--agent", action="store_true", help="Use Codex to extract evidence")
    single.add_argument("--model")
    single.set_defaults(handler=launch_single_worktree)

    batch = subparsers.add_parser("batch", help="Run a configured batch and create one workbook")
    batch.add_argument("--batch", required=True)
    batch.add_argument("--output-dir", required=True)
    batch.add_argument("--run-id")
    batch.add_argument("--agent", action="store_true", help="Extract missing evidence with Codex")
    batch.add_argument("--model")
    batch.set_defaults(handler=launch_batch_worktree)

    archive_input = subparsers.add_parser(
        "input",
        help="Process exactly two ZIP files and route them to the child skills",
    )
    archive_input.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help="Directory containing one personnel ZIP and one display ZIP",
    )
    archive_input.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT.parent / "audit-output"),
        help="External export root; defaults to the project sibling audit-output directory",
    )
    archive_input.add_argument("--run-id")
    archive_input.add_argument("--model")
    archive_input.add_argument(
        "--prepare-only",
        action="store_true",
        help="Validate, classify, and extract the ZIPs without starting Codex or a worktree",
    )
    archive_input.set_defaults(handler=launch_input_worktree)

    worktree = subparsers.add_parser(
        "worktree",
        help="Inspect or remove managed audit worktrees",
    )
    worktree_commands = worktree.add_subparsers(dest="worktree_command", required=True)
    worktree_list = worktree_commands.add_parser("list", help="List managed runs")
    worktree_list.set_defaults(handler=list_worktree_command)
    worktree_status_parser = worktree_commands.add_parser(
        "status", help="Inspect one managed run"
    )
    worktree_status_parser.add_argument("--run-id", required=True)
    worktree_status_parser.set_defaults(handler=status_worktree_command)
    worktree_remove = worktree_commands.add_parser(
        "remove", help="Remove one clean worktree and its run branch"
    )
    worktree_remove.add_argument("--run-id", required=True)
    worktree_remove.add_argument("--confirm-run-id", required=True)
    worktree_remove.set_defaults(handler=remove_worktree_command)

    if include_internal:
        internal = subparsers.add_parser("_execute")
        internal.add_argument("--mode", choices=["run", "batch"], required=True)
        internal.add_argument("--config", required=True)
        internal.add_argument("--evidence")
        internal.add_argument("--worktree", required=True)
        internal.add_argument("--run-id", required=True)
        internal.add_argument("--agent", action="store_true")
        internal.add_argument("--model")
        internal.add_argument("--excel-name")
        internal.set_defaults(handler=execute_internal_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser(include_internal=bool(raw_argv and raw_argv[0] == "_execute"))
    args = parser.parse_args(raw_argv)
    try:
        delivery = args.handler(args)
    except AuditError as exc:
        parser.exit(2, f"audit failed: {exc}\n")
    print(json.dumps(delivery, ensure_ascii=False, indent=2))
    return 0
