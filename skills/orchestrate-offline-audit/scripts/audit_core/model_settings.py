"""Workbench model settings and immutable, per-run connection snapshots."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .common import AuditError

DEFAULT_QWEN_MODEL = "qwen3-vl-plus"
DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_PPIO_MODEL = "qwen/qwen3.5-397b-a17b"
DEFAULT_PPIO_BASE_URL = "https://api.ppio.com/openai/v1"
API_PROVIDERS = {
    "qwen": {"label": "阿里云百炼", "model": DEFAULT_QWEN_MODEL,
             "base_url": DEFAULT_QWEN_BASE_URL, "key_env": "DASHSCOPE_API_KEY"},
    "ppio": {"label": "PPIO（派欧云）", "model": DEFAULT_PPIO_MODEL,
             "base_url": DEFAULT_PPIO_BASE_URL, "key_env": "PPIO_API_KEY"},
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
    if provider == "ppio":
        try:
            valid = (parsed.scheme == "https" and parsed.hostname == "api.ppio.com"
                     and parsed.port in (None, 443) and not parsed.username and not parsed.password
                     and parsed.path in {"/openai", "/openai/v1"} and not parsed.query and not parsed.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in value))
        except ValueError:
            valid = False
        if not valid:
            raise ModelSettingsError("PPIO 请使用 https://api.ppio.com/openai/v1，不能填写百炼地址。")
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
        raise ModelSettingsError("请填写阿里云百炼的 HTTPS OpenAI 兼容地址，以 /compatible-mode/v1 结尾。")
    return value


@dataclass(frozen=True)
class ModelSelection:
    provider: str = "gpt"
    model: str = "gpt-6.1-sol"
    reasoning_effort: str = "high"
    base_url: str = ""
    api_key: str = field(default="", repr=False)

    def subprocess_value(self) -> str:
        return json.dumps({"provider": self.provider, "model": self.model,
                           "reasoning_effort": self.reasoning_effort,
                           "base_url": self.base_url, "api_key": self.api_key})


def selection_from_environment() -> ModelSelection | None:
    raw = os.environ.get(SNAPSHOT_ENV)
    if not raw:
        return None
    try:
        value = json.loads(raw)
        if value["provider"] == "gpt":
            return ModelSelection()
        if value["provider"] in API_PROVIDERS:
            return api_selection(value)
    except (ValueError, KeyError, TypeError):
        pass
    raise ModelSettingsError("本次任务的模型配置无效，请重新启动任务。")


def qwen_selection(value: dict, fallback_key: str = "") -> ModelSelection:
    return api_selection({**value, "provider": "qwen"}, fallback_key)


def api_selection(value: dict, fallback_key: str = "") -> ModelSelection:
    provider = value.get("provider", "qwen")
    if provider not in API_PROVIDERS:
        raise ModelSettingsError("请选择阿里云百炼或 PPIO 服务平台。")
    defaults = API_PROVIDERS[provider]
    model = str(value.get("model") or defaults["model"]).strip()
    pattern = r"qwen/qwen[a-zA-Z0-9._-]+" if provider == "ppio" else r"qwen[a-zA-Z0-9._-]{1,120}"
    if not re.fullmatch(pattern, model) or len(re.sub(r"[-_/]", "", model)) > 40:
        raise ModelSettingsError(f"请填写{defaults['label']}的千问视觉模型名称，例如 {defaults['model']}。"
                                 if provider == "ppio" else "请填写百炼千问模型名称，例如 qwen3-vl-plus。")
    key = str(value.get("api_key") or fallback_key).strip()
    if not key:
        raise ModelSettingsError(f"请先填写{defaults['label']} API Key。当前模型未改变。")
    if len(key) > 2048 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key):
        raise ModelSettingsError("API Key 格式不正确，请重新复制。")
    if provider == "qwen" and key.startswith("sk_"):
        raise ModelSettingsError("当前选择的是阿里云百炼。PPIO 的 sk_ 密钥请在服务平台中选择 PPIO 后填写。")
    if provider == "ppio" and not key.startswith("sk_"):
        raise ModelSettingsError("请填写 PPIO 平台创建的 sk_ 开头 API Key，不能使用百炼或 OpenAI 的 Key。")
    return ModelSelection(provider, model, "default" if provider == "ppio" else "none",
                          validate_base_url(str(value.get("base_url") or defaults["base_url"]), provider), key)


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
        self.path = Path(worktrees_root) / ".settings" / "model.json"
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"revision": 0, "provider": "gpt"}
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
            return os.environ.get(API_PROVIDERS[provider]["key_env"], "").strip()
        try:
            return _crypt_secret(base64.b64decode(encrypted, validate=True), decrypt=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise ModelSettingsError("模型密钥无法读取，请重新填写。") from None

    def public(self) -> dict:
        with self._lock:
            value = self._read()
            active = value["provider"]
            profiles = {}
            for provider, defaults in API_PROVIDERS.items():
                profile = self._profile(value, provider)
                profiles[provider] = {"label": defaults["label"],
                    "model": profile.get("model", defaults["model"]),
                    "base_url": profile.get("base_url", defaults["base_url"]),
                    "has_api_key": bool(profile.get("protected_key") or os.environ.get(defaults["key_env"]))}
            configured = value.get("configured_provider") or (active if active in API_PROVIDERS else
                         "qwen" if self._profile(value, "qwen") else "ppio")
            if configured not in API_PROVIDERS:
                raise ModelSettingsError("已保存的服务平台无效，请重新配置。")
            return {"provider": active, "revision": value["revision"],
                    "active_model": profiles[active]["model"] if active in API_PROVIDERS else "gpt-6.1-sol",
                    "configured_provider": configured, "profiles": profiles,
                    **{k: profiles[configured][k] for k in ("model", "base_url", "has_api_key")},
                    "effective_scope": "new_runs_only"}

    def snapshot(self) -> ModelSelection:
        with self._lock:
            value = self._read()
            provider = value["provider"]
            if provider == "gpt":
                return ModelSelection()
            profile = self._profile(value, provider)
            return api_selection({**profile, "provider": provider}, self._key(profile, provider))

    def candidate(self, payload: dict) -> ModelSelection:
        with self._lock:
            value = self._read()
            provider = payload.get("provider") or value.get("configured_provider") or "qwen"
            if provider not in API_PROVIDERS:
                raise ModelSettingsError("请选择阿里云百炼或 PPIO 服务平台。")
            profile = self._profile(value, provider)
            return api_selection({**profile, **payload, "provider": provider},
                                 self._key(profile, provider) if not payload.get("api_key") else "")

    def save(self, selection: ModelSelection, *, expected_revision: int) -> dict:
        with self._lock:
            old = self._read()
            if old["revision"] != expected_revision:
                raise ModelSettingsConflict("模型配置已在其他页面改变，请刷新后再操作。")
            if selection.provider not in {"gpt", *API_PROVIDERS}:
                raise ModelSettingsError("不支持的模型服务平台。")
            profiles = {provider: self._profile(old, provider) for provider in API_PROVIDERS}
            value = {"provider": selection.provider, "revision": old["revision"] + 1,
                     "profiles": profiles,
                     "configured_provider": old.get("configured_provider") or ("qwen" if profiles["qwen"] else "ppio")}
            if selection.provider in API_PROVIDERS:
                profiles[selection.provider] = {"model": selection.model, "base_url": selection.base_url,
                    "protected_key": base64.b64encode(_crypt_secret(selection.api_key.encode("utf-8"))).decode("ascii")}
                value["configured_provider"] = selection.provider
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix=".model-", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return self.public()
