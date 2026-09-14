"""Read-only record labels derived from persisted input and analysis provenance."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


def source_archive_names(cases: dict[str, Any]) -> list[str]:
    names = []
    for case in cases.values():
        if isinstance(case, dict) and case.get("source_archive"):
            name = PurePosixPath(str(case["source_archive"]).replace("\\", "/")).name
            if name and name not in names:
                names.append(name)
    return names


def archive_display_name(names: list[str]) -> str:
    if not names:
        return ""
    first = re.sub(r"\.zip$", "", names[0], flags=re.IGNORECASE)
    return first if len(names) == 1 else f"{first} 等 {len(names)} 个资料包"


def record_labels(manifest: dict[str, Any], workspace: Path | None = None) -> dict[str, Any]:
    names = list(manifest.get("source_archives") or [])
    started = manifest.get("analysis_started_at")

    def local_file(relative: str) -> Path | None:
        if workspace is None:
            return None
        candidate = workspace / relative
        if candidate.is_symlink() or not candidate.is_file():
            return None
        if not candidate.resolve().is_relative_to(workspace.resolve()):
            return None
        return candidate

    if not names:
        cases_path = local_file("analysis/input-cases.json")
        if cases_path is not None:
            try:
                cases = json.loads(cases_path.read_text(encoding="utf-8"))
                if isinstance(cases, dict):
                    names = source_archive_names(cases)
            except (OSError, ValueError):
                pass
    if not started:
        events_path = local_file("logs/events.jsonl")
        if events_path is not None:
            try:
                with events_path.open(encoding="utf-8") as stream:
                    # Startup events precede the visual calls; never parse model logs.
                    for line in stream.read(65536).splitlines()[:128]:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and event.get("type") == "scenario.started":
                            started = event.get("timestamp")
                            break
            except OSError:
                pass
    source = manifest.get("input_source")
    if source not in ("input", "oss"):
        source = "oss" if re.fullmatch(r"\d{8}-oss-[a-f0-9]{12}", str(manifest.get("run_id") or "")) else "input"
    return {
        "source_archives": names,
        "display_name": archive_display_name(names) or manifest.get("workspace_id"),
        "analysis_started_at": started,
        "input_source": source,
    }
