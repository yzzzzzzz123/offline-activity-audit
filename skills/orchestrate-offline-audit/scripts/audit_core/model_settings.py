"""Workbench model settings and immutable, per-run connection snapshots."""
from __future__ import annotations

import base64
import ctypes
from copy import deepcopy
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .common import AuditError
from .model_catalog import PPIO_EXTRA_IDS, ZIMAO_MODELS, ZIMAO_DEEPSEEK_MODELS, model_connections, public_model_id

DEFAULT_QWEN_MODEL = "qwen3.8-max"
DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_PPIO_MODEL = "qwen/qwen3.8-max"
DEFAULT_PPIO_BASE_URL = "https://api.ppio.com/openai/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_ZIMAO_MODEL = "deepseek-v4-1-flash"
DEFAULT_ZIMAO_BASE_URL = "https://aiclub.zimaocloud.com/model/openApi/v1"
DEFAULT_DOUBAO_MODEL = "doubao-seed-1-6-vision-250815"
DEFAULT_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_HOSTS = frozenset({"ark.cn-beijing.volces.com", "ark.cn-shanghai.volces.com"})
API_PROVIDERS = {
    "qwen": {"label": "阿里云百炼", "model": DEFAULT_QWEN_MODEL,
             "base_url": DEFAULT_QWEN_BASE_URL, "key_env": "DASHSCOPE_API_KEY"},
    "ppio": {"label": "PPIO（派欧云）", "model": DEFAULT_PPIO_MODEL,
             "base_url": DEFAULT_PPIO_BASE_URL, "key_env": "PPIO_API_KEY"},
    "deepseek": {"label": "DeepSeek", "model": DEFAULT_DEEPSEEK_MODEL,
                 "base_url": DEFAULT_DEEPSEEK_BASE_URL, "key_env": "DEEPSEEK_API_KEY"},
    "zimaocloud": {"label": "紫猫云（AI Club）", "model": DEFAULT_ZIMAO_MODEL,
                  "base_url": DEFAULT_ZIMAO_BASE_URL, "key_env": "ZIMAO_API_KEY"},
    "doubao": {"label": "豆包（火山方舟）", "model": DEFAULT_DOUBAO_MODEL,
               "base_url": DEFAULT_DOUBAO_BASE_URL, "key_env": "ARK_API_KEY"},
}
SNAPSHOT_ENV = "OFFLINE_AUDIT_MODEL_SNAPSHOT"


class ModelSettingsError(AuditError):
    pass


class ModelSettingsConflict(ModelSettingsError):
    pass


def validate_base_url(value: str, provider: str = "qwen") -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ModelSettingsError("接口地址格式不正确。") from None
    if provider == "doubao":
        try:
            valid = (parsed.scheme == "https" and parsed.hostname in DOUBAO_HOSTS
                     and parsed.port in (None, 443) and not parsed.username and not parsed.password
                     and parsed.path == "/api/v3" and not parsed.query and not parsed.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in value))
        except ValueError:
            valid = False
        if not valid:
            raise ModelSettingsError("模型接口地址不匹配，请使用该模型的默认连接配置。")
        return DEFAULT_DOUBAO_BASE_URL
    if provider == "zimaocloud":
        try:
            valid = (parsed.scheme == "https" and parsed.hostname == "aiclub.zimaocloud.com"
                     and parsed.port in (None, 443) and not parsed.username and not parsed.password
                     and parsed.path == "/model/openApi/v1" and not parsed.query and not parsed.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in value))
        except ValueError:
            valid = False
        if not valid:
            raise ModelSettingsError("模型接口地址不匹配，请使用该模型的默认连接配置。")
        return DEFAULT_ZIMAO_BASE_URL
    if provider == "deepseek":
        try:
            valid = (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
                     and parsed.port in (None, 443) and not parsed.username and not parsed.password
                     and parsed.path in {"", "/v1"} and not parsed.query and not parsed.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in value))
        except ValueError:
            valid = False
        if not valid:
            raise ModelSettingsError("模型接口地址不匹配，请使用该模型的默认连接配置。")
        return DEFAULT_DEEPSEEK_BASE_URL
    if provider == "ppio":
        try:
            valid = (parsed.scheme == "https" and parsed.hostname == "api.ppio.com"
                     and parsed.port in (None, 443) and not parsed.username and not parsed.password
                     and parsed.path in {"/openai", "/openai/v1"} and not parsed.query and not parsed.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in value))
        except ValueError:
            valid = False
        if not valid:
            raise ModelSettingsError("模型接口地址不匹配，请使用该模型的默认连接配置。")
        return DEFAULT_PPIO_BASE_URL
    hosts = {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
             "dashscope-us.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com"}
    workspace_host = re.fullmatch(
        r"[a-z0-9-]+\.(?:cn-beijing|ap-southeast-1|ap-northeast-1|us-east-1|eu-central-1|cn-hongkong)\.maas\.aliyuncs\.com",
        parsed.hostname or "",
    )
    try:
        valid = (parsed.scheme == "https" and (parsed.hostname in hosts or workspace_host)
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and parsed.path == "/compatible-mode/v1" and not parsed.query and not parsed.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise ModelSettingsError("模型接口地址不匹配，请使用该模型的默认连接配置。")
    return value


@dataclass(frozen=True)
class ModelSelection:
    provider: str = "ppio"
    model: str = DEFAULT_PPIO_MODEL
    reasoning_effort: str = "default"
    base_url: str = DEFAULT_PPIO_BASE_URL
    api_key: str = field(default="", repr=False)
    visual: ModelSelection | None = field(default=None, repr=False)

    def subprocess_value(self) -> str:
        return json.dumps({"provider": self.provider, "model": self.model,
                           "reasoning_effort": self.reasoning_effort,
                           "base_url": self.base_url, "api_key": self.api_key,
                           **({"visual": json.loads(self.visual.subprocess_value())} if self.visual else {})})


def selection_from_environment() -> ModelSelection | None:
    raw = os.environ.get(SNAPSHOT_ENV)
    if not raw:
        return None
    try:
        value = json.loads(raw)
        if value["provider"] in API_PROVIDERS:
            return api_selection(value)
    except (ValueError, KeyError, TypeError):
        pass
    raise ModelSettingsError("本次任务的模型配置无效，请重新启动任务。")


def qwen_selection(value: dict, fallback_key: str = "") -> ModelSelection:
    return api_selection({**value, "provider": "qwen"}, fallback_key)


def _reasoning_effort(provider: str, configured=None, model: str = '') -> str:
    default = "high" if provider == "deepseek" else (
        "default" if provider in {"ppio", "zimaocloud", "doubao"} else "none")
    qwen_control = provider in {'qwen', 'zimaocloud'} and model.startswith('qwen')
    if provider not in {"deepseek", "zimaocloud"} and not qwen_control:
        return default
    effort = default if configured is None else configured
    allowed = {"none", "low", "high", "max"}
    if provider == "zimaocloud":
        allowed.add("default")
    if qwen_control:
        allowed.update({'default', 'medium', 'xhigh', 'enabled'})
    if provider == 'zimaocloud' and model.lower().startswith('deepseek-v3'):
        allowed.add('enabled')
    if not isinstance(effort, str) or effort not in allowed:
        raise ModelSettingsError("模型推理强度无效，请选择该型号支持的官方档位。")
    return effort


def api_selection(value: dict, fallback_key: str = "") -> ModelSelection:
    provider = value.get("provider", "ppio")
    if provider not in API_PROVIDERS:
        raise ModelSettingsError("模型连接配置无效，请重新选择模型。")
    defaults = API_PROVIDERS[provider]
    model = str(value.get("model") or defaults["model"]).strip()
    pattern = r"qwen/qwen[a-zA-Z0-9._-]+" if provider == "ppio" else r"qwen[a-zA-Z0-9._-]{1,120}"
    doubao_pattern = r"(?:doubao-[a-z0-9][a-z0-9.-]{1,60}|ep-[a-z0-9][a-z0-9-]{2,40})"
    if not ((provider == "deepseek" and model in {DEFAULT_DEEPSEEK_MODEL, 'deepseek-v4-pro'})
            or (provider == "zimaocloud" and (model in ZIMAO_MODELS | ZIMAO_DEEPSEEK_MODELS
                or re.fullmatch(r'qwen[a-zA-Z0-9._-]{1,120}', model)))
            or (provider == "doubao" and re.fullmatch(doubao_pattern, model))
            or (provider == "ppio" and model in PPIO_EXTRA_IDS)
            or (provider in {"ppio", "qwen"} and re.fullmatch(pattern, model) and len(re.sub(r"[-_/]", "", model)) <= 40)):
        raise ModelSettingsError("模型名称无效，请从列表选择或填写支持图片识别和 JSON 输出的模型 ID。")
    key = str(value.get("api_key") or fallback_key).strip()
    if not key:
        raise ModelSettingsError("请先填写所选模型的 API Key。当前模型未改变。")
    if len(key) > 2048 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key):
        raise ModelSettingsError("API Key 格式不正确，请重新复制。")
    if provider in {"qwen", "deepseek"} and key.startswith("sk_"):
        raise ModelSettingsError("API Key 与所选模型不匹配，请更换密钥或选择其他模型。")
    if provider == "ppio" and not key.startswith("sk_"):
        raise ModelSettingsError("API Key 与所选模型不匹配，请更换密钥或选择其他模型。")
    if provider == "doubao" and key.startswith("sk_"):
        # Ark keys are account UUIDs; an sk_ secret belongs to another platform.
        raise ModelSettingsError("API Key 与所选模型不匹配，请更换密钥或选择其他模型。")
    effort = _reasoning_effort(provider, value.get("reasoning_effort"), model)
    visual = value.get("visual")
    if visual is not None:
        if not isinstance(visual, dict) or visual.get("visual") is not None:
            raise ModelSettingsError("图片模型快照无效，不允许嵌套路由。")
        visual = api_selection(visual)
        if not public_model_id(visual.model).startswith('qwen'):
            raise ModelSettingsError("照片Agent只能使用已配置的Qwen模型。")
    return ModelSelection(provider, model, effort,
                          validate_base_url(str(value.get("base_url") or defaults["base_url"]), provider), key, visual)


def _crypt_secret(raw: bytes, *, decrypt: bool = False) -> bytes:
    """Windows credentials remain bound to the current Windows user."""
    if os.name != "nt":
        return raw
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(raw)
    incoming = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ModelSettingsError("无法读取或保存当前 Windows 用户的模型密钥，请重新填写。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)


class ModelSettingsStore:
    def __init__(self, worktrees_root: Path):
        from .model_availability import ModelAvailabilityStore
        self.path = Path(worktrees_root) / ".settings" / "model.json"
        self.availability = ModelAvailabilityStore(worktrees_root)
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"revision": 0, "provider": "ppio"}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("provider") not in {"gpt", *API_PROVIDERS}
                    or type(value.get("revision")) is not int
                    or not isinstance(value.get("profiles", {}), dict)
                    or any(k not in API_PROVIDERS or not isinstance(v, dict)
                           for k, v in value.get("profiles", {}).items())):
                raise ValueError()
            return value
        except (OSError, ValueError):
            raise ModelSettingsError("模型配置无法读取，请在工作台重新配置。") from None

    @staticmethod
    def _profile(value: dict, provider: str) -> dict:
        profiles = value.get("profiles", {})
        if provider in profiles:
            return profiles[provider]
        # Read the original single-Bailian format without rewriting it.
        return {k: value[k] for k in ("model", "base_url", "protected_key") if k in value} if provider == "qwen" else {}

    def _key(self, profile: dict, provider: str) -> str:
        encrypted = profile.get("protected_key")
        if not encrypted:
            from .private_credentials import environment_secret
            return environment_secret(API_PROVIDERS[provider]["key_env"])
        try:
            return _crypt_secret(base64.b64decode(encrypted, validate=True), decrypt=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise ModelSettingsError("模型密钥无法读取，请重新填写。") from None

    def public(self, *, candidate: ModelSelection | None = None) -> dict:
        from .private_credentials import environment_secret
        with self._lock:
            value = self._read()
            active = value["provider"] if value["provider"] in API_PROVIDERS else "ppio"
            profiles = {}
            for provider, defaults in API_PROVIDERS.items():
                profile = self._profile(value, provider)
                profiles[provider] = {"label": defaults["label"],
                    "model": profile.get("model", defaults["model"]),
                    "base_url": profile.get("base_url", defaults["base_url"]),
                    "reasoning_effort": _reasoning_effort(provider, profile.get("reasoning_effort"), profile.get('model', defaults['model'])),
                    "has_api_key": bool(profile.get("protected_key") or environment_secret(defaults["key_env"]))}
                if provider == "zimaocloud":
                    # 兼容能力不是可用性证明；也让新页面识别尚未重载的旧后端。
                    profiles[provider]["compatible_model_ids"] = sorted(ZIMAO_MODELS)
            configured = value.get("configured_provider") or active
            if configured not in API_PROVIDERS:
                raise ModelSettingsError("已保存的模型连接无效，请重新配置。")
            if candidate is None:
                profile = self._profile(value, configured)
                try:
                    candidate = api_selection({**profile, "provider": configured}, self._key(profile, configured))
                except ModelSettingsError:
                    candidate = None
            return {"provider": active, "revision": value["revision"],
                    "active_model": profiles[active]["model"],
                    "configured_provider": configured, "profiles": profiles,
                    "model_choices": self.availability.choices(candidate),
                    "model_check_valid_seconds": 86400,
                    "active_model_id": public_model_id(profiles[active]["model"]),
                    **{k: profiles[configured][k] for k in ("model", "base_url", "has_api_key", "reasoning_effort")},
                    "visual_route": self._visual_public(value),
                    "effective_scope": "new_runs_only"}

    def _visual_public(self, value):
        profile = value.get('visual_profile') or {}
        if not profile:
            return None
        return {key: profile.get(key) for key in ('provider', 'model', 'base_url', 'reasoning_effort')} | {
            'has_api_key': bool(profile.get('protected_key')), 'scope': 'images_and_visual_agent'}

    def snapshot(self) -> ModelSelection:
        with self._lock:
            value = self._read()
            provider = value["provider"] if value["provider"] in API_PROVIDERS else "ppio"
            profile = self._profile(value, provider)
            selection = api_selection({**profile, "provider": provider}, self._key(profile, provider))
            # The meeting changes new DeepSeek runs only. Freeze both routes
            # and each provider's own credential before the runner starts.
            if "deepseek" not in selection.model.lower():
                return selection
            from dataclasses import replace
            if value.get('visual_profile'):
                visual_profile = value['visual_profile']
                visual_provider = visual_profile['provider']
                visual = api_selection(visual_profile, self._key(visual_profile, visual_provider))
                return replace(selection, visual=visual)
            # Older installations and explicitly configured single-model
            # runners keep their contract. Never guess a second account/key.
            return selection

    def candidate(self, payload: dict) -> ModelSelection:
        with self._lock:
            value = self._read()
            provider = payload.get("provider") or value.get("configured_provider") or "ppio"
            requested = str(payload.get("model") or "").strip()
            connections = model_connections(requested)
            if not payload.get("provider"):
                entered_key = str(payload.get("api_key") or "").strip()
                if "doubao" in connections and not entered_key.startswith("sk_"):
                    provider = "doubao"
                elif entered_key:
                    provider = ("zimaocloud" if set(connections) == {"zimaocloud"} or
                                (provider == "zimaocloud" and (not requested or provider in connections)) else
                                "ppio" if entered_key.startswith("sk_") else
                                "deepseek" if "deepseek" in connections and "qwen" not in connections else "qwen")
                elif connections and provider not in connections:
                    provider = next(iter(connections))
            if requested:
                if connections:
                    if provider not in connections:
                        raise ModelSettingsError("API Key 与所选模型不匹配，请更换密钥或选择其他模型。")
                    payload = {**payload, "model": connections[provider]}
                elif provider == "ppio" and requested.startswith("qwen") and "/" not in requested:
                    payload = {**payload, "model": "qwen/" + requested}
            if provider not in API_PROVIDERS:
                raise ModelSettingsError("模型连接配置无效，请重新选择模型。")
            profile = self._profile(value, provider)
            return api_selection({**profile, **payload, "provider": provider},
                                 self._key(profile, provider) if not payload.get("api_key") else "")

    def save(self, selection: ModelSelection, *, expected_revision: int) -> dict:
        with self._lock:
            old = self._read()
            if old["revision"] != expected_revision:
                raise ModelSettingsConflict("模型配置已在其他页面改变，请刷新后再操作。")
            if selection.provider not in API_PROVIDERS:
                raise ModelSettingsError("仅支持模型 API 配置，不再支持 GPT 登录模式。")
            selection = api_selection({"provider": selection.provider, "model": selection.model,
                                       "base_url": selection.base_url, "api_key": selection.api_key,
                                       "reasoning_effort": selection.reasoning_effort})
            profiles = {provider: self._profile(old, provider) for provider in API_PROVIDERS}
            value = {"provider": selection.provider, "revision": old["revision"] + 1,
                     "profiles": profiles,
                     "configured_provider": old.get("configured_provider") or ("qwen" if profiles["qwen"] else "ppio")}
            if old.get('visual_profile'):
                value['visual_profile'] = old['visual_profile']
            if selection.provider in API_PROVIDERS:
                profiles[selection.provider] = {"model": selection.model, "base_url": selection.base_url,
                    "reasoning_effort": selection.reasoning_effort,
                    "protected_key": base64.b64encode(_crypt_secret(selection.api_key.encode("utf-8"))).decode("ascii")}
                value["configured_provider"] = selection.provider
            self._write(value)
            return self.public()

    def save_visual(self, selection: ModelSelection, *, expected_revision: int) -> dict:
        """Store an explicitly supplied image credential without replacing DeepSeek's key."""
        with self._lock:
            value = self._read()
            if value['revision'] != expected_revision:
                raise ModelSettingsConflict('模型配置已改变，请重新读取后再保存。')
            selection = api_selection(json.loads(selection.subprocess_value()))
            if not public_model_id(selection.model).startswith('qwen') or selection.visual is not None:
                raise ModelSettingsError('照片Agent须配置Qwen模型，不能嵌套其他路由。')
            value['visual_profile'] = {'provider': selection.provider, 'model': selection.model,
                'base_url': selection.base_url, 'reasoning_effort': selection.reasoning_effort,
                'protected_key': base64.b64encode(_crypt_secret(selection.api_key.encode('utf-8'))).decode('ascii')}
            value['revision'] += 1
            self._write(value)
            return self._visual_public(value)

    def save_routes(self, main: ModelSelection, visual: ModelSelection, *, expected_revision: int) -> dict:
        """Atomically change both routes while preserving the existing encrypted keys."""
        with self._lock:
            old = self._read()
            if old['revision'] != expected_revision:
                raise ModelSettingsConflict('模型配置已在其他页面改变，请刷新后再操作。')
            current = self.snapshot()
            if (current.provider not in {'deepseek', 'zimaocloud'}
                    or not current.model.lower().startswith('deepseek') or not current.visual):
                raise ModelSettingsError('请先配置DeepSeek及独立的Qwen连接。')
            for selected, saved in ((main, current), (visual, current.visual)):
                if (selected.provider, selected.base_url, selected.api_key) != (saved.provider, saved.base_url, saved.api_key):
                    raise ModelSettingsError('模型调整只能使用各自已配置的连接和密钥。')
                api_selection(json.loads(selected.subprocess_value()))
            if not public_model_id(visual.model).startswith('qwen'):
                raise ModelSettingsError('图片模型只开放Qwen。')
            value = deepcopy(old)
            main_profile = value.setdefault('profiles', {}).setdefault(current.provider, {})
            main_profile.update(model=main.model, reasoning_effort=main.reasoning_effort)
            value['visual_profile'].update(model=visual.model, reasoning_effort=visual.reasoning_effort)
            if value != old:
                value['revision'] += 1
                self._write(value)
            return self.public()

    def _write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix='.model-', dir=self.path.parent)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != 'nt':
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
