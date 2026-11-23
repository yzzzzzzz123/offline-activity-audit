"""Lossless issue grouping for new customer summaries, without a model call.

Only identical reasons with the same rule and decision are grouped. Evidence
and individual photo/check records remain in the unmodified view payload.
"""
from __future__ import annotations

from collections import Counter


def issue_lines(reason: str) -> list[str]:
    """Honor issue line breaks, but keep line breaks inside quoted evidence.

    Do not split on punctuation: decimal values, filenames and the explanation
    of one issue must stay intact. Never truncate a long issue to fit a limit.
    """
    opening = {"“": "”", "「": "」", "『": "』", "《": "》",
               "（": "）", "(": ")", "【": "】", "[": "]", '"': '"'}
    stack = []
    lines = []
    start = 0
    for index, character in enumerate(reason):
        if stack and character == stack[-1]:
            stack.pop()
        elif character in opening:
            stack.append(opening[character])
        elif character == "\n" and not stack:
            line = reason[start:index].strip()
            if line:
                lines.append(line)
            start = index + 1
    tail = reason[start:].strip()
    if tail:
        lines.append(tail)
    return lines


def group_summary_issues(issues: list[dict]) -> list[str]:
    """Say a repeated issue once; group only inside one scenario/archive.

    Distinct words, original values, decisions or rule IDs never collapse.
    Unstructured legacy reasons have no grouping contract and stay separate.
    For a repeated photo issue, show up to three example locations instead of
    another exhaustive photo list; all locations stay in the original view.
    """
    groups = {}
    for index, issue in enumerate(issues):
        key = (issue.get("rule_id"), issue.get("status"), issue["reason"],
               issue.get("scope", "")) if issue.get("rule_id") else (index,)
        if key not in groups:
            groups[key] = {"reason": issue["reason"], "locations": {}}
        location = issue.get("location", "")
        source = issue.get("source_file", "")
        located_source = (location, source)
        if location:
            groups[key]["locations"][located_source] = None
    result = []
    for group in groups.values():
        reason = group["reason"]
        located_sources = group["locations"]
        location_counts = Counter(location for location, _ in located_sources)
        locations = []
        for location, source in located_sources:
            if source and location_counts[location] > 1:
                # Different directories can contain different photos with the
                # same basename. Do not silently collapse their identities.
                basename = source.replace("\\", "/").rsplit("/", 1)[-1]
                if location.startswith(basename):
                    location = source + location[len(basename):]
            locations.append(location)
        if len(locations) == 1:
            location = locations[0]
            result.append(f"{location}：{reason}" if location not in reason else reason)
        elif locations:
            examples = "、".join(locations[:3])
            scope = f"多张照片（如{examples}）" if len(locations) > 3 else examples
            result.append(f"{scope}：{reason}")
        else:
            result.append(reason)
    return result
