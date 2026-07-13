"""Tests for the retrieval layer: subgraph extraction around seeds with hop
depth and edge-type filtering, source-snippet reading with margins, and symbol
lookup by simple/qualified name with optional language scoping."""

from pathlib import Path

from codesentry.graph.builder import build_graph
from codesentry.graph.schema import EdgeType, NodeType
from codesentry.retrieval.snippets import find_nodes_by_name, get_node, get_snippet
from codesentry.retrieval.subgraph import extract_subgraph, subgraph_nodes

PY_FIXTURE = Path(__file__).parent / "fixtures" / "sample_python"


def _graph():
    return build_graph(PY_FIXTURE)


def test_subgraph_one_hop_includes_neighbors() -> None:
    graph = _graph()
    seed = "service.py::UserService.register"
    sub = extract_subgraph(graph, [seed], hops=1)
    ids = {n.id for n in subgraph_nodes(sub)}
    assert seed in ids
    assert "service.py::UserService" in ids  # container (CONTAINS)
    assert "models.py::User" in ids  # callee (CALLS)
    assert "repository.py::UserRepository.add" in ids  # callee (CALLS)


def test_subgraph_two_hops_is_larger() -> None:
    graph = _graph()
    seed = "service.py::UserService.register"
    one = subgraph_nodes(extract_subgraph(graph, [seed], hops=1))
    two = subgraph_nodes(extract_subgraph(graph, [seed], hops=2))
    assert len(two) >= len(one)


def test_subgraph_hops_are_clamped() -> None:
    graph = _graph()
    seed = "service.py::UserService.register"
    capped = subgraph_nodes(extract_subgraph(graph, [seed], hops=2))
    over = subgraph_nodes(extract_subgraph(graph, [seed], hops=99))
    assert {n.id for n in capped} == {n.id for n in over}


def test_subgraph_edge_type_filter() -> None:
    graph = _graph()
    seed = "service.py::UserService.register"
    sub = extract_subgraph(graph, [seed], hops=1, edge_types=[EdgeType.CONTAINS])
    ids = {n.id for n in subgraph_nodes(sub)}
    assert "service.py::UserService" in ids  # reached via CONTAINS
    assert "models.py::User" not in ids  # would need CALLS, which is filtered out


def test_subgraph_ignores_missing_seeds() -> None:
    graph = _graph()
    sub = extract_subgraph(graph, ["does.py::NotReal"], hops=1)
    assert subgraph_nodes(sub) == []


def test_get_snippet_returns_source_with_margin() -> None:
    graph = _graph()
    node = get_node(graph, "models.py::User.display_name")
    assert node is not None
    snippet = get_snippet(node, PY_FIXTURE, margin=2)
    assert "def display_name(self)" in snippet
    # The 2-line margin pulls in surrounding lines beyond the definition body.
    assert snippet.count("\n") >= (node.end_line - node.start_line)


def test_get_snippet_clamps_at_file_bounds() -> None:
    graph = _graph()
    node = get_node(graph, "models.py")  # FILE node spans line 1..end
    assert node is not None
    snippet = get_snippet(node, PY_FIXTURE, margin=5)
    assert snippet.startswith('"""Domain models')


def test_get_snippet_missing_file_returns_marker() -> None:
    graph = _graph()
    node = get_node(graph, "models.py::User")
    assert node is not None
    snippet = get_snippet(node, Path("/nonexistent-root"))
    assert snippet.startswith("# <source unavailable")


def test_find_nodes_by_name_simple_and_qualified() -> None:
    graph = _graph()
    by_simple = find_nodes_by_name(graph, "User")
    assert any(n.type is NodeType.CLASS and n.id == "models.py::User" for n in by_simple)
    by_qualified = find_nodes_by_name(graph, "UserRepository.count")
    assert [n.id for n in by_qualified] == ["repository.py::UserRepository.count"]


def test_find_nodes_by_name_language_filter() -> None:
    graph = _graph()
    assert find_nodes_by_name(graph, "User", language="go") == []
    assert find_nodes_by_name(graph, "User", language="python")
