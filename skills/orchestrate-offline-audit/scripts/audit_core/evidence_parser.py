"""Strict LangChain parser: parsing must never repair or invent evidence."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable

from langchain_core.output_parsers import BaseOutputParser
from pydantic import Field, PrivateAttr

from .common import AuditError


class ReadingSourceCoverageError(AuditError):
    """Only server-owned IDs and counts enter the existing bounded retry hint."""

    def __init__(self, expected: list[str], actual: list[str]):
        if (not 1 <= len(expected) <= 6 or len(set(expected)) != len(expected)
                or any(not isinstance(uid, str) or len(uid) > 96
                       or re.fullmatch(r"image-(?:[0-9a-f]{24}|[0-9a-f]{64}|uncached-[0-9]+-[0-9]+)", uid) is None
                       for uid in expected)):
            raise AuditError("材料识别本批来源约束无效")
        counts = dict.fromkeys(expected, 0)
        unknown_count, unknown_paths = 0, []
        for index, uid in enumerate(actual):
            if uid in counts:
                counts[uid] += 1
            else:
                unknown_count += 1
                if len(unknown_paths) < 6:
                    unknown_paths.append(f"documents/{index}/unit_id")
        context = {"expected_count": len(expected), "actual_count": len(actual),
                   "missing": [uid for uid in expected if not counts[uid]],
                   "duplicate": [uid for uid in expected if counts[uid] > 1],
                   "unknown_count": unknown_count, "unknown_paths": unknown_paths,
                   "omitted_unknown_paths": unknown_count > len(unknown_paths)}
        encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        # Never echo unknown model strings, facts, or arbitrary field values.
        # Existing transports send at most 1200 characters of str(error).
        while len(encoded.encode("utf-8")) > 850 and context["unknown_paths"]:
            context["unknown_paths"].pop()
            context["omitted_unknown_paths"] = True
            encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        self.validation_paths = ["documents", *context["unknown_paths"]]
        self.retry_context = encoded
        super().__init__("材料识别必须完整且唯一覆盖本批图片。仅逐字使用本批Schema的unit_id，不改写事实或补造来源。" + encoded)


def _reading_expected_ids(schema_path: Path) -> list[str] | None:
    """Only the exact, bounded per-batch DOCUMENT contract enables reading hints."""
    from .pdf_evidence import DOCUMENT_SCHEMA

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        documents = schema["properties"]["documents"]
        expected = documents["items"]["properties"]["unit_id"].pop("enum")
        minimum, maximum = documents.pop("minItems"), documents.pop("maxItems")
        if (schema != DOCUMENT_SCHEMA or not isinstance(expected, list)
                or type(minimum) is not int or type(maximum) is not int
                or minimum != len(expected) or maximum != len(expected)
                or not 1 <= len(expected) <= 6 or len(set(expected)) != len(expected)
                or any(not isinstance(uid, str) or len(uid) > 96
                       or re.fullmatch(r"image-(?:[0-9a-f]{24}|[0-9a-f]{64}|uncached-[0-9]+-[0-9]+)", uid) is None
                       for uid in expected)):
            return None
        return expected
    except (OSError, KeyError, TypeError, ValueError):
        return None


class ReadingStructureError(AuditError):
    """A closed shape reminder, with no response keys, values or reasoning."""

    def __init__(self, expected: list[str], root_type: str, kinds: list[str]):
        self.root_type = root_type
        self.schema_kinds = kinds
        context = {"expected_root": "object", "required_root_field": "documents",
                   "document_fields": ["unit_id", "roles", "facts", "limitations"],
                   "expected_count": len(expected), "expected_unit_ids": expected,
                   "actual_root_type": root_type, "schema_kinds": list(kinds)}
        prefix = ("仅返回含documents数组的JSON对象；每项的roles、facts、limitations为字符串数组。"
                  "本批ID各一次，不增加字段或补造事实。")
        encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        while len((prefix + encoded).encode("utf-8")) > 1000 and context["schema_kinds"]:
            context["schema_kinds"].pop()
            encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        super().__init__(prefix + encoded)


def _reading_structure_error(value: Any, schema_path: Path, *, invalid_json: bool = False) -> ReadingStructureError | None:
    expected = _reading_expected_ids(schema_path)
    if expected is None:
        return None
    if invalid_json:
        root_type, kinds = "invalid_json", []
    else:
        from jsonschema import Draft202012Validator

        root_type = ("null" if value is None else "boolean" if isinstance(value, bool)
                     else "object" if isinstance(value, dict) else "array" if isinstance(value, list)
                     else "string" if isinstance(value, str) else "number")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        allowed = {"type", "required", "additionalProperties", "enum", "minItems", "maxItems", "minLength"}
        kinds = []
        for index, error in enumerate(Draft202012Validator(schema).iter_errors(value)):
            if error.validator in allowed and error.validator not in kinds:
                kinds.append(error.validator)
            if index >= 15 or len(kinds) == 6:
                break
    return ReadingStructureError(expected, root_type, kinds)


def _reading_schema_error(value: dict, schema_path: Path) -> AuditError | None:
    """Recognize only the exact per-batch DOCUMENT schema, not other evidence."""
    from .pdf_evidence import DOCUMENT_SCHEMA, validate

    expected = _reading_expected_ids(schema_path)
    if expected is None:
        return None
    try:
        # A malformed fact, extra reasoning field, or other shape error stays a
        # schema error; source diagnostics never make invalid evidence acceptable.
        try:
            validate(value, DOCUMENT_SCHEMA)
        except AuditError:
            return _reading_structure_error(value, schema_path)
        actual = [document["unit_id"] for document in value["documents"]]
        if len(actual) == len(expected) and len(set(actual)) == len(expected) and set(actual) == set(expected):
            return None
        return ReadingSourceCoverageError(expected, actual)
    except (AuditError, KeyError, TypeError, ValueError):
        return None


def _reading_document_shape(schema_path: Path) -> bool:
    """The two per-batch image reading schemas are the only ones normalized."""
    from .pdf_evidence import DOCUMENT

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) - {"documents", "field_inventory"}:
        return False
    documents = properties.get("documents")
    items = documents.get("items") if isinstance(documents, dict) else None
    if not isinstance(items, dict) or items.get("additionalProperties") is not False:
        return False
    allowed, item_properties = set(DOCUMENT["properties"]), items.get("properties")
    required = items.get("required")
    return (isinstance(item_properties, dict) and isinstance(required, list)
            and not set(item_properties) - allowed and not set(required) - allowed
            and {"unit_id", "facts", "limitations"} <= set(required))


def _restore_declared_arrays(value: Any, schema_path: Path) -> Any:
    """A missing roles/limitations array declares nothing; it is not a fact.

    Reading replies sometimes drop these two arrays while keeping the image
    facts. Filling them with an empty list keeps the audit moving without
    adding any evidence: nothing is invented, and every real gate (facts,
    source coverage, visible-field inventory) still runs on the reply as sent.
    Facts, unit_ids and unknown keys stay strict so the model is asked again.
    """
    if not isinstance(value, dict) or not _reading_document_shape(schema_path):
        return value
    documents = value.get("documents")
    if type(documents) is not list or not 1 <= len(documents) <= 6:
        return value
    restored: list[Any] | None = None
    for index, document in enumerate(documents):
        if (type(document) is not dict or not isinstance(document.get("unit_id"), str)
                or type(document.get("facts")) is not list):
            continue
        missing = [key for key in ("roles", "limitations")
                   if document.get(key) is None or key not in document]
        if not missing:
            continue
        if restored is None:
            restored = list(documents)
        restored[index] = {**document, **{key: [] for key in missing}}
    if restored is None:
        return value
    print("读图答复缺少 roles/limitations 数组，按“未声明”补空数组后继续校验（不改事实）", flush=True)
    return {**value, "documents": restored}


def _joint_fact_retry_context(schema_path: Path) -> str:
    """Closed intermediate-image shape reminder; never echo a business answer."""
    from .pdf_evidence import STRINGS, obj

    try:
        schema = json.loads(schema_path.read_text(encoding='utf-8'))
        expected, context = {}, {}
        for field, id_key in (('scene_observations', 'unit_id'), ('reference_observations', 'reference_id')):
            array = schema['properties'][field]
            ids = array['items']['properties'][id_key].get('enum', [])
            if (not isinstance(ids, list) or len(ids) != len(set(ids)) or len(ids) > 128
                    or any(not isinstance(uid, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,96}', uid) is None for uid in ids)
                    or field == 'scene_observations' and not ids):
                return ''
            expected[field] = {'type': 'array', 'minItems': len(ids), 'maxItems': len(ids),
                'items': obj({id_key: {'type': 'string', **({'enum': ids} if ids else {})},
                              'facts': STRINGS, 'limitations': STRINGS})}
            # Show the actual required array shape rather than a descriptor
            # object that can itself be mistaken for the requested response.
            # Empty arrays are structural placeholders, never source facts.
            context[field] = [{id_key: uid, 'facts': [], 'limitations': []} for uid in ids]
        if schema != obj(expected):
            return ''
        hint = ('本次仅提取图像事实，不执行资料门禁或核销结论。根对象只能有以下两个数组，'
                '本批ID各一次，不改字段名、不输出gate、checks或bizType。空数组仅占位：'
                '每个来源必须据本图填写可见facts或具体limitations，不能照抄空值或补造事实。结构：'
                + json.dumps(context, ensure_ascii=False, separators=(',', ':')))
        # Both transports already copy at most 1200 error characters into the
        # ordinary retry. Keep the complete closed reminder below that limit.
        return hint if len(hint) <= 1100 and len(hint.encode('utf-8')) <= 4096 else ''
    except (OSError, KeyError, TypeError, ValueError):
        return ''


_SOURCE_CODES = {
    "本类已确认的申请参照值必须引用品牌方本次促销合同，不能以结算单或其他资料代替": "contract_reference",
    "证据引用了未读取的材料来源": "unknown_source",
    "数值引用必须逐字来自本次已提取材料，不能补造申请值或计算依据": "quote_not_in_source",
    "补差日期必须逐字引用该来源原文，不能拼接或改写日期": "quote_not_in_source",
    "数值与引用的原文不一致": "number_quote_mismatch",
    "数量金额要点必须提供原文数值，由程序复算，AI不能只给通过/不通过": "missing_numeric_evidence",
    "数量金额比较的来源必须完整列入该要点证据": "missing_comparison_source",
    "审核必须覆盖全部且仅覆盖PDF登记要点，不得增加旧规则或遗漏要点": "incomplete_checklist",
    "临促逐店逐日照片检查通过时须引用本次临促照片，合同安排不能证明实际照片已提供": "missing_photo_source",
}


def _schema_paths(value: dict, schema_path: Path) -> list[str]:
    """Return schema-owned field names and indexes, never values/error bodies."""
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fields: set[str] = set()

    def visit(node):
        if isinstance(node, dict):
            fields.update(node.get("properties", {}))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
    paths = []
    for error in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value):
        parts = [str(part) if type(part) is int or part in fields else "?"
                 for part in error.absolute_path]
        path = "/".join(parts) or "$"
        if path not in paths:
            paths.append(path[:192])
        if len(paths) == 8:
            break
    return paths or ["$"]


def _operand_diagnostics(error: AuditError, value: dict) -> tuple[list[str], str]:
    """Expose only real schema-owned positions; keep source excerpts private."""
    from .pdf_evidence import OperandEvidenceError

    if not isinstance(error, OperandEvidenceError):
        return ["$"], ""
    paths = getattr(error, "validation_paths", None)
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        return ["$"], ""
    path = paths[0]
    if len(path) > 192 or not re.fullmatch(
            r"(?:checks/[0-9]{1,9}/comparisons/[0-9]{1,9}/(?:left|right/[0-9]{1,9})"
            r"|entry_coverage/total_stores)/(?:quote|number)"
            r"|activity_dates/(?:(?:contract|settlement)/periods|photos)/[0-9]{1,9}/quote", path):
        return ["$"], ""
    try:
        operand = value
        for part in path.split("/")[:-1]:
            operand = operand[int(part)] if part.isdecimal() else operand[part]
        if not isinstance(operand, dict) or not isinstance(operand[path.rsplit("/", 1)[1]], str):
            return ["$"], ""
    except (KeyError, IndexError, TypeError):
        return ["$"], ""
    context = getattr(error, "retry_context", None)
    limits = {"unit_id": 256, "quote": 2048, **({} if path.startswith('activity_dates/') else {"number": 128})}
    if (not isinstance(context, dict) or context.get("path") != path
            or any(not isinstance(context.get(key), str) or context[key] != operand.get(key)
                   or len(context[key].encode("utf-8")) > limit for key, limit in limits.items())
            or not isinstance(context.get("facts"), list) or len(context["facts"]) > 3
            or any(not isinstance(fact, str) or len(fact.encode("utf-8")) > 2048
                   for fact in context["facts"])
            or not isinstance(context.get("facts_omitted"), bool)):
        return [path], ""
    # Do not serialize additional exception attributes or arbitrary object keys.
    safe = {"path": path, **{key: context[key] for key in limits},
            "facts": context["facts"], "facts_omitted": context["facts_omitted"]}
    encoded = json.dumps(safe, ensure_ascii=False)
    return [path], encoded if len(encoded.encode("utf-8")) <= 8192 else ""


def _check_diagnostics(error: AuditError, value: dict):
    """General schema positions only; do not create targeted model retry hints."""
    from .common import CheckEvidenceError
    codes = {'reference_source_role', 'reference_quote', 'reference_name_value', 'seal_source',
             'comparison_value_kind', 'comparison_operation', 'comparison_operator', 'comparison_source',
             'comparison_scope'}
    if not isinstance(error, CheckEvidenceError) or error.validation_code not in codes:
        return None
    if not re.fullmatch(r'reference_evidence/(?:(?:written_name|seal_name)/(?:unit_id|quote|value)'
                        r'|periods/[0-9]{1,9}/quote)|comparisons(?:/[0-9]{1,9}/(?:value_kind|operation|operator'
                        r'|right|(?:left|right/[0-9]{1,9})/unit_id))?',
                        error.field_path):
        return None
    matches = [(index, check) for index, check in enumerate(value.get('checks', []))
               if check.get('rule_id') == error.rule_id]
    if len(matches) != 1:
        return None
    index, current = matches[0]
    try:
        for key in error.field_path.split('/'):
            current = current[int(key)] if key.isdecimal() else current[key]
    except (KeyError, IndexError, TypeError):
        return None
    return error.validation_code, [f'checks/{index}/{error.field_path}'], ''


def _classification_position(value: dict, path: str):
    """Only fixed gate schema fields/indexes can enter public positions."""
    from .pdf_policy import FLAGS

    flags = r"flags(?:/(?:" + "|".join(re.escape(name) for name in FLAGS) + r"))?"
    material = (r"materials(?:/[0-9]{1,9}/(?:id|state|reason|source_ids|"
                r"condition_evidence(?:/[0-9]{1,9}/(?:unit_id|quote|stance))?))?")
    pattern = (r"(?:source_ids|candidate_scenarios|" + flags + "|" + material
               + r"|background_materials/[0-9]{1,9}/source_ids|material_matches"
               + r"(?:/[0-9]{1,9}/(?:scenario|supported|source_ids|" + flags + "|" + material
               + r"|extra_materials/[0-9]{1,9}/source_ids))?)")
    if not isinstance(path, str) or len(path) > 192 or not re.fullmatch(pattern, path):
        raise ValueError("not a classification field")
    current = value
    for part in path.split("/"):
        current = current[int(part)] if part.isdecimal() else current[part]
    return current


def _classification_diagnostics(error: AuditError, value: dict):
    from .pdf_evidence import ClassificationEvidenceError, CLASSIFICATION_ERROR_CODES, _gate_small_value

    if not isinstance(error, ClassificationEvidenceError):
        return None
    code = CLASSIFICATION_ERROR_CODES.get(str(error))
    if code is None or getattr(error, "validation_code", None) != code:
        return None
    paths = getattr(error, "validation_paths", None)
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        return code, ["$"], ""
    path = paths[0]
    if path == "$":
        return code, [path], ""
    try:
        _classification_position(value, path)
    except (KeyError, IndexError, TypeError, ValueError):
        return code, ["$"], ""
    context = getattr(error, "retry_context", None)
    if (not isinstance(context, dict) or context.get("path") != path
            or type(context.get("omitted")) is not bool
            or not isinstance(context.get("fields"), list) or len(context["fields"]) > 4
            or not isinstance(context.get("source_ids"), list) or len(context["source_ids"]) > 16
            or any(not isinstance(uid, str) or len(uid.encode("utf-8")) > 256 for uid in context["source_ids"])
            or len(set(context["source_ids"])) != len(context["source_ids"])
            or not isinstance(context.get("sources"), list) or len(context["sources"]) > 3):
        return code, [path], ""
    fields, sources = [], []
    try:
        for field in context["fields"]:
            if (not isinstance(field, dict) or not _gate_small_value(field.get("value"))
                    or field["value"] != _classification_position(value, field.get("path"))
                    or any(row["path"] == field["path"] for row in fields)):
                return code, [path], ""
            fields.append({"path": field["path"], "value": field["value"]})
        facts_count = 0
        for source in context["sources"]:
            if (not isinstance(source, dict) or source.get("unit_id") not in context["source_ids"]
                    or any(row["unit_id"] == source["unit_id"] for row in sources)
                    or not isinstance(source.get("facts"), list)
                    or len(source["facts"]) > 3
                    or any(not isinstance(fact, str) or len(fact.encode("utf-8")) > 2048 for fact in source["facts"])):
                return code, [path], ""
            facts_count += len(source["facts"])
            if facts_count > 3:
                return code, [path], ""
            sources.append({"unit_id": source["unit_id"], "facts": list(source["facts"])})
    except (KeyError, IndexError, TypeError, ValueError):
        return code, [path], ""
    safe = {"path": path, "fields": fields, "source_ids": list(context["source_ids"]),
            "sources": sources, "omitted": context["omitted"]}
    encoded = json.dumps(safe, ensure_ascii=False)
    return code, [path], encoded if len(encoded.encode("utf-8")) <= 8192 else ""


def _display_diagnostics(error: AuditError, value: dict):
    """Only a typed source error and real schema positions reach telemetry."""
    from .display_source_evidence import DisplaySourceError

    if not isinstance(error, DisplaySourceError) or getattr(error, "code", None) != "display_source_claim":
        return None
    paths = getattr(error, "validation_paths", None)
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        return None
    path = paths[0]
    if len(path) > 192 or not re.fullmatch(
            r"(?:photo_reviews/[0-9]{1,6}/)?checks/[0-9]{1,6}/(?:reason|source_assertions"
            r"(?:/[0-9]{1,6}(?:/statement|/references/[0-9]{1,6}(?:/(?:quote|field|unit_id))?)?)?)", path):
        return None
    current = value
    try:
        for part in path.split("/"):
            if part.isdecimal():
                if not isinstance(current, list):
                    return None
                current = current[int(part)]
            else:
                if not isinstance(current, dict):
                    return None
                current = current[part]
    except (KeyError, IndexError):
        return None
    return "display_source_claim", [path]


def _complete_closing_brackets(text: str):
    """Return the JSON value when only trailing closing brackets are missing.

    A stream cut off after the last real token (e.g. missing one "}" and one
    "]") is a transport truncation, not a business answer. Completing it adds
    no content. Anything else -- unterminated string, stray bracket, excessive
    tail -- returns None so the normal syntax failure is raised.
    """
    stack: list[str] = []
    in_string = escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append("}" if character == "{" else "]")
        elif character in "}]":
            if not stack or stack.pop() != character:
                return None
    if in_string or not stack or len(stack) > 8:
        return None
    try:
        return json.loads(text + "".join(reversed(stack)))
    except ValueError:
        return None


class EvidenceOutputParser(BaseOutputParser[dict[str, Any]]):
    schema_path: Path = Field(exclude=True, repr=False)
    post_validate: Callable[[dict[str, Any]], None] | None = Field(default=None, exclude=True, repr=False)
    _last_diagnostics: dict = PrivateAttr(default_factory=dict)
    _last_retry_context: str = PrivateAttr(default="")

    @property
    def last_diagnostics(self) -> dict:
        return dict(self._last_diagnostics)

    @property
    def last_retry_context(self) -> str:
        """Bounded source data for the same model retry, never public telemetry."""
        return self._last_retry_context

    @property
    def _type(self) -> str:
        return "offline_audit_evidence"

    def parse(self, text: str) -> dict[str, Any]:
        from . import codex_runner as api

        self._last_diagnostics = {}
        self._last_retry_context = ""
        try:
            value = json.loads(api._strip_json_fence(text))
        except json.JSONDecodeError:
            value = _complete_closing_brackets(api._strip_json_fence(text))
            if value is not None:
                self._last_diagnostics = {"validation_stage": "json",
                                          "validation_code": "json_syntax_completed",
                                          "validation_paths": ["$"]}
            else:
                self._last_diagnostics = {"validation_stage": "json", "validation_code": "json_syntax",
                                          "validation_paths": ["$"]}
                shape_error = _reading_structure_error(None, self.schema_path, invalid_json=True)
                if shape_error is not None:
                    self._last_diagnostics.update(validation_root_type=shape_error.root_type,
                                                  validation_schema_kinds=shape_error.schema_kinds)
                    raise shape_error from None
                self._last_retry_context = _joint_fact_retry_context(self.schema_path)
                if self._last_retry_context:
                    raise AuditError(self._last_retry_context) from None
                raise
        if not isinstance(value, dict):
            self._last_diagnostics = {"validation_stage": "json", "validation_code": "json_object",
                                      "validation_paths": ["$"]}
            shape_error = _reading_structure_error(value, self.schema_path)
            if shape_error is not None:
                self._last_diagnostics.update(validation_root_type=shape_error.root_type,
                                              validation_schema_kinds=shape_error.schema_kinds)
                raise shape_error from None
            self._last_retry_context = _joint_fact_retry_context(self.schema_path)
            if self._last_retry_context:
                raise AuditError(self._last_retry_context) from None
            raise AuditError("AI 证据顶层必须是 JSON 对象")
        value = _restore_declared_arrays(value, self.schema_path)
        if api.reported_material_access_failure(value):
            raise api.CodexRequestConfigurationError(
                "模型报告必需材料读取受阻（configuration），不能以无法确认代替成功复核"
            )
        try:
            api.validate_json(value, self.schema_path)
        except AuditError:
            source_error = _reading_schema_error(value, self.schema_path)
            if isinstance(source_error, ReadingSourceCoverageError):
                self._last_diagnostics = {"validation_stage": "schema", "validation_code": "reading_source_coverage",
                                          "validation_paths": source_error.validation_paths}
                self._last_retry_context = source_error.retry_context
                raise source_error from None
            self._last_diagnostics = {"validation_stage": "schema", "validation_code": "schema_validation",
                                      "validation_paths": _schema_paths(value, self.schema_path)}
            if source_error is None:
                self._last_retry_context = _joint_fact_retry_context(self.schema_path)
                if self._last_retry_context:
                    raise AuditError(self._last_retry_context) from None
            if isinstance(source_error, ReadingStructureError):
                self._last_diagnostics.update(validation_root_type=source_error.root_type,
                                              validation_schema_kinds=source_error.schema_kinds)
            if source_error is not None:
                raise source_error from None
            raise
        if self.post_validate is not None:
            try:
                self.post_validate(value)
            except AuditError as error:
                if isinstance(error, ReadingSourceCoverageError):
                    self._last_diagnostics = {"validation_stage": "evidence", "validation_code": "reading_source_coverage",
                                              "validation_paths": error.validation_paths}
                    self._last_retry_context = error.retry_context
                    raise
                display = _display_diagnostics(error, value)
                if display is not None:
                    code, paths = display
                    self._last_diagnostics = {"validation_stage": "evidence", "validation_code": code,
                                              "validation_paths": paths}
                    raise
                gate = _check_diagnostics(error, value) or _classification_diagnostics(error, value)
                if gate is None:
                    paths, self._last_retry_context = _operand_diagnostics(error, value)
                    code = _SOURCE_CODES.get(str(error), "evidence_validation")
                else:
                    code, paths, self._last_retry_context = gate
                self._last_diagnostics = {"validation_stage": "evidence",
                    "validation_code": code,
                    "validation_paths": paths}
                raise
        return value

    def get_format_instructions(self) -> str:
        return "只返回符合本批 JSON Schema 的完整对象，保留原始来源，不补造不可见事实。"
