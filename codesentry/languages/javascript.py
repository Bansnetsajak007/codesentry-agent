"""LanguageAdapter for JavaScript, using the tree-sitter-javascript grammar to
extract class and top-level function declarations, call sites, and
export/import/require statements. Variable-assigned arrow functions, function
expressions, and class expressions are captured as FUNCTION/CLASS nodes named by
their variable. Cross-file relations are stashed in node metadata for the builder;
only real in-file targets (CONTAINS, INHERITS, intra-file CALLS) are emitted here."""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path

import tree_sitter_javascript
from tree_sitter import Language, Node as TSNode, Parser

from codesentry.graph.schema import Edge, EdgeType, Node, NodeType, make_node_id
from codesentry.languages.base import ImportIndex, LanguageAdapter, register_adapter

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tree_sitter_javascript.language())
_PARSER = Parser(_LANGUAGE)

_FUNCTION_VALUES = ("arrow_function", "function_expression")
_JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs")


def _resolve_relative(module: str, importer: str, index: ImportIndex, extensions: tuple[str, ...]) -> str | None:
    """Resolve a relative ECMAScript import (``./x``/``../x``) against the importer's
    directory, trying each extension and an index file, or return None."""

    if not module.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), module))
    if base in index.paths:
        return base
    for ext in extensions:
        if f"{base}{ext}" in index.paths:
            return f"{base}{ext}"
        if f"{base}/index{ext}" in index.paths:
            return f"{base}/index{ext}"
    return None


class JavaScriptAdapter(LanguageAdapter):
    """Parses JavaScript source into universal graph nodes and edges."""

    language_name = "javascript"
    file_extensions = {".js", ".jsx", ".mjs", ".cjs"}

    def resolve_import(self, module: str, importer: str, index: ImportIndex) -> str | None:
        resolved = _resolve_relative(module, importer, index, _JS_EXTENSIONS)
        if resolved is not None:
            return resolved
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
            elif decl.type == "expression_statement" and container.type == "program":
                expr = decl.named_children[0] if decl.named_children else None
                if expr is not None and expr.type == "call_expression":
                    self._add_call_arg_callbacks(
                        expr, source, file_path, prefix, container_id,
                        nodes, edges, name_to_ids,
                    )

    def _add_call_arg_callbacks(
        self,
        call: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        """Capture function expressions passed as arguments of a top-level call
        (Express route handlers, listen callbacks) as FUNCTION nodes named after
        the call, e.g. ``router.post(/insertproduct)``."""

        args = call.child_by_field_name("arguments")
        base = _callback_base_name(call, source)
        if args is None or base is None:
            return
        for arg in args.named_children:
            if arg.type not in _FUNCTION_VALUES:
                continue
            name = base
            if name in name_to_ids:
                name = f"{base}@L{arg.start_point[0] + 1}"
            self._add_function(
                arg, arg, name, prefix, file_path, container_id, None,
                source, nodes, edges, name_to_ids,
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
        node_type: NodeType = NodeType.FUNCTION,
    ) -> None:
        qualified = f"{prefix}{name}"
        node_id = make_node_id(file_path, qualified)
        nested = _nested_def_subtrees(def_node)
        node = Node(
            id=node_id,
            type=node_type,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            signature=_function_signature(def_node, name, source),
            docstring=jsdoc,
            metadata={
                "calls": _collect_calls(
                    def_node, source, frozenset(n.id for n in nested)
                )
            },
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )
        # Functions defined inside this function (React handlers, helpers) become
        # their own nodes, contained by and qualified under this one.
        body = def_node.child_by_field_name("body")
        if body is not None and body.type == "statement_block":
            self._collect_definitions(
                body, source, file_path, f"{qualified}.", node_id,
                nodes, edges, name_to_ids,
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
                node_type=NodeType.METHOD,
            )

    def _resolve_local_edges(
        self,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
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


def _walk(node: TSNode, skip_ids: frozenset[int] = frozenset()):  # type: ignore[no-untyped-def]
    yield node
    for child in node.named_children:
        if child.id in skip_ids:
            continue
        yield from _walk(child, skip_ids)


def _callback_base_name(call: TSNode, source: bytes) -> str | None:
    """Derive a deterministic name for callbacks passed to ``call``: the callee
    (``router.post``) plus its first string argument (``/insertproduct``), giving
    ``router.post(/insertproduct)``. Returns None for unnameable callees."""

    callee = call.child_by_field_name("function")
    if callee is None:
        return None
    if callee.type == "identifier":
        name = _text(callee, source)
    elif callee.type == "member_expression":
        obj = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if prop is None:
            return None
        name = _text(prop, source)
        if obj is not None and obj.type == "identifier":
            name = f"{_text(obj, source)}.{name}"
    else:
        return None
    args = call.child_by_field_name("arguments")
    if args is not None:
        for arg in args.named_children:
            if arg.type == "string":
                return f"{name}({_string_value(arg, source)})"
    return name


def _nested_def_subtrees(def_node: TSNode) -> list[TSNode]:
    """Subtrees directly inside ``def_node``'s statement-block body that are
    captured as their own definition nodes, so their calls are attributed to the
    nested definition rather than to ``def_node``."""

    body = def_node.child_by_field_name("body")
    if body is None or body.type != "statement_block":
        return []
    found: list[TSNode] = []
    for child in body.named_children:
        if child.type in ("function_declaration", "class_declaration"):
            found.append(child)
        elif child.type in ("lexical_declaration", "variable_declaration"):
            for declarator in child.named_children:
                if declarator.type != "variable_declarator":
                    continue
                value = declarator.child_by_field_name("value")
                if value is not None and value.type in (*_FUNCTION_VALUES, "class"):
                    found.append(value)
    return found


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


def _collect_calls(
    node: TSNode, source: bytes, skip_ids: frozenset[int] = frozenset()
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for sub in _walk(node, skip_ids):
        if sub.type == "call_expression":
            callee = sub.child_by_field_name("function")
        elif sub.type == "new_expression":
            callee = sub.child_by_field_name("constructor")
        else:
            continue
        if callee is None:
            continue
        if callee.type == "identifier":
            calls.append({"name": _text(callee, source), "line": sub.start_point[0] + 1})
        elif callee.type == "member_expression":
            prop = callee.child_by_field_name("property")
            if prop is None:
                continue
            entry: dict[str, object] = {
                "name": _text(prop, source),
                "line": sub.start_point[0] + 1,
                "member": True,
            }
            obj = callee.child_by_field_name("object")
            if obj is not None and obj.type == "identifier":
                entry["recv"] = _text(obj, source)
            calls.append(entry)
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
