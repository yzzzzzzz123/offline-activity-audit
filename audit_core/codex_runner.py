from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from .common import AuditError, validate_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_BY_SCENARIO = {
    "personnel_incentive": PROJECT_ROOT / "skills" / "audit-personnel-incentive",
    "promotional_display": PROJECT_ROOT / "skills" / "audit-promotional-display",
}
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 1200


class CodexExtractionError(AuditError):
    """Raised when visual extraction fails after all attempts."""


def _find_codex() -> str:
    override = os.environ.get("OFFLINE_AUDIT_CODEX", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file():
            raise AuditError(f"OFFLINE_AUDIT_CODEX 指向的文件不存在：{candidate}")
        return str(candidate)

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if os.name == "nt" and local_app_data:
        bundled_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        bundled = sorted(
            bundled_root.glob("*/codex.exe"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if bundled:
            return str(bundled[0].resolve())

    executable = shutil.which("codex")
    if not executable:
        raise AuditError("未找到 codex 命令，无法执行视觉证据提取")
    return executable


def _write_bundled_model_catalog(
    codex: str,
    destination: Path,
    selected_model: str,
) -> None:
    completed = subprocess.run(
        [codex, "debug", "models", "--bundled"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-600:]
        raise AuditError(f"无法读取 Codex 内置模型目录：{detail}")
    try:
        catalog = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AuditError(f"Codex 内置模型目录不是有效 JSON：{exc}") from exc
    models = catalog.get("models") if isinstance(catalog, dict) else None
    slugs = {
        str(item.get("slug"))
        for item in models or []
        if isinstance(item, dict) and item.get("slug")
    }
    if selected_model not in slugs:
        raise AuditError(
            f"模型 {selected_model} 不在当前 Codex CLI 的内置模型目录中"
        )
    destination.write_text(completed.stdout, encoding="utf-8")


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        value = "\n".join(lines).strip()
    return value


def _copy_images(sources: list[Path], destination: Path) -> list[Path]:
    copied = [destination / source.name for source in sources]
    for source, target in zip(sources, copied, strict=True):
        shutil.copy2(source, target)
    return copied


def _extract_scanned_pdf_pages(source: Path, destination: Path) -> list[Path]:
    """Losslessly expose one full-page scan per PDF page to the vision model."""
    try:
        reader = PdfReader(source)
    except Exception as exc:  # pypdf exposes several parser-specific exceptions
        raise AuditError(f"合同 PDF 无法读取：{source.name}：{exc}") from exc
    if not reader.pages:
        raise AuditError(f"合同 PDF 没有页面：{source.name}")

    rendered: list[Path] = []
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            embedded = list(page.images)
        except Exception as exc:
            raise AuditError(f"合同 PDF 第 {page_no} 页图像读取失败：{exc}") from exc
        if len(embedded) != 1:
            raise AuditError(
                f"合同 PDF 第 {page_no} 页不是单张完整扫描页（检测到 {len(embedded)} 张图），"
                "为避免漏读或拼错内容，已停止视觉识别"
            )
        image = embedded[0].image
        target = destination / f"contract-page-{page_no:02d}.png"
        if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
            image = image.convert("RGB")
        image.save(target, format="PNG", optimize=False)
        rendered.append(target)
    return rendered


def _write_subset_schema(
    full_schema: Path,
    destination: Path,
    payload_property: str,
    title: str,
) -> Path:
    source = json.loads(full_schema.read_text(encoding="utf-8"))
    names = ["schema_version", "scenario", payload_property, "extraction_notes"]
    schema = {
        "$schema": source.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": names,
        "properties": {name: source["properties"][name] for name in names},
    }
    destination.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def _personnel_prompt(skill_dir: Path, images: list[Path], schema: Path) -> str:
    names = "\n".join(f"- `{path.name}`" for path in images)
    return f"""Read `{skill_dir / 'SKILL.md'}` completely, then read its directly linked audit rules.

Inspect every attached image below at original resolution and return exactly one JSON object conforming to `{schema}`.

{names}

This is visual extraction only. Do not calculate an approval conclusion, Excel quantity, or Excel product correspondence. The sales Excel is deliberately absent from this AI workspace. Do not search the repository, `input/`, `worktrees/`, prior outputs, caches, or a gold-standard workbook for missing facts. Preserve source basenames exactly. Use null or a limitation note instead of guessing.

Read the settlement image and every transfer screenshot. Extract all settlement lines in printed order. Set `barcode_visible` only when the barcode is actually legible on the settlement; never infer it from product identity or quantity. Represent each distinct business transfer once, retain its visible occurrence count, and explain any sender/receiver-view deduplication. A chat title is not a store mapping. A weekday or clock time is not a complete transfer date.
"""


def _contract_prompt(
    skill_dir: Path,
    original_pdf: Path,
    page_images: list[Path],
    schema: Path,
) -> str:
    pages = "\n".join(f"- `{path.name}`" for path in page_images)
    return f"""Read `{skill_dir / 'SKILL.md'}` completely and then its linked audit rules. This is a focused contract pass; use the focused output schema `{schema}` instead of the full evidence schema.

The original contract is `{original_pdf.name}`. Its {len(page_images)} scanned pages were extracted losslessly and attached in page order:

{pages}

Inspect every page at original resolution and return exactly one JSON object conforming to the focused schema. `contract.source_file` must be exactly `{original_pdf.name}`, never a rendered page filename.

Use only explicit core contract terms for dates, stores, display standard, fee, claim, product scope, and promotion requirements. Do not convert a product or sales table appended after the contract into a contractual promotion condition. Generic scope such as `参半所有系列` covers the whole brand range and is not a narrow mandatory-product subset: set `requires_specific_products` to false and `required_products` to an empty array. Set `requires_promotion` to true only when the contract explicitly requires a discount, gift, multi-buy, special price, or other named promotion mechanic. Read the per-store fee from the explicit fee/calculation wording; never infer it from the claim alone. Preserve all contract stores in printed order. Use null or a limitation note instead of guessing.
"""


def _photo_prompt(
    skill_dir: Path,
    images: list[Path],
    schema: Path,
    contract_result: dict[str, Any],
) -> str:
    names = "\n".join(f"- `{path.name}`" for path in images)
    contract_json = json.dumps(contract_result["contract"], ensure_ascii=False, indent=2)
    return f"""Read `{skill_dir / 'SKILL.md'}` completely and then its linked audit rules. This is a focused field-photo pass; use the focused output schema `{schema}` instead of the full evidence schema.

Inspect every attached field photo at original resolution:

{names}

The following already-validated contract JSON is authoritative only for contract store order, activity dates, display standard, product scope, and promotion requirements. Do not rewrite it and do not use it to invent facts that are not visible in a photo:

```json
{contract_json}
```

Return exactly one photo-review row for every contract store line, in contract order, including an empty `photo_files` list when no photo can be assigned. Preserve attached photo basenames exactly. `recognized_products` may contain only products visibly supported by packaging; never use an Excel-derived name. Extract ordinary visible prices separately from explicit promotion signals. A normal price tag alone is not a promotion. An explicit promotion signal requires visible special-price wording, old/new price, discount, gift, multi-buy, 1+1, 3+2, or value-pack wording. Filenames are routing leads only and cannot independently prove date, location, product, promotion, or display compliance. Use null, `unclear`, or a limitation note instead of guessing.

The mandatory display standard has two independent ways to pass: a clearly supported `1平米堆头`, or a clearly countable `4纵陈列`. For every `display_observation`, set `matched_standard` to exactly one of `stack_1sqm`, `four_vertical`, `both`, `none`, or `unclear`. Use `standard_evidence=meets` only with `stack_1sqm`, `four_vertical`, or `both`; use `does_not_meet` only with `none`; and use `unclear` only with `unclear`. The `description` must state the visible basis and which alternative is met; never write only a generic phrase such as `陈列符合`. If the photo cannot establish one-square-metre area and cannot clearly count four vertical facings/columns, return `unclear`. Do not decide whether photos are duplicated or reused across stores; deterministic code performs that separate anti-fraud check.
"""


def _validate_personnel_sources(case: dict[str, Any], evidence: dict[str, Any]) -> None:
    expected_settlement = Path(case["settlement_image"]).name
    if Path(str(evidence["settlement"]["source_file"])).name != expected_settlement:
        raise AuditError("AI 返回的结算单文件名不属于本次材料")
    allowed = {Path(path).name for path in case["transfer_images"]}
    used = {
        Path(name).name
        for transfer in evidence.get("transfers") or []
        for name in transfer.get("source_files") or []
    }
    unknown = sorted(used - allowed)
    if unknown:
        raise AuditError("AI 返回了不存在的转账截图文件名：" + "、".join(unknown))


def _validate_contract_result(case: dict[str, Any], evidence: dict[str, Any]) -> None:
    contract = evidence["contract"]
    expected_contract = Path(case["contract_pdf"]).name
    if Path(str(contract["source_file"])).name != expected_contract:
        raise AuditError("AI 返回的合同文件名不属于本次材料")
    line_numbers = [int(store["line_no"]) for store in contract["stores"]]
    if len(line_numbers) != len(set(line_numbers)):
        raise AuditError("AI 返回的合同门店序号存在重复")
    if not contract["requires_specific_products"] and contract["required_products"]:
        raise AuditError("合同未要求限定产品时，required_products 必须为空")
    if contract["requires_specific_products"] and not contract["required_products"]:
        raise AuditError("合同要求限定产品时，required_products 不能为空")
    if not contract["requires_promotion"] and contract["required_promotion"] not in {None, ""}:
        raise AuditError("合同未要求促销形式时，required_promotion 必须为空")


def _validate_photo_result(
    case: dict[str, Any],
    contract_result: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    allowed = {Path(path).name for path in case["photo_files"]}
    reviews = evidence.get("photo_reviews") or []
    used = {
        Path(name).name
        for review in reviews
        for name in review.get("photo_files") or []
    }
    unknown = sorted(used - allowed)
    if unknown:
        raise AuditError("AI 返回了不存在的现场照片文件名：" + "、".join(unknown))

    stores = contract_result["contract"]["stores"]
    expected_lines = [int(store["line_no"]) for store in stores]
    actual_lines = [int(review["store_line_no"]) for review in reviews]
    if actual_lines != expected_lines:
        raise AuditError(
            "现场照片核对必须按合同顺序逐店返回；"
            f"期望 {expected_lines}，实际 {actual_lines}"
        )
    expected_names = {
        int(store["line_no"]): str(store["store_name"]).strip()
        for store in stores
    }
    for review in reviews:
        line_no = int(review["store_line_no"])
        if str(review["contract_store_name"]).strip() != expected_names[line_no]:
            raise AuditError(f"合同第 {line_no} 家门店名称被现场照片核对改写")
        observation = review.get("display_observation") or {}
        evidence_level = str(observation.get("standard_evidence") or "")
        matched_standard = str(observation.get("matched_standard") or "")
        expected_standards = {
            "meets": {"stack_1sqm", "four_vertical", "both"},
            "does_not_meet": {"none"},
            "unclear": {"unclear"},
        }
        if (
            evidence_level not in expected_standards
            or matched_standard not in expected_standards[evidence_level]
        ):
            raise AuditError(
                f"合同第 {line_no} 家门店的陈列结论与命中标准不一致："
                f"standard_evidence={evidence_level}, matched_standard={matched_standard}"
            )
        description = str(observation.get("description") or "").strip()
        if evidence_level == "meets" and description in {"陈列符合", "符合", "达标"}:
            raise AuditError(
                f"合同第 {line_no} 家门店的陈列依据过于笼统，必须说明1平米堆头或4纵陈列的可见依据"
            )


def _validate_source_names(case: dict[str, Any], evidence: dict[str, Any]) -> None:
    if str(case["scenario"]) == "personnel_incentive":
        _validate_personnel_sources(case, evidence)
        return
    _validate_contract_result(case, evidence)
    _validate_photo_result(
        case,
        {"contract": evidence["contract"]},
        {"photo_reviews": evidence.get("photo_reviews") or []},
    )


def _run_codex_json(
    *,
    codex: str,
    model_root: Path,
    skill_dir: Path,
    schema: Path,
    raw_output: Path,
    prompt: str,
    images: list[Path],
    selected_model: str,
    model_catalog: Path,
    label: str,
    max_attempts: int,
    attempt_timeout_seconds: int,
    post_validate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    command = [
        codex,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(model_root),
        "--add-dir",
        str(skill_dir),
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(raw_output),
        "--model",
        selected_model,
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        f"model_catalog_json={json.dumps(str(model_catalog), ensure_ascii=False)}",
    ]
    for image in images:
        command.extend(["--image", str(image)])

    last_error = "AI 提取未启动"
    for attempt in range(1, max_attempts + 1):
        print(
            f"AI 正在识别 {label}（第 {attempt}/{max_attempts} 次，模型 {selected_model}）...",
            flush=True,
        )
        raw_output.unlink(missing_ok=True)
        try:
            completed = subprocess.run(
                command,
                cwd=model_root,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=attempt_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            last_error = f"单次视觉识别超过 {attempt_timeout_seconds} 秒"
            print(
                f"AI 识别 {label} 第 {attempt}/{max_attempts} 次失败：{last_error}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
            continue
        try:
            if completed.returncode != 0:
                detail = "\n".join(
                    value.strip()
                    for value in (completed.stderr, completed.stdout)
                    if value.strip()
                )[-4000:]
                raise AuditError(f"codex 退出码 {completed.returncode}：{detail}")
            if not raw_output.is_file():
                raise AuditError("codex 未生成结构化证据")
            value = json.loads(_strip_json_fence(raw_output.read_text(encoding="utf-8")))
            if not isinstance(value, dict):
                raise AuditError("AI 证据顶层必须是 JSON 对象")
            validate_json(value, schema)
            if post_validate is not None:
                post_validate(value)
            return value
        except (AuditError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            print(
                f"AI 识别 {label} 第 {attempt}/{max_attempts} 次失败：{last_error}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise CodexExtractionError(
        f"AI 证据提取连续失败 {max_attempts} 次：{last_error}"
    )


def extract_with_codex(
    case: dict[str, Any],
    temporary_root: str | Path,
    *,
    model: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    attempt_timeout_seconds: int = DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    scenario = str(case.get("scenario") or "")
    skill_dir = SKILL_BY_SCENARIO.get(scenario)
    if skill_dir is None:
        raise AuditError(f"不支持的 AI 提取场景：{scenario}")
    codex = _find_codex()
    if max_attempts < 1:
        raise AuditError("max_attempts 必须大于等于 1")
    if attempt_timeout_seconds < 1:
        raise AuditError("attempt_timeout_seconds 必须大于等于 1")

    root = Path(temporary_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    full_schema = skill_dir / "references" / "evidence.schema.json"
    selected_model = model or DEFAULT_MODEL
    model_catalog = root / ".codex-model-catalog.json"
    if not model_catalog.is_file():
        _write_bundled_model_catalog(codex, model_catalog, selected_model)

    if scenario == "personnel_incentive":
        model_root = root / "model-personnel_incentive"
        model_root.mkdir(parents=True, exist_ok=False)
        sources = [Path(case["settlement_image"]), *map(Path, case["transfer_images"])]
        images = _copy_images(sources, model_root)
        return _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_personnel_prompt(skill_dir, images, full_schema),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="人员激励材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            post_validate=lambda value: _validate_personnel_sources(case, value),
        )

    contract_root = root / "model-promotional_display-contract"
    contract_root.mkdir(parents=True, exist_ok=False)
    contract_source = Path(case["contract_pdf"])
    copied_contract = contract_root / contract_source.name
    shutil.copy2(contract_source, copied_contract)
    page_images = _extract_scanned_pdf_pages(copied_contract, contract_root)
    contract_schema = _write_subset_schema(
        full_schema,
        contract_root / "contract-evidence.schema.json",
        "contract",
        "Promotional display contract visual evidence",
    )
    contract_result = _run_codex_json(
        codex=codex,
        model_root=contract_root,
        skill_dir=skill_dir,
        schema=contract_schema,
        raw_output=contract_root / "contract-evidence.json",
        prompt=_contract_prompt(skill_dir, copied_contract, page_images, contract_schema),
        images=page_images,
        selected_model=selected_model,
        model_catalog=model_catalog,
        label="堆头合同",
        max_attempts=max_attempts,
        attempt_timeout_seconds=attempt_timeout_seconds,
        post_validate=lambda value: _validate_contract_result(case, value),
    )

    photo_root = root / "model-promotional_display-photos"
    photo_root.mkdir(parents=True, exist_ok=False)
    photo_sources = [Path(value) for value in case["photo_files"]]
    photo_images = _copy_images(photo_sources, photo_root)
    photo_schema = _write_subset_schema(
        full_schema,
        photo_root / "photo-evidence.schema.json",
        "photo_reviews",
        "Promotional display field-photo visual evidence",
    )
    photo_result = _run_codex_json(
        codex=codex,
        model_root=photo_root,
        skill_dir=skill_dir,
        schema=photo_schema,
        raw_output=photo_root / "photo-evidence.json",
        prompt=_photo_prompt(skill_dir, photo_images, photo_schema, contract_result),
        images=photo_images,
        selected_model=selected_model,
        model_catalog=model_catalog,
        label="堆头现场照片",
        max_attempts=max_attempts,
        attempt_timeout_seconds=attempt_timeout_seconds,
        post_validate=lambda value: _validate_photo_result(case, contract_result, value),
    )

    merged = {
        "schema_version": "2.0",
        "scenario": "promotional_display",
        "contract": contract_result["contract"],
        "photo_reviews": photo_result["photo_reviews"],
        "extraction_notes": [
            *contract_result.get("extraction_notes", []),
            *photo_result.get("extraction_notes", []),
        ],
    }
    validate_json(merged, full_schema)
    _validate_source_names(case, merged)
    return merged
