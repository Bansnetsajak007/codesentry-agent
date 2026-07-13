"""Tests for the TypeScript adapter: interfaces and type aliases as CLASS nodes
tagged by kind, IMPLEMENTS/INHERITS edges, method and interface-method nodes,
decorator and generics metadata, `import type` metadata, typed signatures,
intra-file CALLS, TSX parsing, and resilience to malformed source."""

from pathlib import Path

from codesentry.graph.schema import EdgeType, Node, NodeType
from codesentry.languages.typescript import TypeScriptAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "sample_ts"


def _parse(filename: str) -> tuple[list[Node], list]:
    adapter = TypeScriptAdapter()
    source = (FIXTURE / filename).read_bytes()
    return adapter.parse_file(Path(filename), source)


def _by_qname(nodes: list[Node]) -> dict[str, Node]:
    return {n.qualified_name: n for n in nodes}


def test_interface_and_type_alias_as_class_nodes() -> None:
    nodes, _ = _parse("models.ts")
    by_q = _by_qname(nodes)
    assert by_q["Named"].type is NodeType.CLASS
    assert by_q["Named"].metadata["kind"] == "interface"
    assert by_q["UserId"].type is NodeType.CLASS
    assert by_q["UserId"].metadata["kind"] == "type"
    assert by_q["User"].metadata["kind"] == "class"


def test_interface_method_and_class_method_nodes() -> None:
    nodes, _ = _parse("models.ts")
    by_q = _by_qname(nodes)
    assert by_q["Named.displayName"].type is NodeType.METHOD
    assert by_q["User.displayName"].type is NodeType.METHOD


def test_implements_and_inherits_edges() -> None:
    _, edges = _parse("models.ts")
    implements = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.IMPLEMENTS}
    inherits = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.INHERITS}
    assert ("models.ts::User", "models.ts::Named") in implements
    assert ("models.ts::AdminUser", "models.ts::User") in inherits
    assert ("models.ts::AdminUser", "models.ts::Named") in implements


def test_decorator_and_generics_metadata() -> None:
    nodes, _ = _parse("models.ts")
    by_q = _by_qname(nodes)
    assert by_q["User"].metadata["decorators"] == ["sealed"]
    assert by_q["Box"].metadata["type_parameters"] == "<T>"


def test_typed_signatures() -> None:
    nodes, _ = _parse("repository.ts")
    by_q = _by_qname(nodes)
    assert by_q["UserRepository.count"].signature == "count(): number"
    assert by_q["UserRepository.get"].signature == "get(name: string): User | undefined"


def test_import_type_metadata() -> None:
    nodes, _ = _parse("repository.ts")
    imports = _by_qname(nodes)["repository.ts"].metadata["imports"]
    entry = next(i for i in imports if i["modules"] == ["./models"])
    assert entry["type"] is True
    assert entry["names"] == ["User"]

    service_nodes, _ = _parse("service.ts")
    svc_imports = _by_qname(service_nodes)["service.ts"].metadata["imports"]
    assert all(i["type"] is False for i in svc_imports)


def test_intrafile_call_edges() -> None:
    _, util_edges = _parse("utils.ts")
    util_calls = {(e.source_id, e.target_id) for e in util_edges if e.type is EdgeType.CALLS}
    assert ("utils.ts::slugify", "utils.ts::normalize") in util_calls

    _, svc_edges = _parse("service.ts")
    svc_calls = {(e.source_id, e.target_id) for e in svc_edges if e.type is EdgeType.CALLS}
    assert ("service.ts::makeDefaultService", "service.ts::UserService") in svc_calls


def test_tsx_file_is_parsed() -> None:
    nodes, _ = _parse("widget.tsx")
    by_q = _by_qname(nodes)
    assert by_q["Greeting"].type is NodeType.FUNCTION
    assert by_q["Greeting"].docstring == "Render a greeting for a user."


def test_malformed_source_does_not_crash() -> None:
    adapter = TypeScriptAdapter()
    nodes, edges = adapter.parse_file(
        Path("broken.ts"), b"class { function ( : number ;;; interface"
    )
    assert isinstance(nodes, list)
    assert isinstance(edges, list)
