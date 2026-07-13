"""Repository indexer that walks a repo, respects .gitignore, dispatches each file
to its language adapter, merges the emitted nodes and edges into a single networkx
MultiDiGraph, and performs best-effort cross-file resolution of calls, imports,
inheritance, and Go receiver methods.

Cross-file resolution is driven entirely off universal node metadata (imports,
calls, bases, implements, receiver_type); the only language-specific step is
mapping an import module to a file, which is delegated to each adapter's
resolve_import. The builder never branches on a node's language."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import networkx as nx
import pathspec

import codesentry.languages  # noqa: F401  (imports register the adapters)
from codesentry.graph.schema import Edge, EdgeType, Node, NodeType
from codesentry.languages.base import ImportIndex, get_adapter_for_file

logger = logging.getLogger(__name__)

_ALWAYS_IGNORE = {".git", ".codesentry"}
_DEF_TYPES = (NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD)


def build_graph(repo_path: Path) -> nx.MultiDiGraph:
    """Walk ``repo_path``, parse every file with a registered adapter, merge the
    results into a MultiDiGraph, resolve cross-file relations, and return the graph.
    A ``summary`` dict is attached to ``graph.graph``."""

    repo_path = repo_path.resolve()
    spec = _load_gitignore(repo_path)
    graph: nx.MultiDiGraph = nx.MultiDiGraph()

    files_skipped = 0
    for path in _iter_source_files(repo_path, spec):
        adapter = get_adapter_for_file(path)
        if adapter is None:
            files_skipped += 1
            continue
        rel_path = path.relative_to(repo_path)
        try:
            source = path.read_bytes()
        except OSError:
            logger.warning("Could not read %s; skipping", rel_path)
            continue
        nodes, edges = adapter.parse_file(rel_path, source)
        _merge(graph, nodes, edges)

    summary = _resolve_cross_file(graph)
    summary["files_skipped"] = files_skipped
    graph.graph["summary"] = summary
    _log_summary(graph, summary)
    return graph


def _merge(graph: nx.MultiDiGraph, nodes: list[Node], edges: list[Edge]) -> None:
    for node in nodes:
        graph.add_node(node.id, node=node)
    for edge in edges:
        graph.add_edge(edge.source_id, edge.target_id, type=edge.type.value, edge=edge)


def _resolve_cross_file(graph: nx.MultiDiGraph) -> dict[str, Any]:
    """Add cross-file IMPORTS, CALLS, INHERITS/IMPLEMENTS, and Go receiver-method
    CONTAINS edges based on node metadata. Returns a summary dict."""

    all_nodes: dict[str, Node] = {
        nid: data["node"] for nid, data in graph.nodes(data=True)
    }
    file_nodes = [n for n in all_nodes.values() if n.type is NodeType.FILE]

    defs_by_name: dict[str, list[str]] = defaultdict(list)
    for nid, node in all_nodes.items():
        if node.type in _DEF_TYPES:
            defs_by_name[node.name].append(nid)

    files_by_dir: dict[str, list[str]] = defaultdict(list)
    by_stem: dict[str, list[str]] = defaultdict(list)
    package_of: dict[str, str | None] = {}
    files_by_package: dict[str, list[str]] = defaultdict(list)
    for fn in file_nodes:
        by_stem[Path(fn.file_path).stem].append(fn.file_path)
        files_by_dir[Path(fn.file_path).parent.as_posix()].append(fn.file_path)
        package = fn.metadata.get("package")
        package_of[fn.file_path] = package
        if package:
            files_by_package[package].append(fn.file_path)

    index = ImportIndex(
        paths={fn.file_path for fn in file_nodes},
        by_stem=dict(by_stem),
        package_of=package_of,
        files_by_package=dict(files_by_package),
    )

    existing = {(u, v, data["type"]) for u, v, data in graph.edges(data=True)}
    imported_files: dict[str, set[str]] = defaultdict(set)

    # 1. IMPORTS edges (adapter-driven module -> file resolution).
    for fn in file_nodes:
        adapter = get_adapter_for_file(Path(fn.file_path))
        if adapter is None:
            continue
        for entry in fn.metadata.get("imports", []):
            for module in entry.get("modules", []):
                target = adapter.resolve_import(module, fn.file_path, index)
                if target and target != fn.file_path and target in index.paths:
                    imported_files[fn.file_path].add(target)
                    _add_edge(graph, existing, fn.file_path, target, EdgeType.IMPORTS)

    def scope_files(file_path: str) -> set[str]:
        scope = {file_path} | imported_files.get(file_path, set())
        adapter = get_adapter_for_file(Path(file_path))
        if adapter is not None and adapter.package_level_visibility:
            scope |= set(files_by_dir.get(Path(file_path).parent.as_posix(), []))
        return scope

    def resolve_name(
        name: str, file_path: str, self_id: str, kinds: tuple[NodeType, ...]
    ) -> str | None:
        candidates = [
            nid
            for nid in defs_by_name.get(name, [])
            if nid != self_id and all_nodes[nid].type in kinds
        ]
        scope = scope_files(file_path)
        scoped = [nid for nid in candidates if all_nodes[nid].file_path in scope]
        if len(scoped) == 1:
            return scoped[0]
        if not scoped and len(candidates) == 1:
            return candidates[0]
        return None

    def resolve_call(name: str, file_path: str, self_id: str) -> str | None:
        # A bare name is first treated as a function/method call, then (e.g. for
        # constructors) as a class instantiation, so class/constructor name clashes
        # do not make either ambiguous.
        return resolve_name(
            name, file_path, self_id, (NodeType.FUNCTION, NodeType.METHOD)
        ) or resolve_name(name, file_path, self_id, (NodeType.CLASS,))

    # 2. Go receiver methods whose struct lives in another file of the package.
    for nid, node in all_nodes.items():
        if node.type is not NodeType.METHOD or "receiver_type" not in node.metadata:
            continue
        if _has_class_parent(graph, all_nodes, nid):
            continue
        target = resolve_name(
            node.metadata["receiver_type"], node.file_path, nid, (NodeType.CLASS,)
        )
        if target is not None:
            _detach_file_container(graph, all_nodes, nid)
            _add_edge(graph, existing, target, nid, EdgeType.CONTAINS)

    # 3. Cross-file CALLS.
    unresolved_calls = 0
    for nid, node in all_nodes.items():
        if node.type not in (NodeType.FUNCTION, NodeType.METHOD):
            continue
        for call in node.metadata.get("calls", []):
            target = resolve_call(call["name"], node.file_path, nid)
            if target is None:
                unresolved_calls += 1
                continue
            _add_edge(
                graph, existing, nid, target, EdgeType.CALLS,
                metadata={"line": call["line"]},
            )

    # 4. Cross-file INHERITS / IMPLEMENTS.
    for nid, node in all_nodes.items():
        if node.type is not NodeType.CLASS:
            continue
        for base in node.metadata.get("bases", []):
            target = resolve_name(base, node.file_path, nid, (NodeType.CLASS,))
            if target is not None:
                _add_edge(graph, existing, nid, target, EdgeType.INHERITS)
        for iface in node.metadata.get("implements", []):
            target = resolve_name(iface, node.file_path, nid, (NodeType.CLASS,))
            if target is not None:
                _add_edge(graph, existing, nid, target, EdgeType.IMPLEMENTS)

    return {
        "files_indexed": len(file_nodes),
        "files_with_parse_errors": sum(
            1 for fn in file_nodes if fn.metadata.get("parse_error")
        ),
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "unresolved_calls": unresolved_calls,
    }


def _add_edge(
    graph: nx.MultiDiGraph,
    existing: set[tuple[str, str, str]],
    source_id: str,
    target_id: str,
    edge_type: EdgeType,
    metadata: dict[str, Any] | None = None,
) -> None:
    key = (source_id, target_id, edge_type.value)
    if key in existing:
        return
    edge = Edge(
        source_id=source_id,
        target_id=target_id,
        type=edge_type,
        metadata=metadata or {},
    )
    graph.add_edge(source_id, target_id, type=edge_type.value, edge=edge)
    existing.add(key)


def _has_class_parent(
    graph: nx.MultiDiGraph, all_nodes: dict[str, Node], node_id: str
) -> bool:
    for source, _, data in graph.in_edges(node_id, data=True):
        if data["type"] == EdgeType.CONTAINS.value and all_nodes[source].type is NodeType.CLASS:
            return True
    return False


def _detach_file_container(
    graph: nx.MultiDiGraph, all_nodes: dict[str, Node], node_id: str
) -> None:
    for source, _, key, data in list(graph.in_edges(node_id, keys=True, data=True)):
        if data["type"] == EdgeType.CONTAINS.value and all_nodes[source].type is NodeType.FILE:
            graph.remove_edge(source, node_id, key)


def _log_summary(graph: nx.MultiDiGraph, summary: dict[str, Any]) -> None:
    logger.info(
        "Indexed %d files (%d skipped, %d with parse errors): %d nodes, %d edges, "
        "%d unresolved calls",
        summary["files_indexed"],
        summary["files_skipped"],
        summary["files_with_parse_errors"],
        summary["nodes"],
        summary["edges"],
        summary["unresolved_calls"],
    )


def _load_gitignore(repo_path: Path) -> pathspec.PathSpec[Any] | None:
    gitignore = repo_path / ".gitignore"
    if not gitignore.is_file():
        return None
    lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _iter_source_files(
    repo_path: Path, spec: pathspec.PathSpec[Any] | None
) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dir_ = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _ALWAYS_IGNORE
            and not _is_ignored(dir_ / d, repo_path, spec, is_dir=True)
        ]
        for filename in filenames:
            path = dir_ / filename
            if _is_ignored(path, repo_path, spec, is_dir=False):
                continue
            found.append(path)
    return sorted(found)


def _is_ignored(
    path: Path,
    repo_path: Path,
    spec: pathspec.PathSpec[Any] | None,
    is_dir: bool,
) -> bool:
    if spec is None:
        return False
    rel = path.relative_to(repo_path).as_posix()
    if is_dir:
        rel += "/"
    return spec.match_file(rel)
