"""Prepare the actual customer configuration without inventing customer endpoints."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

APPLICATION_MEMBER = "config/application.env"
DRAFT_MEMBER = "config/.delivery-draft.json"
# This package targets the Canban customer server; local/Baota callbacks are separate.
CALLBACK_URL = "https://offline-verify.canban.cn:8828/api/v1/ai/analyze/callback"


def prepared_configuration(root: Path) -> dict[str, bytes]:
    lines = (root / "deploy/application.env.example").read_text(encoding="utf-8").splitlines()
    overrides = {"OFFLINE_AUDIT_OSS_ENABLED": "1", "OFFLINE_AUDIT_OSS_CALLBACK_URL": CALLBACK_URL}
    lines = [line.partition("=")[0] + "=" + overrides[line.partition("=")[0]]
             if line.partition("=")[0] in overrides else line for line in lines]
    lines = ["# 实际运行配置已预生成。客户 RDS 地址和服务器内网 IP 须用下方初始化命令填写。",
             "# 在完成初始化前不要直接启动服务；下面命令只需替换客户现场参数。",
             "# python deploy/manage.py configure --bind 服务器内网IP --rds-host RDS内网地址 --rds-ca CA证书路径",
             "# 此 HTTPS 回调仅用于参半（小阔）服务器；本机和宝塔使用独立的内网 HTTP 回调，不从本包覆盖。",
             *lines]
    text = "\n".join(lines).replace(
        "# 业务接口：先保持接收关闭，完成配置和验收后改为 1。",
        "# 业务接口已预配：完成 RDS、CA、密码与模型 Key 初始化后启动正式服务。"
    ) + "\n"
    application = text.encode("utf-8")
    marker = json.dumps({"format": 1, "application_sha256": hashlib.sha256(application).hexdigest()},
                        sort_keys=True).encode("utf-8") + b"\n"
    return {APPLICATION_MEMBER: application, DRAFT_MEMBER: marker}


def read_delivery_draft(root: Path) -> bytes | None:
    from audit_core.private_credentials import read_private_file
    directory = root / "config"
    if not directory.exists():
        return None
    expected = {Path(APPLICATION_MEMBER).name, Path(DRAFT_MEMBER).name}
    if directory.is_symlink() or not directory.is_dir() or {path.name for path in directory.iterdir()} != expected:
        raise RuntimeError("config/ already exists; existing settings and keys were preserved.")
    try:
        application = read_private_file(root / APPLICATION_MEMBER).encode("utf-8")
        marker = json.loads(read_private_file(root / DRAFT_MEMBER))
        if set(marker) != {"format", "application_sha256"} or marker["format"] != 1:
            raise ValueError()
        # read_private_file strips final newlines; compare the same representation.
        actual = (root / APPLICATION_MEMBER).read_bytes()
        if actual.rstrip(b"\r\n") != application or hashlib.sha256(actual).hexdigest() != marker["application_sha256"]:
            raise ValueError()
        return actual
    except (ValueError, TypeError, KeyError):
        raise RuntimeError("The delivered config was modified; existing values were preserved. Complete it manually using README 6.1.") from None
