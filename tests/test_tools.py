"""Non-LLM tests for the agent tools, tool registry, OpenAI schema generation, and
the LLMClient normalization (mocked, never calling the real API)."""

from pathlib import Path

import pytest

from pydantic import ValidationError

from codesentry.agent import tools
from codesentry.agent.llm import LLMClient, StructuredOutputError
from codesentry.agent.schemas import (
    AnswerWithCitations,
    FindSymbolInput,
    GetCalleesInput,
    GetCallersInput,
    GetDefinitionInput,
    GetNeighborsInput,
    GrepInput,
    ListFilesInput,
    ListLanguagesInput,
    ReadFileInput,
)
from codesentry.agent.tools import TOOL_REGISTRY, ToolContext, openai_tool_schemas
from codesentry.graph.builder import build_graph

PY_FIXTURE = Path(__file__).parent / "fixtures" / "sample_python"


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(graph=build_graph(PY_FIXTURE), repo_root=PY_FIXTURE)


def test_list_files(ctx: ToolContext) -> None:
    out = tools.list_files(ctx, ListFilesInput())
    assert "models.py (python)" in out
    filtered = tools.list_files(ctx, ListFilesInput(pattern="*repository*"))
    assert "repository.py" in filtered and "models.py" not in filtered
    assert tools.list_files(ctx, ListFilesInput(language="go")) == "No files matched."


def test_read_file_with_line_numbers(ctx: ToolContext) -> None:
    out = tools.read_file(ctx, ReadFileInput(path="models.py", start_line=1, end_line=5))
    assert "1 | " in out
    assert "class User" in out
    assert tools.read_file(ctx, ReadFileInput(path="nope.py")).startswith("Error")


def test_find_symbol_simple_and_qualified(ctx: ToolContext) -> None:
    out = tools.find_symbol(ctx, FindSymbolInput(name="count"))
    assert "repository.py::UserRepository.count" in out
    scoped = tools.find_symbol(ctx, FindSymbolInput(name="User", language="go"))
    assert scoped.startswith("No symbol")


def test_get_definition(ctx: ToolContext) -> None:
    out = tools.get_definition(ctx, GetDefinitionInput(node_id="models.py::User.display_name"))
    assert "def display_name(self)" in out
    assert tools.get_definition(ctx, GetDefinitionInput(node_id="x::y")).startswith("No node")


def test_get_callers_and_callees(ctx: ToolContext) -> None:
    callers = tools.get_callers(
        ctx, GetCallersInput(node_id="repository.py::UserRepository.add")
    )
    assert "service.py::UserService.register" in callers
    callees = tools.get_callees(
        ctx, GetCalleesInput(node_id="service.py::UserService.register")
    )
    assert "repository.py::UserRepository.add" in callees


def test_get_neighbors(ctx: ToolContext) -> None:
    out = tools.get_neighbors(
        ctx, GetNeighborsInput(node_id="service.py::UserService.register", hops=1)
    )
    assert "service.py::UserService" in out
    assert "models.py::User" in out


def test_grep_and_invalid_regex(ctx: ToolContext) -> None:
    out = tools.grep(ctx, GrepInput(pattern="off-by-one"))
    assert "repository.py:" in out
    scoped = tools.grep(ctx, GrepInput(pattern="def ", path_glob="*utils*"))
    assert "utils.py:" in scoped and "models.py:" not in scoped
    assert tools.grep(ctx, GrepInput(pattern="(")).startswith("Error: invalid regex")


def test_list_languages(ctx: ToolContext) -> None:
    assert tools.list_languages(ctx, ListLanguagesInput()) == "python: 4"


def test_registry_covers_nine_tools_and_dispatches(ctx: ToolContext) -> None:
    assert set(TOOL_REGISTRY) == {
        "list_files", "read_file", "find_symbol", "get_definition",
        "get_callers", "get_callees", "get_neighbors", "grep", "list_languages",
    }
    spec = TOOL_REGISTRY["find_symbol"]
    params = spec.input_model.model_validate({"name": "count"})
    result = spec.func(ctx, params)
    assert "UserRepository.count" in result


def test_openai_tool_schemas_shape() -> None:
    schemas = openai_tool_schemas()
    assert len(schemas) == 9
    names = {s["function"]["name"] for s in schemas}
    assert "find_symbol" in names
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["type"] == "object"
        assert schema["function"]["description"]


def test_llm_chat_with_tools_normalizes(mocker) -> None:  # type: ignore[no-untyped-def]
    call = mocker.MagicMock()
    call.id = "call_1"
    call.type = "function"
    call.function.name = "find_symbol"
    call.function.arguments = '{"name": "User"}'
    message = mocker.MagicMock(content=None, tool_calls=[call])
    choice = mocker.MagicMock(message=message, finish_reason="tool_calls")
    usage = mocker.MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    response = mocker.MagicMock(choices=[choice], usage=usage)
    fake = mocker.MagicMock()
    fake.chat.completions.create.return_value = response
    mocker.patch("codesentry.agent.llm.OpenAI", return_value=fake)

    client = LLMClient(api_key="x", model="gpt-4.1")
    result = client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0].name == "find_symbol"
    assert result.tool_calls[0].arguments == {"name": "User"}
    assert result.usage.total_tokens == 15


def test_llm_parse_structured_returns_model(mocker) -> None:  # type: ignore[no-untyped-def]
    parsed = AnswerWithCitations(answer="ok", citations=[])
    message = mocker.MagicMock(parsed=parsed)
    choice = mocker.MagicMock(message=message)
    response = mocker.MagicMock(choices=[choice])
    fake = mocker.MagicMock()
    fake.beta.chat.completions.parse.return_value = response
    mocker.patch("codesentry.agent.llm.OpenAI", return_value=fake)

    client = LLMClient(api_key="x", model="gpt-4.1")
    result = client.parse_structured([{"role": "user", "content": "x"}], AnswerWithCitations)
    assert result.answer == "ok"


def test_llm_parse_structured_raises_structured_output_error(mocker) -> None:  # type: ignore[no-untyped-def]
    # A provider that returns non-conforming JSON makes the SDK raise a pydantic
    # ValidationError, which LLMClient normalizes to StructuredOutputError.
    try:
        AnswerWithCitations.model_validate({"thought": "x", "action": "final"})
    except ValidationError as exc:
        validation_error = exc
    fake = mocker.MagicMock()
    fake.beta.chat.completions.parse.side_effect = validation_error
    mocker.patch("codesentry.agent.llm.OpenAI", return_value=fake)

    client = LLMClient(api_key="x", model="bigpickle")
    with pytest.raises(StructuredOutputError):
        client.parse_structured([{"role": "user", "content": "x"}], AnswerWithCitations)


def test_llm_complete_returns_content(mocker) -> None:  # type: ignore[no-untyped-def]
    message = mocker.MagicMock(content="plain answer with models.py:4")
    choice = mocker.MagicMock(message=message)
    response = mocker.MagicMock(choices=[choice])
    fake = mocker.MagicMock()
    fake.chat.completions.create.return_value = response
    mocker.patch("codesentry.agent.llm.OpenAI", return_value=fake)

    client = LLMClient(api_key="x", model="bigpickle")
    assert client.complete([{"role": "user", "content": "x"}]) == (
        "plain answer with models.py:4"
    )
