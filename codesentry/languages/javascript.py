"""LanguageAdapter for JavaScript, using the tree-sitter-javascript grammar to
extract class and top-level function declarations, call sites, and
export/import/require statements. Variable-assigned arrow functions, function
expressions, and class expressions are captured as FUNCTION/CLASS nodes named by
their variable. Cross-file relations are stashed in node metadata for the builder;
only real in-file targets (CONTAINS, INHERITS, intra-file CALLS) are emitted here."""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_javascript
from tree_sitter import Language, Node as TSNode, Parser

from codesentry.graph.schema import Edge, EdgeType, Node, NodeType, make_node_id
from codesentry.languages.base import LanguageAdapter, register_adapter

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tree_sitter_javascript.language())
_PARSER = Parser(_LANGUAGE)

_FUNCTION_VALUES = ("arrow_function", "function_expression")


class JavaScriptAdapter(LanguageAdapter):
    """Parses JavaScript source into universal graph nodes and edges."""

    language_name = "javascript"
    file_extensions = {".js", ".jsx", ".mjs", ".cjs"}

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
                metadata={"imports": _collect_imports(root, source)},
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
        for child in container.named_children:
            jsdoc = _jsdoc_before(child, source)
            decl = child
            if child.type == "export_statement":
                inner = child.child_by_field_name("declaration")
                if inner is None:
                    continue
                decl = inner

            if decl.type == "function_declaration":
                name = _field_text(decl, "name", source)
                if name is not None:
                    self._add_function(
                        decl, decl, name, prefix, file_path, container_id, jsdoc,
                        source, nodes, edges, name_to_ids,
                    )
            elif decl.type == "class_declaration":
                name = _field_text(decl, "name", source)
                if name is not None:
                    self._add_class(
                        decl, name, prefix, file_path, container_id, jsdoc,
                        source, nodes, edges, name_to_ids,
                    )
            elif decl.type in ("lexical_declaration", "variable_declaration"):
                self._collect_declarators(
                    decl, source, file_path, prefix, container_id, jsdoc,
                    nodes, edges, name_to_ids,
                )

    def _collect_declarators(
        self,
        decl: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        jsdoc: str | None,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        for declarator in decl.named_children:
            if declarator.type != "variable_declarator":
                continue
            name = _field_text(declarator, "name", source)
            value = declarator.child_by_field_name("value")
            if name is None or value is None:
                continue
            if value.type in _FUNCTION_VALUES:
                self._add_function(
                    declarator, value, name, prefix, file_path, container_id, jsdoc,
                    source, nodes, edges, name_to_ids,
                )
            elif value.type == "class":
                self._add_class(
                    value, name, prefix, file_path, container_id, jsdoc,
                    source, nodes, edges, name_to_ids, span=declarator,
                )

    def _add_function(
        self,
        span: TSNode,
        def_node: TSNode,
        name: str,
        prefix: str,
        file_path: str,
        container_id: str,
        jsdoc: str | None,
        source: bytes,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        qualified = f"{prefix}{name}"
        node_id = make_node_id(file_path, qualified)
        node = Node(
            id=node_id,
            type=NodeType.METHOD if prefix else NodeType.FUNCTION,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            signature=_function_signature(def_node, name, source),
            docstring=jsdoc,
            metadata={"calls": _collect_calls(def_node, source)},
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )

    def _add_class(
        self,
        class_node: TSNode,
        name: str,
        prefix: str,
        file_path: str,
        container_id: str,
        jsdoc: str | None,
        source: bytes,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
        span: TSNode | None = None,
    ) -> None:
        span = span or class_node
        qualified = f"{prefix}{name}"
        node_id = make_node_id(file_path, qualified)
        bases = _base_names(class_node, source)
        node = Node(
            id=node_id,
            type=NodeType.CLASS,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            signature=_class_signature(class_node, name, source),
            docstring=jsdoc,
            metadata={"bases": bases},
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )

        body = class_node.child_by_field_name("body")
        if body is not None:
            self._add_methods(
                body, source, file_path, f"{qualified}.", node_id,
                nodes, edges, name_to_ids,
            )

    def _add_methods(
        self,
        body: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        for member in body.named_children:
            if member.type != "method_definition":
                continue
            name = _field_text(member, "name", source)
            if name is None:
                continue
            self._add_function(
                member, member, name, prefix, file_path, container_id,
                _jsdoc_before(member, source), source, nodes, edges, name_to_ids,
            )

    def _resolve_local_edges(
        self,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        def unique_target(name: str) -> str | None:
            ids = name_to_ids.get(name, [])
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
                    target = unique_target(call["name"])
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
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _text(node: TSNode, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _field_text(node: TSNode, field: str, source: bytes) -> str | None:
    child = node.child_by_field_name(field)
    return _text(child, source) if child is not None else None


def _function_signature(def_node: TSNode, name: str, source: bytes) -> str | None:
    params = def_node.child_by_field_name("parameters")
    params_text = _text(params, source) if params is not None else "()"
    if def_node.type == "function_declaration":
        return f"function {name}{params_text}"
    return f"{name}{params_text}"


def _class_signature(class_node: TSNode, name: str, source: bytes) -> str:
    heritage = _child_of_type(class_node, "class_heritage")
    if heritage is not None:
        return f"class {name} {_text(heritage, source)}".strip()
    return f"class {name}"


def _base_names(class_node: TSNode, source: bytes) -> list[str]:
    heritage = _child_of_type(class_node, "class_heritage")
    if heritage is None:
        return []
    return [
        _text(child, source)
        for child in heritage.named_children
        if child.type in ("identifier", "member_expression")
    ]


def _collect_calls(node: TSNode, source: bytes) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for sub in _walk(node):
        if sub.type == "call_expression":
            callee = sub.child_by_field_name("function")
        elif sub.type == "new_expression":
            callee = sub.child_by_field_name("constructor")
        else:
            continue
        if callee is None:
            continue
        if callee.type == "identifier":
            name = _text(callee, source)
        elif callee.type == "member_expression":
            prop = callee.child_by_field_name("property")
            if prop is None:
                continue
            name = _text(prop, source)
        else:
            continue
        calls.append({"name": name, "line": sub.start_point[0] + 1})
    return calls


def _collect_imports(root: TSNode, source: bytes) -> list[dict[str, object]]:
    imports: list[dict[str, object]] = []
    for child in root.named_children:
        if child.type == "import_statement":
            source_node = child.child_by_field_name("source")
            module = _string_value(source_node, source) if source_node else ""
            names = _import_clause_names(child, source)
            imports.append(
                {
                    "modules": [module],
                    "names": names,
                    "line": child.start_point[0] + 1,
                    "kind": "esm",
                }
            )
        elif child.type in ("lexical_declaration", "variable_declaration"):
            imports.extend(_require_imports(child, source))
    return imports


def _import_clause_names(import_stmt: TSNode, source: bytes) -> list[str]:
    clause = _child_of_type(import_stmt, "import_clause")
    if clause is None:
        return []
    names: list[str] = []
    for child in clause.named_children:
        if child.type == "identifier":
            names.append(_text(child, source))
        elif child.type == "named_imports":
            for spec in child.named_children:
                if spec.type == "import_specifier":
                    field = spec.child_by_field_name("name")
                    if field is not None:
                        names.append(_text(field, source))
        elif child.type == "namespace_import":
            ident = _child_of_type(child, "identifier")
            if ident is not None:
                names.append(_text(ident, source))
    return names


def _require_imports(decl: TSNode, source: bytes) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for declarator in decl.named_children:
        if declarator.type != "variable_declarator":
            continue
        value = declarator.child_by_field_name("value")
        if value is None or value.type != "call_expression":
            continue
        func = value.child_by_field_name("function")
        if func is None or _text(func, source) != "require":
            continue
        args = value.child_by_field_name("arguments")
        module = ""
        if args is not None:
            for arg in args.named_children:
                if arg.type == "string":
                    module = _string_value(arg, source)
                    break
        name = _field_text(declarator, "name", source) or ""
        results.append(
            {
                "modules": [module],
                "names": [name] if name else [],
                "line": decl.start_point[0] + 1,
                "kind": "require",
            }
        )
    return results


def _string_value(node: TSNode, source: bytes) -> str:
    text = _text(node, source)
    if len(text) >= 2 and text[0] in "\"'`":
        return text[1:-1]
    return text


def _child_of_type(node: TSNode, type_name: str) -> TSNode | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


def _jsdoc_before(node: TSNode, source: bytes) -> str | None:
    prev = node.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    raw = _text(prev, source)
    if not raw.startswith("/**"):
        return None
    return _clean_jsdoc(raw)


def _clean_jsdoc(raw: str) -> str:
    inner = raw
    if inner.startswith("/**"):
        inner = inner[3:]
    if inner.endswith("*/"):
        inner = inner[:-2]
    lines = []
    for line in inner.splitlines():
        stripped = line.strip()
        if stripped.startswith("*"):
            stripped = stripped[1:].strip()
        lines.append(stripped)
    return "\n".join(lines).strip()


register_adapter(JavaScriptAdapter())
