"""Tests for the Python adapter: parsing the sample fixture into universal nodes
and edges, and asserting classes/methods/functions, CONTAINS/INHERITS/intra-file
CALLS edges, decorator and import metadata, docstrings, signatures, and resilience
to malformed source."""

from pathlib import Path

from codesentry.graph.schema import EdgeType, Node, NodeType
from codesentry.languages.python import PythonAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "sample_python"


def _parse(filename: str) -> tuple[list[Node], list]:
    adapter = PythonAdapter()
    source = (FIXTURE / filename).read_bytes()
    return adapter.parse_file(Path(filename), source)


def _by_qname(nodes: list[Node]) -> dict[str, Node]:
    return {n.qualified_name: n for n in nodes}


def test_file_and_class_and_method_nodes() -> None:
    nodes, _ = _parse("models.py")
    by_q = _by_qname(nodes)
    assert by_q["models.py"].type is NodeType.FILE
    assert by_q["User"].type is NodeType.CLASS
    assert by_q["User.__init__"].type is NodeType.METHOD
    assert by_q["User.display_name"].type is NodeType.METHOD


def test_docstrings_and_signatures() -> None:
    nodes, _ = _parse("models.py")
    by_q = _by_qname(nodes)
    assert by_q["models.py"].docstring == "Domain models for the sample app."
    assert by_q["User"].docstring == "A user account."
    assert by_q["User.display_name"].docstring == "Return a friendly, title-cased name."
    assert by_q["User.display_name"].signature == "def display_name(self) -> str"
    assert by_q["User"].signature == "class User"
    assert by_q["AdminUser"].signature == "class AdminUser(User)"


def test_contains_edges() -> None:
    nodes, edges = _parse("models.py")
    contains = {
        (e.source_id, e.target_id) for e in edges if e.type is EdgeType.CONTAINS
    }
    assert ("models.py", "models.py::User") in contains
    assert ("models.py::User", "models.py::User.display_name") in contains


def test_inherits_edge_and_bases_metadata() -> None:
    nodes, edges = _parse("models.py")
    by_q = _by_qname(nodes)
    assert by_q["AdminUser"].metadata["bases"] == ["User"]
    inherits = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.INHERITS}
    assert ("models.py::AdminUser", "models.py::User") in inherits


def test_decorator_metadata() -> None:
    nodes, _ = _parse("service.py")
    by_q = _by_qname(nodes)
    assert by_q["make_default_service"].metadata["decorators"] == ["lru_cache"]


def test_import_metadata() -> None:
    nodes, _ = _parse("service.py")
    file_node = _by_qname(nodes)["service.py"]
    imports = file_node.metadata["imports"]
    modules = {m for entry in imports for m in entry["modules"]}
    assert {"functools", "models", "repository"} <= modules
    models_entry = next(e for e in imports if e["modules"] == ["models"])
    assert models_entry["names"] == ["User"]


def test_intrafile_call_edge_resolves_to_local_definition() -> None:
    nodes, edges = _parse("utils.py")
    calls = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CALLS}
    assert ("utils.py::slugify", "utils.py::_normalize") in calls


def test_call_to_class_constructor_resolves_intrafile() -> None:
    nodes, edges = _parse("service.py")
    calls = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CALLS}
    assert ("service.py::make_default_service", "service.py::UserService") in calls


def test_unresolved_calls_are_stashed_in_metadata() -> None:
    nodes, _ = _parse("service.py")
    register = _by_qname(nodes)["UserService.register"]
    call_names = {c["name"] for c in register.metadata["calls"]}
    # Cross-file callees stay in metadata; no edge is emitted for them here.
    assert {"User", "add"} <= call_names


def test_malformed_source_does_not_crash() -> None:
    adapter = PythonAdapter()
    nodes, edges = adapter.parse_file(Path("broken.py"), b"def broken(:\n    retur\n")
    assert isinstance(nodes, list)
    assert isinstance(edges, list)
