"""Mocked tests for the agent loop: happy path, multi-tool-call path, iteration-
limit error, and malformed tool arguments. The LLM is a scripted fake that never
touches the network; the graph and tools are real."""

from pathlib import Path

import pytest

from codesentry.agent.llm import ChatResponse, ToolCall
from codesentry.agent.loop import AgentIterationLimitError, run_agent
from codesentry.agent.schemas import AnswerWithCitations, Citation
from codesentry.graph.builder import build_graph

PY_FIXTURE = Path(__file__).parent / "fixtures" / "sample_python"


class FakeLLM:
    """Returns scripted ChatResponses in order (falling back to ``default`` when the
    script is exhausted) and a fixed structured answer. Records the messages it saw."""

    def __init__(
        self,
        responses: list[ChatResponse],
        final: AnswerWithCitations,
        default: ChatResponse | None = None,
    ) -> None:
        self._responses = list(responses)
        self._final = final
        self._default = default
        self.chat_calls: list[list[dict]] = []
        self.parsed_messages: list[dict] | None = None

    def chat_with_tools(self, messages, tools):  # type: ignore[no-untyped-def]
        self.chat_calls.append([dict(m) for m in messages])
        if self._responses:
            return self._responses.pop(0)
        assert self._default is not None
        return self._default

    def parse_structured(self, messages, schema):  # type: ignore[no-untyped-def]
        self.parsed_messages = [dict(m) for m in messages]
        return self._final


def _graph():
    return build_graph(PY_FIXTURE)


def _stop() -> ChatResponse:
    return ChatResponse(content="done", tool_calls=[], finish_reason="stop")


def _tool_call_response(name: str, arguments: dict) -> ChatResponse:
    return ChatResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def test_happy_path_no_tools() -> None:
    final = AnswerWithCitations(
        answer="It is a class.",
        citations=[Citation(file="models.py", start_line=4, end_line=13)],
    )
    llm = FakeLLM([_stop()], final)
    result = run_agent("What is User?", _graph(), llm, repo_root=PY_FIXTURE)  # type: ignore[arg-type]
    assert result.answer == "It is a class."
    assert result.citations[0].file == "models.py"
    assert len(llm.chat_calls) == 1
    assert llm.parsed_messages is not None


def test_multi_tool_call_path_executes_real_tool() -> None:
    final = AnswerWithCitations(answer="found", citations=[])
    llm = FakeLLM(
        [_tool_call_response("find_symbol", {"name": "User"}), _stop()], final
    )
    result = run_agent("Find User", _graph(), llm, repo_root=PY_FIXTURE)  # type: ignore[arg-type]
    assert result.answer == "found"
    assert len(llm.chat_calls) == 2
    # The second chat call must include the tool result for the find_symbol call.
    tool_messages = [m for m in llm.chat_calls[1] if m.get("role") == "tool"]
    assert tool_messages
    assert "models.py::User" in tool_messages[0]["content"]


def test_iteration_limit_raises() -> None:
    final = AnswerWithCitations(answer="never", citations=[])
    forever = _tool_call_response("list_languages", {})
    llm = FakeLLM([], final, default=forever)
    with pytest.raises(AgentIterationLimitError):
        run_agent("loop", _graph(), llm, repo_root=PY_FIXTURE, max_iterations=3)  # type: ignore[arg-type]
    assert len(llm.chat_calls) == 3


def test_malformed_tool_arguments_are_fed_back() -> None:
    final = AnswerWithCitations(answer="recovered", citations=[])
    # find_symbol requires 'name'; omit it to trigger a validation error.
    llm = FakeLLM([_tool_call_response("find_symbol", {}), _stop()], final)
    result = run_agent("bad args", _graph(), llm, repo_root=PY_FIXTURE)  # type: ignore[arg-type]
    assert result.answer == "recovered"
    tool_messages = [m for m in llm.chat_calls[1] if m.get("role") == "tool"]
    assert tool_messages[0]["content"].startswith("Error: invalid arguments")
