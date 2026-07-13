"""Diff reviewer that, for each changed file, detects its language, gathers
cross-language graph context (the changed symbols and their callers/callees), and
invokes the review agent via a single structured parse_structured call per file to
return line-level review comments."""

from __future__ import annotations

from pathlib import Path

import networkx as nx

from codesentry.agent.llm import LLMClient
from codesentry.agent.prompts import REVIEW_SYSTEM_PROMPT
from codesentry.agent.schemas import ReviewComment, ReviewResult
from codesentry.graph.schema import EdgeType, Node, NodeType
from codesentry.languages.base import get_adapter_for_file
from codesentry.review.diff import FileDiff, parse_diff

_DEF_TYPES = (NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD)


def review_diff(
    diff_text: str, graph: nx.MultiDiGraph, llm: LLMClient
) -> list[ReviewComment]:
    """Review a unified diff and return line-level comments. Each changed file is
    reviewed with one structured LLM call, given the diff plus graph context for the
    symbols it touches."""

    comments: list[ReviewComment] = []
    for file_diff in parse_diff(diff_text):
        adapter = get_adapter_for_file(Path(file_diff.path))
        language = adapter.language_name if adapter is not None else None
        changed = _changed_symbols(graph, file_diff)
        context = _context_block(graph, changed)
        messages = [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(file_diff, language, context)},
        ]
        result = llm.parse_structured(messages, ReviewResult)
        comments.extend(result.comments)
    return comments


def _changed_symbols(graph: nx.MultiDiGraph, file_diff: FileDiff) -> list[Node]:
    """Graph definitions in the changed file whose line span overlaps a changed line."""

    touched = file_diff.changed_line_numbers
    symbols: list[Node] = []
    for _, data in graph.nodes(data=True):
        node: Node = data["node"]
        if node.type not in _DEF_TYPES or node.file_path != file_diff.path:
            continue
        if any(node.start_line <= line <= node.end_line for line in touched):
            symbols.append(node)
    return sorted(symbols, key=lambda n: n.start_line)


def _context_block(graph: nx.MultiDiGraph, changed: list[Node]) -> str:
    if not changed:
        return "(no indexed symbols overlap the changed lines)"
    blocks: list[str] = []
    for node in changed:
        callers = _related(graph, node.id, incoming=True)
        callees = _related(graph, node.id, incoming=False)
        lines = [
            f"- {node.qualified_name} ({node.language}) "
            f"at {node.file_path}:{node.start_line}-{node.end_line}",
            f"    signature: {node.signature or 'n/a'}",
            f"    callers: {_fmt(callers) or 'none'}",
            f"    callees: {_fmt(callees) or 'none'}",
        ]
        cross = [n for n in callers + callees if n.language != node.language]
        if cross:
            lines.append(f"    cross-language neighbors: {_fmt(cross)}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _related(graph: nx.MultiDiGraph, node_id: str, incoming: bool) -> list[Node]:
    edges = (
        graph.in_edges(node_id, data=True)
        if incoming
        else graph.out_edges(node_id, data=True)
    )
    seen: set[str] = set()
    result: list[Node] = []
    for source, target, data in edges:
        if data["type"] != EdgeType.CALLS.value:
            continue
        other = source if incoming else target
        if other in seen or not graph.has_node(other):
            continue
        seen.add(other)
        result.append(graph.nodes[other]["node"])
    return result


def _fmt(nodes: list[Node]) -> str:
    return ", ".join(
        f"{n.qualified_name} ({n.file_path}:{n.start_line}, {n.language})" for n in nodes
    )


def _user_message(file_diff: FileDiff, language: str | None, context: str) -> str:
    return (
        f"Review this change to `{file_diff.path}` "
        f"({language or 'unknown language'}).\n\n"
        f"Unified diff:\n{file_diff.raw}\n\n"
        f"Repository context for the changed symbols:\n{context}\n\n"
        "Report only real defects as structured comments, each anchored to a real "
        "line in this file."
    )
