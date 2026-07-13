"""Subgraph extraction that, given seed node IDs, returns those nodes plus their
neighbors along CALLS, IMPORTS, INHERITS, IMPLEMENTS, and CONTAINS edges up to a
configurable hop depth (default 1, capped at 2 for Phase 1).

Neighbors are gathered in both directions, so a seed brings in its callers and
callees, its container and its members, and its parents and subtypes."""

from __future__ import annotations

from collections.abc import Iterable

import networkx as nx

from codesentry.graph.schema import EdgeType, Node

_MAX_HOPS = 2
_DEFAULT_EDGE_TYPES = frozenset(
    e.value
    for e in (
        EdgeType.CONTAINS,
        EdgeType.CALLS,
        EdgeType.IMPORTS,
        EdgeType.INHERITS,
        EdgeType.IMPLEMENTS,
    )
)


def extract_subgraph(
    graph: nx.MultiDiGraph,
    seed_ids: Iterable[str],
    hops: int = 1,
    edge_types: Iterable[EdgeType] | None = None,
) -> nx.MultiDiGraph:
    """Return the induced subgraph of the given seeds plus every node reachable from
    them within ``hops`` steps along the allowed edge types (in either direction).

    ``hops`` is clamped to ``[1, 2]``. Seed ids absent from ``graph`` are ignored;
    if no seed is present the returned graph is empty."""

    hops = max(1, min(hops, _MAX_HOPS))
    allowed = (
        _DEFAULT_EDGE_TYPES
        if edge_types is None
        else frozenset(e.value for e in edge_types)
    )

    visited: set[str] = {sid for sid in seed_ids if graph.has_node(sid)}
    frontier = set(visited)
    for _ in range(hops):
        nxt: set[str] = set()
        for node_id in frontier:
            for _, target, data in graph.out_edges(node_id, data=True):
                if data["type"] in allowed and target not in visited:
                    nxt.add(target)
            for source, _, data in graph.in_edges(node_id, data=True):
                if data["type"] in allowed and source not in visited:
                    nxt.add(source)
        if not nxt:
            break
        visited |= nxt
        frontier = nxt

    subgraph: nx.MultiDiGraph = graph.subgraph(visited).copy()
    return subgraph


def subgraph_nodes(graph: nx.MultiDiGraph) -> list[Node]:
    """Return the Node objects held on every node of ``graph``, sorted by id."""

    nodes = [data["node"] for _, data in graph.nodes(data=True)]
    return sorted(nodes, key=lambda n: n.id)
