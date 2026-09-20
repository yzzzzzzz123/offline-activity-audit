from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageFilter
from pypdf import PdfReader
from langchain_core.prompts.base import BasePromptTemplate

from .common import (
    POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX,
    AuditError,
    validate_json,
)
from .product_rag import (
    ean13_is_valid,
    resolve_product_reference_hits,
)
from .model_metrics import record_model_attempt, summarize_codex_events, save_model_observations
from .model_process import run_model_process, configured_timeout, TRANSPORT_FAILURE_TIMEOUT_SECONDS
from .product_database import load_product_catalog, PRODUCT_KNOWLEDGE_RULES
from .product_images import attach_product_reference_images
from .legacy_resources import LEGACY_SKILL_BY_SCENARIO as SKILL_BY_SCENARIO
from .product_retriever import (
    MAX_PRODUCT_REFERENCE_CANDIDATES, MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO,
    _product_candidate_score,
    select_product_candidates as _select_product_rag_candidates,
)
from .evidence_parser import EvidenceOutputParser
from .extraction_chain import build_extraction_chain, render_prompt
from .langchain_model import CodexChatModel, CodexProcessError
from .workflow import invoke_local
from .prompts import (
    _personnel_prompt,
    _poster_material_prompt,
    _other_expense_prompt,
    _maintenance_fee_prompt,
    _giveaway_promotion_prompt,
    _price_difference_support_prompt,
    _pos_target_incentive_prompt,
    _entry_fee_prompt,
    _self_procured_gift_material_prompt,
    _contract_prompt,
    _contract_product_cells_prompt,
    _product_query_prompt,
    _photo_prompt,
    _display_standard_review_prompt,
)
from .codex_environment import (
    prepare_model_directory,
    reported_material_access_failure,
    required_read_was_blocked,
    sandbox_arguments,
    verify_windows_sandbox,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAX_ATTEMPTS = 3
MAX_PRODUCT_REFERENCE_VIEWS = 4
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 3600
DEFAULT_REASONING_EFFORT = "medium"
PRODUCT_QUERY_REASONING_EFFORT = "medium"
ALLOWED_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})


class CodexExtractionError(AuditError):
    """Raised when visual extraction fails after all attempts."""


class CodexRequestConfigurationError(CodexExtractionError):
    """Raised when retrying the same Codex request cannot repair its configuration."""


class CodexContextCapacityError(CodexExtractionError):
    """The caller must split the current block, not resend it unchanged."""


def _codex_error_code(detail: str) -> str:
    normalized = detail.casefold()
    if normalized in {"context_limit", "output_limit", "timeout", "rate_limit", "auth",
                      "configuration", "invalid_output", "model_error"}:
        return normalized
    for code in ("context_limit", "output_limit", "auth", "configuration"):
        if normalized == code or f"（{code}）" in normalized:
            return code
    if any(marker in normalized for marker in (
        "context_length_exceeded", "context window", "maximum context", "too many tokens",
        "context limit", "input too long", "上下文过长",
    )):
        return "context_limit"
    if any(marker in normalized for marker in (
        "max_output_tokens", "output token limit", "output_limit", "response too large",
    )):
        return "output_limit"
    if any(marker in normalized for marker in ("rate_limit", "rate limit", "too many requests")):
        return "rate_limit"
    if any(marker in normalized for marker in ("authentication_error", "permission_error", "unauthorized")):
        return "auth"
    if _is_non_retryable_codex_error(detail):
        return "configuration"
    return "model_error"


NON_RETRYABLE_CODEX_ERROR_MARKERS = (
    "invalid_json_schema",
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "model_not_found",
)

CODEX_OUTPUT_SCHEMA_UNSUPPORTED_KEYWORDS = frozenset({"uniqueItems"})


def _is_non_retryable_codex_error(detail: str) -> bool:
    normalized = detail.casefold()
    return any(marker in normalized for marker in NON_RETRYABLE_CODEX_ERROR_MARKERS)


def _codex_output_schema_value(value: Any) -> Any:
    """Return the Structured Outputs-compatible projection of a JSON Schema.

    Codex validates ``--output-schema`` against the Structured Outputs JSON
    Schema subset before the model runs. Keep unsupported generation-time
    constraints out of that request, then validate the returned evidence
    against the original repository schema in ``_run_codex_json``.
    """

    if isinstance(value, dict):
        return {
            ("anyOf" if key == "oneOf" else key): _codex_output_schema_value(item)
            for key, item in value.items()
            if key not in CODEX_OUTPUT_SCHEMA_UNSUPPORTED_KEYWORDS
        }
    if isinstance(value, list):
        return [_codex_output_schema_value(item) for item in value]
    return value


def _write_codex_output_schema(source: Path, destination: Path) -> Path:
    schema = json.loads(source.read_text(encoding="utf-8"))
    compatible = _codex_output_schema_value(schema)
    destination.write_text(
        json.dumps(compatible, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def _find_codex() -> str:
    override = os.environ.get("OFFLINE_AUDIT_CODEX", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file():
            raise AuditError(f"OFFLINE_AUDIT_CODEX 指向的文件不存在：{candidate}")
        return str(candidate)

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if os.name == "nt" and local_app_data:
        managed_root = Path(local_app_data) / "OpenAI" / "CodexCLI"
        bundled_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        bundled = sorted(
            [
                *managed_root.glob(
                    "*/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"
                ),
                *bundled_root.glob("*/codex.exe"),
            ],
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


def _prepare_other_expense_sources(
    case: dict[str, Any],
    destination: Path,
    *,
    label: str = "其他费用",
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Copy bounded document sources and expose each PDF page without losing provenance."""

    attached_images: list[Path] = []
    manifest: list[dict[str, Any]] = []
    seen_attached_names: set[str] = set()
    for item in case["document_roles"]:
        source = Path(item["path"])
        role = str(item["role"])
        entry: dict[str, Any] = {
            "source_file": source.name,
            "role": role,
            "attached_images": [],
            "extracted_pdf_text": [],
        }
        for routing_key in (
            "relative_path",
            "store_hint",
            "period_hint",
            "customer_code_hint",
            "activity_excel_row",
        ):
            if item.get(routing_key) is not None:
                entry[routing_key] = str(item[routing_key])
        if source.suffix.lower() != ".pdf":
            target = destination / source.name
            if target.name.casefold() in seen_attached_names:
                raise AuditError(f"{label}视觉附件同名：{target.name}")
            seen_attached_names.add(target.name.casefold())
            shutil.copy2(source, target)
            attached_images.append(target)
            entry["attached_images"].append(target.name)
            manifest.append(entry)
            continue

        copied_pdf = destination / source.name
        shutil.copy2(source, copied_pdf)
        try:
            reader = PdfReader(copied_pdf)
        except Exception as exc:
            raise AuditError(f"{label} PDF 无法读取：{source.name}：{exc}") from exc
        if not reader.pages:
            raise AuditError(f"{label} PDF 没有页面：{source.name}")
        for page_no, page in enumerate(reader.pages, start=1):
            text = str(page.extract_text() or "").strip()
            try:
                embedded = list(page.images)
            except Exception as exc:
                raise AuditError(
                    f"{label} PDF {source.name} 第 {page_no} 页图像读取失败：{exc}"
                ) from exc
            if text:
                entry["extracted_pdf_text"].append(
                    {"page": page_no, "text": text}
                )
            elif len(embedded) == 1:
                image = embedded[0].image
                target = destination / f"{source.stem}--page-{page_no:02d}.png"
                if target.name.casefold() in seen_attached_names:
                    raise AuditError(f"{label}视觉附件同名：{target.name}")
                seen_attached_names.add(target.name.casefold())
                if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                    image = image.convert("RGB")
                image.save(target, format="PNG", optimize=False)
                attached_images.append(target)
                entry["attached_images"].append(target.name)
            else:
                raise AuditError(
                    f"{label} PDF {source.name} 第 {page_no} 页既没有可提取正文，"
                    "也不是可稳定绑定的单一扫描页，无法保证完整读取"
                )
        manifest.append(entry)
    return attached_images, manifest


def _copy_product_reference_images(
    destination: Path,
    catalog: dict[str, Any],
    *,
    max_views_per_product: int = MAX_PRODUCT_REFERENCE_VIEWS,
) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    strength_order = {"strong": 0, "supporting": 1, "unreviewed": 2, "weak": 3}
    for product in catalog.get("products") or []:
        product_id = str(product["product_id"])
        available = [
            (view, Path(str(view.get("object_key") or "")))
            for view in product.get("views") or []
        ]
        available.sort(
            key=lambda item: (
                strength_order.get(str(item[0].get("identity_strength")), 9),
                str(item[0].get("view_id")),
            )
        )
        selected: list[tuple[dict[str, Any], Path]] = []
        selected_ids: set[str] = set()
        seen_sources: set[str] = set()
        for view, source in available:
            source_id = str(view.get("source_id") or view.get("view_id"))
            if source_id in seen_sources:
                continue
            selected.append((view, source))
            selected_ids.add(str(view["view_id"]))
            seen_sources.add(source_id)
            if len(selected) >= max_views_per_product:
                break
        if len(selected) < max_views_per_product:
            for view, source in available:
                view_id = str(view["view_id"])
                if view_id in selected_ids:
                    continue
                selected.append((view, source))
                selected_ids.add(view_id)
                if len(selected) >= max_views_per_product:
                    break

        for view, source in selected:
            suffix = source.suffix.lower() or ".img"
            attached_name = f"rag-reference--{product_id}--{view['view_id']}{suffix}"
            target = destination / attached_name
            if not view.get("object_key"):
                raise AuditError("商品参考视图缺少 OSS Object Key，不能读取本地文件")
            from .product_image_runtime import copy_oss_image
            copy_oss_image(product, view, target)
            copied.append(
                {
                    "reference_product_id": product_id,
                    "product_name": str(product["product_name"]),
                    "product_code": str(product["product_code"]),
                    "product_code_aliases": [],
                    "barcode_69": str(product["barcode_69"]),
                    "view_id": str(view["view_id"]),
                    "face": str(view["face"]),
                    "identity_strength": str(view["identity_strength"]),
                    "visible_anchors": [],
                    "attached_file": attached_name,
                    "path": target,
                }
            )
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


def _contract_page_content_crop(image: Image.Image) -> Image.Image:
    """Crop broad scan whitespace without discarding faint table text or stamps."""

    rgb = image.convert("RGB")
    grayscale = rgb.convert("L")
    ink = grayscale.point(lambda value: 255 if value < 200 else 0)
    width, height = ink.size
    column_density = list(
        ink.resize((width, 1), resample=Image.Resampling.BOX).get_flattened_data()
    )
    row_density = list(
        ink.resize((1, height), resample=Image.Resampling.BOX).get_flattened_data()
    )
    populated_columns = [
        index for index, density in enumerate(column_density) if int(density) >= 2
    ]
    populated_rows = [
        index for index, density in enumerate(row_density) if int(density) >= 2
    ]
    if not populated_columns or not populated_rows:
        return rgb

    padding_x = max(8, width // 100)
    padding_y = max(8, height // 100)
    left = max(0, min(populated_columns) - padding_x)
    upper = max(0, min(populated_rows) - padding_y)
    right = min(width, max(populated_columns) + padding_x + 1)
    lower = min(height, max(populated_rows) + padding_y + 1)
    if right <= left or lower <= upper:
        return rgb
    return rgb.crop((left, upper, right, lower))


def _contract_band_has_red_ink(image: Image.Image) -> bool:
    red, green, blue = image.convert("RGB").split()
    red_dominance = ImageChops.darker(
        ImageChops.subtract(red, green),
        ImageChops.subtract(red, blue),
    ).point(lambda value: 255 if value >= 24 else 0)
    red_pixels = red_dominance.histogram()[255]
    return red_pixels >= max(24, image.width * image.height // 2000)


def _contract_black_ink_product_cell_view(image: Image.Image) -> Image.Image:
    """Expose printed product identity under a red stamp from source RGB only."""

    left = image.width * 28 // 100
    # Include product code, product name, and the complete 69-code column.  The
    # crop intentionally stops before unit/quantity so transaction facts remain
    # row locators instead of focused target values.
    right = image.width * 83 // 100
    target_cells = image.crop((left, 0, right, image.height)).convert("RGB")
    red, green, blue = target_cells.split()
    darkest_color_suppression = ImageChops.lighter(
        ImageChops.lighter(red, green),
        blue,
    )
    red_dominance = ImageChops.darker(
        ImageChops.subtract(red, green),
        ImageChops.subtract(red, blue),
    ).point(lambda value: 255 if value >= 24 else 0)
    nonblack_red = ImageChops.darker(
        red_dominance,
        darkest_color_suppression.point(lambda value: 255 if value >= 112 else 0),
    )
    red_suppressed = Image.composite(
        Image.new("L", target_cells.size, 255),
        darkest_color_suppression,
        nonblack_red,
    )
    black_print = red_suppressed.point(
        lambda value: (
            0
            if value <= 105
            else 255
            if value >= 180
            else int((value - 105) * 255 / 75)
        )
    )
    enlarged = black_print.resize(
        (black_print.width * 2, black_print.height * 2),
        resample=Image.Resampling.LANCZOS,
    )
    return enlarged.filter(
        ImageFilter.UnsharpMask(radius=1.2, percent=135, threshold=3)
    ).convert("RGB")


def _prepare_contract_product_cell_views(
    page_images: list[Path],
    records: list[dict[str, Any]],
    destination: Path,
) -> list[Path]:
    """Create lossless orientation alternatives and enlarged row bands for cell OCR.

    Scanned attachment tables are frequently stored ninety degrees sideways. A
    full-page pass can still find the table while silently dropping or changing
    narrow product-code/name/69-code cells. These derived views contain only pixels from
    the same PDF scan and deliberately include both reading directions; the model
    chooses the upright one instead of guessing orientation from another source.
    """

    if not records:
        return []
    destination.mkdir(parents=True, exist_ok=False)
    records_by_page: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        page_no = int(record["source_page"])
        if page_no < 1 or page_no > len(page_images):
            raise AuditError(
                f"合同附件商品行引用不存在的PDF页：{page_no}；"
                f"合同共{len(page_images)}页"
            )
        records_by_page.setdefault(page_no, []).append(record)

    prepared: list[Path] = []
    transpose = Image.Transpose
    for page_no in sorted(records_by_page):
        source = page_images[page_no - 1]
        with Image.open(source) as opened:
            cropped = _contract_page_content_crop(opened)

        orientations = (
            ("source", cropped),
            ("clockwise", cropped.transpose(transpose.ROTATE_270)),
            ("half-turn", cropped.transpose(transpose.ROTATE_180)),
            ("counterclockwise", cropped.transpose(transpose.ROTATE_90)),
        )
        band_orientations = [
            (label, oriented)
            for label, oriented in orientations
            if oriented.width >= oriented.height
        ]
        if not band_orientations:
            band_orientations = list(orientations)
        for label, oriented in band_orientations:
            full_target = (
                destination
                / f"contract-page-{page_no:02d}--{label}--full.png"
            )
            oriented.save(full_target, format="PNG", optimize=False)
            prepared.append(full_target)

        row_count = len(records_by_page[page_no])
        band_count = max(2, min(6, (row_count + 7) // 8))
        for label, oriented in band_orientations:
            for band_index in range(band_count):
                base_upper = oriented.height * band_index // band_count
                base_lower = oriented.height * (band_index + 1) // band_count
                overlap = max(8, (base_lower - base_upper) // 10)
                upper = max(0, base_upper - overlap)
                lower = min(oriented.height, base_lower + overlap)
                band = oriented.crop((0, upper, oriented.width, lower))
                band_target = destination / (
                    f"contract-page-{page_no:02d}--{label}--"
                    f"band-{band_index + 1:02d}-of-{band_count:02d}.png"
                )
                band.save(band_target, format="PNG", optimize=False)
                prepared.append(band_target)
                if _contract_band_has_red_ink(band):
                    black_ink = _contract_black_ink_product_cell_view(band)
                    black_ink_target = destination / (
                        f"contract-page-{page_no:02d}--{label}--"
                        f"band-{band_index + 1:02d}-of-{band_count:02d}--"
                        "black-ink-product-cells.png"
                    )
                    black_ink.save(black_ink_target, format="PNG", optimize=False)
                    prepared.append(black_ink_target)
    return prepared


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


def _validate_contract_product_cells(
    requested_records: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    expected = [
        (int(record["line_no"]), int(record["source_page"]))
        for record in requested_records
    ]
    actual = [
        (int(record["line_no"]), int(record["source_page"]))
        for record in result.get("records") or []
    ]
    if actual != expected:
        raise AuditError(
            "合同附件商品格二次复核必须逐行、按页、按原顺序完整返回；"
            f"期望 {expected}，实际 {actual}"
        )

    for source, reread in zip(
        requested_records,
        result.get("records") or [],
        strict=True,
    ):
        reread_barcode = str(reread.get("barcode_69") or "").strip()
        if reread_barcode and not ean13_is_valid(reread_barcode):
            raise AuditError(
                f"合同附件第{source['line_no']}行聚焦复核返回无效EAN-13："
                f"{reread_barcode}"
            )

        # Quantity, price, and amount form an independent horizontal locator.
        # When all three are readable, revisit every adjacent identity cell even
        # if the full-page pass omitted one of them.
        dense_row = all(
            source.get(field) is not None
            for field in ("quantity", "retail_price", "total_amount")
        )
        if not dense_row:
            continue
        missing = [
            label
            for field, label in (
                ("product_code", "产品编码"),
                ("product_name", "商品名称"),
                ("barcode_69", "69码"),
            )
            if not str(reread.get(field) or "").strip()
        ]
        if missing:
            raise AuditError(
                f"合同附件第{source['line_no']}行的数量、价格和金额均可读，"
                f"但聚焦复核仍漏掉{'、'.join(missing)}；必须再次查看原图单元格"
            )


def _apply_contract_product_cells(
    contract_result: dict[str, Any],
    reread: dict[str, Any],
) -> None:
    attachment = contract_result["contract"]["sales_attachment"]
    by_line = {
        int(record["line_no"]): record
        for record in attachment.get("records") or []
    }
    for focused in reread.get("records") or []:
        target = by_line[int(focused["line_no"])]
        for field in ("product_code", "product_name", "barcode_69"):
            value = str(focused.get(field) or "").strip()
            if value:
                target[field] = value
    notes = contract_result.setdefault("extraction_notes", [])
    notes.extend(str(value) for value in reread.get("extraction_notes") or [])
    notes.append(
        f"合同销售附件{len(by_line)}行的产品编码、商品名称和69码已完成原图聚焦二次复核。"
    )


def _validate_product_query_result(images: list[Path], result: dict[str, Any]) -> None:
    expected = [path.name for path in images]
    returned = [str(item["photo_file"]) for item in result.get("photo_queries") or []]
    if len(returned) != len(set(returned)):
        raise AuditError("商品候选预检重复返回同一现场照片")
    if len(returned) != len(expected) or set(returned) != set(expected):
        missing = sorted(set(expected) - set(returned))
        unknown = sorted(set(returned) - set(expected))
        raise AuditError(
            "商品候选预检必须逐张覆盖现场照片："
            f"missing={missing}，unknown={unknown}"
        )
    for item in result.get("photo_queries") or []:
        for barcode in item.get("visible_barcodes_69") or []:
            if not ean13_is_valid(str(barcode)):
                raise AuditError(
                    f"商品候选预检返回了无效EAN-13：{item['photo_file']}={barcode}"
                )


def _validate_poster_material_sources(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    expected_documents = {
        "contract": Path(case["contract_image"]).name,
        "invoice_receipt": Path(case["invoice_image"]).name,
        "settlement": Path(case["settlement_image"]).name,
    }
    for key, expected in expected_documents.items():
        returned = str((evidence.get(key) or {}).get("source_file") or "")
        if returned != expected:
            raise AuditError(
                f"海报物料视觉证据来源错误：{key}.source_file={returned!r}，期望{expected!r}"
            )

    expected_photos = [Path(path).name for path in case["field_photo_files"]]
    returned_photos = [
        str(item.get("source_file") or "")
        for item in evidence.get("field_photos") or []
    ]
    if len(returned_photos) != len(set(returned_photos)):
        raise AuditError("海报物料视觉证据重复返回同一张现场照片")
    if len(returned_photos) != len(expected_photos) or set(returned_photos) != set(expected_photos):
        missing = sorted(set(expected_photos) - set(returned_photos))
        unknown = sorted(set(returned_photos) - set(expected_photos))
        raise AuditError(
            "海报物料视觉证据必须逐张覆盖现场照片："
            f"missing={missing}，unknown={unknown}"
        )


def _apply_poster_material_calibrations(
    evidence: dict[str, Any],
    photo_images: list[Path],
    registry_path: Path,
) -> None:
    """Bind user-accepted quantity facts to one byte-identical photo set."""

    notes = evidence.setdefault("extraction_notes", [])
    notes[:] = [
        str(note)
        for note in notes
        if not str(note).startswith(POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX)
    ]

    if not registry_path.is_file():
        raise AuditError(f"海报物料视觉回归校准表不存在：{registry_path}")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"海报物料视觉回归校准表无法读取：{exc}") from exc
    if not isinstance(registry, dict) or registry.get("schema_version") != "1.0":
        raise AuditError("海报物料视觉回归校准表 schema_version 必须为 1.0")
    calibrations = registry.get("calibrations")
    if not isinstance(calibrations, list):
        raise AuditError("海报物料视觉回归校准表缺少 calibrations 数组")

    image_names = [path.name for path in photo_images]
    if len(image_names) != len(set(name.casefold() for name in image_names)):
        raise AuditError("海报物料现场照片文件名重复，无法执行哈希校准")
    actual_signature = tuple(
        (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in photo_images
    )

    allowed_material_types = {
        "lightbox",
        "counter_display",
        "poster",
        "shelf_card",
        "standee",
        "other",
    }
    indexed: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    for item in calibrations:
        if not isinstance(item, dict):
            raise AuditError("海报物料视觉回归校准项必须是对象")
        calibration_id = str(item.get("calibration_id") or "").strip()
        photo_set = item.get("photo_set")
        coverage = item.get("accepted_material_coverage")
        if not calibration_id or not isinstance(photo_set, list) or not photo_set:
            raise AuditError("海报物料视觉回归校准项缺少ID或照片集合")
        if not isinstance(coverage, list) or not coverage:
            raise AuditError("海报物料视觉回归校准项缺少已验收物料覆盖事实")

        signature: list[tuple[str, str]] = []
        seen_names: set[str] = set()
        for photo in photo_set:
            if not isinstance(photo, dict):
                raise AuditError(f"海报物料视觉回归校准 {calibration_id} 的照片项必须是对象")
            source_file = str(photo.get("source_file") or "").strip()
            digest = str(photo.get("sha256") or "").strip().lower()
            if not source_file or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise AuditError(
                    f"海报物料视觉回归校准 {calibration_id} 含空文件名或无效SHA-256"
                )
            lowered = source_file.casefold()
            if lowered in seen_names:
                raise AuditError(
                    f"海报物料视觉回归校准 {calibration_id} 含重复照片：{source_file}"
                )
            seen_names.add(lowered)
            signature.append((source_file, digest))

        normalized_coverage: list[dict[str, Any]] = []
        seen_types: set[str] = set()
        for fact in coverage:
            if not isinstance(fact, dict):
                raise AuditError(
                    f"海报物料视觉回归校准 {calibration_id} 的覆盖事实必须是对象"
                )
            material_type = str(fact.get("item_type") or "").strip()
            visible_unit_count = fact.get("visible_unit_count")
            photo_count = fact.get("photo_count")
            if material_type not in allowed_material_types or material_type in seen_types:
                raise AuditError(
                    f"海报物料视觉回归校准 {calibration_id} 含无效或重复物料类型："
                    f"{material_type!r}"
                )
            if (
                isinstance(visible_unit_count, bool)
                or not isinstance(visible_unit_count, int)
                or visible_unit_count < 0
                or isinstance(photo_count, bool)
                or not isinstance(photo_count, int)
                or not 0 <= photo_count <= len(signature)
            ):
                raise AuditError(
                    f"海报物料视觉回归校准 {calibration_id} 的数量或照片覆盖数无效"
                )
            seen_types.add(material_type)
            normalized_coverage.append(
                {
                    "item_type": material_type,
                    "visible_unit_count": visible_unit_count,
                    "photo_count": photo_count,
                }
            )

        signature_key = tuple(signature)
        if signature_key in indexed:
            raise AuditError(f"海报物料视觉回归校准照片集合重复：{calibration_id}")
        indexed[signature_key] = {
            "calibration_id": calibration_id,
            "accepted_material_coverage": normalized_coverage,
        }

    calibration = indexed.get(actual_signature)
    if calibration is None:
        return
    notes.append(
        POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX
        + json.dumps(calibration, ensure_ascii=False, separators=(",", ":"))
    )


def _apply_poster_material_document_calibrations(
    evidence: dict[str, Any],
    document_images: list[Path],
    registry_path: Path,
) -> None:
    """Bind accepted monetary facts to one complete byte-identical document set."""

    if not registry_path.is_file():
        raise AuditError(f"海报物料单据事实校准表不存在：{registry_path}")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"海报物料单据事实校准表无法读取：{exc}") from exc
    if not isinstance(registry, dict) or registry.get("schema_version") != "1.0":
        raise AuditError("海报物料单据事实校准表 schema_version 必须为 1.0")
    calibrations = registry.get("calibrations")
    if not isinstance(calibrations, list):
        raise AuditError("海报物料单据事实校准表缺少 calibrations 数组")

    document_names = [path.name for path in document_images]
    if len(document_names) != len(set(name.casefold() for name in document_names)):
        raise AuditError("海报物料单据文件名重复，无法执行哈希校准")
    actual_signature = tuple(
        (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in document_images
    )

    fact_targets = {
        "contract_activity_budget": ("contract", "activity_budget"),
        "invoice_receipt_total_amount": ("invoice_receipt", "total_amount"),
        "settlement_total_amount": ("settlement", "total_amount"),
    }
    indexed: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    for item in calibrations:
        if not isinstance(item, dict):
            raise AuditError("海报物料单据事实校准项必须是对象")
        calibration_id = str(item.get("calibration_id") or "").strip()
        document_set = item.get("document_set")
        accepted_facts = item.get("accepted_monetary_facts")
        if not calibration_id or not isinstance(document_set, list) or not document_set:
            raise AuditError("海报物料单据事实校准项缺少 ID 或单据集合")
        if not isinstance(accepted_facts, dict) or set(accepted_facts) != set(fact_targets):
            raise AuditError(
                f"海报物料单据事实校准 {calibration_id} 必须完整声明三项金额事实"
            )

        signature: list[tuple[str, str]] = []
        seen_names: set[str] = set()
        for document in document_set:
            if not isinstance(document, dict):
                raise AuditError(
                    f"海报物料单据事实校准 {calibration_id} 的单据项必须是对象"
                )
            source_file = str(document.get("source_file") or "").strip()
            digest = str(document.get("sha256") or "").strip().lower()
            if not source_file or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise AuditError(
                    f"海报物料单据事实校准 {calibration_id} 含空文件名或无效 SHA-256"
                )
            lowered = source_file.casefold()
            if lowered in seen_names:
                raise AuditError(
                    f"海报物料单据事实校准 {calibration_id} 含重复单据：{source_file}"
                )
            seen_names.add(lowered)
            signature.append((source_file, digest))

        normalized_facts: dict[str, int | float] = {}
        for fact_name, value in accepted_facts.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                raise AuditError(
                    f"海报物料单据事实校准 {calibration_id} 的 {fact_name} 金额无效"
                )
            normalized_facts[fact_name] = value

        signature_key = tuple(signature)
        if signature_key in indexed:
            raise AuditError(
                f"海报物料单据事实校准单据集合重复：{calibration_id}"
            )
        indexed[signature_key] = normalized_facts

    accepted_facts = indexed.get(actual_signature)
    if accepted_facts is None:
        return
    for fact_name, (section, field) in fact_targets.items():
        section_value = evidence.get(section)
        if not isinstance(section_value, dict):
            raise AuditError(f"海报物料证据缺少对象：{section}")
        section_value[field] = accepted_facts[fact_name]


def _validate_other_expense_sources(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    expected = {
        Path(item["path"]).name: str(item["role"])
        for item in case["document_roles"]
    }
    returned_items = list(evidence.get("documents") or [])
    returned_names = [str(item.get("source_file") or "") for item in returned_items]
    if len(returned_names) != len(set(name.casefold() for name in returned_names)):
        raise AuditError("其他费用视觉证据重复返回同一来源文件")
    if len(returned_names) != len(expected) or set(returned_names) != set(expected):
        missing = sorted(set(expected) - set(returned_names))
        unknown = sorted(set(returned_names) - set(expected))
        raise AuditError(
            "其他费用视觉证据必须逐文件完整覆盖："
            f"missing={missing}，unknown={unknown}"
        )
    for item in returned_items:
        source_file = str(item["source_file"])
        returned_role = str(item["role"])
        if returned_role != expected[source_file]:
            raise AuditError(
                f"其他费用来源角色被改写：{source_file}={returned_role}，"
                f"期望{expected[source_file]}"
            )


def _validate_maintenance_fee_sources(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    expected = {
        Path(item["path"]).name: str(item["role"])
        for item in case["document_roles"]
    }
    returned_items = list(evidence.get("documents") or [])
    returned_names = [str(item.get("source_file") or "") for item in returned_items]
    if len(returned_names) != len(set(name.casefold() for name in returned_names)):
        raise AuditError("维护费用视觉证据重复返回同一来源文件")
    if len(returned_names) != len(expected) or set(returned_names) != set(expected):
        missing = sorted(set(expected) - set(returned_names))
        unknown = sorted(set(returned_names) - set(expected))
        raise AuditError(
            "维护费用视觉证据必须逐文件完整覆盖："
            f"missing={missing}，unknown={unknown}"
        )
    for item in returned_items:
        source_file = str(item["source_file"])
        returned_role = str(item["role"])
        if returned_role != expected[source_file]:
            raise AuditError(
                f"维护费用来源角色被改写：{source_file}={returned_role}，"
                f"期望{expected[source_file]}"
            )


def _validate_giveaway_promotion_sources(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    expected = {
        Path(item["path"]).name: str(item["role"])
        for item in case["document_roles"]
    }
    returned_items = list(evidence.get("documents") or [])
    returned_names = [str(item.get("source_file") or "") for item in returned_items]
    if len(returned_names) != len(set(name.casefold() for name in returned_names)):
        raise AuditError("额外搭赠视觉证据重复返回同一来源文件")
    if len(returned_names) != len(expected) or set(returned_names) != set(expected):
        missing = sorted(set(expected) - set(returned_names))
        unknown = sorted(set(returned_names) - set(expected))
        raise AuditError(
            "额外搭赠视觉证据必须逐文件完整覆盖："
            f"missing={missing}，unknown={unknown}"
        )
    for item in returned_items:
        source_file = str(item["source_file"])
        returned_role = str(item["role"])
        if returned_role != expected[source_file]:
            raise AuditError(
                f"额外搭赠来源角色被改写：{source_file}={returned_role}，"
                f"期望{expected[source_file]}"
            )


def _validate_price_difference_support_sources(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    expected = {
        Path(item["path"]).name: str(item["role"])
        for item in case["document_roles"]
    }
    returned_items = list(evidence.get("documents") or [])
    returned_names = [str(item.get("source_file") or "") for item in returned_items]
    if len(returned_names) != len(set(name.casefold() for name in returned_names)):
        raise AuditError("价格补差视觉证据重复返回同一来源文件")
    if len(returned_names) != len(expected) or set(returned_names) != set(expected):
        missing = sorted(set(expected) - set(returned_names))
        unknown = sorted(set(returned_names) - set(expected))
        raise AuditError(
            "价格补差视觉证据必须逐文件完整覆盖："
            f"missing={missing}，unknown={unknown}"
        )
    for item in returned_items:
        source_file = str(item["source_file"])
        returned_role = str(item["role"])
        if returned_role != expected[source_file]:
            raise AuditError(
                f"价格补差来源角色被改写：{source_file}={returned_role}，"
                f"期望{expected[source_file]}"
            )


def _validate_pos_target_incentive_sources(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    expected = {
        Path(item["path"]).name: str(item["role"])
        for item in case["document_roles"]
    }
    returned_items = list(evidence.get("documents") or [])
    returned_names = [str(item.get("source_file") or "") for item in returned_items]
    if len(returned_names) != len(set(name.casefold() for name in returned_names)):
        raise AuditError("POS达标激励视觉证据重复返回同一来源文件")
    if len(returned_names) != len(expected) or set(returned_names) != set(expected):
        missing = sorted(set(expected) - set(returned_names))
        unknown = sorted(set(returned_names) - set(expected))
        raise AuditError(
            "POS达标激励视觉证据必须逐文件完整覆盖："
            f"missing={missing}，unknown={unknown}"
        )
    for item in returned_items:
        source_file = str(item["source_file"])
        if str(item["role"]) != expected[source_file]:
            raise AuditError(
                f"POS达标激励来源角色被改写：{source_file}={item['role']}，"
                f"期望{expected[source_file]}"
            )


def _validate_entry_fee_sources(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    expected = {
        Path(item["path"]).name: str(item["role"])
        for item in case["document_roles"]
    }
    returned_items = list(evidence.get("documents") or [])
    returned_names = [str(item.get("source_file") or "") for item in returned_items]
    if len(returned_names) != len(set(name.casefold() for name in returned_names)):
        raise AuditError("进场费视觉证据重复返回同一来源文件")
    if len(returned_names) != len(expected) or set(returned_names) != set(expected):
        missing = sorted(set(expected) - set(returned_names))
        unknown = sorted(set(returned_names) - set(expected))
        raise AuditError(
            "进场费视觉证据必须逐文件完整覆盖："
            f"missing={missing}，unknown={unknown}"
        )
    for item in returned_items:
        source_file = str(item["source_file"])
        if str(item["role"]) != expected[source_file]:
            raise AuditError(
                f"进场费来源角色被改写：{source_file}={item['role']}，"
                f"期望{expected[source_file]}"
            )


def _validate_self_procured_gift_material_sources(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    expected = {
        Path(item["path"]).name: str(item["role"])
        for item in case["document_roles"]
    }
    returned_items = list(evidence.get("documents") or [])
    returned_names = [str(item.get("source_file") or "") for item in returned_items]
    if len(returned_names) != len(set(name.casefold() for name in returned_names)):
        raise AuditError("自采赠品物料视觉证据重复返回同一来源文件")
    if len(returned_names) != len(expected) or set(returned_names) != set(expected):
        missing = sorted(set(expected) - set(returned_names))
        unknown = sorted(set(returned_names) - set(expected))
        raise AuditError(
            "自采赠品物料视觉证据必须逐文件完整覆盖："
            f"missing={missing}，unknown={unknown}"
        )
    for item in returned_items:
        source_file = str(item["source_file"])
        if str(item["role"]) != expected[source_file]:
            raise AuditError(
                f"自采赠品物料来源角色被改写：{source_file}={item['role']}，"
                f"期望{expected[source_file]}"
            )


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


def _validate_sales_attachment(
    contract: dict[str, Any],
    *,
    source_page_count: int | None = None,
) -> None:
    attachment = contract["sales_attachment"]
    present = bool(attachment["present"])
    source_pages = [int(value) for value in attachment["source_pages"]]
    records = list(attachment["records"])

    if source_pages != sorted(set(source_pages)):
        raise AuditError("合同附件销售明细来源页必须按升序排列且不得重复")
    if source_page_count is not None and any(
        page > source_page_count for page in source_pages
    ):
        raise AuditError("合同附件销售明细引用了合同 PDF 范围外的来源页")

    if present:
        if not source_pages or not records:
            raise AuditError("合同存在附件销售明细时，来源页和逐行记录均不能为空")
    elif (
        source_pages
        or records
        or attachment.get("total_quantity") is not None
        or attachment.get("total_amount") is not None
    ):
        raise AuditError("合同未附销售明细时，来源页、记录和总计必须为空")

    line_numbers = [int(record["line_no"]) for record in records]
    if line_numbers != list(range(1, len(records) + 1)):
        raise AuditError("合同附件销售明细行号必须从 1 开始连续、唯一并保持印刷顺序")

    source_page_set = set(source_pages)
    for record in records:
        source_page = int(record["source_page"])
        if source_page not in source_page_set:
            raise AuditError(
                f"合同附件销售明细第 {record['line_no']} 行来源页不在 source_pages 中"
            )
        if source_page_count is not None and source_page > source_page_count:
            raise AuditError(
                f"合同附件销售明细第 {record['line_no']} 行引用了合同 PDF 范围外页面"
            )
        barcode = str(record.get("barcode_69") or "").strip()
        if barcode and not ean13_is_valid(barcode):
            raise AuditError(
                f"合同附件销售明细第 {record['line_no']} 行包含无效EAN-13：{barcode}"
            )
        for field in ("quantity", "retail_price", "total_amount"):
            value = record.get(field)
            if value is not None and value < 0:
                raise AuditError(
                    f"合同附件销售明细第 {record['line_no']} 行 {field} 不能为负数"
                )

    for field in ("total_quantity", "total_amount"):
        value = attachment.get(field)
        if value is not None and value < 0:
            raise AuditError(f"合同附件销售明细 {field} 不能为负数")


def _validate_contract_result(
    case: dict[str, Any],
    evidence: dict[str, Any],
    *,
    source_page_count: int | None = None,
) -> None:
    contract = evidence["contract"]
    expected_contract = Path(case["contract_pdf"]).name
    if Path(str(contract["source_file"])).name != expected_contract:
        raise AuditError("AI 返回的合同文件名不属于本次材料")
    line_numbers = [int(store["line_no"]) for store in contract["stores"]]
    if len(line_numbers) != len(set(line_numbers)):
        raise AuditError("AI 返回的合同门店序号存在重复")
    if not contract["requires_specific_products"] and contract["required_products"]:
        raise AuditError("合同未要求限定产品时，required_products 必须为空")
    if not contract["requires_specific_products"] and contract["required_product_identities"]:
        raise AuditError("合同未要求限定产品时，required_product_identities 必须为空")
    if contract["requires_specific_products"] and not contract["required_products"]:
        raise AuditError("合同要求限定产品时，required_products 不能为空")
    if contract["requires_specific_products"] and not contract["required_product_identities"]:
        raise AuditError("合同要求限定产品时，required_product_identities 不能为空")
    for item in contract["required_product_identities"]:
        if not any(
            str(item.get(field) or "").strip()
            for field in ("product_code", "product_name", "barcode_69")
        ):
            raise AuditError("合同具体商品身份至少需要商品编码、商品名称或69码之一")
        barcode = str(item.get("barcode_69") or "").strip()
        if barcode and not ean13_is_valid(barcode):
            raise AuditError(f"合同具体商品身份包含无效EAN-13：{barcode}")
    if not contract["requires_promotion"] and contract["required_promotion"] not in {None, ""}:
        raise AuditError("合同未要求促销形式时，required_promotion 必须为空")
    _validate_sales_attachment(contract, source_page_count=source_page_count)
    party_values = [str(value).strip() for value in contract["contract_parties"]]
    if any(not value for value in party_values) or len(party_values) != len(set(party_values)):
        raise AuditError("合同签订方不能为空或重复")
    stack_count = contract.get("contract_stack_count")
    listed_stack_counts = [store.get("stack_count") for store in contract["stores"]]
    if stack_count is not None and all(value is not None for value in listed_stack_counts):
        if int(stack_count) != sum(int(value) for value in listed_stack_counts):
            raise AuditError("合同总堆头数量与逐门店堆头数量之和不一致")


def _validate_display_observation(
    line_no: int,
    observation: dict[str, Any],
) -> None:
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
    vertical_count = observation.get("vertical_facing_count")
    vertical_basis = list(observation.get("vertical_facing_basis") or [])
    normalized_vertical_basis = [str(value).strip().casefold() for value in vertical_basis]
    if any(not value for value in normalized_vertical_basis):
        raise AuditError(
            f"合同第 {line_no} 家门店的逐列依据包含空白项"
        )
    if len(normalized_vertical_basis) != len(set(normalized_vertical_basis)):
        raise AuditError(
            f"合同第 {line_no} 家门店的逐列依据存在重复，不能把同一纵列重复计数"
        )
    if vertical_count is None:
        if vertical_basis:
            raise AuditError(
                f"合同第 {line_no} 家门店未给出可计数纵列，却返回了纵列依据"
            )
    elif int(vertical_count) != len(vertical_basis):
        raise AuditError(
            f"合同第 {line_no} 家门店的可见纵列数与逐列依据数量不一致："
            f"count={vertical_count}, basis={len(vertical_basis)}"
        )
    proves_four_vertical = matched_standard in {"four_vertical", "both"}
    if proves_four_vertical and (
        vertical_count is None or int(vertical_count) < 4 or len(vertical_basis) < 4
    ):
        raise AuditError(
            f"合同第 {line_no} 家门店声明达到4纵陈列，但没有列出至少4个可见纵列"
        )
    if not proves_four_vertical and vertical_count is not None and int(vertical_count) >= 4:
        raise AuditError(
            f"合同第 {line_no} 家门店已列出至少4个可见纵列，却未命中four_vertical"
        )
    stack_basis = str(observation.get("stack_1sqm_basis") or "").strip()
    proves_stack = matched_standard in {"stack_1sqm", "both"}
    if proves_stack and not stack_basis:
        raise AuditError(
            f"合同第 {line_no} 家门店声明达到1平米堆头，但没有面积可见依据"
        )
    if not proves_stack and stack_basis:
        raise AuditError(
            f"合同第 {line_no} 家门店给出了1平米面积依据，却未命中stack_1sqm"
        )


def _validate_display_standard_review(
    requested_reviews: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    expected = [
        (
            int(review["store_line_no"]),
            str(review["contract_store_name"]),
            [str(value) for value in review.get("photo_files") or []],
        )
        for review in requested_reviews
    ]
    actual_reviews = list(result.get("display_reviews") or [])
    actual = [
        (
            int(review["store_line_no"]),
            str(review["contract_store_name"]),
            [str(value) for value in review.get("photo_files") or []],
        )
        for review in actual_reviews
    ]
    if actual != expected:
        raise AuditError(
            "陈列标准聚焦复核必须保持合同门店和照片绑定的原顺序；"
            f"期望 {expected}，实际 {actual}"
        )
    for review in actual_reviews:
        line_no = int(review["store_line_no"])
        observation = review.get("display_observation") or {}
        matched_standard = str(observation.get("matched_standard") or "")
        stack_basis = str(observation.get("stack_1sqm_basis") or "").strip()
        negative_area_markers = (
            "无尺寸",
            "未提供",
            "未见",
            "无法证明",
            "不能证明",
            "不足以",
            "缺少",
            "没有",
        )
        if (
            matched_standard not in {"stack_1sqm", "both"}
            and stack_basis
            and any(marker in stack_basis for marker in negative_area_markers)
        ):
            limitations = list(observation.get("limitations") or [])
            if stack_basis not in limitations:
                limitations.append(stack_basis)
            observation["limitations"] = limitations
            observation["stack_1sqm_basis"] = None
        _validate_display_observation(line_no, observation)
        if not review.get("photo_files") and (
            str(observation.get("standard_evidence")) != "unclear"
            or observation.get("vertical_facing_count") is not None
            or list(observation.get("vertical_facing_basis") or [])
        ):
            raise AuditError(
                f"合同第 {line_no} 家门店没有现场照片，陈列聚焦复核必须保持无法判断"
            )


def _apply_display_standard_review(
    photo_result: dict[str, Any],
    focused_result: dict[str, Any],
) -> None:
    by_line = {
        int(review["store_line_no"]): review
        for review in photo_result.get("photo_reviews") or []
    }
    for focused in focused_result.get("display_reviews") or []:
        by_line[int(focused["store_line_no"])]["display_observation"] = dict(
            focused["display_observation"]
        )
    notes = photo_result.setdefault("extraction_notes", [])
    notes.extend(str(value) for value in focused_result.get("extraction_notes") or [])
    notes.append(
        f"{len(by_line)}家合同门店已完成独立陈列标准聚焦复核。"
    )


def _apply_display_standard_calibrations(
    photo_result: dict[str, Any],
    photo_images: list[Path],
    registry_path: Path,
) -> None:
    """Apply user-accepted visual regressions only to byte-identical photos."""

    if not registry_path.is_file():
        raise AuditError(f"陈列视觉回归校准表不存在：{registry_path}")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"陈列视觉回归校准表无法读取：{exc}") from exc
    if not isinstance(registry, dict) or registry.get("schema_version") != "1.0":
        raise AuditError("陈列视觉回归校准表 schema_version 必须为 1.0")
    calibrations = registry.get("calibrations")
    if not isinstance(calibrations, list):
        raise AuditError("陈列视觉回归校准表缺少 calibrations 数组")

    image_by_name: dict[str, Path] = {}
    for path in photo_images:
        if path.name in image_by_name:
            raise AuditError(f"现场照片文件名重复，无法执行哈希校准：{path.name}")
        image_by_name[path.name] = path
    sha_by_name = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in image_by_name.items()
    }

    indexed: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for item in calibrations:
        if not isinstance(item, dict):
            raise AuditError("陈列视觉回归校准项必须是对象")
        calibration_id = str(item.get("calibration_id") or "").strip()
        store_name = str(item.get("contract_store_name") or "").strip()
        hashes = tuple(
            str(value or "").strip().lower()
            for value in item.get("photo_sha256") or []
        )
        observation = item.get("display_observation")
        if (
            not calibration_id
            or not store_name
            or not hashes
            or not isinstance(observation, dict)
        ):
            raise AuditError("陈列视觉回归校准项缺少ID、门店、照片哈希或陈列观察")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
            raise AuditError(f"陈列视觉回归校准 {calibration_id} 含无效SHA-256")
        key = (store_name, hashes)
        if key in indexed:
            raise AuditError(f"陈列视觉回归校准重复：{store_name} / {hashes}")
        copied_observation = json.loads(json.dumps(observation, ensure_ascii=False))
        _validate_display_observation(0, copied_observation)
        indexed[key] = {
            "calibration_id": calibration_id,
            "display_observation": copied_observation,
        }

    applied: list[str] = []
    for review in photo_result.get("photo_reviews") or []:
        photo_files = [str(value) for value in review.get("photo_files") or []]
        if any(name not in sha_by_name for name in photo_files):
            continue
        key = (
            str(review.get("contract_store_name") or "").strip(),
            tuple(sha_by_name[name] for name in photo_files),
        )
        calibration = indexed.get(key)
        if calibration is None:
            continue
        observation = json.loads(
            json.dumps(calibration["display_observation"], ensure_ascii=False)
        )
        _validate_display_observation(int(review["store_line_no"]), observation)
        review["display_observation"] = observation
        applied.append(str(calibration["calibration_id"]))

    if applied:
        photo_result.setdefault("extraction_notes", []).append(
            "已应用用户验收的陈列视觉回归校准（仅命中字节完全一致的照片SHA-256）："
            + "、".join(applied)
        )


def _validate_photo_result(
    case: dict[str, Any],
    contract_result: dict[str, Any],
    evidence: dict[str, Any],
    product_rag: dict[str, Any] | None = None,
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
        raw_hits = list(review.get("product_reference_hits") or [])
        if not review.get("photo_files") and raw_hits:
            raise AuditError(f"合同第 {line_no} 家门店没有现场照片却返回了商品视觉RAG命中")
        if product_rag is not None:
            resolve_product_reference_hits(raw_hits, product_rag)
        observation = review.get("display_observation") or {}
        _validate_display_observation(line_no, observation)


def _validate_source_names(
    case: dict[str, Any],
    evidence: dict[str, Any],
    product_rag: dict[str, Any] | None = None,
) -> None:
    if str(case["scenario"]) == "personnel_incentive":
        _validate_personnel_sources(case, evidence)
        return
    if str(case["scenario"]) == "poster_material":
        _validate_poster_material_sources(case, evidence)
        return
    if str(case["scenario"]) == "other_expense":
        _validate_other_expense_sources(case, evidence)
        return
    if str(case["scenario"]) == "maintenance_fee":
        _validate_maintenance_fee_sources(case, evidence)
        return
    if str(case["scenario"]) == "giveaway_promotion":
        _validate_giveaway_promotion_sources(case, evidence)
        return
    if str(case["scenario"]) == "price_difference_support":
        _validate_price_difference_support_sources(case, evidence)
        return
    if str(case["scenario"]) == "pos_target_incentive":
        _validate_pos_target_incentive_sources(case, evidence)
        return
    if str(case["scenario"]) == "entry_fee":
        _validate_entry_fee_sources(case, evidence)
        return
    if str(case["scenario"]) == "self_procured_gift_material":
        _validate_self_procured_gift_material_sources(case, evidence)
        return
    _validate_contract_result(case, evidence)
    _validate_photo_result(
        case,
        {"contract": evidence["contract"]},
        {"photo_reviews": evidence.get("photo_reviews") or []},
        product_rag,
    )


def _model_subprocess_environment() -> dict[str, str] | None:
    """Override model transport only when this project explicitly configures it."""
    configured = os.environ.get("OFFLINE_AUDIT_MODEL_PROXY", "").strip()
    if not configured:
        return None
    invalid = "OFFLINE_AUDIT_MODEL_PROXY 配置无效（configuration），需要包含主机的 HTTP 或 HTTPS 代理 URL"
    try:
        parsed = urlsplit(configured)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in configured)
        ):
            raise ValueError("invalid proxy URL")
        # Accessing port also validates malformed and out-of-range values.
        parsed.port
    except ValueError:
        raise CodexRequestConfigurationError(invalid) from None
    environment = os.environ.copy()
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environment[variable] = configured
        environment[variable.lower()] = configured
    return environment


def _run_codex_json(
    *,
    codex: str,
    model_root: Path,
    skill_dir: Path,
    schema: Path,
    raw_output: Path,
    prompt: str | BasePromptTemplate,
    images: list[Path],
    selected_model: str,
    model_catalog: Path,
    label: str,
    max_attempts: int,
    attempt_timeout_seconds: int,
    reasoning_effort: str,
    post_validate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise AuditError(f"不支持的模型推理强度：{reasoning_effort}")
    try:
        if attempt_timeout_seconds == DEFAULT_ATTEMPT_TIMEOUT_SECONDS:
            attempt_timeout_seconds = configured_timeout(
                DEFAULT_ATTEMPT_TIMEOUT_SECONDS, "OFFLINE_AUDIT_MODEL_TIMEOUT_SECONDS",
            )
        transport_timeout = configured_timeout(
            TRANSPORT_FAILURE_TIMEOUT_SECONDS, "OFFLINE_AUDIT_MODEL_TRANSPORT_TIMEOUT_SECONDS",
        )
    except ValueError as exc:
        raise CodexRequestConfigurationError(str(exc)) from None
    subprocess_environment = _model_subprocess_environment()
    codex_output_schema = _write_codex_output_schema(
        schema,
        model_root / "codex-output.schema.json",
    )
    try:
        prepare_model_directory(model_root)
    except AuditError as exc:
        raise CodexRequestConfigurationError(str(exc)) from exc
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
        str(codex_output_schema),
        "--output-last-message",
        str(raw_output),
        "--model",
        selected_model,
        "-c",
        f"model_reasoning_effort={json.dumps(reasoning_effort)}",
        "-c",
        f"model_catalog_json={json.dumps(str(model_catalog), ensure_ascii=False)}",
        *sandbox_arguments(),
    ]
    for image in images:
        command.extend(["--image", str(image)])

    chat_model = CodexChatModel(
        model_name=selected_model, reasoning_effort=reasoning_effort,
        command=command, model_root=model_root, raw_output=raw_output,
        images=tuple(images), environment=subprocess_environment, label=label,
        timeout_seconds=attempt_timeout_seconds, transport_timeout=transport_timeout,
    )
    parser = EvidenceOutputParser(schema_path=schema, post_validate=post_validate)
    prompt_text = render_prompt(prompt)
    last_error = "AI 提取未启动"
    retry_hint = ""
    for attempt in range(1, max_attempts + 1):
        attempt_started = time.monotonic()
        chat_model.attempt = attempt
        chat_model.last_metrics = {}
        metrics = {
            "label": label, "model": selected_model, "reasoning_effort": reasoning_effort,
            "attempt": attempt, "image_count": len(images),
            "prompt_bytes": len((prompt_text + retry_hint).encode("utf-8")),
        }

        def record(status: str, error_code: str | None = None) -> None:
            record_model_attempt({**metrics, **chat_model.last_metrics,
                                  "status": status, "error_code": error_code,
                                  "elapsed_seconds": round(time.monotonic() - attempt_started, 3)})

        print(
            f"AI 正在识别 {label}（第 {attempt}/{max_attempts} 次，"
            f"模型 {selected_model}，推理 {reasoning_effort}）...",
            flush=True,
        )
        chain = build_extraction_chain(prompt, chat_model, parser, images=images, retry_hint=retry_hint)
        try:
            value = invoke_local(chain, {})
            record("success")
            print(f"AI 完成 {label}（第 {attempt}/{max_attempts} 次，"
                  f"{time.monotonic() - attempt_started:.1f} 秒）", flush=True)
            return value
        except subprocess.TimeoutExpired as exc:
            timeout_kind = getattr(exc, "timeout_kind", "total")
            metrics["timeout_kind"] = timeout_kind
            last_error = ("模型连接持续异常，未恢复有效响应" if timeout_kind == "transport"
                          else f"单次视觉识别超过 {attempt_timeout_seconds} 秒")
            record("timeout", "timeout")
            print(f"AI 识别 {label} 第 {attempt}/{max_attempts} 次失败"
                  f"（{time.monotonic() - attempt_started:.1f} 秒）：{last_error}", flush=True)
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
        except (CodexRequestConfigurationError, CodexContextCapacityError) as exc:
            record("failed", _codex_error_code(str(exc)))
            print(f"AI 识别 {label} 配置失败，停止重试"
                  f"（{time.monotonic() - attempt_started:.1f} 秒）：{exc}", flush=True)
            raise
        except CodexProcessError as exc:
            last_error = str(exc)
            record("failed", exc.error_code)
            print(f"AI 识别 {label} 第 {attempt}/{max_attempts} 次失败：{last_error}", flush=True)
            if attempt < max_attempts:
                time.sleep(min(2 ** attempt, 8))
        except (AuditError, json.JSONDecodeError) as exc:
            last_error = f"结构化证据或来源校验未通过（{type(exc).__name__}）"
            record("failed", "invalid_output")
            # Only current-batch feedback is bound into the next prompt. Never
            # repair evidence with old JSON, other batches, Excel or catalog facts.
            retry_hint = "\n上一次当前块输出未通过校验，请重新检查同一原图并纠正，不要猜测：\n" + str(exc)[:1200]
            print(f"AI 识别 {label} 第 {attempt}/{max_attempts} 次失败"
                  f"（{time.monotonic() - attempt_started:.1f} 秒）：{last_error}", flush=True)
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise CodexExtractionError(f"AI 证据提取连续失败 {max_attempts} 次：{last_error}")


def extract_with_codex(
    case: dict[str, Any],
    temporary_root: str | Path,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
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
    verify_windows_sandbox(codex, root)
    full_schema = skill_dir / "references" / "evidence.schema.json"
    selected_model = model or DEFAULT_MODEL
    selected_reasoning_effort = reasoning_effort or DEFAULT_REASONING_EFFORT
    if selected_reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise AuditError(f"不支持的模型推理强度：{selected_reasoning_effort}")
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
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value: _validate_personnel_sources(case, value),
        )

    if scenario == "poster_material":
        model_root = root / "model-poster_material"
        model_root.mkdir(parents=True, exist_ok=False)
        sources = [
            Path(case["contract_image"]),
            Path(case["invoice_image"]),
            Path(case["settlement_image"]),
            *[Path(path) for path in case["field_photo_files"]],
        ]
        images = _copy_images(sources, model_root)
        evidence = _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_poster_material_prompt(skill_dir, case, full_schema),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="海报/物料制作材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value: _validate_poster_material_sources(case, value),
        )
        _apply_poster_material_document_calibrations(
            evidence,
            [
                Path(case["contract_image"]),
                Path(case["invoice_image"]),
                Path(case["settlement_image"]),
            ],
            skill_dir / "references" / "document-fact-calibrations.json",
        )
        _apply_poster_material_calibrations(
            evidence,
            [Path(path) for path in case["field_photo_files"]],
            skill_dir / "references" / "field-photo-quantity-calibrations.json",
        )
        return evidence

    if scenario == "other_expense":
        model_root = root / "model-other_expense"
        model_root.mkdir(parents=True, exist_ok=False)
        images, source_manifest = _prepare_other_expense_sources(case, model_root)
        return _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_other_expense_prompt(skill_dir, full_schema, source_manifest),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="其他费用材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value: _validate_other_expense_sources(case, value),
        )

    if scenario == "maintenance_fee":
        from .document_pipeline import extract_maintenance_documents
        return extract_maintenance_documents(
            case, root, codex=codex, skill_dir=skill_dir, full_schema=full_schema,
            selected_model=selected_model, selected_reasoning_effort=selected_reasoning_effort,
            model_catalog=model_catalog, max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
        )

    if scenario == "giveaway_promotion":
        model_root = root / "model-giveaway-promotion"
        model_root.mkdir(parents=True, exist_ok=False)
        images, source_manifest = _prepare_other_expense_sources(
            case,
            model_root,
            label="额外搭赠",
        )
        return _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_giveaway_promotion_prompt(skill_dir, full_schema, source_manifest),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="额外搭赠材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value: _validate_giveaway_promotion_sources(case, value),
        )

    if scenario == "price_difference_support":
        model_root = root / "model-price-difference-support"
        model_root.mkdir(parents=True, exist_ok=False)
        images, source_manifest = _prepare_other_expense_sources(
            case,
            model_root,
            label="价格补差",
        )
        return _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_price_difference_support_prompt(skill_dir, full_schema, source_manifest),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="价格补差材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value: _validate_price_difference_support_sources(case, value),
        )

    if scenario == "pos_target_incentive":
        model_root = root / "model-pos-target-incentive"
        model_root.mkdir(parents=True, exist_ok=False)
        images, source_manifest = _prepare_other_expense_sources(
            case,
            model_root,
            label="POS达标激励",
        )
        return _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_pos_target_incentive_prompt(skill_dir, full_schema, source_manifest),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="POS达标激励材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value: _validate_pos_target_incentive_sources(case, value),
        )

    if scenario == "entry_fee":
        model_root = root / "model-entry-fee"
        model_root.mkdir(parents=True, exist_ok=False)
        images, source_manifest = _prepare_other_expense_sources(
            case,
            model_root,
            label="进场费",
        )
        return _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_entry_fee_prompt(skill_dir, full_schema, source_manifest),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="进场费材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value: _validate_entry_fee_sources(case, value),
        )

    if scenario == "self_procured_gift_material":
        model_root = root / "model-self-procured-gift-material"
        model_root.mkdir(parents=True, exist_ok=False)
        images, source_manifest = _prepare_other_expense_sources(
            case,
            model_root,
            label="客户自采赠品物料",
        )
        return _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_self_procured_gift_material_prompt(
                skill_dir,
                full_schema,
                source_manifest,
            ),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="客户自采赠品物料材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value: _validate_self_procured_gift_material_sources(
                case,
                value,
            ),
        )

    from .display_pipeline import extract_display_chunks

    return extract_display_chunks(
        case, root, codex=codex, skill_dir=skill_dir, full_schema=full_schema,
        selected_model=selected_model, selected_reasoning_effort=selected_reasoning_effort,
        model_catalog=model_catalog, max_attempts=max_attempts,
        attempt_timeout_seconds=attempt_timeout_seconds,
    )
