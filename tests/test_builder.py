"""Tests for the builder's cross-file resolution pass: IMPORTS edges, cross-file
CALLS, cross-file INHERITS/IMPLEMENTS, Go package-level visibility, Java same-
package heritage, the import-scoped + global-unique linking strategy (including a
cross-language link over a shared name), ambiguity dropping, and the run summary."""

from pathlib import Path

from codesentry.graph.builder import build_graph
from codesentry.graph.schema import EdgeType, NodeType
from codesentry.graph.store import per_language_file_counts

FIXTURES = Path(__file__).parent / "fixtures"


def _edges(graph, edge_type: EdgeType) -> set[tuple[str, str]]:
    return {
        (u, v) for u, v, d in graph.edges(data=True) if d["type"] == edge_type.value
    }


def test_python_cross_file_imports_and_calls() -> None:
    graph = build_graph(FIXTURES / "sample_python")
    imports = _edges(graph, EdgeType.IMPORTS)
    calls = _edges(graph, EdgeType.CALLS)
    assert ("service.py", "repository.py") in imports
    assert ("service.py", "models.py") in imports
    assert (
        "service.py::UserService.register",
        "repository.py::UserRepository.add",
    ) in calls
    assert (
        "service.py::UserService.headcount",
        "repository.py::UserRepository.count",
    ) in calls


def test_typescript_cross_file_imports_and_calls() -> None:
    graph = build_graph(FIXTURES / "sample_ts")
    imports = _edges(graph, EdgeType.IMPORTS)
    calls = _edges(graph, EdgeType.CALLS)
    assert ("service.ts", "repository.ts") in imports
    assert ("widget.tsx", "models.ts") in imports
    assert (
        "service.ts::UserService.headcount",
        "repository.ts::UserRepository.count",
    ) in calls


def test_go_package_level_visibility_calls() -> None:
    # Go files in the same directory resolve calls without an explicit import.
    graph = build_graph(FIXTURES / "sample_go")
    calls = _edges(graph, EdgeType.CALLS)
    assert (
        "service.go::UserService.Register",
        "repository.go::UserRepository.Add",
    ) in calls
    assert ("service.go::MakeDefaultService", "repository.go::NewUserRepository") in calls


def test_java_cross_file_heritage_and_calls() -> None:
    graph = build_graph(FIXTURES / "sample_java")
    inherits = _edges(graph, EdgeType.INHERITS)
    implements = _edges(graph, EdgeType.IMPLEMENTS)
    calls = _edges(graph, EdgeType.CALLS)
    assert ("AdminUser.java::AdminUser", "User.java::User") in inherits
    assert ("User.java::User", "Named.java::Named") in implements
    assert (
        "UserService.java::UserService.headcount",
        "UserRepository.java::UserRepository.count",
    ) in calls


def test_mixed_repo_merges_and_links_across_languages() -> None:
    graph = build_graph(FIXTURES / "sample_mixed")
    assert per_language_file_counts(graph) == {"python": 1, "typescript": 1}
    # The TS caller links to the uniquely-named Python function via global-unique.
    calls = _edges(graph, EdgeType.CALLS)
    assert ("client.ts::loadUser", "server.py::getUser") in calls


def test_ambiguous_call_is_dropped(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from a import helper\nfrom b import helper\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    graph = build_graph(tmp_path)
    calls = _edges(graph, EdgeType.CALLS)
    assert not any(
        u == "main.py::run" and v.endswith("::helper") for (u, v) in calls
    )


def test_vendor_and_build_dirs_are_skipped_without_gitignore(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text(
        "export function main(): void {}\n", encoding="utf-8"
    )
    nested = tmp_path / "node_modules" / "pkg" / "node_modules" / "dep"
    nested.mkdir(parents=True)
    (nested / "index.d.ts").write_text(
        "export declare function x(): void;\n", encoding="utf-8"
    )
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("function y(){}\n", encoding="utf-8")

    graph = build_graph(tmp_path)
    file_paths = {
        data["node"].file_path
        for _, data in graph.nodes(data=True)
        if data["node"].type is NodeType.FILE
    }
    assert "src/app.ts" in file_paths
    assert not any("node_modules" in path for path in file_paths)
    assert not any(path.startswith("dist/") for path in file_paths)


def test_summary_is_attached() -> None:
    graph = build_graph(FIXTURES / "sample_python")
    summary = graph.graph["summary"]
    assert summary["files_indexed"] == 4
    assert summary["files_skipped"] == 1  # the .gitkeep has no adapter
    assert summary["files_with_parse_errors"] == 0
    assert "unresolved_calls" in summary
