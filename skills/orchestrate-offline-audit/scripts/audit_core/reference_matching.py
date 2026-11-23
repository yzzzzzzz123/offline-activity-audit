"""Local reference matching: exact numbers, conservative name variants."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import re
import unicodedata

TEXT_MATCH_POLICY = (
    '勤策本单字段有有效值时才对齐，并以勤策为参照；空字段仅跳过该字段的勤策对齐，'
    '不报客户缺资料、不补0、不影响已交资料自身审核。数字、日期、69码按实际含义及单位精确比对，'
    '不得用近似匹配或容差消除差异；0是有效数字。文字名称允许合理模糊匹配：'
    '线上/线下后缀、空格、标点及轻微写法差异不单独报错。'
    '门店核心名称相符且无其他冲突即可对应，不要求水印完整复写勤策详细地址；'
    '实际不同分店、不同数字编号或有多家合理候选时不能强行合并。'
    '完整原始名称及来源仍保留，不改写证据原文。'
)


def numeric_equal(left, right):
    """Decimal equality without rounding; bool/missing/nonfinite are not numbers."""
    try:
        if any(v is None or isinstance(v, bool) or str(v).strip() == '' for v in (left, right)):
            return False
        values = [Decimal(str(v).replace(',', '').strip()) for v in (left, right)]
        return all(v.is_finite() for v in values) and values[0] == values[1]
    except InvalidOperation:
        return False


def _name(value):
    value = unicodedata.normalize('NFKC', str(value or '')).lower()
    value = re.sub(r'[\s\W_]+', '', value)
    return re.sub(r'(?:线上店|线下店|线上|线下)$', '', value)


def name_matches(left, right):
    """Known suffixes or one minor textual edit, never a different branch number.

    This helper is for names only, not identifiers, quantities, dates or prices.
    Candidate selection still must be unique within this case's actual scope.
    """
    a, b = _name(left), _name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if re.findall(r'\d+', a) != re.findall(r'\d+', b):
        return False
    if re.findall(r'[一二三四五六七八九十]+(?:号店|分店|店)', a) != re.findall(r'[一二三四五六七八九十]+(?:号店|分店|店)', b):
        return False
    if min(len(a), len(b)) < 8 or abs(len(a) - len(b)) > 1:
        return False
    edits = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    changed = [(i2-i1, j2-j1) for tag, i1, i2, j1, j2 in edits if tag != 'equal']
    return len(changed) == 1 and max(changed[0]) == 1


def unique_name_match(observed, candidates):
    matched = [name for name in dict.fromkeys(candidates) if name_matches(observed, name)]
    return matched[0] if len(matched) == 1 else None
