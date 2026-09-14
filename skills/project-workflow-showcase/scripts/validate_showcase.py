#!/usr/bin/env python3
"""Validate a self-contained, source-grounded project showcase HTML."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import urlparse


PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD|FIXME|LOREM IPSUM)\b|待补充|占位内容", re.I)
CSS_REMOTE_RE = re.compile(r"@import\s+|url\(\s*['\"]?\s*(?:https?:)?//", re.I)
HTTP_CALL_RE = re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(", re.I)


class ShowcaseParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: CounterLike = CounterLike()
        self.ids: list[str] = []
        self.hash_links: list[str] = []
        self.remote_refs: list[str] = []
        self.source_paths: list[str] = []
        self.missing_img_alt = 0
        self.button_without_type = 0
        self.html_lang = ""
        self.has_charset = False
        self.has_viewport = False
        self.in_title = False
        self.title_parts: list[str] = []
        self.styles: list[str] = []
        self.scripts: list[str] = []
        self._capture_style = False
        self._capture_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags[tag] += 1
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "html":
            self.html_lang = values.get("lang", "")
        if tag == "meta":
            if values.get("charset", "").lower() == "utf-8":
                self.has_charset = True
            if values.get("name", "").lower() == "viewport" and "width=device-width" in values.get("content", ""):
                self.has_viewport = True
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "a" and values.get("href", "").startswith("#"):
            self.hash_links.append(values["href"][1:])
        if tag == "img" and not values.get("alt"):
            self.missing_img_alt += 1
        if tag == "button" and not values.get("type"):
            self.button_without_type += 1
        if tag == "title":
            self.in_title = True
        if tag == "style":
            self._capture_style = True
        if tag == "script":
            self._capture_script = True

        for key in ("src", "href", "action", "poster"):
            raw = values.get(key, "").strip()
            if not raw or raw.startswith(("#", "data:", "mailto:", "tel:")):
                continue
            parsed = urlparse(raw)
            if parsed.scheme in {"http", "https", "ws", "wss"} or raw.startswith("//"):
                self.remote_refs.append(f"{tag}[{key}]={raw}")
            elif tag in {"script", "link", "img", "iframe", "object", "embed", "video", "audio", "source"}:
                self.remote_refs.append(f"external asset {tag}[{key}]={raw}")

        raw_sources = values.get("data-sources", "") or values.get("data-source", "")
        for item in raw_sources.split("|"):
            item = item.strip()
            if item:
                self.source_paths.append(item)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "style":
            self._capture_style = False
        if tag == "script":
            self._capture_script = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self._capture_style:
            self.styles.append(data)
        if self._capture_script:
            self.scripts.append(data)


class CounterLike(dict[str, int]):
    def __missing__(self, key: str) -> int:
        return 0


def validate_source_path(raw: str, project_root: Path) -> str | None:
    normalized = raw.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or ".." in posix.parts or re.match(r"^[A-Za-z]:", normalized):
        return f"source path must be project-relative: {raw}"
    candidate = (project_root / Path(*posix.parts)).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return f"source path escapes project root: {raw}"
    if not candidate.is_file():
        return f"source path does not exist: {raw}"
    return None


def validate(html_path: Path, project_root: Path | None) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw = html_path.read_bytes()
    except OSError as exc:
        return {"valid": False, "errors": [f"cannot read HTML: {exc}"], "warnings": []}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"valid": False, "errors": [f"HTML is not UTF-8: {exc}"], "warnings": []}

    if not re.match(r"\s*<!doctype\s+html", text, re.I):
        errors.append("missing <!DOCTYPE html>")
    if PLACEHOLDER_RE.search(text):
        errors.append("placeholder text remains")

    parser = ShowcaseParser()
    try:
        parser.feed(text)
    except Exception as exc:  # HTMLParser can surface malformed declarations.
        errors.append(f"HTML parser error: {exc}")

    if parser.tags["html"] != 1:
        errors.append("expected exactly one html element")
    if parser.tags["head"] != 1 or parser.tags["body"] != 1:
        errors.append("expected exactly one head and one body")
    if not parser.html_lang:
        errors.append("html element is missing lang")
    if not parser.has_charset:
        errors.append("missing UTF-8 charset meta")
    if not parser.has_viewport:
        errors.append("missing responsive viewport meta")
    if not "".join(parser.title_parts).strip():
        errors.append("document title is empty")
    if parser.tags["h1"] != 1:
        errors.append("expected exactly one h1")
    if parser.tags["main"] != 1:
        errors.append("expected exactly one main landmark")
    if parser.tags["nav"] < 1:
        errors.append("missing local navigation")
    if parser.tags["section"] < 2:
        errors.append("expected at least two factual sections")
    if parser.tags["style"] < 1:
        errors.append("missing inline style")
    if parser.remote_refs:
        errors.extend(f"not self-contained: {item}" for item in parser.remote_refs)
    if parser.missing_img_alt:
        errors.append(f"{parser.missing_img_alt} image(s) are missing alt text")
    if parser.button_without_type:
        errors.append(f"{parser.button_without_type} button(s) are missing an explicit type")

    duplicates = sorted({item for item in parser.ids if parser.ids.count(item) > 1})
    if duplicates:
        errors.append("duplicate ids: " + ", ".join(duplicates))
    id_set = set(parser.ids)
    missing_targets = sorted({target for target in parser.hash_links if target and target not in id_set})
    if missing_targets:
        errors.append("hash links target missing ids: " + ", ".join(missing_targets))

    css = "\n".join(parser.styles)
    scripts = "\n".join(parser.scripts)
    if CSS_REMOTE_RE.search(css):
        errors.append("CSS contains an external import or URL")
    if HTTP_CALL_RE.search(scripts):
        errors.append("inline script contains a network API primitive")

    unique_sources = list(dict.fromkeys(parser.source_paths))
    if not unique_sources:
        errors.append("no data-sources provenance found")
    elif len(unique_sources) < 3:
        warnings.append("fewer than three unique source paths are cited")
    if project_root is not None:
        for source in unique_sources:
            issue = validate_source_path(source, project_root)
            if issue:
                errors.append(issue)

    return {
        "valid": not errors,
        "html": str(html_path),
        "project_root": str(project_root) if project_root else None,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "bytes": len(raw),
            "sections": parser.tags["section"],
            "navigation_links": len(parser.hash_links),
            "unique_source_paths": len(unique_sources),
            "inline_style_blocks": parser.tags["style"],
            "inline_script_blocks": parser.tags["script"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a project workflow showcase HTML.")
    parser.add_argument("html", type=Path, help="HTML file to validate")
    parser.add_argument("--project-root", type=Path, help="Project root for source-path checks")
    parser.add_argument("--pretty", action="store_true", help="Indent JSON output")
    args = parser.parse_args()

    html_path = args.html.expanduser().resolve()
    project_root = args.project_root.expanduser().resolve() if args.project_root else None
    if project_root is not None and not project_root.is_dir():
        parser.error(f"project root is not a directory: {project_root}")
    result = validate(html_path, project_root)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
