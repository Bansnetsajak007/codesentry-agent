"""Persistence for the code graph: save and load the networkx graph as a pickle
alongside a sidecar JSON metadata file recording repo path, index time, node/edge
counts, per-language file counts, git commit, and the CodeSentry version.

This is the Phase 1 step-4 slice; the sidecar schema may be extended in step 9."""

from __future__ import annotations

import json
import pickle
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from codesentry import __version__
from codesentry.graph.schema import Node, NodeType


def save_graph(
    graph: nx.MultiDiGraph, path: Path, *, repo_path: Path | None = None
) -> None:
    """Pickle ``graph`` to ``path`` and write a sidecar ``<path>.meta.json`` with
    index metadata. ``repo_path``, when given, records the source repo and its
    current git commit."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(graph, fh)

    meta = _build_metadata(graph, repo_path)
    _meta_path(path).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_graph(path: Path) -> nx.MultiDiGraph:
    """Load and return the pickled graph at ``path``."""

    with Path(path).open("rb") as fh:
        graph: nx.MultiDiGraph = pickle.load(fh)
    return graph


def load_metadata(path: Path) -> dict[str, Any]:
    """Load the sidecar metadata JSON for the graph pickle at ``path``."""

    data: dict[str, Any] = json.loads(
        _meta_path(Path(path)).read_text(encoding="utf-8")
    )
    return data


def per_language_file_counts(graph: nx.MultiDiGraph) -> dict[str, int]:
    """Count FILE nodes grouped by language."""

    counter: Counter[str] = Counter()
    for _, data in graph.nodes(data=True):
        node: Node = data["node"]
        if node.type is NodeType.FILE:
            counter[node.language] += 1
    return dict(sorted(counter.items()))


def _build_metadata(
    graph: nx.MultiDiGraph, repo_path: Path | None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "codesentry_version": __version__,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "repo_path": str(repo_path.resolve()) if repo_path is not None else None,
        "git_commit": _git_commit(repo_path) if repo_path is not None else None,
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "files_per_language": per_language_file_counts(graph),
    }
    summary = graph.graph.get("summary")
    if isinstance(summary, dict):
        metadata["resolution"] = summary
    return metadata


def _git_commit(repo_path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _meta_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".meta.json")
