"""Tests for the universal graph schema plus the step-4 builder/store slice:
node/edge construction, enum typing, independent metadata defaults, id formatting,
serialization round-trips, extra-field rejection, and building/persisting a graph
from the pure-Python fixture."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from codesentry.graph.builder import build_graph
from codesentry.graph.schema import (
    Edge,
    EdgeType,
    Node,
    NodeType,
    make_node_id,
)
from codesentry.graph.store import (
    load_graph,
    load_metadata,
    per_language_file_counts,
    save_graph,
)

PY_FIXTURE = Path(__file__).parent / "fixtures" / "sample_python"


def _sample_node() -> Node:
    return Node(
        id="src/auth.py::LoginHandler.login",
        type=NodeType.METHOD,
        name="login",
        qualified_name="LoginHandler.login",
        file_path="src/auth.py",
        language="python",
        start_line=10,
        end_line=25,
        signature="def login(self, user: str) -> bool",
        docstring="Authenticate a user.",
        metadata={"decorators": ["staticmethod"]},
    )


def test_node_fields_and_enum_type() -> None:
    node = _sample_node()
    assert node.type is NodeType.METHOD
    assert node.name == "login"
    assert node.qualified_name == "LoginHandler.login"
    assert node.language == "python"
    assert node.start_line == 10
    assert node.end_line == 25
    assert node.metadata["decorators"] == ["staticmethod"]


def test_optional_fields_default_to_none() -> None:
    node = Node(
        id="a.py::f",
        type=NodeType.FUNCTION,
        name="f",
        qualified_name="f",
        file_path="a.py",
        language="python",
        start_line=1,
        end_line=2,
    )
    assert node.signature is None
    assert node.docstring is None


def test_metadata_defaults_are_independent() -> None:
    first = Node(
        id="a.py::f",
        type=NodeType.FUNCTION,
        name="f",
        qualified_name="f",
        file_path="a.py",
        language="python",
        start_line=1,
        end_line=2,
    )
    second = Node(
        id="a.py::g",
        type=NodeType.FUNCTION,
        name="g",
        qualified_name="g",
        file_path="a.py",
        language="python",
        start_line=3,
        end_line=4,
    )
    first.metadata["x"] = 1
    assert second.metadata == {}


def test_edge_fields_and_enum_type() -> None:
    edge = Edge(
        source_id="a.py::f",
        target_id="a.py::g",
        type=EdgeType.CALLS,
        metadata={"line": 12},
    )
    assert edge.type is EdgeType.CALLS
    assert edge.source_id == "a.py::f"
    assert edge.target_id == "a.py::g"
    assert edge.metadata["line"] == 12


def test_make_node_id() -> None:
    assert (
        make_node_id("src/auth.py", "LoginHandler.login")
        == "src/auth.py::LoginHandler.login"
    )


def test_node_round_trip() -> None:
    node = _sample_node()
    restored = Node.model_validate(node.model_dump())
    assert restored == node


def test_edge_round_trip() -> None:
    edge = Edge(source_id="a.py::f", target_id="a.py::g", type=EdgeType.CALLS)
    restored = Edge.model_validate(edge.model_dump())
    assert restored == edge


def test_node_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        Node(
            id="a.py::f",
            type=NodeType.FUNCTION,
            name="f",
            qualified_name="f",
            file_path="a.py",
            language="python",
            start_line=1,
            end_line=2,
            bogus="nope",  # type: ignore[call-arg]
        )


def test_edge_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        Edge(
            source_id="a.py::f",
            target_id="a.py::g",
            type=EdgeType.CALLS,
            bogus="nope",  # type: ignore[call-arg]
        )


def test_build_graph_on_python_fixture() -> None:
    graph = build_graph(PY_FIXTURE)
    assert per_language_file_counts(graph) == {"python": 4}
    assert graph.has_node("models.py::User")
    assert graph.has_node("models.py::AdminUser")
    # INHERITS edge survives the merge into the MultiDiGraph.
    types = {
        data["type"]
        for _, _, data in graph.edges("models.py::AdminUser", data=True)
    }
    assert EdgeType.INHERITS.value in types


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    graph = build_graph(PY_FIXTURE)
    graph_path = tmp_path / ".codesentry" / "graph.pkl"
    save_graph(graph, graph_path, repo_path=PY_FIXTURE)

    loaded = load_graph(graph_path)
    assert loaded.number_of_nodes() == graph.number_of_nodes()
    assert loaded.number_of_edges() == graph.number_of_edges()

    meta = load_metadata(graph_path)
    assert meta["files_per_language"] == {"python": 4}
    assert meta["node_count"] == graph.number_of_nodes()
    assert meta["codesentry_version"]
