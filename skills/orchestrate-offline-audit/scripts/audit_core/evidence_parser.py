"""Strict LangChain parser: parsing must never repair or invent evidence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from langchain_core.output_parsers import BaseOutputParser
from pydantic import Field

from .common import AuditError


class EvidenceOutputParser(BaseOutputParser[dict[str, Any]]):
    schema_path: Path = Field(exclude=True, repr=False)
    post_validate: Callable[[dict[str, Any]], None] | None = Field(default=None, exclude=True, repr=False)

    @property
    def _type(self) -> str:
        return "offline_audit_evidence"

    def parse(self, text: str) -> dict[str, Any]:
        from . import codex_runner as api

        value = json.loads(api._strip_json_fence(text))
        if not isinstance(value, dict):
            raise AuditError("AI 证据顶层必须是 JSON 对象")
        if api.reported_material_access_failure(value):
            raise api.CodexRequestConfigurationError(
                "模型报告必需材料读取受阻（configuration），不能以无法确认代替成功复核"
            )
        api.validate_json(value, self.schema_path)
        if self.post_validate is not None:
            self.post_validate(value)
        return value

    def get_format_instructions(self) -> str:
        return "只返回符合本批 JSON Schema 的完整对象，保留原始来源，不补造不可见事实。"
