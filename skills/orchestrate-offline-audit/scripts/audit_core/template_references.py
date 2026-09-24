"""User-designated template assets, kept outside current-case evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


TEMPLATE_CHECKS = {
    "promotional_display": ("settlement_template_seal", "settlement"),
    "poster_material": ("settlement_template_seal", "settlement"),
    "entry_fee": ("entry_agreement", "entry_agreement"),
}


def template_guide(skill: Path) -> str:
    path = skill / "references/template-reference.md"
    return path.read_text(encoding="utf-8") if path.is_file() else "未配置本类模板参考说明。"


def _checked_asset(skill: Path, record: dict) -> Path:
    path = (skill / record["file"]).resolve()
    if not path.is_relative_to((skill / "assets/templates").resolve()):
        raise ValueError("模板文件路径不在指定资产目录")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != record["sha256"]:
        raise ValueError("模板文件与指定来源指纹不一致")
    return path


def template_comparison(skill: Path, scenario: str) -> tuple[list[dict], list[Path]]:
    """Return reference-only image metadata; never create case unit/source IDs.

    A missing/broken reference is a system comparison gap, not a missing customer
    material. Other five types have examples, but no template compliance check.
    """
    if scenario not in TEMPLATE_CHECKS:
        return [], []
    rule_id, material_id = TEMPLATE_CHECKS[scenario]
    try:
        manifest = json.loads((skill / "references/template-manifest.json").read_text(encoding="utf-8"))
        if manifest["scenario"] != scenario:
            raise ValueError("模板来源清单的类型不匹配")
        selected = [record for record in manifest["documents"]
                    if record["usage"] == "template_check" and record["role"] == material_id
                    and record["rule_ids"] == [rule_id]]
        if not selected:
            raise ValueError("未配置本类指定模板")
        references, images = [], []
        for record in selected:
            _checked_asset(skill, record)
            if not record["preview_images"]:
                raise ValueError("指定模板缺少可查看页面")
            for preview in record["preview_images"]:
                path = _checked_asset(skill, preview)
                images.append(path)
                references.append({"reference_only": True, "rule_id": rule_id,
                                   "title": record["title"], "page": preview["page"], "image": path.name})
        return references, images
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [{"reference_only": True, "rule_id": rule_id, "unavailable": True,
                 "reason": f"系统指定模板暂时无法读取：{exc}。模板比对记无法核验，不算客户缺交资料。"}], []
