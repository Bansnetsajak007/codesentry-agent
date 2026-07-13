"""The sole home of the OpenAI SDK dependency: an LLMClient abstraction exposing
chat_with_tools and parse_structured so that swapping providers later is a
one-file change and nothing else in the codebase imports openai directly."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from openai import OpenAI
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class TokenUsage:
    """Token accounting for a single LLM response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    """A single tool call requested by the model, with arguments parsed to a dict."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    """A normalized chat completion: text content, requested tool calls, the finish
    reason, and token usage."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)


class LLMClient:
    """Thin, provider-specific wrapper over the OpenAI Chat Completions API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._max_tokens = max_tokens

    def chat_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        """Call the model with function-calling tools and tool_choice='auto', and
        return a normalized ChatResponse."""

        response = self._client.chat.completions.create(
            model=self._model,
            messages=cast(Any, messages),
            tools=cast(Any, tools),
            tool_choice="auto",
            max_tokens=self._max_tokens,
        )
        choice = response.choices[0]
        message = choice.message
        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            if call.type != "function":
                continue  # custom tool calls are not used by CodeSentry
            tool_calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=_parse_arguments(call.function.arguments),
                )
            )
        return ChatResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=_usage(response.usage),
        )

    def parse_structured(self, messages: list[dict[str, Any]], schema: type[T]) -> T:
        """Call the model and parse its reply into ``schema`` via structured outputs."""

        response = self._client.beta.chat.completions.parse(
            model=self._model,
            messages=cast(Any, messages),
            response_format=schema,
            max_tokens=self._max_tokens,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError("Model did not return a parseable structured response")
        return parsed


def _parse_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _usage(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        total_tokens=getattr(usage, "total_tokens", 0) or 0,
    )
