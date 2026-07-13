"""The agent's toolbox: graph- and file-backed tools (list_files, read_file,
find_symbol, get_definition, get_callers, get_callees, get_neighbors, grep,
list_languages) with OpenAI function schemas and a registry for name-based dispatch.

Each tool takes a bound ToolContext (the graph plus repo root) and a validated
Pydantic input, and returns a plain, LLM-readable string. Tool docstrings become
the LLM-facing descriptions."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx

from codesentry.agent.schemas import (
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
from codesentry.graph.schema import EdgeType, Node, NodeType
from codesentry.retrieval.snippets import find_nodes_by_name, get_node, get_snippet
from codesentry.retrieval.subgraph import extract_subgraph, subgraph_nodes

_GREP_LIMIT = 200


@dataclass
class ToolContext:
    """Runtime context bound to every tool call: the code graph and the repo root
    used to read source files."""

    graph: nx.MultiDiGraph
    repo_root: Path


def _ref(node: Node) -> str:
    return f"{node.id} [{node.type.value}] {node.file_path}:{node.start_line} ({node.language})"


def _file_nodes(ctx: ToolContext) -> list[Node]:
    return [
        data["node"]
        for _, data in ctx.graph.nodes(data=True)
        if data["node"].type is NodeType.FILE
    ]


def list_files(ctx: ToolContext, params: ListFilesInput) -> str:
    """List indexed files, optionally filtered by a glob pattern and/or language."""
    paths = []
    for node in _file_nodes(ctx):
        if params.language is not None and node.language != params.language:
            continue
        if params.pattern is not None and not fnmatch.fnmatch(node.file_path, params.pattern):
            continue
        paths.append(f"{node.file_path} ({node.language})")
    if not paths:
        return "No files matched."
    return "\n".join(sorted(paths))


def read_file(ctx: ToolContext, params: ReadFileInput) -> str:
    """Read a file (or a 1-based line range of it) with line numbers for citation."""
    path = ctx.repo_root / params.path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"Error: cannot read {params.path}"
    start = params.start_line or 1
    end = params.end_line or len(lines)
    start = max(1, start)
    end = min(len(lines), end)
    if start > end:
        return f"Error: empty or invalid range for {params.path}"
    width = len(str(end))
    return "\n".join(
        f"{i:>{width}} | {lines[i - 1]}" for i in range(start, end + 1)
    )


def find_symbol(ctx: ToolContext, params: FindSymbolInput) -> str:
    """Find symbols (classes, functions, methods) by simple or qualified name."""
    matches = find_nodes_by_name(ctx.graph, params.name, params.language)
    if not matches:
        return f"No symbol named {params.name!r} found."
    lines = []
    for node in matches:
        signature = f" — {node.signature}" if node.signature else ""
        lines.append(f"{_ref(node)}{signature}")
    return "\n".join(lines)


def get_definition(ctx: ToolContext, params: GetDefinitionInput) -> str:
    """Return the exact source definition of a node by its id."""
    node = get_node(ctx.graph, params.node_id)
    if node is None:
        return f"No node with id {params.node_id!r}."
    header = f"{node.file_path}:{node.start_line}-{node.end_line} ({node.language})"
    return f"{header}\n{get_snippet(node, ctx.repo_root)}"


def get_callers(ctx: ToolContext, params: GetCallersInput) -> str:
    """List functions/methods that call the given node (reverse CALLS edges)."""
    node = get_node(ctx.graph, params.node_id)
    if node is None:
        return f"No node with id {params.node_id!r}."
    callers = _neighbors_by_edge(ctx, params.node_id, EdgeType.CALLS, incoming=True)
    if not callers:
        return f"No callers of {params.node_id} found in the indexed graph."
    return "\n".join(_ref(n) for n in callers)


def get_callees(ctx: ToolContext, params: GetCalleesInput) -> str:
    """List functions/methods called by the given node (forward CALLS edges)."""
    node = get_node(ctx.graph, params.node_id)
    if node is None:
        return f"No node with id {params.node_id!r}."
    callees = _neighbors_by_edge(ctx, params.node_id, EdgeType.CALLS, incoming=False)
    if not callees:
        return f"No callees of {params.node_id} found in the indexed graph."
    return "\n".join(_ref(n) for n in callees)


def get_neighbors(ctx: ToolContext, params: GetNeighborsInput) -> str:
    """Return the graph neighborhood around a node up to `hops` (1 or 2) steps."""
    if not ctx.graph.has_node(params.node_id):
        return f"No node with id {params.node_id!r}."
    sub = extract_subgraph(ctx.graph, [params.node_id], hops=params.hops)
    neighbors = [n for n in subgraph_nodes(sub) if n.id != params.node_id]
    if not neighbors:
        return f"{params.node_id} has no neighbors in the indexed graph."
    return "\n".join(_ref(n) for n in neighbors)


def grep(ctx: ToolContext, params: GrepInput) -> str:
    """Regex-search the text of indexed files, optionally restricted by a path glob."""
    try:
        pattern = re.compile(params.pattern)
    except re.error as exc:
        return f"Error: invalid regex {params.pattern!r}: {exc}"
    results: list[str] = []
    for node in sorted(_file_nodes(ctx), key=lambda n: n.file_path):
        if params.path_glob is not None and not fnmatch.fnmatch(node.file_path, params.path_glob):
            continue
        try:
            lines = (ctx.repo_root / node.file_path).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if pattern.search(line):
                results.append(f"{node.file_path}:{lineno}: {line.strip()}")
                if len(results) >= _GREP_LIMIT:
                    results.append(f"... (truncated at {_GREP_LIMIT} matches)")
                    return "\n".join(results)
    return "\n".join(results) if results else f"No matches for {params.pattern!r}."


def list_languages(ctx: ToolContext, params: ListLanguagesInput) -> str:
    """List the languages present in the indexed repo with per-language file counts."""
    counts: dict[str, int] = {}
    for node in _file_nodes(ctx):
        counts[node.language] = counts.get(node.language, 0) + 1
    if not counts:
        return "No indexed files."
    return "\n".join(f"{lang}: {count}" for lang, count in sorted(counts.items()))


def _neighbors_by_edge(
    ctx: ToolContext, node_id: str, edge_type: EdgeType, incoming: bool
) -> list[Node]:
    edges = (
        ctx.graph.in_edges(node_id, data=True)
        if incoming
        else ctx.graph.out_edges(node_id, data=True)
    )
    result: list[Node] = []
    seen: set[str] = set()
    for source, target, data in edges:
        if data["type"] != edge_type.value:
            continue
        other = source if incoming else target
        if other in seen or not ctx.graph.has_node(other):
            continue
        seen.add(other)
        result.append(ctx.graph.nodes[other]["node"])
    return sorted(result, key=lambda n: n.id)


@dataclass(frozen=True)
class ToolDef:
    """A registered tool: its name, LLM-facing description, input model, and the
    context-taking callable that executes it."""

    name: str
    description: str
    input_model: type[Any]
    func: Callable[[ToolContext, Any], str]


def _tool(name: str, func: Callable[[ToolContext, Any], str], input_model: type[Any]) -> ToolDef:
    description = (func.__doc__ or "").strip()
    return ToolDef(name=name, description=description, input_model=input_model, func=func)


TOOL_REGISTRY: dict[str, ToolDef] = {
    "list_files": _tool("list_files", list_files, ListFilesInput),
    "read_file": _tool("read_file", read_file, ReadFileInput),
    "find_symbol": _tool("find_symbol", find_symbol, FindSymbolInput),
    "get_definition": _tool("get_definition", get_definition, GetDefinitionInput),
    "get_callers": _tool("get_callers", get_callers, GetCallersInput),
    "get_callees": _tool("get_callees", get_callees, GetCalleesInput),
    "get_neighbors": _tool("get_neighbors", get_neighbors, GetNeighborsInput),
    "grep": _tool("grep", grep, GrepInput),
    "list_languages": _tool("list_languages", list_languages, ListLanguagesInput),
}


def openai_tool_schemas() -> list[dict[str, Any]]:
    """Return the tool definitions in OpenAI's function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_model.model_json_schema(),
            },
        }
        for spec in TOOL_REGISTRY.values()
    ]
