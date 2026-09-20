from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import unicodedata
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .common import AuditError, load_json, validate_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCATION_REFERENCE_ROOT = (
    PROJECT_ROOT / "contracts" / "legacy" / "promotional_display" / "references"
)
LOCATION_REGISTRY_PATH = LOCATION_REFERENCE_ROOT / "location-resolution-registry.json"
LOCATION_REGISTRY_SCHEMA_PATH = (
    LOCATION_REFERENCE_ROOT / "location-resolution-registry.schema.json"
)

BAIDU_MAPS_MCP_ENDPOINT = "https://mcp.map.baidu.com/mcp"
NEARBY_PASS_METERS = 100.0
UNRELATED_MIN_METERS = 300.0
CANDIDATE_PAIR_MIN_RELEVANCE = 0.60
DEFAULT_TIMEOUT_SECONDS = 30

TERMINAL_RELATIONSHIPS = {"same_place", "parent_child", "nearby", "unrelated"}
PASS_RELATIONSHIPS = {"same_place", "parent_child", "nearby"}
PROVINCE_CITY_PATTERN = re.compile(
    r"(?:[\u4e00-\u9fff]{2,8}(?:省|自治区))"
    r"(?P<city>[\u4e00-\u9fff]{2,8}(?:市(?!场)|自治州|地区|盟))"
)
CITY_PATTERN = re.compile(
    r"(?:北京市|上海市|天津市|重庆市|香港特别行政区|澳门特别行政区|"
    r"[\u4e00-\u9fff]{2,8}(?:市(?!场)|自治州|地区|盟))"
)
ROAD_NUMBER_PATTERN = re.compile(
    r"(?P<road>[\u4e00-\u9fff]{1,12}(?:路|街|道|大道|中路|西路|东路|南路|北路))"
    r"(?P<number>\d{1,6})号"
)


class LocationResolver(Protocol):
    def resolve(
        self,
        *,
        contract_name: str,
        contract_address: str | None,
        visible_location: str,
        city_hint: str | None,
    ) -> dict[str, Any]: ...


McpLookup = Callable[[list[dict[str, str]]], list[dict[str, Any]]]


def location_confidence(status: str) -> str:
    """Translate a location relationship into the user-facing confidence model."""

    if status in PASS_RELATIONSHIPS:
        return "high"
    if status in {"not_applicable", "not_needed"}:
        return "not_applicable"
    return "low"


def _canonical(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _bigrams(value: Any) -> set[str]:
    text = _canonical(value)
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def city_hint_from_values(*values: Any) -> str | None:
    for value in values:
        text = str(value or "")
        province_match = PROVINCE_CITY_PATTERN.search(text)
        if province_match:
            return province_match.group("city")
        match = CITY_PATTERN.search(text)
        if match:
            return match.group(0)
    return None


def _poi(
    *,
    name: Any,
    address: Any,
    poi_id: Any = None,
    longitude: Any = None,
    latitude: Any = None,
    source_title: Any = None,
    source_url: Any = None,
) -> dict[str, Any]:
    try:
        lon = float(longitude) if longitude not in (None, "") else None
        lat = float(latitude) if latitude not in (None, "") else None
    except (TypeError, ValueError):
        lon = lat = None
    if lon is not None and not -180 <= lon <= 180:
        lon = None
    if lat is not None and not -90 <= lat <= 90:
        lat = None
    result = {
        "name": str(name or "").strip(),
        "address": str(address or "").strip(),
        "poi_id": str(poi_id or "").strip() or None,
        "longitude": lon,
        "latitude": lat,
    }
    if source_title not in (None, ""):
        result["source_title"] = str(source_title).strip()
    if source_url not in (None, ""):
        result["source_url"] = str(source_url).strip()
    return result


def _empty_resolution(
    *,
    status: str,
    provider: str,
    transport: str,
    contract_query: str,
    watermark_query: str,
    city_hint: str | None,
    mcp_attempted: bool,
    mcp_status: str,
    basis: str,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "confidence": location_confidence(status),
        "provider": provider,
        "transport": transport,
        "mcp_attempted": mcp_attempted,
        "mcp_status": mcp_status,
        "contract_query": contract_query,
        "watermark_query": watermark_query,
        "city_hint": city_hint,
        "coordinate_system": None,
        "contract_poi": None,
        "watermark_poi": None,
        "distance_meters": None,
        "nearby_pass_meters": NEARBY_PASS_METERS,
        "unrelated_min_meters": UNRELATED_MIN_METERS,
        "evidence": list(evidence or []),
        "basis": basis,
    }


def deterministic_location_resolution(
    *,
    status: str,
    contract_name: str,
    visible_location: str | None,
    basis: str,
) -> dict[str, Any]:
    return _empty_resolution(
        status=status,
        provider="deterministic_name",
        transport="local",
        contract_query=contract_name,
        watermark_query=str(visible_location or ""),
        city_hint=city_hint_from_values(visible_location, contract_name),
        mcp_attempted=False,
        mcp_status="not_needed",
        basis=basis,
    )


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {str(key): _model_dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_dump(item) for item in value]
    return value


def _json_candidates(text: str) -> list[Any]:
    clean = text.strip()
    candidates = [clean]
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", clean, flags=re.IGNORECASE)
    candidates.extend(fenced)
    first_object = clean.find("{")
    last_object = clean.rfind("}")
    if 0 <= first_object < last_object:
        candidates.append(clean[first_object : last_object + 1])
    values: list[Any] = []
    for candidate in candidates:
        try:
            values.append(json.loads(candidate))
        except (TypeError, json.JSONDecodeError):
            continue
    return values


def _tool_result_payloads(result: Any) -> list[Any]:
    dumped = _model_dump(result)
    payloads: list[Any] = []
    if isinstance(dumped, dict):
        structured = dumped.get("structuredContent") or dumped.get("structured_content")
        if structured is not None:
            payloads.append(structured)
        for item in dumped.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                payloads.extend(_json_candidates(str(item.get("text") or "")))
    payloads.append(dumped)
    return payloads


def _baidu_maps_mcp_url(api_key: str) -> str:
    """Build the fixed Baidu MCP URL without accepting an endpoint override."""

    return f"{BAIDU_MAPS_MCP_ENDPOINT}?{urlencode({'ak': api_key})}"


def _redact_source_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        if not parsed.scheme or not parsed.netloc:
            return text
        safe_query = [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in {"ak", "api_key", "apikey", "key"}
        ]
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(safe_query),
                parsed.fragment,
            )
        )
    except ValueError:
        return None


def _ensure_baidu_tool_result_ok(result: Any, payloads: list[Any]) -> None:
    dumped = _model_dump(result)
    if isinstance(dumped, dict) and bool(
        dumped.get("isError") or dumped.get("is_error")
    ):
        raise AuditError("百度地图 MCP map_search_places 返回工具错误")
    for payload in payloads:
        if not isinstance(payload, dict) or "status" not in payload:
            continue
        status = payload.get("status")
        if status not in (0, "0", None):
            raise AuditError(
                f"百度地图 MCP map_search_places 返回业务错误（status={status}）"
            )


async def _baidu_maps_mcp_lookup_async(
    api_key: str,
    queries: list[dict[str, str]],
    *,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    try:
        import httpx  # type: ignore[import-not-found]
        from mcp import ClientSession  # type: ignore[import-not-found]
        from mcp.client.streamable_http import (  # type: ignore[import-not-found]
            streamable_http_client,
        )
    except ImportError as exc:
        raise AuditError("未安装 mcp Python SDK，请先执行 py -3 -m pip install -e .") from exc

    async def execute() -> list[dict[str, Any]]:
        endpoint = _baidu_maps_mcp_url(api_key)
        async with httpx.AsyncClient(
            headers={"Accept": "application/json, text/event-stream"},
            timeout=timeout_seconds,
        ) as http_client, streamable_http_client(
            endpoint,
            http_client=http_client,
        ) as transport:
            read_stream, write_stream = transport[0], transport[1]
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            ) as session:
                await session.initialize()
                listed = await session.list_tools()
                tools = list(getattr(listed, "tools", []) or [])
                names = [str(getattr(tool, "name", "")) for tool in tools]
                tool_name = next(
                    (
                        name
                        for name in names
                        if name == "map_search_places"
                        or name.endswith("map_search_places")
                    ),
                    None,
                )
                if tool_name is None:
                    raise AuditError(
                        "百度地图 MCP 未提供 map_search_places 工具；可用工具："
                        + "、".join(name for name in names if name)
                    )
                results: list[dict[str, Any]] = []
                for query in queries:
                    query_text = str(
                        query.get("query") or query.get("keywords") or ""
                    ).strip()
                    if not query_text:
                        raise AuditError("百度地图 MCP 地点查询词不能为空")
                    arguments = {
                        "query": query_text,
                        "region": str(
                            query.get("region") or query.get("city") or "全国"
                        ).strip(),
                    }
                    result = await session.call_tool(tool_name, arguments=arguments)
                    payloads = _tool_result_payloads(result)
                    _ensure_baidu_tool_result_ok(result, payloads)
                    results.append(
                        {
                            "query": dict(query),
                            "tool": tool_name,
                            "payloads": payloads,
                        }
                    )
                return results

    return await asyncio.wait_for(execute(), timeout=timeout_seconds)


def _run_async(coroutine: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # pragma: no cover - defensive embedding path
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def _windows_user_environment(name: str) -> str:
    """Read a per-user Windows environment setting without copying it into the repo."""

    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except (FileNotFoundError, OSError):
        return ""
    return str(value).strip()


def _configured_secret(name: str) -> str:
    return str(os.environ.get(name) or "").strip() or _windows_user_environment(name)


def baidu_maps_mcp_lookup_from_environment() -> McpLookup | None:
    api_key = _configured_secret("BAIDU_MAPS_API_KEY")
    if not api_key:
        return None
    raw_timeout = str(
        os.environ.get("OFFLINE_AUDIT_LOCATION_MCP_TIMEOUT_SECONDS") or ""
    ).strip()
    try:
        timeout = int(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    except ValueError as exc:
        raise AuditError("OFFLINE_AUDIT_LOCATION_MCP_TIMEOUT_SECONDS 必须是整数") from exc
    if not 5 <= timeout <= 120:
        raise AuditError("地点 MCP 超时必须在 5 至 120 秒之间")

    def lookup(queries: list[dict[str, str]]) -> list[dict[str, Any]]:
        try:
            return _run_async(
                _baidu_maps_mcp_lookup_async(
                    api_key,
                    queries,
                    timeout_seconds=timeout,
                )
            )
        except Exception as exc:
            raise AuditError(
                f"百度地图 MCP 调用失败（{BAIDU_MAPS_MCP_ENDPOINT}，{type(exc).__name__}）"
            ) from None

    return lookup


def _location_parts(value: Any) -> tuple[Any, Any]:
    if isinstance(value, str) and "," in value:
        left, right = value.split(",", 1)
        return left.strip(), right.strip()
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return value[0], value[1]
    if isinstance(value, dict):
        return (
            value.get("lng") or value.get("lon") or value.get("longitude"),
            value.get("lat") or value.get("latitude"),
        )
    return None, None


def _candidate_from_mapping(value: dict[str, Any]) -> dict[str, Any] | None:
    lowered = {str(key).lower(): item for key, item in value.items()}
    attribution = lowered.get("attribution")
    if not isinstance(attribution, dict):
        attribution = {}
    source_title = attribution.get("title")
    source_url = attribution.get("url")
    name = lowered.get("name") or lowered.get("title") or source_title
    if isinstance(name, str):
        name = re.sub(
            r"\s+-\s+(?:Google Maps|百度地图)\s*$", "", name, flags=re.IGNORECASE
        )
    location = lowered.get("location") or lowered.get("coordinate")
    longitude, latitude = _location_parts(location)
    longitude = lowered.get("longitude") or lowered.get("lng") or longitude
    latitude = lowered.get("latitude") or lowered.get("lat") or latitude
    address = (
        lowered.get("address")
        or lowered.get("formatted_address")
        or lowered.get("formattedaddress")
        or lowered.get("addr")
    )
    if not name or (longitude in (None, "") and latitude in (None, "") and not address):
        return None
    address_text = str(address or "").strip()
    address_key = _canonical(address_text)
    address_prefixes: list[str] = []
    seen_prefixes: set[str] = set()
    for item in (
        lowered.get("province"),
        lowered.get("cityname") or lowered.get("city"),
        lowered.get("area") or lowered.get("adname") or lowered.get("district"),
    ):
        part = str(item or "").strip()
        part_key = _canonical(part)
        if not part_key or part_key in address_key or part_key in seen_prefixes:
            continue
        address_prefixes.append(part)
        seen_prefixes.add(part_key)
    full_address = "".join(address_prefixes) + address_text
    return _poi(
        name=name,
        address=full_address,
        poi_id=(
            lowered.get("id")
            or lowered.get("place")
            or lowered.get("uid")
            or lowered.get("poi_id")
        ),
        longitude=longitude,
        latitude=latitude,
        source_title=source_title,
        source_url=_redact_source_url(source_url),
    )


def _collect_pois(value: Any, output: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        candidate = _candidate_from_mapping(value)
        if candidate is not None:
            output.append(candidate)
        for key, item in value.items():
            if str(key).lower() in {
                "pois", "places", "results", "result", "data", "content", "features"
            }:
                _collect_pois(item, output)
    elif isinstance(value, list):
        for item in value:
            _collect_pois(item, output)


def extract_pois(payloads: list[Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for payload in payloads:
        _collect_pois(payload, candidates)
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in candidates:
        key = (
            item.get("poi_id"),
            _canonical(item.get("name")),
            _canonical(item.get("address")),
            item.get("longitude"),
            item.get("latitude"),
        )
        unique.setdefault(key, item)
    return list(unique.values())


def _poi_score(query: str, poi: dict[str, Any]) -> float:
    query_text = _canonical(query)
    name_text = _canonical(poi.get("name"))
    address_text = _canonical(poi.get("address"))
    if not query_text or not name_text:
        return 0.0
    if query_text in name_text or name_text in query_text:
        return 1.0
    query_grams = _bigrams(query)
    poi_grams = _bigrams(f"{poi.get('name') or ''}{poi.get('address') or ''}")
    overlap = len(query_grams & poi_grams) / max(1, min(len(query_grams), len(poi_grams)))
    address_bonus = 0.15 if address_text and address_text in query_text else 0.0
    return min(1.0, overlap + address_bonus)


LOCATION_IDENTITY_GENERIC_TOKENS = (
    "生活购物广场",
    "生活购物中心",
    "购物广场",
    "购物中心",
    "百货广场",
    "百货商场",
    "生活超市",
    "连锁超市",
    "超级市场",
    "百货",
    "商场",
    "超市",
    "旗舰店",
    "分店",
    "门店",
    "广场",
    "店",
)


def _location_identity_text(value: Any) -> str:
    """Keep distinguishing place text while dropping administrative/store wrappers."""

    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"(?:中华人民共和国|中国)", "", text)
    text = re.sub(r"[\u4e00-\u9fff]{2,8}(?:省|自治区)", "", text)
    text = CITY_PATTERN.sub("", text)
    canonical = _canonical(text)
    for token in LOCATION_IDENTITY_GENERIC_TOKENS:
        canonical = canonical.replace(token, "")
    return canonical


def _longest_common_substring_length(left: str, right: str) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for left_character in left:
        current = [0]
        for index, right_character in enumerate(right, 1):
            length = previous[index - 1] + 1 if left_character == right_character else 0
            current.append(length)
            best = max(best, length)
        previous = current
    return best


def _candidate_pair_relevance(query: str, poi: dict[str, Any]) -> float:
    """Score a POI for relation-aware pairing without requiring standalone uniqueness."""

    raw_score = _poi_score(query, poi)
    query_identity = _location_identity_text(query)
    poi_identities = [
        _location_identity_text(poi.get("name")),
        _location_identity_text(
            f"{poi.get('name') or ''}{poi.get('address') or ''}"
        ),
    ]
    poi_identities = [value for value in poi_identities if value]
    if not query_identity or not poi_identities:
        return raw_score
    query_grams = _bigrams(query_identity)
    identity_scores: list[float] = []
    for poi_identity in poi_identities:
        if query_identity in poi_identity:
            identity_scores.append(1.0)
            continue
        if poi_identity in query_identity:
            identity_scores.append(len(poi_identity) / max(1, len(query_identity)))
            continue
        poi_grams = _bigrams(poi_identity)
        bigram_score = len(query_grams & poi_grams) / max(
            1, min(len(query_grams), len(poi_grams))
        )
        common_span_score = _longest_common_substring_length(
            query_identity, poi_identity
        ) / max(1, len(query_identity))
        identity_scores.append(max(bigram_score, common_span_score))
    identity_score = max(identity_scores, default=0.0)
    return min(1.0, max(raw_score, identity_score))


def _credible_pair_anchor(
    query: str,
    candidates: list[dict[str, Any]],
    selected: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep a unique-side selection only when it clearly represents the query itself."""

    if selected is None:
        return None
    if len(candidates) == 1:
        return selected
    raw_name = str(selected.get("name") or "").strip()
    query_identity = _location_identity_text(query)
    name_identity = _location_identity_text(raw_name)
    if not query_identity or len(name_identity) < 3:
        return None
    if re.search(r"(?:省|市|区|县|镇|乡|街道|村)$", raw_name) and (
        _canonical(query) != _canonical(raw_name)
    ):
        return None
    if query_identity in name_identity or name_identity in query_identity:
        return selected
    return None


def select_unique_poi(
    query: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    if len(candidates) == 1:
        return candidates[0], "搜索仅返回一个地点候选"
    ranked = sorted(
        ((_poi_score(query, item), index, item) for index, item in enumerate(candidates)),
        key=lambda value: (-value[0], value[1]),
    )
    if not ranked or ranked[0][0] < 0.35:
        return None, "没有找到与查询名称足够对应的POI"
    top_score, _, top = ranked[0]
    if len(ranked) > 1:
        second_score = ranked[1][0]
        if top_score < 0.9 and top_score - second_score < 0.15:
            return None, "搜索结果存在多个相近候选，不能唯一定位"
    return top, "按名称与地址相关性唯一选择POI"


def select_compatible_poi_pair(
    *,
    contract_query: str,
    contract_candidates: list[dict[str, Any]],
    watermark_query: str,
    watermark_candidates: list[dict[str, Any]],
    contract_anchor: dict[str, Any] | None = None,
    watermark_anchor: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Find a credible spatial pair while preserving any independently selected side."""

    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    relevance_scope = (
        "双方候选"
        if contract_anchor is None and watermark_anchor is None
        else "未唯一定位侧候选"
    )
    anchor_note = (
        "保留已唯一定位侧，并"
        if contract_anchor is not None or watermark_anchor is not None
        else ""
    )
    relationship_priority = {"same_place": 3, "parent_child": 2, "nearby": 1}
    contract_pool = [contract_anchor] if contract_anchor is not None else contract_candidates
    watermark_pool = (
        [watermark_anchor] if watermark_anchor is not None else watermark_candidates
    )
    for contract_index, contract_poi in enumerate(contract_pool):
        contract_score = _candidate_pair_relevance(contract_query, contract_poi)
        if (
            contract_anchor is None
            and contract_score < CANDIDATE_PAIR_MIN_RELEVANCE
        ):
            continue
        for watermark_index, watermark_poi in enumerate(watermark_pool):
            watermark_score = _candidate_pair_relevance(watermark_query, watermark_poi)
            if (
                watermark_anchor is None
                and watermark_score < CANDIDATE_PAIR_MIN_RELEVANCE
            ):
                continue
            relationship, distance, relationship_basis = classify_poi_relationship(
                contract_poi, watermark_poi
            )
            if relationship not in PASS_RELATIONSHIPS:
                continue
            same_poi_id = bool(
                contract_poi.get("poi_id")
                and contract_poi.get("poi_id") == watermark_poi.get("poi_id")
            )
            distance_rank = -(
                float(distance) if distance is not None else UNRELATED_MIN_METERS
            )
            rank = (
                int(same_poi_id),
                relationship_priority[relationship],
                min(contract_score, watermark_score),
                (contract_score + watermark_score) / 2,
                distance_rank,
                -contract_index,
                -watermark_index,
            )
            ranked.append(
                (
                    rank,
                    {
                        "contract_poi": contract_poi,
                        "watermark_poi": watermark_poi,
                        "relationship": relationship,
                        "distance_meters": distance,
                        "relationship_basis": relationship_basis,
                    },
                )
            )
    if not ranked:
        return (
            None,
            f"候选集合之间没有找到{relevance_scope}名称相关性达到预设门槛，"
            "且同址、同建筑或100米内的地点配对",
        )
    _, selected = max(ranked, key=lambda value: value[0])
    return (
        selected,
        f"不要求两次搜索各自唯一；{anchor_note}从候选集合中选出"
        f"{relevance_scope}名称相关性达到预设门槛的可信地点配对",
    )


def _search_evidence_lines(
    *,
    role: str,
    query: str,
    candidates: list[dict[str, Any]],
    selection_basis: str,
) -> list[str]:
    lines = [
        f"map_search_places {role}查询：{query}；返回{len(candidates)}个候选；{selection_basis}"
    ]
    for index, candidate in enumerate(candidates[:5], 1):
        coordinates = ""
        if candidate.get("longitude") is not None and candidate.get("latitude") is not None:
            coordinates = (
                f"；坐标{candidate['longitude']},{candidate['latitude']}（BD09LL）"
            )
        source = ""
        if candidate.get("source_url"):
            source = (
                "；地图服务来源："
                f"{candidate.get('source_title') or candidate.get('name') or '地点结果'} "
                f"{candidate['source_url']}"
            )
        lines.append(
            f"{role}候选{index}：{candidate.get('name') or '未命名地点'}"
            f"｜{candidate.get('address') or '未返回地址'}{coordinates}{source}"
        )
    return lines


def _haversine_meters(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    values = (
        left.get("longitude"), left.get("latitude"),
        right.get("longitude"), right.get("latitude"),
    )
    if any(value is None for value in values):
        return None
    lon1, lat1, lon2, lat2 = (math.radians(float(value)) for value in values)
    delta_lon = lon2 - lon1
    delta_lat = lat2 - lat1
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return round(6371008.8 * 2 * math.asin(math.sqrt(haversine)), 1)


def _same_building_address(left: str, right: str) -> bool:
    left_text = _canonical(left)
    right_text = _canonical(right)
    if not left_text or not right_text:
        return False
    if min(len(left_text), len(right_text)) >= 8 and (
        left_text in right_text or right_text in left_text
    ):
        return True
    left_match = ROAD_NUMBER_PATTERN.search(str(left or ""))
    right_match = ROAD_NUMBER_PATTERN.search(str(right or ""))
    if not left_match or not right_match:
        return False
    return (
        _canonical(left_match.group("road")) == _canonical(right_match.group("road"))
        and left_match.group("number") == right_match.group("number")
    )


def classify_poi_relationship(
    contract_poi: dict[str, Any],
    watermark_poi: dict[str, Any],
) -> tuple[str, float | None, str]:
    distance = _haversine_meters(contract_poi, watermark_poi)
    contract_id = str(contract_poi.get("poi_id") or "").strip()
    watermark_id = str(watermark_poi.get("poi_id") or "").strip()
    if contract_id and contract_id == watermark_id:
        return "same_place", distance, "两次搜索返回同一POI ID"

    contract_name = _canonical(contract_poi.get("name"))
    watermark_name = _canonical(watermark_poi.get("name"))
    same_name = bool(contract_name and watermark_name) and (
        contract_name in watermark_name or watermark_name in contract_name
    )
    same_building = _same_building_address(
        str(contract_poi.get("address") or ""),
        str(watermark_poi.get("address") or ""),
    )
    if same_name:
        if distance is None or distance < UNRELATED_MIN_METERS:
            return "same_place", distance, "POI名称一致且坐标未形成明显冲突"
        return "ambiguous", distance, "POI名称一致但坐标相距过远，数据互相冲突"
    if same_building:
        if distance is None or distance < UNRELATED_MIN_METERS:
            return "parent_child", distance, "地址显示位于同一建筑，属于商场与店内门店关系"
        return "ambiguous", distance, "门牌地址显示同一建筑但坐标相距过远，数据互相冲突"
    if distance is not None and distance <= NEARBY_PASS_METERS:
        return "nearby", distance, f"选定的两个POI直线距离不超过{NEARBY_PASS_METERS:g}米"
    if distance is not None and distance >= UNRELATED_MIN_METERS:
        return "unrelated", distance, f"选定的两个POI直线距离不少于{UNRELATED_MIN_METERS:g}米"
    return (
        "ambiguous",
        distance,
        f"地点距离位于{NEARBY_PASS_METERS:g}至{UNRELATED_MIN_METERS:g}米灰区，或缺少可比坐标",
    )


def _registry_resolution(
    registry: dict[str, Any],
    *,
    contract_name: str,
    visible_location: str,
    city_hint: str | None,
    mcp_attempted: bool,
    mcp_status: str,
) -> dict[str, Any] | None:
    contract_key = _canonical(contract_name)
    visible_key = _canonical(visible_location)
    for entry in registry.get("entries") or []:
        if _canonical(entry.get("contract_name")) != contract_key:
            continue
        if _canonical(entry.get("visible_location")) != visible_key:
            continue
        relationship = str(entry["relationship"])
        sources = list(entry.get("sources") or [])
        source_labels = [
            f"{source['publisher']}《{source['title']}》（核验日期{source['checked_on']}）"
            for source in sources
        ]
        mcp_note = (
            "百度地图 MCP已调用但未形成唯一结论"
            if mcp_attempted
            else "百度地图 MCP未配置"
        )
        basis = f"{mcp_note}；采用已复核权威地点映射。{entry['basis']}"
        return {
            "status": relationship,
            "confidence": location_confidence(relationship),
            "provider": "authoritative_registry",
            "transport": "local_verified_sources",
            "mcp_attempted": mcp_attempted,
            "mcp_status": mcp_status,
            "contract_query": contract_name,
            "watermark_query": visible_location,
            "city_hint": city_hint or str(entry.get("city") or "") or None,
            "coordinate_system": None,
            "contract_poi": _poi(
                name=entry["contract_name"],
                address=entry["contract_address"],
            ),
            "watermark_poi": _poi(
                name=entry["visible_location"],
                address=entry["watermark_address"],
            ),
            "distance_meters": entry.get("distance_meters"),
            "nearby_pass_meters": NEARBY_PASS_METERS,
            "unrelated_min_meters": UNRELATED_MIN_METERS,
            "evidence": [*source_labels, str(entry["basis"])],
            "basis": basis,
        }
    return None


class StoreLocationResolver:
    def __init__(
        self,
        *,
        mcp_lookup: McpLookup | None,
        registry: dict[str, Any],
    ) -> None:
        self._mcp_lookup = mcp_lookup
        self._registry = registry
        self._cache: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def resolve(
        self,
        *,
        contract_name: str,
        contract_address: str | None,
        visible_location: str,
        city_hint: str | None,
    ) -> dict[str, Any]:
        city = city_hint or city_hint_from_values(
            visible_location, contract_address, contract_name
        )
        cache_key = (
            _canonical(contract_name),
            _canonical(contract_address),
            _canonical(visible_location),
            _canonical(city),
        )
        if cache_key in self._cache:
            return dict(self._cache[cache_key])

        contract_query = " ".join(
            value for value in (contract_name, contract_address) if str(value or "").strip()
        )
        queries = [
            {"query": contract_query, "region": city or "全国"},
            {"query": visible_location, "region": city or "全国"},
        ]
        mcp_attempted = self._mcp_lookup is not None
        mcp_status = "not_configured"
        mcp_resolution: dict[str, Any] | None = None
        if self._mcp_lookup is not None:
            try:
                responses = self._mcp_lookup(queries)
                if len(responses) != 2:
                    raise AuditError("地点 MCP 必须为合同地点和水印地点各返回一次结果")
                contract_candidates = extract_pois(list(responses[0].get("payloads") or []))
                watermark_candidates = extract_pois(list(responses[1].get("payloads") or []))
                contract_poi, contract_selection = select_unique_poi(
                    contract_query, contract_candidates
                )
                watermark_poi, watermark_selection = select_unique_poi(
                    visible_location, watermark_candidates
                )
                candidate_pair: dict[str, Any] | None = None
                candidate_pair_basis = ""
                if contract_poi is None or watermark_poi is None:
                    candidate_pair, candidate_pair_basis = select_compatible_poi_pair(
                        contract_query=contract_query,
                        contract_candidates=contract_candidates,
                        watermark_query=visible_location,
                        watermark_candidates=watermark_candidates,
                        contract_anchor=_credible_pair_anchor(
                            contract_query, contract_candidates, contract_poi
                        ),
                        watermark_anchor=_credible_pair_anchor(
                            visible_location, watermark_candidates, watermark_poi
                        ),
                    )
                    if candidate_pair is not None:
                        contract_poi = candidate_pair["contract_poi"]
                        watermark_poi = candidate_pair["watermark_poi"]

                if contract_poi is None or watermark_poi is None:
                    mcp_status = "ambiguous"
                    ambiguous_evidence = [
                        *_search_evidence_lines(
                            role="合同地点",
                            query=contract_query,
                            candidates=contract_candidates,
                            selection_basis=contract_selection,
                        ),
                        *_search_evidence_lines(
                            role="水印地点",
                            query=visible_location,
                            candidates=watermark_candidates,
                            selection_basis=watermark_selection,
                        ),
                    ]
                    mcp_resolution = _empty_resolution(
                        status="ambiguous",
                        provider="baidu_maps_mcp",
                        transport="streamable_http",
                        contract_query=contract_query,
                        watermark_query=visible_location,
                        city_hint=city,
                        mcp_attempted=True,
                        mcp_status=mcp_status,
                        basis=(
                            f"百度地图 MCP候选配对未通过：{candidate_pair_basis}；"
                            f"合同地点{contract_selection}；水印地点{watermark_selection}。"
                        ),
                        evidence=ambiguous_evidence,
                    )
                else:
                    if candidate_pair is not None:
                        relationship = str(candidate_pair["relationship"])
                        distance = candidate_pair["distance_meters"]
                        relationship_basis = str(
                            candidate_pair["relationship_basis"]
                        )
                    else:
                        relationship, distance, relationship_basis = (
                            classify_poi_relationship(contract_poi, watermark_poi)
                        )
                    mcp_status = relationship
                    distance_text = (
                        f"，直线距离{distance:g}米" if distance is not None else ""
                    )
                    if candidate_pair is not None:
                        contract_evidence_candidates = contract_candidates
                        watermark_evidence_candidates = watermark_candidates
                        contract_evidence_basis = (
                            f"{contract_selection}；{candidate_pair_basis}"
                        )
                        watermark_evidence_basis = (
                            f"{watermark_selection}；{candidate_pair_basis}"
                        )
                        resolution_basis = (
                            f"百度地图 MCP不要求两次搜索结果各自唯一，已从候选集合选定合同地点"
                            f"“{contract_poi['name']}”和水印地点“{watermark_poi['name']}”"
                            f"{distance_text}；{relationship_basis}。"
                        )
                    else:
                        contract_evidence_candidates = [contract_poi]
                        watermark_evidence_candidates = [watermark_poi]
                        contract_evidence_basis = contract_selection
                        watermark_evidence_basis = watermark_selection
                        resolution_basis = (
                            f"百度地图 MCP分别唯一定位合同地点“{contract_poi['name']}”和水印地点"
                            f"“{watermark_poi['name']}”{distance_text}；{relationship_basis}。"
                        )
                    mcp_resolution = {
                        "status": relationship,
                        "confidence": location_confidence(relationship),
                        "provider": "baidu_maps_mcp",
                        "transport": "streamable_http",
                        "mcp_attempted": True,
                        "mcp_status": relationship,
                        "contract_query": contract_query,
                        "watermark_query": visible_location,
                        "city_hint": city,
                        "coordinate_system": "BD09LL",
                        "contract_poi": contract_poi,
                        "watermark_poi": watermark_poi,
                        "distance_meters": distance,
                        "nearby_pass_meters": NEARBY_PASS_METERS,
                        "unrelated_min_meters": UNRELATED_MIN_METERS,
                        "evidence": [
                            *_search_evidence_lines(
                                role="合同地点",
                                query=contract_query,
                                candidates=contract_evidence_candidates,
                                selection_basis=contract_evidence_basis,
                            ),
                            *_search_evidence_lines(
                                role="水印地点",
                                query=visible_location,
                                candidates=watermark_evidence_candidates,
                                selection_basis=watermark_evidence_basis,
                            ),
                            relationship_basis,
                        ],
                        "basis": resolution_basis,
                    }
            except Exception as exc:
                mcp_status = "unavailable"
                mcp_resolution = _empty_resolution(
                    status="unavailable",
                    provider="baidu_maps_mcp",
                    transport="streamable_http",
                    contract_query=contract_query,
                    watermark_query=visible_location,
                    city_hint=city,
                    mcp_attempted=True,
                    mcp_status=mcp_status,
                    basis=f"百度地图 MCP本次不可用（{type(exc).__name__}）；未据此判定门店错误。",
                )

        registry_resolution = _registry_resolution(
            self._registry,
            contract_name=contract_name,
            visible_location=visible_location,
            city_hint=city,
            mcp_attempted=mcp_attempted,
            mcp_status=mcp_status,
        )

        if mcp_resolution and mcp_resolution["status"] in TERMINAL_RELATIONSHIPS:
            if (
                registry_resolution
                and registry_resolution["status"] in TERMINAL_RELATIONSHIPS
                and (
                    (mcp_resolution["status"] in PASS_RELATIONSHIPS)
                    != (registry_resolution["status"] in PASS_RELATIONSHIPS)
                )
            ):
                resolution = _empty_resolution(
                    status="ambiguous",
                    provider="baidu_maps_mcp+authoritative_registry",
                    transport="conflict_review",
                    contract_query=contract_query,
                    watermark_query=visible_location,
                    city_hint=city,
                    mcp_attempted=True,
                    mcp_status=mcp_status,
                    basis="百度地图 MCP与权威地点映射结论冲突，不能自动通过或判错，转人工复核。",
                    evidence=[
                        str(mcp_resolution["basis"]),
                        str(registry_resolution["basis"]),
                    ],
                )
            else:
                resolution = mcp_resolution
        elif registry_resolution is not None:
            resolution = dict(registry_resolution)
            if mcp_resolution is not None:
                resolution["evidence"] = [
                    *list(mcp_resolution.get("evidence") or []),
                    *list(registry_resolution.get("evidence") or []),
                ]
        elif mcp_resolution is not None:
            resolution = mcp_resolution
        else:
            resolution = _empty_resolution(
                status="unavailable",
                provider="none",
                transport="not_configured",
                contract_query=contract_query,
                watermark_query=visible_location,
                city_hint=city,
                mcp_attempted=False,
                mcp_status="not_configured",
                basis=(
                    "合同门店与照片水印名称不同，但百度地图 MCP未配置，且没有命中已复核权威地点映射；"
                    "当前地点置信度低，转待人工核验。"
                ),
            )
        self._cache[cache_key] = resolution
        return dict(resolution)


def load_location_registry(
    path: str | Path = LOCATION_REGISTRY_PATH,
) -> dict[str, Any]:
    registry_path = Path(path)
    registry = load_json(registry_path)
    validate_json(registry, LOCATION_REGISTRY_SCHEMA_PATH)
    return registry


def default_location_resolver() -> StoreLocationResolver:
    return StoreLocationResolver(
        mcp_lookup=baidu_maps_mcp_lookup_from_environment(),
        registry=load_location_registry(),
    )
