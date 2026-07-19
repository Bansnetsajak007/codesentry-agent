"""LanguageAdapter for Python, using the tree-sitter-python grammar to extract
functions, classes and their methods, call sites, imports, and inheritance,
recording decorators in node metadata. Cross-file relations (imports and calls to
names defined elsewhere) are stashed in node metadata for the builder to resolve;
only edges with real in-file targets (CONTAINS, INHERITS, intra-file CALLS) are
emitted here."""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Node as TSNode, Parser

from codesentry.graph.schema import Edge, EdgeType, Node, NodeType, make_node_id
from codesentry.languages.base import ImportIndex, LanguageAdapter, register_adapter

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tree_sitter_python.language())
_PARSER = Parser(_LANGUAGE)

_STRING_PREFIXES = "rRbBuUfF"
_QUOTES = ('"""', "'''", '"', "'")


class PythonAdapter(LanguageAdapter):
    """Parses Python source into universal graph nodes and edges."""

    language_name = "python"
    file_extensions = {".py", ".pyi"}

    def resolve_import(self, module: str, importer: str, index: ImportIndex) -> str | None:
        rel = "/".join(p for p in module.strip().split(".") if p)
        if not rel:
            return None
        candidates = [f"{rel}.py", f"{rel}/__init__.py"]
        parent = Path(importer).parent.as_posix()
        if parent not in ("", "."):
            candidates += [f"{parent}/{rel}.py", f"{parent}/{rel}/__init__.py"]
        for candidate in candidates:
            if candidate in index.paths:
                return candidate
        return super().resolve_import(module, importer, index)

    def parse_file(self, path: Path, source: bytes) -> tuple[list[Node], list[Edge]]:
        file_path = path.as_posix()
        nodes: list[Node] = []
        edges: list[Edge] = []
        try:
            tree = _PARSER.parse(source)
            root = tree.root_node
            if root.has_error:
                logger.warning("Parse errors in %s; emitting partial results", file_path)

            file_node = Node(
                id=file_path,
                type=NodeType.FILE,
                name=path.name,
                qualified_name=file_path,
                file_path=file_path,
                language=self.language_name,
                start_line=1,
                end_line=root.end_point[0] + 1,
                docstring=_module_docstring(root, source),
                metadata={
                    "imports": _collect_imports(root, source),
                    "parse_error": root.has_error,
                },
            )
            nodes.append(file_node)

            name_to_ids: dict[str, list[str]] = {}
            self._collect_definitions(
                root, source, file_path, "", file_path, nodes, edges, name_to_ids
            )
            self._resolve_local_edges(nodes, edges, name_to_ids)
        except Exception:  # never crash the indexer on a single bad file
            logger.warning("Failed to parse %s; skipping", file_path, exc_info=True)
            return nodes, edges
        return nodes, edges

    def _collect_definitions(
        self,
        container: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        """Walk the direct children of ``container`` for class/function definitions,
        emitting nodes and CONTAINS edges and recursing into class bodies. ``prefix``
        is the dotted qualified-name prefix (empty at module level)."""

        for child in container.named_children:
            decorators: list[str] = []
            defn = child
            if child.type == "decorated_definition":
                decorators = _decorator_names(child, source)
                inner = child.child_by_field_name("definition")
                if inner is None:
                    continue
                defn = inner

            if defn.type == "function_definition":
                self._add_function(
                    defn, source, file_path, prefix, container_id, decorators,
                    nodes, edges, name_to_ids,
                )
            elif defn.type == "class_definition":
                self._add_class(
                    defn, source, file_path, prefix, container_id, decorators,
                    nodes, edges, name_to_ids,
                )

    def _add_function(
        self,
        defn: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        decorators: list[str],
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        name = _field_text(defn, "name", source)
        if name is None:
            return
        qualified = f"{prefix}{name}"
        node_id = make_node_id(file_path, qualified)
        node = Node(
            id=node_id,
            type=NodeType.METHOD if prefix else NodeType.FUNCTION,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=defn.start_point[0] + 1,
            end_line=defn.end_point[0] + 1,
            signature=_signature(defn, source),
            docstring=_definition_docstring(defn, source),
            metadata={"decorators": decorators, "calls": _collect_calls(defn, source)},
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )

    def _add_class(
        self,
        defn: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        decorators: list[str],
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        name = _field_text(defn, "name", source)
        if name is None:
            return
        qualified = f"{prefix}{name}"
        node_id = make_node_id(file_path, qualified)
        bases = _base_names(defn, source)
        node = Node(
            id=node_id,
            type=NodeType.CLASS,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=defn.start_point[0] + 1,
            end_line=defn.end_point[0] + 1,
            signature=_signature(defn, source),
            docstring=_definition_docstring(defn, source),
            metadata={"decorators": decorators, "bases": bases},
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )

        body = defn.child_by_field_name("body")
        if body is not None:
            self._collect_definitions(
                body, source, file_path, f"{qualified}.", node_id,
                nodes, edges, name_to_ids,
            )

    def _resolve_local_edges(
        self,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        """Emit INHERITS and intra-file CALLS edges for names that resolve to exactly
        one definition in this file. Ambiguous names are left for the builder."""

        types_by_id = {node.id: node.type for node in nodes}

        def unique_target(name: str) -> str | None:
            ids = name_to_ids.get(name, [])
            return ids[0] if len(ids) == 1 else None

        def unique_call_target(call: dict[str, object]) -> str | None:
            # Member calls (obj.method()) may only bind to methods or classes;
            # binding them to a same-named top-level function would be wrong.
            ids = name_to_ids.get(str(call["name"]), [])
            if call.get("member"):
                ids = [
                    i for i in ids
                    if types_by_id[i] in (NodeType.METHOD, NodeType.CLASS)
                ]
            return ids[0] if len(ids) == 1 else None

        for node in nodes:
            if node.type is NodeType.CLASS:
                for base in node.metadata.get("bases", []):
                    target = unique_target(base)
                    if target is not None:
                        edges.append(
                            Edge(source_id=node.id, target_id=target, type=EdgeType.INHERITS)
                        )
            if node.type in (NodeType.FUNCTION, NodeType.METHOD):
                for call in node.metadata.get("calls", []):
                    target = unique_call_target(call)
                    if target is not None and target != node.id:
                        edges.append(
                            Edge(
                                source_id=node.id,
                                target_id=target,
                                type=EdgeType.CALLS,
                                metadata={"line": call["line"]},
                            )
                        )


def _walk(node: TSNode):  # type: ignore[no-untyped-def]
    """Yield ``node`` and all of its descendants, depth-first."""
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _field_text(node: TSNode, field: str, source: bytes) -> str | None:
    child = node.child_by_field_name(field)
    if child is None:
        return None
    return source[child.start_byte : child.end_byte].decode("utf-8", "replace")


def _signature(defn: TSNode, source: bytes) -> str | None:
    """Reconstruct the definition header (excluding the trailing colon and body) by
    slicing from the node start to the end of the last header field, so intervening
    comments or newlines are never captured."""

    if defn.type == "function_definition":
        end = (
            defn.child_by_field_name("return_type")
            or defn.child_by_field_name("parameters")
            or defn.child_by_field_name("name")
        )
    elif defn.type == "class_definition":
        end = defn.child_by_field_name("superclasses") or defn.child_by_field_name("name")
    else:
        end = None
    if end is None:
        return None
    return source[defn.start_byte : end.end_byte].decode("utf-8", "replace").strip() or None


def _decorator_names(decorated: TSNode, source: bytes) -> list[str]:
    names: list[str] = []
    for child in decorated.named_children:
        if child.type == "decorator":
            text = source[child.start_byte : child.end_byte].decode("utf-8", "replace")
            names.append(text.lstrip("@").split("(", 1)[0].strip())
    return names


def _base_names(defn: TSNode, source: bytes) -> list[str]:
    supers = defn.child_by_field_name("superclasses")
    if supers is None:
        return []
    names: list[str] = []
    for child in supers.named_children:
        if child.type in ("identifier", "attribute", "keyword_argument"):
            text = source[child.start_byte : child.end_byte].decode("utf-8", "replace")
            names.append(text)
    return names


def _collect_calls(defn: TSNode, source: bytes) -> list[dict[str, object]]:
    body = defn.child_by_field_name("body")
    if body is None:
        return []
    calls: list[dict[str, object]] = []
    for node in _walk(body):
        if node.type != "call":
            continue
        func = node.child_by_field_name("function")
        if func is None:
            continue
        if func.type == "identifier":
            name = source[func.start_byte : func.end_byte].decode("utf-8", "replace")
            calls.append({"name": name, "line": node.start_point[0] + 1})
        elif func.type == "attribute":
            attr = func.child_by_field_name("attribute")
            if attr is None:
                continue
            name = source[attr.start_byte : attr.end_byte].decode("utf-8", "replace")
            entry: dict[str, object] = {
                "name": name,
                "line": node.start_point[0] + 1,
                "member": True,
            }
            obj = func.child_by_field_name("object")
            if obj is not None and obj.type == "identifier":
                entry["recv"] = source[obj.start_byte : obj.end_byte].decode(
                    "utf-8", "replace"
                )
            calls.append(entry)
    return calls


def _collect_imports(root: TSNode, source: bytes) -> list[dict[str, object]]:
    imports: list[dict[str, object]] = []
    for child in root.named_children:
        if child.type == "import_statement":
            modules = [
                source[n.start_byte : n.end_byte].decode("utf-8", "replace")
                for n in child.named_children
                if n.type in ("dotted_name", "aliased_import")
            ]
            imports.append(
                {"modules": modules, "names": [], "line": child.start_point[0] + 1}
            )
        elif child.type == "import_from_statement":
            module_node = child.child_by_field_name("module_name")
            module = (
                source[module_node.start_byte : module_node.end_byte].decode(
                    "utf-8", "replace"
                )
                if module_node is not None
                else ""
            )
            module_span = (
                (module_node.start_byte, module_node.end_byte)
                if module_node is not None
                else None
            )
            names: list[str] = []
            for n in child.named_children:
                if module_span is not None and (n.start_byte, n.end_byte) == module_span:
                    continue
                if n.type in ("dotted_name", "identifier", "aliased_import"):
                    names.append(
                        source[n.start_byte : n.end_byte].decode("utf-8", "replace")
                    )
                elif n.type == "wildcard_import":
                    names.append("*")
            imports.append(
                {"modules": [module], "names": names, "line": child.start_point[0] + 1}
            )
    return imports


def _module_docstring(root: TSNode, source: bytes) -> str | None:
    if not root.named_children:
        return None
    return _docstring_from_statement(root.named_children[0], source)


def _definition_docstring(defn: TSNode, source: bytes) -> str | None:
    body = defn.child_by_field_name("body")
    if body is None or not body.named_children:
        return None
    return _docstring_from_statement(body.named_children[0], source)


def _docstring_from_statement(stmt: TSNode, source: bytes) -> str | None:
    if stmt.type != "expression_statement" or not stmt.named_children:
        return None
    string_node = stmt.named_children[0]
    if string_node.type != "string":
        return None
    raw = source[string_node.start_byte : string_node.end_byte].decode("utf-8", "replace")
    return _clean_docstring(raw)


def _clean_docstring(raw: str) -> str:
    s = raw.strip()
    idx = 0
    while idx < len(s) and s[idx] in _STRING_PREFIXES:
        idx += 1
    s = s[idx:]
    for quote in _QUOTES:
        if s.startswith(quote) and s.endswith(quote) and len(s) >= 2 * len(quote):
            s = s[len(quote) : -len(quote)]
            break
    return textwrap.dedent(s).strip()


register_adapter(PythonAdapter())
