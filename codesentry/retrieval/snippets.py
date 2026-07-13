"""Source-snippet retrieval that reads a node's exact lines from disk with a small
margin, plus symbol lookup helpers for finding graph nodes by name, optionally
scoped to a single language."""

from __future__ import annotations

from pathlib import Path

import networkx as nx

from codesentry.graph.schema import Node, NodeType

_DEFAULT_MARGIN = 2


def get_snippet(node: Node, repo_root: Path, margin: int = _DEFAULT_MARGIN) -> str:
    """Return the exact source lines spanning ``node`` with ``margin`` lines of
    context on each side. ``node.file_path`` is resolved relative to ``repo_root``.
    If the file cannot be read, a short marker string is returned instead."""

    path = repo_root / node.file_path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"# <source unavailable for {node.file_path}>"

    lines = text.splitlines()
    start = max(1, node.start_line - margin)
    end = min(len(lines), node.end_line + margin)
    if start > len(lines):
        return ""
    return "\n".join(lines[start - 1 : end])


def find_nodes_by_name(
    graph: nx.MultiDiGraph, name: str, language: str | None = None
) -> list[Node]:
    """Return graph nodes whose simple or qualified name equals ``name``, optionally
    restricted to one ``language``. FILE nodes are excluded. Results are sorted by
    node id for determinism."""

    matches: list[Node] = []
    for _, data in graph.nodes(data=True):
        node: Node = data["node"]
        if node.type is NodeType.FILE:
            continue
        if language is not None and node.language != language:
            continue
        if node.name == name or node.qualified_name == name:
            matches.append(node)
    return sorted(matches, key=lambda n: n.id)


def get_node(graph: nx.MultiDiGraph, node_id: str) -> Node | None:
    """Return the Node with the given id, or None if it is not in the graph."""

    if not graph.has_node(node_id):
        return None
    node: Node = graph.nodes[node_id]["node"]
    return node
