"""Executable LCEL data flow for every source-bounded vision attempt."""
from __future__ import annotations

from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.prompts.base import BasePromptTemplate
from langchain_core.runnables import RunnableLambda, RunnableSequence

from .evidence_parser import EvidenceOutputParser
from .langchain_model import CodexChatModel


def as_prompt(value: str | BasePromptTemplate) -> BasePromptTemplate:
    if isinstance(value, BasePromptTemplate):
        return value
    # Compatibility for callers supplying literal instructions. Braces in
    # business text are values, never interpreted as additional placeholders.
    return PromptTemplate.from_template("{instructions}").partial(instructions=value)


def render_prompt(value: str | BasePromptTemplate) -> str:
    return as_prompt(value).format_prompt().to_string()


def build_extraction_chain(
    prompt: str | BasePromptTemplate, model: CodexChatModel,
    parser: EvidenceOutputParser, *, images: list[Path], retry_hint: str = "",
) -> RunnableSequence:
    blocks = [{"type": "text", "text": "{instructions}{retry_hint}"}]
    image_values = {}
    for index, path in enumerate(images):
        key = f"image_{index}"
        blocks.append({"type": "image_url", "image_url": {"url": "{" + key + "}"}})
        image_values[key] = path.absolute().as_uri()
    messages = ChatPromptTemplate.from_messages([("human", blocks)])
    return (
        as_prompt(prompt)
        | RunnableLambda(lambda value: {
            "instructions": value.to_string(), "retry_hint": retry_hint, **image_values,
        }, name="attach_current_materials")
        | messages
        | model
        | parser
    )
