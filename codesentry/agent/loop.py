"""The hand-rolled agent loop that drives the LLM through iterative tool calls
against the code graph, dispatching and validating each tool call, tracking token
usage, and producing a final structured AnswerWithCitations."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import networkx as nx
from pydantic import ValidationError
from rich.console import Console

from codesentry.agent.llm import (
    ChatResponse,
    LLMClient,
    StructuredOutputError,
    ToolCall,
)
from codesentry.agent.prompts import FINAL_ANSWER_INSTRUCTION, QA_SYSTEM_PROMPT
from codesentry.agent.schemas import AnswerWithCitations, Citation
from codesentry.agent.tools import TOOL_REGISTRY, ToolContext, openai_tool_schemas

_console = Console(stderr=True)

_CITATION_RE = re.compile(
    r"([\w./\\-]+\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts|go|java)):(\d+)"
)


class AgentIterationLimitError(RuntimeError):
    """Raised when the agent does not converge within max_iterations."""


def run_agent(
    query: str,
    graph: nx.MultiDiGraph,
    llm: LLMClient,
    repo_root: Path,
    max_iterations: int = 50,
    system_prompt: str = QA_SYSTEM_PROMPT,
) -> AnswerWithCitations:
    """Run the tool-using agent loop for ``query`` against ``graph`` and return a
    structured, cited answer. Raises AgentIterationLimitError if the model keeps
    calling tools past ``max_iterations`` without settling on an answer."""

    ctx = ToolContext(graph=graph, repo_root=repo_root)
    tool_schemas = openai_tool_schemas()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]
    totals = {"prompt": 0, "completion": 0, "total": 0}

    for _ in range(max_iterations):
        response = llm.chat_with_tools(messages, tool_schemas)
        _accumulate(totals, response)
        messages.append(_assistant_message(response))

        if not response.tool_calls:
            answer = _finalize(llm, messages)
            _log_usage(totals)
            return answer

        for call in response.tool_calls:
            result = _dispatch(ctx, call)
            _log_tool_call(call, result)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )

    _log_usage(totals)
    raise AgentIterationLimitError(
        f"Agent did not converge within {max_iterations} iterations"
    )


def _dispatch(ctx: ToolContext, call: ToolCall) -> str:
    """Validate and execute a single tool call, returning its string result. Errors
    (unknown tool, bad arguments, execution failure) are returned as text so the
    model can see them and recover rather than crashing the loop."""

    spec = TOOL_REGISTRY.get(call.name)
    if spec is None:
        return f"Error: unknown tool {call.name!r}"
    try:
        params = spec.input_model.model_validate(call.arguments)
    except ValidationError as exc:
        return f"Error: invalid arguments for {call.name}: {exc}"
    try:
        return spec.func(ctx, params)
    except Exception as exc:  # a tool bug must not kill the whole run
        return f"Error executing {call.name}: {exc}"


def _finalize(llm: LLMClient, messages: list[dict[str, Any]]) -> AnswerWithCitations:
    final_messages = messages + [
        {"role": "user", "content": FINAL_ANSWER_INSTRUCTION}
    ]
    try:
        return llm.parse_structured(final_messages, AnswerWithCitations)
    except StructuredOutputError:
        # The provider did not honor strict structured outputs (common with
        # non-OpenAI models). Fall back to a plain completion and recover any
        # file:line citations from the prose the model already produced.
        _console.print(
            "[dim]structured output unsupported by provider; using plain completion"
            "[/dim]"
        )
        text = llm.complete(final_messages)
        return AnswerWithCitations(answer=text, citations=_extract_citations(text))


def _extract_citations(text: str) -> list[Citation]:
    seen: set[tuple[str, int]] = set()
    citations: list[Citation] = []
    for path, line_str in _CITATION_RE.findall(text):
        line = int(line_str)
        key = (path, line)
        if key in seen:
            continue
        seen.add(key)
        citations.append(Citation(file=path, start_line=line, end_line=line))
    return citations


def _assistant_message(response: ChatResponse) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in response.tool_calls
        ]
    return message


def _accumulate(totals: dict[str, int], response: ChatResponse) -> None:
    totals["prompt"] += response.usage.prompt_tokens
    totals["completion"] += response.usage.completion_tokens
    totals["total"] += response.usage.total_tokens


def _log_tool_call(call: ToolCall, result: str) -> None:
    args = ", ".join(f"{k}={v!r}" for k, v in call.arguments.items())
    _console.print(
        f"[dim]→ {call.name}({args}) → {len(result)} chars[/dim]"
    )


def _log_usage(totals: dict[str, int]) -> None:
    _console.print(
        f"[dim]tokens: prompt={totals['prompt']} "
        f"completion={totals['completion']} total={totals['total']}[/dim]"
    )
