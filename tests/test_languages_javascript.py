"""Tests for the JavaScript adapter: parsing the sample fixture into universal
nodes and edges (including arrow-function declarations), CONTAINS/INHERITS/
intra-file CALLS edges, ESM and require import metadata, JSDoc docstrings, and
resilience to malformed source. Also verifies the indexer handles a mixed .py/.js
repository."""

from pathlib import Path

from codesentry.graph.builder import build_graph
from codesentry.graph.schema import EdgeType, Node, NodeType
from codesentry.graph.store import per_language_file_counts
from codesentry.languages.javascript import JavaScriptAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "sample_js"
PY_FIXTURE = Path(__file__).parent / "fixtures" / "sample_python"


def _parse(filename: str) -> tuple[list[Node], list]:
    adapter = JavaScriptAdapter()
    source = (FIXTURE / filename).read_bytes()
    return adapter.parse_file(Path(filename), source)


def _by_qname(nodes: list[Node]) -> dict[str, Node]:
    return {n.qualified_name: n for n in nodes}


def test_class_and_method_nodes() -> None:
    nodes, _ = _parse("models.js")
    by_q = _by_qname(nodes)
    assert by_q["models.js"].type is NodeType.FILE
    assert by_q["User"].type is NodeType.CLASS
    assert by_q["User.constructor"].type is NodeType.METHOD
    assert by_q["User.displayName"].type is NodeType.METHOD


def test_arrow_function_captured_as_function() -> None:
    nodes, _ = _parse("service.js")
    by_q = _by_qname(nodes)
    assert by_q["makeDefaultService"].type is NodeType.FUNCTION
    assert by_q["makeDefaultService"].signature == "makeDefaultService()"


def test_jsdoc_docstrings() -> None:
    nodes, _ = _parse("models.js")
    assert _by_qname(nodes)["User.displayName"].docstring == (
        "Return a friendly, title-cased name."
    )
    service_nodes, _ = _parse("service.js")
    assert _by_qname(service_nodes)["makeDefaultService"].docstring == (
        "Build a default service instance."
    )


def test_contains_and_inherits_edges() -> None:
    nodes, edges = _parse("models.js")
    contains = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CONTAINS}
    assert ("models.js", "models.js::User") in contains
    assert ("models.js::User", "models.js::User.displayName") in contains
    inherits = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.INHERITS}
    assert ("models.js::AdminUser", "models.js::User") in inherits


def test_intrafile_call_edges() -> None:
    _, util_edges = _parse("utils.js")
    util_calls = {(e.source_id, e.target_id) for e in util_edges if e.type is EdgeType.CALLS}
    assert ("utils.js::slugify", "utils.js::normalize") in util_calls

    _, svc_edges = _parse("service.js")
    svc_calls = {(e.source_id, e.target_id) for e in svc_edges if e.type is EdgeType.CALLS}
    assert ("service.js::makeDefaultService", "service.js::UserService") in svc_calls


def test_esm_import_metadata() -> None:
    nodes, _ = _parse("service.js")
    imports = _by_qname(nodes)["service.js"].metadata["imports"]
    esm = [i for i in imports if i["kind"] == "esm"]
    modules = {m for e in esm for m in e["modules"]}
    assert modules == {"./models.js", "./repository.js"}


def test_require_import_metadata() -> None:
    nodes, _ = _parse("utils.js")
    imports = _by_qname(nodes)["utils.js"].metadata["imports"]
    require = next(i for i in imports if i["kind"] == "require")
    assert require["modules"] == ["path"]
    assert require["names"] == ["path"]


def test_malformed_source_does_not_crash() -> None:
    adapter = JavaScriptAdapter()
    nodes, edges = adapter.parse_file(Path("broken.js"), b"function ( { const ;;;")
    assert isinstance(nodes, list)
    assert isinstance(edges, list)


def test_indexer_handles_mixed_python_and_javascript(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes((PY_FIXTURE / "utils.py").read_bytes())
    (tmp_path / "b.js").write_bytes((FIXTURE / "utils.js").read_bytes())
    graph = build_graph(tmp_path)
    assert per_language_file_counts(graph) == {"javascript": 1, "python": 1}


def test_nested_functions_captured() -> None:
    src = (
        b"export default function Products() {\n"
        b"    const getProducts = async () => { return fetch('/api'); };\n"
        b"    function deleteProduct(id) { return getProducts(); }\n"
        b"    deleteProduct(1);\n"
        b"    return getProducts();\n"
        b"}\n"
    )
    nodes, edges = JavaScriptAdapter().parse_file(Path("app.js"), src)
    by_q = _by_qname(nodes)
    assert by_q["Products.getProducts"].type is NodeType.FUNCTION
    assert by_q["Products.deleteProduct"].type is NodeType.FUNCTION
    contains = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CONTAINS}
    assert ("app.js::Products", "app.js::Products.getProducts") in contains
    calls = {(e.source_id, e.target_id) for e in edges if e.type is EdgeType.CALLS}
    assert ("app.js::Products", "app.js::Products.deleteProduct") in calls
    assert ("app.js::Products.deleteProduct", "app.js::Products.getProducts") in calls
    parent_call_names = {c["name"] for c in by_q["Products"].metadata["calls"]}
    assert "fetch" not in parent_call_names  # belongs to the nested function


def test_top_level_route_callbacks_captured() -> None:
    src = (
        b"router.post('/insertproduct', async (req, res) => { res.json(1); });\n"
        b"app.listen(5000, () => {});\n"
    )
    nodes, _ = JavaScriptAdapter().parse_file(Path("router.js"), src)
    by_q = _by_qname(nodes)
    assert by_q["router.post(/insertproduct)"].type is NodeType.FUNCTION
    assert by_q["app.listen"].type is NodeType.FUNCTION


def test_member_calls_carry_member_flag_and_receiver() -> None:
    src = b"function run(res) { res.json(1); helper(); }\n"
    nodes, _ = JavaScriptAdapter().parse_file(Path("m.js"), src)
    calls = {str(c["name"]): c for c in _by_qname(nodes)["run"].metadata["calls"]}
    assert calls["json"].get("member") is True
    assert calls["json"].get("recv") == "res"
    assert "member" not in calls["helper"]
