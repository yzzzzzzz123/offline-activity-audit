"""LangChain chat model for the existing isolated Codex CLI transport."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from .common import AuditError


class CodexProcessError(AuditError):
    def __init__(self, returncode: int, error_code: str):
        self.error_code = error_code
        super().__init__(f"codex 退出码 {returncode}（{error_code}）")


class CodexChatModel(BaseChatModel):
    """One model attempt. The extraction chain owns validation and retry policy.

    Local image URLs in HumanMessage are checked against this call's immutable
    attachment list, then passed to the CLI without any HTTP fetch. The CLI
    retains its read-only sandbox, timeout and process-tree containment.
    """

    model_name: str
    reasoning_effort: str
    command: list[str] = Field(exclude=True, repr=False)
    model_root: Path = Field(exclude=True, repr=False)
    raw_output: Path = Field(exclude=True, repr=False)
    images: tuple[Path, ...] = Field(exclude=True, repr=False)
    environment: dict[str, str] | None = Field(default=None, exclude=True, repr=False)
    label: str
    timeout_seconds: int
    transport_timeout: int
    attempt: int = 1
    last_metrics: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)
    cache: bool = False

    @property
    def _llm_type(self) -> str:
        return "offline-audit-codex"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name, "reasoning_effort": self.reasoning_effort}

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        from . import codex_runner as api

        self.last_metrics = {}
        if stop or len(messages) != 1 or not isinstance(messages[0], HumanMessage):
            raise api.CodexRequestConfigurationError("核销模型只接收本批单条材料消息，不接受对话历史或 stop 参数")
        content = messages[0].content
        if not isinstance(content, list):
            raise api.CodexRequestConfigurationError("核销模型必须接收含文字与明确附件清单的材料消息")
        texts, urls = [], []
        for block in content:
            if not isinstance(block, dict):
                raise api.CodexRequestConfigurationError("核销材料消息含未知内容类型")
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "image_url" and isinstance(block.get("image_url"), dict):
                urls.append(block["image_url"].get("url"))
            else:
                raise api.CodexRequestConfigurationError("核销材料消息含未支持的附件类型")
        if len(texts) != 1 or urls != [p.absolute().as_uri() for p in self.images]:
            raise api.CodexRequestConfigurationError("核销消息的文字或附件与本批隔离来源不一致")

        self.raw_output.unlink(missing_ok=True)
        progress_name = "call-progress-" + hashlib.sha256(str(self.model_root).encode("utf-8")).hexdigest()[:16]
        try:
            completed = api.run_model_process(
                self.command, label=self.label, transport_timeout=self.transport_timeout,
                progress_callback=lambda value: api.save_model_observations(progress_name, {"attempt": self.attempt, **value}),
                cwd=self.model_root, input=texts[0], text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=self.timeout_seconds, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=self.environment,
            )
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            self.last_metrics.update(api.summarize_codex_events(partial))
            self.last_metrics.update(getattr(exc, "progress", {}))
            raise

        self.last_metrics.update(api.summarize_codex_events(completed.stdout))
        if api.required_read_was_blocked(completed.stderr):
            raise api.CodexRequestConfigurationError(
                "原图或技能文件的必需读取被沙箱拒绝（configuration），不能作为业务无法确认或模型评分"
            )
        if completed.returncode != 0:
            code = api._codex_error_code("\n".join(v.strip() for v in (completed.stderr, completed.stdout) if v.strip()))
            if code in {"context_limit", "output_limit"}:
                raise api.CodexContextCapacityError(f"当前块超出模型容量（{code}），必须切分后重新提取")
            if code in {"auth", "configuration"}:
                raise api.CodexRequestConfigurationError(f"codex 请求配置或认证错误（{code}），重复执行无法修复")
            raise CodexProcessError(completed.returncode, code)
        if not self.raw_output.is_file():
            raise AuditError("codex 未生成结构化证据")
        message = AIMessage(content=self.raw_output.read_text(encoding="utf-8"), response_metadata=dict(self.last_metrics))
        return ChatResult(generations=[ChatGeneration(message=message)])
