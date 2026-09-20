from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker


class AuditError(RuntimeError):
    """Raised when evidence cannot be audited safely."""


MONEY_QUANT = Decimal("0.01")
POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX = (
    "[deterministic-poster-material-quantity-calibration]"
)


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuditError(f"JSON file does not exist: {source}") from exc
    except json.JSONDecodeError as exc:
        raise AuditError(f"Invalid JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"Top-level JSON value must be an object: {source}")
    return value


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def validate_json(value: dict[str, Any], schema_path: str | Path) -> None:
    schema = load_json(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda err: list(err.path))
    if not errors:
        return
    rendered: list[str] = []
    for error in errors[:20]:
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        rendered.append(f"{location}: {error.message}")
    if len(errors) > 20:
        rendered.append(f"... and {len(errors) - 20} more validation errors")
    raise AuditError("Evidence schema validation failed:\n" + "\n".join(rendered))


def resolve_root(config_path: str | Path, config: dict[str, Any]) -> Path:
    config_file = Path(config_path).resolve()
    raw = str(config.get("case_root") or ".")
    root = Path(raw)
    if not root.is_absolute():
        root = config_file.parent / root
    return root.resolve()


def resolve_case_file(
    config_path: str | Path,
    config: dict[str, Any],
    key: str,
    *,
    required: bool = True,
) -> Path | None:
    files = config.get("files")
    if not isinstance(files, dict):
        raise AuditError("Case config must contain a files object")
    raw = files.get(key)
    if raw in (None, ""):
        if required:
            raise AuditError(f"Case config is missing files.{key}")
        return None
    path = Path(str(raw))
    if not path.is_absolute():
        path = resolve_root(config_path, config) / path
    path = path.resolve()
    if required and not path.exists():
        raise AuditError(f"Evidence path does not exist for files.{key}: {path}")
    return path


def resolve_case_files(
    config_path: str | Path,
    config: dict[str, Any],
    key: str,
) -> list[Path]:
    files = config.get("files")
    if not isinstance(files, dict):
        raise AuditError("Case config must contain a files object")
    raw = files.get(key, [])
    if not isinstance(raw, list):
        raise AuditError(f"files.{key} must be an array")
    root = resolve_root(config_path, config)
    result: list[Path] = []
    for item in raw:
        path = Path(str(item))
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.exists():
            raise AuditError(f"Evidence path does not exist in files.{key}: {path}")
        result.append(path)
    return result


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_manifest(paths: Iterable[Path], root: Path | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted({item.resolve() for item in paths}, key=lambda item: str(item).lower()):
        if path.is_dir():
            continue
        try:
            display = str(path.relative_to(root)) if root else str(path)
        except ValueError:
            display = str(path)
        stat = path.stat()
        records.append(
            {
                "path": display,
                "absolute_path": str(path),
                "size": stat.st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("參半", "参半")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def barcode_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def decimal_value(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AuditError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise AuditError(f"{label} must be numeric: {value!r}") from exc
    if not result.is_finite():
        raise AuditError(f"{label} must be finite")
    return result


def money(value: Any, *, label: str = "amount") -> Decimal:
    return decimal_value(value, label=label).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def json_number(value: Decimal | int | float) -> int | float:
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return float(decimal.normalize())


def clean_identifier(value: str, fallback: str = "run") -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", normalized).strip("-._")
    return cleaned[:80] or fallback


def exception(
    severity: str,
    code: str,
    message: str,
    impact: str,
    suggestion: str,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "message": message,
        "impact": impact,
        "suggestion": suggestion,
    }
    if source:
        item["source"] = source
    return item


def unique_by(items: Iterable[dict[str, Any]], key: str, label: str) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for item in items:
        identity = item.get(key)
        if identity in result:
            raise AuditError(f"Duplicate {label}: {identity}")
        result[identity] = item
    return result


def optional_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def normalized_name_score(left: Any, right: Any) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()
