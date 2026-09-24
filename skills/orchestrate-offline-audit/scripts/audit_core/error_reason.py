"""Pure customer reason projection; original evidence and actions stay intact."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .archive_input import scenario_from_archive_name
from .customer_language import POS_PAIR_REASON, concise_reason
from .scenario_registry import SCENARIO_LABELS


_FAILURE = re.compile(r"不匹配|不一致|不完整|不符合|不满足|不通过|不可核验|缺失|缺少|缺页|未提交|未显示|未识别|未覆盖|未确认|未明确|未见|未发现|未提供|未完成|未登记|未能|无法|不能|尚不能|难以|模糊|不清晰|不够清晰|重叠|损坏|待确认|歧义|相差|差额|差异|少\s*[\d.]|多\s*[\d.]|完全相同|重复提交")
_NONREASON_LABEL = r"处理方式|操作建议|补交建议|建议|要重新提交(?:什么)?|需要补交|重新提交|置信度|审核结论|核验结果|本项结果|结论|核销影响|影响"
_ALIASES = {"现场商品与合同": "现场商品与合同无法确认", "陈列标准": "陈列标准无法核验", "活动日期": "活动日期无法核验", "销售Excel对合同附件": "销售Excel与合同附件无法核对"}
_DISPLAY_GLOBAL_LABELS = {"合同核心六项", "合同核心七项", "销售Excel对合同", "销售Excel对合同附件", "销售Excel文件内部", "合同商品知识库", "现场商品与合同"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _heading(row: dict[str, Any]) -> str:
    return re.sub(r"^(?:错误项|问题|错误原因)\s*[：:]\s*", "", _text(row.get("heading")))


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _business_names(reason: str, files: list[str]) -> str:
    for source in sorted(files, key=len, reverse=True):
        reason = reason.replace(source, _basename(source))
    return re.sub(r"[^\s，,；;：:。！？、（）()<>]*[/\\][^\s，,；;：:。！？、（）()<>]+\.(?:zip|rar|pdf|jpe?g|png|webp|xls[mx]?)\b",
                  lambda match: _basename(match.group(0)), reason, flags=re.I)


def _clauses(value: str) -> list[str]:
    """Keep actual/expected values inside the same parenthesized comparison."""
    parts, current, depth = [], [], 0
    for character in value:
        if character in "（(":
            depth += 1
        elif character in "）)":
            depth = max(0, depth - 1)
        if character in "\n；;。" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts


def _file_subject(value: str) -> tuple[str, str]:
    """A filename before a colon is a subject, even if it starts with 请/建议."""
    match = re.match(r"^([^：:\n]+\.(?:zip|rar|pdf|jpe?g|png|webp|xls[mx]?))\s*[：:]\s*(.*)$", value, flags=re.I)
    return (match.group(1), match.group(2)) if match else ("", value)


def _row_subject(sheet: dict[str, Any], row: dict[str, Any]) -> str:
    title = _heading(row)
    if not title or re.match(r"^(?:错误项|问题|错误原因)\s*[：:]", _text(row.get("heading"))):
        return ""
    if title in {"实际申请金额", "收款人与日期", "数量核验", "金额核验", "核销结果", "核验结果", "合同", "结算单", "金额", "数量"}:
        return ""
    return title if not _FAILURE.search(title) else ""


def _clean_reason(value: Any, *, comparison: bool = False) -> str:
    raw = _text(value).replace("\r", "\n")
    raw = re.sub(r"[，,]\s*(?:建议|请|需(?:补交|补充|重新)|需要(?:补交|补充|重新|确认)|应(?:补交|补充|重新))[^。；;\n]*",
                 lambda match: match.group(0) if _file_subject(match.group(0).lstrip(" ，,"))[0] else "", raw)
    # Separate labelled prose even when a legacy cell put it on one line.
    raw = re.sub(rf"(?<!\w)(?=(?:{_NONREASON_LABEL}|错误原因|主要问题)\s*[：:])", "\n", raw)
    parts: list[str] = []
    failed_value_fields = [match.group(1) for match in re.finditer(r"(金额|数量|单价|商品编码|条形码)\s*[：:]\s*(?:不一致|不匹配)", raw)]
    for clause in _clauses(raw):
        clause = clause.strip(" ，,：:")
        if not clause or re.match(rf"^(?:{_NONREASON_LABEL})\s*[：:]", clause):
            continue
        if clause.startswith("已保留各包的材料归属"):
            continue
        clause = re.sub(r"^(?:错误原因|识别结果|识别内容|已识别内容)\s*[：:]\s*", "", clause)
        if re.fullmatch(r"(?:[^，,；;\n]+\.xls[mx]?|(?:销售\s*)?Excel)\s*[：:]?\s*(?:读取到|识别到)\s*\d+\s*家门店", clause, flags=re.I):
            continue
        file_subject, clause = _file_subject(clause)
        if re.match(r"^(?:请|建议|应补交|需补交|需要(?:补交|补充|重新|确认)|补交|重新提交|重新拍摄|更正|确认.*后继续核销)", clause):
            continue
        if clause.startswith("补充") and not _FAILURE.search(clause):
            continue
        if re.match(r"^(?:暂不能核销|暂不支持自动核销|不能自动核销|阻断自动核销|资料需补正|无需补交|无需重新提交|已完成.*(?:识别|盘点))$", clause):
            continue
        clause = re.sub(r"^已盘点本包全部材料[，,]\s*", "", clause)
        # These legacy follow-on sentences restate the consequence, after the
        # actual missing identities or amount difference was already recorded.
        if parts and (clause.startswith("因此") or "金额证据链未闭合" in clause):
            continue
        clause = re.sub(r"^(?:因此|所以)", "", clause)
        failure = bool(_FAILURE.search(re.sub(r"模糊匹配|精确匹配", "", clause)))
        if not failure and re.search(r"精确匹配|模糊匹配|全部对应|金额一致|数量一致|已满足|已通过|可识别|已经识别|均一致|核验通过|已经完成", clause):
            continue
        comparison_value = any(field in clause for field in failed_value_fields) and bool(re.search(r"(?:实际|应有|应为|合同|结算|申请|转账|Excel|销售).*[0-9]", clause))
        if comparison and not failure and not comparison_value:
            continue
        if not clause or re.match(r"^(?:问题文件|对照文件|文件|来源)\s*[：:]", clause):
            continue
        clause = re.sub(r"\bNone\b", "未识别", clause)
        if file_subject:
            clause = f"{_basename(file_subject)}：{clause}"
        if clause not in parts:
            parts.append(clause)
    return "；".join(parts).strip(" ，,；;：:。")


def _source_files(sheet: dict[str, Any], row: dict[str, Any]) -> list[str]:
    values = row.get("values") or []
    headers = sheet.get("headers") or []
    problem_values = [value for value in values if re.search(r"(?:^|\n)问题文件\s*[：:]", _text(value))]
    source_columns = [values[i] for i, header in enumerate(headers)
                      if re.match(r"^(?:问题来源|错误来源|问题文件)", _text(header)) and i < len(values)]
    selected = problem_values or source_columns
    files = []
    label = "问题文件" if problem_values else "(?:文件|来源)"
    for value in selected:
        for match in re.finditer(rf"(?:^|\n){label}\s*[：:]\s*([^\n]+)", _text(value)):
            for name in re.split(r"[、]", match.group(1)):
                name = name.strip()
                if name and name not in {"未识别", "本包缺失资料", "未提交对应文件"} and name not in files:
                    files.append(name)
    return files


def _finish_reason(reason: str, files: list[str], subject: str = "") -> str:
    reason = _business_names(reason, files)
    files = list(dict.fromkeys(_basename(name) for name in files))
    subject = _business_names(subject, [])
    reason = reason.strip(" ，,；;：:。")
    if subject and subject not in reason:
        reason = f"{subject}：{reason}"
    missing = [name for name in files if name not in reason]
    if missing:
        reason = "、".join(missing) + "：" + reason
    return reason + "。" if reason else ""


def _contract_code_reason(sheet: dict[str, Any], row: dict[str, Any], detail: str) -> str | None:
    """Read the actual contract fields, never substitute catalog identifiers."""
    if not re.search(r"(?:知识库|商品资料).{0,30}未登记(?:合同业务|合同|业务|商品)?编码|(?:product_code_aliases|别名).{0,30}未登记合同编码|合同(?:业务)?编码.{0,25}未(?:在商品资料中)?登记", detail):
        return None
    title = _heading(row)
    values, headers = row.get("values") or [], sheet.get("headers") or []
    sources = [values[i] for i, header in enumerate(headers) if i < len(values)
               and _text(header).startswith("合同") and not re.search(r"知识库|规则|要求|对比", _text(header))]
    if not sources and values and "合同" in title:
        sources = [values[0]]
    if not sources and "合同" not in title:
        return None
    source = "\n".join(_text(value) for value in sources)

    def field(name: str) -> str:
        match = re.search(rf"(?:^|\n){name}\s*[：:]\s*([^\n]+)", source)
        value = match.group(1).strip() if match else ""
        return "" if value in {"未识别", "未显示", "无", "None"} else value

    name, code = field("商品名称"), field("商品编码")
    # A legacy comparison-only cell is not an actual contract identifier.
    if _FAILURE.search(code) or code.startswith(("精确匹配", "模糊匹配")):
        code = ""
    page, line = re.search(r"(?:PDF|合同)第(\d+)页", title), re.search(r"第(\d+)行", title)
    location = f"合同第{page.group(1)}页第{line.group(1)}行" if page and line else (f"合同第{line.group(1)}行" if line else "合同")
    subject = location + (f"，商品“{name}”" if name else "")
    return f"{subject}：合同商品编码" + (f"“{code}”" if code else "") + "未在商品资料中登记"


def _photo_review_failure(lines: list[str]) -> str:
    observations = [line for line in lines if line.startswith("视觉依据：")]
    if any(re.search(r"原图|照片|原始分辨率", line) and re.search(
            r"访问限制阻断|拒绝访问|访问被拒绝|文件访问限制|读取.{0,12}(?:受阻|被拒绝)|无法以原始分辨率重新打开照片", line)
           for line in observations):
        return "系统无法读取现场原图，陈列列数和堆头面积尚未核验"
    if any(re.search(r"(?:本次无法完成.*原始分辨率.*(?:独立)?复核|未能完成.*原始分辨率.*复核|未能.*重新打开原始分辨率.*无法完成.*(?:计数|复核)|未能.*原始分辨率.*重新打开照片|(?:必做|独立)复核未(?:能)?完成)", line)
           for line in observations):
        return "现场照片复核未完成，尚未确认陈列列数和堆头面积"
    return ""


def _marked_reasons(values: list[Any], title: str, sources: list[str], *, local_display: bool = False) -> list[str]:
    topics = []
    for value in values:
        marked = re.search(r"主要问题\s*[：:]\s*([^\n]+)", _text(value))
        if not marked:
            continue
        text = re.split(rf"(?:{_NONREASON_LABEL})\s*[：:]", marked.group(1), maxsplit=1)[0]
        topics.extend(part.strip(" 。") for part in re.split(r"[、，,；;]", text) if part.strip(" 。"))
    lines = [line.strip() for value in values for line in _text(value).splitlines()]
    reasons = []
    for topic in dict.fromkeys(topics):
        if local_display and topic in _DISPLAY_GLOBAL_LABELS:
            continue
        reason = ""
        if topic == "现场商品知识库":
            photo = _text(values[1]) if len(values) > 1 else "\n".join(lines)
            product_status = "\n".join(line for line in photo.splitlines() if re.match(r"^(?:知识库结论|现场商品)[：:]", line))
            if (re.search(r"置信度[：:]\s*[高中]", product_status)
                    and not re.search(r"置信度[：:]\s*低|未确认|未识别|未找到|不匹配|无法", product_status)):
                continue
            reason = "现场商品尚未确认"
        elif topic == "陈列标准":
            detail = next((line for line in lines if line.startswith("陈列标准核验：")), "")
            review_failure = _photo_review_failure(lines)
            if review_failure:
                reason = review_failure
            elif detail:
                reason = _clean_reason(detail.replace("陈列标准核验：", "陈列标准", 1))
        elif topic == "现场商品与合同":
            detail = next((line for line in lines if "已确认现场商品 → 合同商品范围：" in line), "")
            if detail:
                detail = detail.split("已确认现场商品 → 合同商品范围：", 1)[1]
                detail = re.sub(r"^置信度[：:]\s*[高中低][（(]?", "", detail)
                detail = re.split(r"[；;]\s*合同来源", detail, maxsplit=1)[0].rstrip("。；;）)")
                reason = _clean_reason(detail)
        elif topic == "活动日期":
            detail = next((line for line in lines if "活动日期" in line and _FAILURE.search(line) and not line.startswith("主要问题")), "")
            if detail:
                reason = _clean_reason(detail)
        elif local_display and topic == "门店水印缺失或无法核对":
            locations = [match.group(1).strip() for line in lines
                         if (match := re.fullmatch(r"识别地点\s*[：:]\s*(.+)", line))]
            if locations and all(re.fullmatch(r"未识别[。.]?", location) for location in locations):
                reason = "现场照片未显示可与合同门店对应的名称或地址"
        reason = reason or _ALIASES.get(topic, topic if _FAILURE.search(topic) else topic + "无法核验")
        reasons.append(_finish_reason(reason, sources, title))
    return list(dict.fromkeys(reasons))


def project_error_reasons(sheet: dict[str, Any], row: dict[str, Any], diagnostic_issue: dict[str, Any] | None = None) -> list[str]:
    """Return each saved reason without advice, approval, confidence or a bound."""
    if row.get("status") != "issue":
        return []
    if diagnostic_issue is not None:
        files = [_text(name) for name in diagnostic_issue.get("source_files") or [] if _text(name)]
        if diagnostic_issue.get("code") == "singleton_role_ambiguous":
            return [_finish_reason("主件与补充件关系未明确", files)]
        reason = _clean_reason(diagnostic_issue.get("observed"))
        if not reason:
            reason = _clean_reason(diagnostic_issue.get("title")) or "具体错误原因未记录"
        return [_finish_reason(reason, files)]
    values = row.get("values") if isinstance(row.get("values"), list) else []
    headers = sheet.get("headers") if isinstance(sheet.get("headers"), list) else []
    title = _heading(row)
    if "合同主基准" in title or title.startswith("合计"):
        return []
    sources = _source_files(sheet, row)
    explicit = [values[i] for i, header in enumerate(headers) if "错误原因" in _text(header) and i < len(values)]
    if not explicit:
        for value in values:
            marked = re.search(r"错误原因\s*[：:]\s*", _text(value))
            if marked:
                explicit.append(_text(value)[marked.end():])
    if explicit:
        reasons = [_clean_reason(value) for value in explicit]
        subject = _row_subject(sheet, row)
        formatted = []
        for reason in reasons:
            if not reason:
                continue
            contract_code = _contract_code_reason(sheet, row, reason)
            formatted.append(_finish_reason(contract_code or reason, sources, "" if contract_code else subject))
            if contract_code:
                for part in _clauses(reason):
                    if _FAILURE.search(part) and not re.search(r"商品编码|合同编码|product_code_aliases|知识库主编码", part):
                        formatted.append(_finish_reason(part, sources, subject))
        return list(dict.fromkeys(formatted)) or ["具体错误原因未记录。"]
    observed = [values[i] for i, header in enumerate(headers) if re.match(r"^(?:已识别内容|识别内容)(?:\s|$)", _text(header)) and i < len(values)]
    if observed:
        reasons = [_clean_reason(value) for value in observed]
        return list(dict.fromkeys(_finish_reason(reason, sources) for reason in reasons if reason)) or [_finish_reason(title or "具体错误原因未记录", sources)]
    local_display = sheet.get("scenario") == "promotional_display" or sheet.get("name") == "堆头核销"
    marked_reasons = _marked_reasons(values, title, sources, local_display=local_display)
    if marked_reasons or (local_display and any("主要问题：" in _text(value) for value in values)):
        return marked_reasons
    comparisons = [values[i] for i, header in enumerate(headers) if "对比结果" in _text(header) and i < len(values)]
    reasons = []
    for value in comparisons or values:
        cleaned = _clean_reason(value, comparison=True)
        if cleaned:
            for part in _clauses(cleaned):
                contract_code = _contract_code_reason(sheet, row, part)
                reasons.append(_finish_reason(contract_code or part, sources, "" if contract_code else title if title and not _FAILURE.search(title) else ""))
    if reasons:
        return list(dict.fromkeys(reasons))
    # Totals/context rows do not supply a distinct error and must not invent one.
    if "合同主基准" in title or title.startswith("合计"):
        return []
    return [_finish_reason(title if _FAILURE.search(title) else "具体错误原因未记录", sources)]


def _attachment_reasons_by_scope(sheet: dict[str, Any], row: dict[str, Any]) -> dict[str, list[str]] | None:
    """Keep each saved attachment comparison in its own existing card scope."""
    values = row.get("values") or []
    if not re.match(r"^合同销售附件第\d+行", _heading(row)) or len(values) <= 3:
        return None
    comparison = _text(values[3])
    markers = list(re.finditer(r"(?m)^(合同商品\s*→\s*商品知识库|合同附件\s*→\s*销售Excel)\s*$", comparison))
    if not markers:
        return None
    sections = {"knowledge": [], "sales": []}
    scoped_sheet = dict(sheet)
    headers = list(sheet.get("headers") or [])
    headers.extend("" for _ in range(max(0, 4 - len(headers))))
    headers[3] = "具体对比结果"
    scoped_sheet["headers"] = headers
    for index, marker in enumerate(markers):
        scope = "knowledge" if marker.group(1).startswith("合同商品") else "sales"
        end = markers[index + 1].start() if index + 1 < len(markers) else len(comparison)
        section = comparison[marker.end():end].strip()
        # A successful scope must stay empty; the generic missing-reason
        # fallback is only meaningful for an actual unrepresented error.
        if not _clean_reason(section, comparison=True):
            continue
        scoped_row = dict(row)
        scoped_values = list(values)
        scoped_values[3] = section
        scoped_row["values"] = scoped_values
        for reason in project_error_reasons(scoped_sheet, scoped_row):
            if reason not in sections[scope]:
                sections[scope].append(reason)
    return sections


def _project_legacy_diagnostic_rows(sheet: dict[str, Any]) -> None:
    """Drop a superseded content-based type guess only from the copied view.

    Old material diagnostics could question a type that the ZIP name already
    selected. The saved type and the current filename rule must agree before
    that one issue can be omitted. Keep the parallel rows/issues aligned so a
    later material error never receives its neighbour's reason or action.
    """
    if sheet.get("projection_kind") != "material_diagnostic":
        return
    archive = _basename(_text(sheet.get("source_archive")))
    declared = scenario_from_archive_name(archive) if archive else None
    if not declared or declared != sheet.get("scenario"):
        return
    rows, issues = sheet.get("rows"), sheet.get("diagnostic_issues")
    if not isinstance(rows, list) or not isinstance(issues, list) or len(rows) != len(issues):
        return
    removed = {
        index for index, (row, issue) in enumerate(zip(rows, issues))
        if isinstance(row, dict) and isinstance(issue, dict)
        and row.get("status") == "issue" and issue.get("code") == "scenario_unconfirmed"
        and _text(issue.get("title")) and _heading(row) == _text(issue["title"])
    }
    if not removed:
        return
    sheet["rows"] = [row for index, row in enumerate(rows) if index not in removed]
    sheet["diagnostic_issues"] = [issue for index, issue in enumerate(issues) if index not in removed]
    remaining_errors = [row for row in sheet["rows"] if isinstance(row, dict) and row.get("status") == "issue"]
    counts = dict(sheet.get("audit_counts") or {})
    counts.update({
        "source_row_count": len(sheet["rows"]),
        "error_count": len(remaining_errors),
        "detail_error_count": sum(row.get("section") == "detail" for row in remaining_errors),
        "context_error_count": sum(row.get("section") != "detail" for row in remaining_errors),
    })
    sheet["audit_counts"] = counts


def attach_error_reasons(view_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Add display-only fields to a copy, preserving raw values and actions."""
    projected = deepcopy(view_payload)
    if not isinstance(projected, dict):
        return projected
    for sheet in projected.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        _project_legacy_diagnostic_rows(sheet)
        diagnostic = list(sheet.get("diagnostic_issues") or []) if sheet.get("projection_kind") == "material_diagnostic" else []
        for index, row in enumerate(sheet.get("rows") or []):
            if not isinstance(row, dict) or row.get("status") != "issue":
                continue
            issue = diagnostic[index] if index < len(diagnostic) and isinstance(diagnostic[index], dict) else None
            if (sheet.get("projection_kind") == "pdf_policy" or sheet.get("decision_source") == "material_content"
                    or (sheet.get("projection_kind") == "classification_rejection"
                        and (row.get("error_reasons") or row.get("error_reason")))):
                reasons = list(row.get("error_reasons") or ([row["error_reason"]] if row.get("error_reason") else []))
            else:
                reasons = project_error_reasons(sheet, row, issue)
            files = list((row.get("card_evidence") or {}).get("source_files") or [])
            files.extend(_source_files(sheet, row))
            reasons = [concise_reason(reason, archive=_text(sheet.get("source_archive")),
                                      label=SCENARIO_LABELS.get(sheet.get("scenario"), ""), files=files)
                       for reason in reasons]
            reasons = list(dict.fromkeys(reason for reason in reasons if reason))
            row["error_reasons"] = reasons
            row["error_reason"] = "\n".join(reasons)
            scoped = _attachment_reasons_by_scope(sheet, row)
            if scoped is not None:
                row["error_reasons_by_scope"] = scoped
        if sheet.get("projection_kind") == "classification_rejection":
            rows = sheet.get("rows") or []
            specific_pos_gap = any(re.search(r"缺少(?:盖章版销售明细|Excel版销售明细|销售明细盖章版|POS明细Excel版|销售明细Excel版)",
                                             reason)
                                   for row in rows for reason in row.get("error_reasons") or [])
            if specific_pos_gap:
                # The pair reminder is the same missing item, not a second problem.
                sheet["rows"] = [row for row in rows if row.get("error_reasons") != [POS_PAIR_REASON]]
                if len(sheet["rows"]) != len(rows):
                    counts = dict(sheet.get("audit_counts") or {})
                    issues = [row for row in sheet["rows"] if row.get("status") == "issue"]
                    counts.update(source_row_count=len(sheet["rows"]), error_count=len(issues),
                                  detail_error_count=sum(row.get("section") == "detail" for row in issues),
                                  context_error_count=sum(row.get("section") != "detail" for row in issues))
                    sheet["audit_counts"] = counts
    return projected
