"""Candidate model names and their private connection mappings.

Candidates are never selectable until a credential-bound image/JSON probe passes.
PPIO metadata checked 2026-10-29; text-only models are excluded from its mapping.
Sources: https://help.aliyun.com/zh/model-studio/vision-model/
https://help.aliyun.com/en/model-studio/qwen-structured-output
https://ppio.com/model/moonshotai/kimi-k3
https://ppio.com/model/minimax/minimax-m3
https://ppio.com/model/deepseek/deepseek-v4-flash-vision-exp
"""
from __future__ import annotations


QWEN_GROUPS = {
    "千问 3.8": (
        "qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash", "qwen3.8-27b",
        "qwen3.8-2.4t-a95b", "qwen3.8-omni-flash",
    ),
    "千问 3.7": (
        "qwen3.7-max", "qwen3.7-max-2026-06-08", "qwen3.7-plus", "qwen3.7-plus-2026-05-26",
        "qwen3.7-flash", "qwen3.7-flash-2026-07-15",
    ),
    "千问 3.6": (
        "qwen3.6-plus", "qwen3.6-plus-2026-04-02", "qwen3.6-flash", "qwen3.6-flash-2026-04-16",
        "qwen3.6-27b", "qwen3.6-35b-a3b",
    ),
    "千问 3.5": (
        "qwen3.5-plus", "qwen3.5-plus-2026-04-20", "qwen3.5-plus-2026-02-15",
        "qwen3.5-flash", "qwen3.5-flash-2026-02-23", "qwen3.5-397b-a17b",
        "qwen3.5-122b-a10b", "qwen3.5-35b-a3b", "qwen3.5-27b",
        "qwen3.5-omni-plus", "qwen3.5-omni-plus-2026-03-15",
    ),
    "千问 VL": (
        "qwen3-vl-plus", "qwen3-vl-flash", "qwen3-vl-flash-2026-01-22",
        "qwen3-vl-flash-2025-10-15", "qwen3-vl-32b-instruct", "qwen3-vl-30b-a3b-instruct",
        "qwen3-vl-8b-instruct", "qwen3-vl-235b-a22b-instruct", "qwen-vl-max", "qwen-vl-plus",
    ),
}
PPIO_QWEN_MODELS = frozenset({
    "qwen3.8-max", "qwen3.8-flash", "qwen3.8-27b",
    "qwen3.7-plus", "qwen3.6-27b", "qwen3.6-35b-a3b",
    "qwen3.5-397b-a17b", "qwen3.5-122b-a10b", "qwen3.5-35b-a3b", "qwen3.5-27b",
})
# 2026-11-17：用户账户 /models 只读目录已确认；目录存在不等于图片/JSON实测通过。
# 紫猫云仅开放本次确认的型号，不按名称前缀推测其他型号或复用其他平台凭据。
ZIMAO_DOUBAO_MODEL = "doubao-seed-2-1-pro-260915"
ZIMAO_QWEN_MODEL = "qwen3.8-max"
ZIMAO_MODELS = frozenset({"deepseek-v4-1-flash", ZIMAO_DOUBAO_MODEL, ZIMAO_QWEN_MODEL})
# Account /models checked read-only on 2026-11-19. These text routes are
# separate from the legacy image/JSON candidate list above.
ZIMAO_DEEPSEEK_MODELS = frozenset({
    'deepseek-v4-pro-0813', 'deepseek-v4-1-flash', 'DeepSeek-V3.2',
    'deepseek-v4-flash-0731', 'deepseek-v4-pro',
})
# Volcengine Ark exposes no model directory endpoint, so these ids are only
# candidates: each one still has to pass its own image/JSON probe, and an id
# that is absent from the account simply fails that probe.
DOUBAO_MODELS = (
    ("doubao-seed-1-6-vision-250815", "豆包 Seed 1.6 Vision", "豆包（火山方舟）"),
    ("doubao-seed-1-6-250615", "豆包 Seed 1.6", "豆包（火山方舟）"),
    ("doubao-seed-1-6-flash-250715", "豆包 Seed 1.6 Flash", "豆包（火山方舟）"),
    ("doubao-seed-1-6-thinking-250715", "豆包 Seed 1.6 Thinking", "豆包（火山方舟）"),
    ("doubao-1-5-vision-pro-32k-250115", "豆包 1.5 Vision Pro 32K", "豆包（火山方舟）"),
)
EXTRA_MODELS = (
    ("kimi-k3.1-pro", "Kimi K3.1 Pro", "Kimi", "moonshotai/kimi-k3.1-pro"),
    ("kimi-k3.1-pro-highspeed", "Kimi K3.1 Pro 高速版", "Kimi", "moonshotai/kimi-k3.1-pro-highspeed"),
    ("kimi-k3", "Kimi K3", "Kimi", "moonshotai/kimi-k3"),
    ("kimi-k2.7-code", "Kimi K2.7 Code", "Kimi", "moonshotai/kimi-k2.7-code"),
    ("kimi-k2.6", "Kimi K2.6", "Kimi", "moonshotai/kimi-k2.6"),
    ("kimi-k2.5", "Kimi K2.5", "Kimi", "moonshotai/kimi-k2.5"),
    ("glm-4.6v", "GLM 4.6V", "GLM", "zai-org/glm-4.6v"),
    ("glm-4.5v", "GLM 4.5V", "GLM", "zai-org/glm-4.5v"),
    ("glm-5.3-flash", "GLM 5.3 Flash", "GLM", "zai-org/glm-5.3-flash"),
    ("glm-4.1v-9b-thinking", "GLM 4.1V 9B Thinking", "GLM", "thudm/glm-4.1v-9b-thinking"),
    ("minimax-m3", "MiniMax M3", "MiniMax", "minimax/minimax-m3"),
    ("minimax-m3.1-flash", "MiniMax M3.1 Flash", "MiniMax", "minimax/minimax-m3.1-flash"),
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash", "DeepSeek", "deepseek/deepseek-v4.1-flash"),
    ("deepseek-v4-flash-vision-exp", "DeepSeek V4 Flash Vision（实验版）", "DeepSeek",
     "deepseek/deepseek-v4-flash-vision-exp"),
    ("mimo-v2.6-flash", "MiMo V2.6 Flash", "MiMo", "xiaomimimo/mimo-v2.6-flash"),
    ("mimo-v2.6-pro", "MiMo V2.6 Pro", "MiMo", "xiaomimimo/mimo-v2.6-pro"),
    ("step-5-preview", "Step 5 Preview", "Step", "stepfun/step-5-preview"),
)


def model_connections(model_id: str) -> dict[str, str]:
    if model_id == 'deepseek-v4-pro':
        return {'deepseek': model_id}
    if model_id == "deepseek-v4-1-flash":
        return {"zimaocloud": "deepseek-v4-1-flash"}
    if model_id in {"deepseek-flash", "deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash"}:
        return {"deepseek": "deepseek-flash", "ppio": "deepseek/deepseek-v4.1-flash",
                "zimaocloud": "deepseek-v4-1-flash"}
    if model_id == ZIMAO_DOUBAO_MODEL:
        return {"zimaocloud": ZIMAO_DOUBAO_MODEL}
    for name, _, _ in DOUBAO_MODELS:
        if model_id == name:
            return {"doubao": name}
    normalized = str(model_id).removeprefix("qwen/")
    for names in QWEN_GROUPS.values():
        if normalized in names:
            connections = {"qwen": normalized}
            if normalized in PPIO_QWEN_MODELS:
                connections["ppio"] = "qwen/" + normalized
            if normalized == ZIMAO_QWEN_MODEL:
                connections["zimaocloud"] = normalized
            return connections
    for name, _, _, actual in EXTRA_MODELS:
        if model_id in {name, actual}:
            return {"ppio": actual}
    return {}


def public_model_id(model_id: str) -> str:
    if model_id in {"deepseek-flash", "deepseek-v4-1-flash"}:
        return "deepseek-v4.1-flash"
    for name, _, _, actual in EXTRA_MODELS:
        if model_id == actual:
            return name
    return str(model_id).removeprefix("qwen/")


def model_choices() -> list[dict[str, object]]:
    return [
        {"id": name, "label": name, "group": group, "recommended": name == "qwen3.8-max"}
        for group, names in QWEN_GROUPS.items() for name in names
    ] + [{"id": name, "label": label, "group": group, "recommended": False}
         for name, label, group, _ in EXTRA_MODELS
         ] + [{"id": name, "label": label, "group": group, "recommended": False}
              for name, label, group in DOUBAO_MODELS] + [
                  {"id": ZIMAO_DOUBAO_MODEL, "label": "豆包 Seed 2.1 Pro", "group": "豆包", "recommended": False}]


PPIO_EXTRA_IDS = frozenset(actual for _, _, _, actual in EXTRA_MODELS)
