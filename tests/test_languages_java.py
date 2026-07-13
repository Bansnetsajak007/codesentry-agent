"""Tests for the Java adapter: classes/interfaces/enums as CLASS nodes tagged by
kind, methods and constructors, nested classes with Outer.Inner qualified names,
annotation and javadoc metadata, import/package metadata, intra-file CALLS, and
INHERITS/IMPLEMENTS edges (captured as metadata cross-file, emitted as edges when
the target is in the same file), plus malformed-source resilience."""

from pathlib import Path

from codesentry.graph.schema import Edge, EdgeType, Node, NodeType
from codesentry.languages.java import JavaAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "sample_java"


def _parse(filename: str) -> tuple[list[Node], list[Edge]]:
    adapter = JavaAdapter()
    source = (FIXTURE / filename).read_bytes()
    return adapter.parse_file(Path(filename), source)


def _by_qname(nodes: list[Node]) -> dict[str, Node]:
    return {n.qualified_name: n for n in nodes}


def test_interface_and_class_kinds() -> None:
    named_nodes, _ = _parse("Named.java")
    assert _by_qname(named_nodes)["Named"].metadata["kind"] == "interface"
    user_nodes, _ = _parse("User.java")
    assert _by_qname(user_nodes)["User"].metadata["kind"] == "class"


def test_method_and_constructor_nodes() -> None:
    nodes, _ = _parse("User.java")
    by_q = _by_qname(nodes)
    assert by_q["User.User"].type is NodeType.METHOD  # constructor
    assert by_q["User.displayName"].type is NodeType.METHOD
    assert by_q["User.displayName"].signature == "String displayName()"


def test_annotation_and_javadoc_metadata() -> None:
    nodes, _ = _parse("User.java")
    by_q = _by_qname(nodes)
    assert by_q["User.displayName"].metadata["annotations"] == ["Override"]
    assert by_q["User.displayName"].docstring == "Return a title-cased name."
    assert by_q["User"].docstring == "A user with a name and email."


def test_nested_class_qualified_name_and_contains() -> None:
    nodes, edges = _parse("UserService.java")
    by_q = _by_qname(nodes)
    assert by_q["UserService.Stats"].type is NodeType.CLASS
    assert by_q["UserService.Stats.total"].type is NodeType.METHOD
    contains = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CONTAINS}
    assert ("UserService.java::UserService", "UserService.java::UserService.Stats") in contains
    assert (
        "UserService.java::UserService.Stats",
        "UserService.java::UserService.Stats.total",
    ) in contains


def test_extends_and_implements_captured_in_metadata() -> None:
    admin_nodes, _ = _parse("AdminUser.java")
    assert _by_qname(admin_nodes)["AdminUser"].metadata["bases"] == ["User"]
    user_nodes, _ = _parse("User.java")
    assert _by_qname(user_nodes)["User"].metadata["implements"] == ["Named"]


def test_import_and_package_metadata() -> None:
    nodes, _ = _parse("UserRepository.java")
    file_node = _by_qname(nodes)["UserRepository.java"]
    assert file_node.metadata["package"] == "com.example.sample"
    modules = {m for e in file_node.metadata["imports"] for m in e["modules"]}
    assert {"java.util.Map", "java.util.HashMap"} <= modules


def test_intrafile_call_edge() -> None:
    _, edges = _parse("Utils.java")
    calls = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CALLS}
    assert ("Utils.java::Utils.slugify", "Utils.java::Utils.normalize") in calls


def test_intrafile_inherits_and_implements_edges() -> None:
    source = b"""
    interface Shape { double area(); }
    class Circle implements Shape { public double area() { return 3.14; } }
    class Ball extends Circle { }
    """
    nodes, edges = JavaAdapter().parse_file(Path("Shapes.java"), source)
    implements = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.IMPLEMENTS}
    inherits = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.INHERITS}
    assert ("Shapes.java::Circle", "Shapes.java::Shape") in implements
    assert ("Shapes.java::Ball", "Shapes.java::Circle") in inherits


def test_malformed_source_does_not_crash() -> None:
    adapter = JavaAdapter()
    nodes, edges = adapter.parse_file(Path("Broken.java"), b"public class { void ( ;;; }")
    assert isinstance(nodes, list)
    assert isinstance(edges, list)
