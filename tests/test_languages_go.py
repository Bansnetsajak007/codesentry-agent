"""Tests for the Go adapter: structs and interfaces as CLASS nodes tagged by kind,
methods recorded under their receiver type via CONTAINS, interface method nodes,
top-level functions, godoc docstrings, intra-file CALLS, import metadata, embedding
modeled as INHERITS, the Phase 1 no-IMPLEMENTS guarantee, and malformed resilience."""

from pathlib import Path

from codesentry.graph.schema import EdgeType, Node, NodeType
from codesentry.languages.go import GoAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "sample_go"


def _parse(filename: str) -> tuple[list[Node], list]:
    adapter = GoAdapter()
    source = (FIXTURE / filename).read_bytes()
    return adapter.parse_file(Path(filename), source)


def _by_qname(nodes: list[Node]) -> dict[str, Node]:
    return {n.qualified_name: n for n in nodes}


def test_struct_and_interface_class_nodes() -> None:
    nodes, _ = _parse("models.go")
    by_q = _by_qname(nodes)
    assert by_q["User"].type is NodeType.CLASS
    assert by_q["User"].metadata["kind"] == "struct"
    assert by_q["Named"].metadata["kind"] == "interface"
    assert by_q["User"].metadata["exported"] is True


def test_method_recorded_under_receiver_struct() -> None:
    nodes, edges = _parse("models.go")
    by_q = _by_qname(nodes)
    method = by_q["User.DisplayName"]
    assert method.type is NodeType.METHOD
    assert method.metadata["receiver_type"] == "User"
    assert method.metadata["receiver_pointer"] is True
    contains = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CONTAINS}
    assert ("models.go::User", "models.go::User.DisplayName") in contains


def test_interface_method_node() -> None:
    nodes, edges = _parse("models.go")
    by_q = _by_qname(nodes)
    assert by_q["Named.DisplayName"].type is NodeType.METHOD
    contains = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CONTAINS}
    assert ("models.go::Named", "models.go::Named.DisplayName") in contains


def test_top_level_function_and_godoc() -> None:
    nodes, _ = _parse("repository.go")
    by_q = _by_qname(nodes)
    fn = by_q["NewUserRepository"]
    assert fn.type is NodeType.FUNCTION
    assert fn.docstring == "NewUserRepository builds an empty repository."
    assert fn.signature == "func NewUserRepository() *UserRepository"


def test_embedding_modeled_as_inherits() -> None:
    nodes, edges = _parse("models.go")
    by_q = _by_qname(nodes)
    assert by_q["AdminUser"].metadata["embeds"] == ["User"]
    inherits = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.INHERITS}
    assert ("models.go::AdminUser", "models.go::User") in inherits


def test_intrafile_call_edge() -> None:
    _, edges = _parse("utils.go")
    calls = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CALLS}
    assert ("utils.go::Slugify", "utils.go::normalize") in calls


def test_import_and_package_metadata() -> None:
    nodes, _ = _parse("models.go")
    file_node = _by_qname(nodes)["models.go"]
    assert file_node.metadata["package"] == "sample"
    modules = {m for e in file_node.metadata["imports"] for m in e["modules"]}
    assert "strings" in modules


def test_no_implements_edges_in_phase1() -> None:
    for filename in ["models.go", "repository.go", "service.go", "utils.go"]:
        _, edges = _parse(filename)
        assert not [e for e in edges if e.type is EdgeType.IMPLEMENTS]


def test_malformed_source_does_not_crash() -> None:
    adapter = GoAdapter()
    nodes, edges = adapter.parse_file(Path("broken.go"), b"package p\nfunc ( { type struct")
    assert isinstance(nodes, list)
    assert isinstance(edges, list)
