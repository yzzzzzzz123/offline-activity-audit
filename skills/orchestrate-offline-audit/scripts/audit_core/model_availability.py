"""Credential-bound, expiring evidence that a model accepts our image/JSON API.

The catalogue is only a source of candidates. Public reads never call providers;
an explicit refresh uses synthetic images and never reads business materials.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

import httpx

from .model_catalog import DOUBAO_MODELS, model_choices, model_connections

PROBE_VERSION = 1
VALID_SECONDS = 24 * 60 * 60


def _identity(selection) -> str:
    # This high-entropy credential digest remains private and never reaches the UI.
    raw = json.dumps([PROBE_VERSION, selection.provider, selection.base_url, selection.api_key])
    return hashlib.sha256(raw.encode()).hexdigest()


class ModelAvailabilityStore:
    def __init__(self, root: Path):
        self.path = Path(root) / '.settings' / 'model-availability.json'
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if value.get('version') == PROBE_VERSION and isinstance(value.get('entries'), dict):
                return value['entries']
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def _write(self, entries: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix='.availability-', dir=self.path.parent)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump({'version': PROBE_VERSION, 'entries': entries}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != 'nt':
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def record(self, selection, *, passed: bool) -> None:
        with self._lock:
            entries = self._read()
            models = entries.setdefault(_identity(selection), {})
            if passed:
                models[selection.model] = time.time()
            else:
                models.pop(selection.model, None)
            self._write(entries)

    def clear(self, selection) -> None:
        with self._lock:
            entries = self._read()
            entries.pop(_identity(selection), None)
            self._write(entries)

    def choices(self, selection) -> list[dict]:
        if selection is None:
            return []
        with self._lock:
            models = self._read().get(_identity(selection), {})
            if not isinstance(models, dict):
                return []
            now = time.time()
            result = []
            for item in model_choices():
                actual = model_connections(item['id']).get(selection.provider)
                checked = models.get(actual)
                if type(checked) in (int, float) and 0 <= now - checked < VALID_SECONDS:
                    result.append({**item, 'verified_at': checked})
            # A verified endpoint id (for example an Ark inference endpoint)
            # is usable even when the catalogue lists only family names.
            listed = {model_connections(item['id']).get(selection.provider) for item in model_choices()}
            own = selection.model
            checked = models.get(own)
            if (own and own not in listed and type(checked) in (int, float)
                    and 0 <= now - checked < VALID_SECONDS):
                result.append({'id': own, 'label': own, 'group': '本次接口已测试型号',
                               'recommended': False, 'verified_at': checked})
            return result

    def require(self, selection) -> None:
        if not any(row['id'] == selection.model
                   or model_connections(row['id']).get(selection.provider) == selection.model
                   for row in self.choices(selection)):
            from .model_settings import ModelSettingsError
            raise ModelSettingsError('该模型尚未通过当前 API Key 的可用性检查或检查已过期，请先检查可用模型。')


def fetch_candidate_models(selection, *, transport=None) -> list:
    if selection.provider == "doubao":
        # Ark publishes no model directory; the catalogue is the candidate list
        # and every id must still pass its own credential-bound probe.
        return [replace(selection, model=name) for name, _, _ in DOUBAO_MODELS]
    from .qwen_model import QwenRequestError, _http_error
    try:
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=False, transport=transport) as client:
            with client.stream('GET', selection.base_url + '/models',
                               headers={'Authorization': 'Bearer ' + selection.api_key}) as response:
                if response.status_code != 200:
                    raise _http_error(response.status_code, b'', selection.provider)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise ValueError()
        data = json.loads(body)
        if not isinstance(data.get('data'), list):
            raise ValueError()
        available = {m['id']: m for m in data['data'] if isinstance(m, dict) and isinstance(m.get('id'), str)}
    except httpx.TimeoutException:
        raise QwenRequestError('模型目录查询超时，请稍后重试。', 'timeout') from None
    except httpx.HTTPError:
        raise QwenRequestError('模型目录暂时无法读取，请稍后重试。') from None
    except (ValueError, TypeError, AttributeError):
        raise QwenRequestError('模型目录返回格式无效，请稍后重试。', 'invalid_output') from None
    result = []
    for item in model_choices():
        actual = model_connections(item['id']).get(selection.provider)
        metadata = available.get(actual)
        if metadata is None:
            continue
        modalities = metadata.get('input_modalities')
        if modalities is not None and (not isinstance(modalities, list) or 'image' not in modalities):
            continue
        if metadata.get('status', 1) != 1:
            continue
        result.append(replace(selection, model=actual))
    return result


def refresh_available_models(selection, store: ModelAvailabilityStore) -> dict:
    from .qwen_model import QwenRequestError, probe_qwen
    # Invalidate previous evidence even when credentials or the directory fail.
    store.clear(selection)
    candidates = fetch_candidate_models(selection)
    passed = 0
    failures: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix='model-availability') as pool:
        futures = {pool.submit(probe_qwen, item): item for item in candidates}
        for future in as_completed(futures):
            item = futures[future]
            try:
                future.result()
            except QwenRequestError as exc:
                failures[exc.error_code] = failures.get(exc.error_code, 0) + 1
            else:
                store.record(item, passed=True)
                passed += 1
    return {'checked': len(candidates), 'available': passed, 'unavailable': len(candidates) - passed,
            'failure_counts': failures, 'valid_seconds': VALID_SECONDS}
