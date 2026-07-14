"""Diff reviewer that, for each changed file, detects its language, gathers
cross-language graph context (the changed symbols and their callers/callees), and
invokes the review agent via a single structured parse_structured call per file to
return line-level review comments."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import networkx as nx
from pydantic import ValidationError

from codesentry.agent.llm import LLMClient, StructuredOutputError
from codesentry.agent.prompts import REVIEW_SYSTEM_PROMPT
from codesentry.agent.schemas import ReviewComment, ReviewResult
from codesentry.graph.schema import EdgeType, Node, NodeType
from codesentry.languages.base import get_adapter_for_file
from codesentry.review.diff import FileDiff, parse_diff

logger = logging.getLogger(__name__)

_DEF_TYPES = (NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD)
_MESSAGE_KEYS = ("message", "comment", "issue", "description", "text", "suggestion")
_VALID_SEVERITIES = {"info", "warning", "error"}
_JSON_FALLBACK_INSTRUCTION = (
    "Return ONLY a JSON object of the form "
    '{"comments": [{"file": string, "line": integer, '
    '"severity": "info"|"warning"|"error", "message": string, '
    '"suggestion": string or null}]}. '
    "Use an empty list if there are no real defects. No prose, no markdown fences."
)


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
        try:
            result = llm.parse_structured(messages, ReviewResult)
            comments.extend(result.comments)
        except StructuredOutputError:
            # The provider did not honor strict structured outputs (common with
            # non-OpenAI models). Retry as a plain completion and leniently recover
            # whatever comments the model produced, rather than crashing the review.
            logger.warning(
                "structured review output failed for %s; using lenient fallback",
                file_diff.path,
            )
            comments.extend(_lenient_review(llm, messages, file_diff))
    return comments


def _lenient_review(
    llm: LLMClient, messages: list[dict[str, str]], file_diff: FileDiff
) -> list[ReviewComment]:
    """Fallback for providers that ignore strict schemas: ask for plain JSON and
    coerce each item into a ReviewComment, tolerating missing or renamed fields."""

    prompt = messages + [{"role": "user", "content": _JSON_FALLBACK_INSTRUCTION}]
    payload = _extract_json(llm.complete(prompt))
    raw = payload.get("comments") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    default_line = min(file_diff.changed_line_numbers, default=1)
    recovered: list[ReviewComment] = []
    for item in raw:
        comment = _coerce_comment(item, file_diff.path, default_line)
        if comment is not None:
            recovered.append(comment)
    return recovered


def _coerce_comment(
    item: Any, default_file: str, default_line: int
) -> ReviewComment | None:
    if not isinstance(item, dict):
        return None
    message = next(
        (str(item[key]).strip() for key in _MESSAGE_KEYS if item.get(key)), ""
    )
    if not message:
        return None
    severity = str(item.get("severity", "warning")).lower()
    if severity not in _VALID_SEVERITIES:
        severity = "warning"
    line = item.get("line")
    if not isinstance(line, int) or isinstance(line, bool):
        line = default_line
    raw_suggestion = item.get("suggestion")
    suggestion = str(raw_suggestion).strip() if raw_suggestion else None
    if suggestion == message:  # text only lived in 'suggestion'; don't duplicate it
        suggestion = None
    try:
        return ReviewComment.model_validate(
            {
                "file": item.get("file") or default_file,
                "line": line,
                "severity": severity,
                "message": message,
                "suggestion": suggestion,
            }
        )
    except ValidationError:
        return None


def _extract_json(text: str) -> Any:
    """Best-effort extraction of a JSON object/array from a model reply that may be
    wrapped in prose or ```json fences."""

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = min(
        (i for i in (candidate.find("{"), candidate.find("[")) if i != -1),
        default=-1,
    )
    if start == -1:
        return None
    end = max(candidate.rfind("}"), candidate.rfind("]"))
    if end < start:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


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
