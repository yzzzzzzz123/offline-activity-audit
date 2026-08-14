from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .common import (
    AuditError,
    load_json,
    resolve_case_file,
    resolve_case_files,
    resolve_root,
    validate_json,
    write_json,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_BY_SCENARIO = {
    "personnel_incentive": PROJECT_ROOT / "skills" / "audit-personnel-incentive",
    "promotional_display": PROJECT_ROOT / "skills" / "audit-promotional-display",
}
MODEL_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
DEFAULT_MAX_ATTEMPTS = 3


class CodexExtractionError(AuditError):
    def __init__(self, message: str, model_usage: dict[str, Any]):
        super().__init__(message)
        self.model_usage = model_usage


def _usage_from_events(value: str) -> dict[str, int]:
    usage = {field: 0 for field in MODEL_USAGE_FIELDS}
    for line in value.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_usage = event.get("usage")
        if not isinstance(event_usage, dict):
            continue
        for field in MODEL_USAGE_FIELDS:
            raw = event_usage.get(field)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                usage[field] += int(raw)
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _model_usage_summary(
    attempts: list[dict[str, Any]],
    *,
    model: str | None,
) -> dict[str, Any]:
    totals = {field: 0 for field in (*MODEL_USAGE_FIELDS, "total_tokens")}
    for attempt in attempts:
        usage = attempt["usage"]
        for field in totals:
            totals[field] += int(usage.get(field) or 0)
    failed = sum(attempt["status"] != "success" for attempt in attempts)
    return {
        "schema_version": "1.0",
        "provider": "codex_cli",
        "model": model or "default",
        "model_invocation_count": len(attempts),
        "successful_invocation_count": len(attempts) - failed,
        "failed_invocation_count": failed,
        "retry_count": max(0, len(attempts) - 1),
        **totals,
        "attempts": attempts,
    }


def _case_attachments(case_path: Path, case: dict[str, Any]) -> list[Path]:
    scenario = case.get("scenario")
    values: list[Path] = []
    if scenario == "personnel_incentive":
        settlement = resolve_case_file(case_path, case, "settlement_image", required=False)
        if settlement:
            values.append(settlement)
        values.extend(resolve_case_files(case_path, case, "transfer_images"))
    elif scenario == "promotional_display":
        photo_dir = resolve_case_file(case_path, case, "photo_dir", required=False)
        if photo_dir and photo_dir.is_dir():
            values.extend(
                sorted(
                    [
                        item
                        for item in photo_dir.iterdir()
                        if item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                    ],
                    key=lambda item: item.name.lower(),
                )
            )
    return values


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def extract_with_codex(
    case_path: str | Path,
    output_path: str | Path,
    run_dir: str | Path,
    *,
    model: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    case_file = Path(case_path).resolve()
    case = load_json(case_file)
    scenario = str(case.get("scenario") or "")
    skill_dir = SKILL_BY_SCENARIO.get(scenario)
    if skill_dir is None:
        raise AuditError(f"Unsupported scenario for Codex extraction: {scenario}")
    codex = shutil.which("codex")
    if not codex:
        raise AuditError("codex executable was not found; provide --evidence or install Codex CLI")
    schema = skill_dir / "references" / "evidence.schema.json"
    run_root = Path(run_dir).resolve()
    prompt_dir = run_root / "prompt"
    context_dir = run_root / "context"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    context_dir.mkdir(parents=True, exist_ok=True)
    raw_output = context_dir / "codex-last-message.json"
    prompt_path = prompt_dir / "extract-evidence.md"
    prompt = f"""Use the skill at `{skill_dir}` to extract audit evidence for the case config at `{case_file}`.

Read `{skill_dir / 'SKILL.md'}` completely before taking any task action. Read the directly linked audit rules and evidence schema. Inspect every source file listed by the case config. This turn is extraction only: do not calculate the final supported amount and do not edit source evidence.

Return exactly one JSON object conforming to `{schema}`. Preserve source filenames, settlement/contract line numbers, visible values, mapping basis, uncertainty, transfer duplicate occurrence counts, and photo limitations. For personnel incentive cases, map each settlement product to exactly one Excel barcode and retain ambiguous mappings as null/ambiguous rather than guessing. For promotional display cases, inspect each contracted store independently and never use the filename as independent date/location proof.

For promotional display `photo_files`, use only the image basename or a path relative to the configured `photo_dir`. Do not prepend the original ZIP root or repeat the configured photo directory.
"""
    prompt_path.write_text(prompt, encoding="utf-8")
    command = [
        codex,
        "exec",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(PROJECT_ROOT),
        "--add-dir",
        str(resolve_root(case_file, case)),
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(raw_output),
    ]
    if model:
        command.extend(["--model", model])
    for attachment in _case_attachments(case_file, case):
        command.extend(["--image", str(attachment)])
    if max_attempts < 1:
        raise AuditError("max_attempts must be at least 1")
    attempts: list[dict[str, Any]] = []
    last_error = "Codex evidence extraction did not start"
    for attempt_no in range(1, max_attempts + 1):
        raw_output.unlink(missing_ok=True)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        usage = _usage_from_events(completed.stdout)
        attempt: dict[str, Any] = {
            "attempt_no": attempt_no,
            "status": "failed",
            "exit_code": completed.returncode,
            "usage": usage,
        }
        try:
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-1200:]
                raise AuditError(
                    f"Codex evidence extraction exited with {completed.returncode}: {detail}"
                )
            if not raw_output.exists():
                raise AuditError("Codex completed without structured evidence output")
            try:
                value = json.loads(_strip_json_fence(raw_output.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                raise AuditError(f"Codex output was not valid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise AuditError("Codex evidence output must be a JSON object")
            validate_json(value, schema)
        except AuditError as exc:
            last_error = str(exc)
            attempt["failure_reason"] = last_error
            attempts.append(attempt)
            if attempt_no < max_attempts:
                time.sleep(min(2 ** (attempt_no - 1), 4))
            continue

        attempt["status"] = "success"
        attempts.append(attempt)
        model_usage = _model_usage_summary(attempts, model=model)
        write_json(output_path, value)
        return value, model_usage

    model_usage = _model_usage_summary(attempts, model=model)
    raise CodexExtractionError(
        f"Codex evidence extraction failed after {len(attempts)} attempts: {last_error}",
        model_usage,
    )
