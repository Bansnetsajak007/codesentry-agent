"""Repository indexer that walks a repo, respects .gitignore, dispatches each file
to its language adapter, merges the emitted nodes and edges into a single networkx
MultiDiGraph, and performs best-effort cross-file resolution of calls and imports.

This is the Phase 1 step-4 slice: it walks and merges only. Cross-file resolution
of CALLS/IMPORTS and the end-of-run summary logging are added in build-order step 9."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import networkx as nx
import pathspec

import codesentry.languages  # noqa: F401  (imports register the adapters)
from codesentry.graph.schema import Edge, Node
from codesentry.languages.base import get_adapter_for_file

logger = logging.getLogger(__name__)

_ALWAYS_IGNORE = {".git", ".codesentry"}


def build_graph(repo_path: Path) -> nx.MultiDiGraph:
    """Walk ``repo_path``, parse every file with a registered adapter, and return a
    MultiDiGraph whose nodes carry a ``Node`` under the ``node`` attribute and whose
    edges carry an ``Edge`` under the ``edge`` attribute."""

    repo_path = repo_path.resolve()
    spec = _load_gitignore(repo_path)
    graph: nx.MultiDiGraph = nx.MultiDiGraph()

    for path in _iter_source_files(repo_path, spec):
        adapter = get_adapter_for_file(path)
        if adapter is None:
            continue
        rel_path = path.relative_to(repo_path)
        try:
            source = path.read_bytes()
        except OSError:
            logger.warning("Could not read %s; skipping", rel_path)
            continue
        nodes, edges = adapter.parse_file(rel_path, source)
        _merge(graph, nodes, edges)

    return graph


def _merge(graph: nx.MultiDiGraph, nodes: list[Node], edges: list[Edge]) -> None:
    for node in nodes:
        graph.add_node(node.id, node=node)
    for edge in edges:
        graph.add_edge(edge.source_id, edge.target_id, type=edge.type.value, edge=edge)


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
