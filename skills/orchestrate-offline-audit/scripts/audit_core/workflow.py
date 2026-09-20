"""Local LCEL execution with scoped state, isolated callbacks and no cloud trace."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import contextmanager
from typing import TypeVar

from langchain_core.callbacks import CallbackManager
from langchain_core.globals import get_debug, get_verbose
from langchain_core.runnables import RunnableLambda, RunnableSequence
from langsmith import tracing_context
from .common import AuditError


State = TypeVar("State")


def build_audit_chain(stages: Sequence[tuple[str, Callable[[State], State]]]) -> RunnableSequence:
    return RunnableSequence(
        *(RunnableLambda(action, name=name) for name, action in stages),
        name="offline_activity_audit",
    )


def invoke_audit_chain(chain: RunnableSequence, state: State) -> State:
    # Override ambient LangSmith tracing locally, without changing user settings.
    # A fresh callback manager also avoids inheriting an enclosing chain's handlers.
    return invoke_local(chain, state)


def invoke_local(runnable, value, *, max_concurrency: int = 1):
    with local_config(max_concurrency=max_concurrency) as config:
        return runnable.invoke(value, config=config)


def batch_local(runnable, values, *, max_concurrency: int):
    with local_config(max_concurrency=max_concurrency) as config:
        return runnable.batch(values, config=config)


@contextmanager
def local_config(*, max_concurrency: int = 1):
    if get_debug() or get_verbose():
        raise AuditError("核销不允许 LangChain 全局调试输出业务材料，请关闭框架 debug/verbose 后运行")
    with tracing_context(enabled=False, parent=False):
        yield {
            "callbacks": CallbackManager(handlers=[], inheritable_handlers=[]),
            "max_concurrency": max_concurrency,
        }
