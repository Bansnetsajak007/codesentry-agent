"""LanguageAdapter for TypeScript, using the tree-sitter-typescript grammar to
extract classes and functions plus type aliases and interfaces as CLASS nodes
tagged in metadata, alongside call sites and import/export statements. This adapter
is intentionally standalone (it duplicates the ECMAScript traversal rather than
sharing code with the JavaScript adapter) so the two languages stay independent.
Cross-file relations are stashed in node metadata for the builder; only real
in-file targets (CONTAINS, INHERITS, IMPLEMENTS, intra-file CALLS) are emitted."""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node as TSNode, Parser

from codesentry.graph.schema import Edge, EdgeType, Node, NodeType, make_node_id
from codesentry.languages.base import ImportIndex, LanguageAdapter, register_adapter

logger = logging.getLogger(__name__)

_TS_LANGUAGE = Language(tstypescript.language_typescript())
_TSX_LANGUAGE = Language(tstypescript.language_tsx())
_PARSER_TS = Parser(_TS_LANGUAGE)
_PARSER_TSX = Parser(_TSX_LANGUAGE)

_FUNCTION_VALUES = ("arrow_function", "function_expression")
_CLASS_DECLS = ("class_declaration", "abstract_class_declaration")
_TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts")


class TypeScriptAdapter(LanguageAdapter):
    """Parses TypeScript (and TSX) source into universal graph nodes and edges."""

    language_name = "typescript"
    file_extensions = {".ts", ".mts", ".cts", ".tsx"}

    def resolve_import(self, module: str, importer: str, index: ImportIndex) -> str | None:
        if module.startswith("."):
            base = posixpath.normpath(
                posixpath.join(posixpath.dirname(importer), module)
            )
            if base in index.paths:
                return base
            for ext in _TS_EXTENSIONS:
                if f"{base}{ext}" in index.paths:
                    return f"{base}{ext}"
                if f"{base}/index{ext}" in index.paths:
                    return f"{base}/index{ext}"
        return super().resolve_import(module, importer, index)

    def parse_file(self, path: Path, source: bytes) -> tuple[list[Node], list[Edge]]:
        file_path = path.as_posix()
        nodes: list[Node] = []
        edges: list[Edge] = []
        parser = _PARSER_TSX if path.suffix.lower() == ".tsx" else _PARSER_TS
        try:
            tree = parser.parse(source)
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
                        decl, decl, name, prefix, file_path, container_id, [], jsdoc,
                        source, nodes, edges, name_to_ids,
                    )
            elif decl.type in _CLASS_DECLS:
                name = _field_text(decl, "name", source)
                if name is not None:
                    self._add_class(
                        decl, name, "class", prefix, file_path, container_id,
                        _collect_decorators(child, decl, source), jsdoc,
                        source, nodes, edges, name_to_ids,
                    )
            elif decl.type == "interface_declaration":
                self._add_class(
                    decl, _field_text(decl, "name", source) or "", "interface", prefix,
                    file_path, container_id, [], jsdoc,
                    source, nodes, edges, name_to_ids,
                )
            elif decl.type == "type_alias_declaration":
                self._add_class(
                    decl, _field_text(decl, "name", source) or "", "type", prefix,
                    file_path, container_id, [], jsdoc,
                    source, nodes, edges, name_to_ids,
                )
            elif decl.type == "enum_declaration":
                self._add_class(
                    decl, _field_text(decl, "name", source) or "", "enum", prefix,
                    file_path, container_id, [], jsdoc,
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
                    declarator, value, name, prefix, file_path, container_id, [], jsdoc,
                    source, nodes, edges, name_to_ids,
                )
            elif value.type == "class":
                self._add_class(
                    value, name, "class", prefix, file_path, container_id, [], jsdoc,
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
        decorators: list[str],
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
            metadata={"decorators": decorators, "calls": _collect_calls(def_node, source)},
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )

    def _add_class(
        self,
        decl: TSNode,
        name: str,
        kind: str,
        prefix: str,
        file_path: str,
        container_id: str,
        decorators: list[str],
        jsdoc: str | None,
        source: bytes,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
        span: TSNode | None = None,
    ) -> None:
        if not name:
            return
        span = span or decl
        qualified = f"{prefix}{name}"
        node_id = make_node_id(file_path, qualified)
        extends, implements = _heritage(decl, kind, source)
        metadata: dict[str, object] = {
            "kind": kind,
            "decorators": decorators,
            "bases": extends,
            "implements": implements,
        }
        type_params = decl.child_by_field_name("type_parameters")
        if type_params is not None:
            metadata["type_parameters"] = _text(type_params, source)
        node = Node(
            id=node_id,
            type=NodeType.CLASS,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            signature=_class_signature(decl, name, kind, source),
            docstring=jsdoc,
            metadata=metadata,
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )

        body = decl.child_by_field_name("body")
        if body is not None and kind in ("class", "interface"):
            self._add_members(
                body, source, file_path, f"{qualified}.", node_id,
                nodes, edges, name_to_ids,
            )

    def _add_members(
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
        pending: list[str] = []
        for member in body.named_children:
            if member.type == "decorator":
                pending.append(_decorator_name(member, source))
                continue
            if member.type in ("method_definition", "method_signature"):
                name = _field_text(member, "name", source)
                if name is not None:
                    self._add_function(
                        member, member, name, prefix, file_path, container_id,
                        pending, _jsdoc_before(member, source),
                        source, nodes, edges, name_to_ids,
                    )
            pending = []

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
                for iface in node.metadata.get("implements", []):
                    target = unique_target(iface)
                    if target is not None:
                        edges.append(
                            Edge(source_id=node.id, target_id=target, type=EdgeType.IMPLEMENTS)
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
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _text(node: TSNode, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _field_text(node: TSNode, field: str, source: bytes) -> str | None:
    child = node.child_by_field_name(field)
    return _text(child, source) if child is not None else None


def _child_of_type(node: TSNode, type_name: str) -> TSNode | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


def _function_signature(def_node: TSNode, name: str, source: bytes) -> str | None:
    params = def_node.child_by_field_name("parameters")
    ret = def_node.child_by_field_name("return_type")
    end = ret or params or def_node.child_by_field_name("name")
    if def_node.type in _FUNCTION_VALUES:
        if params is None or end is None:
            return f"{name}()"
        return f"{name}{source[params.start_byte : end.end_byte].decode('utf-8', 'replace')}"
    if end is None:
        return None
    return source[def_node.start_byte : end.end_byte].decode("utf-8", "replace").strip()


def _class_signature(decl: TSNode, name: str, kind: str, source: bytes) -> str:
    if kind == "type":
        return _text(decl, source).rstrip(";").strip()
    if kind == "enum":
        return f"enum {name}"
    keyword = "interface" if kind == "interface" else "class"
    body = decl.child_by_field_name("body")
    end = body.start_byte if body is not None else decl.end_byte
    header = source[decl.start_byte : end].decode("utf-8", "replace").strip()
    # header still begins with the keyword; normalize whitespace lightly.
    return " ".join(header.split())


def _heritage(decl: TSNode, kind: str, source: bytes) -> tuple[list[str], list[str]]:
    if kind == "interface":
        return _interface_extends(decl, source), []
    if kind in ("type", "enum"):
        return [], []
    heritage = _child_of_type(decl, "class_heritage")
    if heritage is None:
        return [], []
    extends: list[str] = []
    implements: list[str] = []
    for clause in heritage.named_children:
        names = [
            _text(c, source)
            for c in clause.named_children
            if c.type in ("identifier", "type_identifier", "member_expression", "generic_type")
        ]
        if clause.type == "extends_clause":
            extends.extend(names)
        elif clause.type == "implements_clause":
            implements.extend(names)
    return extends, implements


def _interface_extends(decl: TSNode, source: bytes) -> list[str]:
    clause = _child_of_type(decl, "extends_type_clause")
    if clause is None:
        return []
    return [
        _text(c, source)
        for c in clause.named_children
        if c.type in ("type_identifier", "identifier", "generic_type", "member_expression")
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
        # `import ... from "m"` and re-exports `export { x } from "m"` both carry a
        # source string; plain `export class ...` does not and is handled elsewhere.
        if child.type in ("import_statement", "export_statement"):
            source_node = child.child_by_field_name("source")
            if source_node is None:
                continue
            imports.append(
                {
                    "modules": [_string_value(source_node, source)],
                    "names": _import_clause_names(child, source),
                    "line": child.start_point[0] + 1,
                    "kind": "esm",
                    "type": _is_type_import(child, source),
                }
            )
        elif child.type in ("lexical_declaration", "variable_declaration"):
            imports.extend(_require_imports(child, source))
    return imports


def _is_type_import(stmt: TSNode, source: bytes) -> bool:
    clause = _child_of_type(stmt, "import_clause")
    end = clause.start_byte if clause is not None else stmt.end_byte
    prefix = source[stmt.start_byte : end].decode("utf-8", "replace")
    return "type" in prefix.split()


def _import_clause_names(stmt: TSNode, source: bytes) -> list[str]:
    clause = _child_of_type(stmt, "import_clause")
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
                "type": False,
            }
        )
    return results


def _string_value(node: TSNode, source: bytes) -> str:
    text = _text(node, source)
    if len(text) >= 2 and text[0] in "\"'`":
        return text[1:-1]
    return text


def _collect_decorators(outer: TSNode, decl: TSNode, source: bytes) -> list[str]:
    seen: set[int] = set()
    names: list[str] = []
    for candidate in (*outer.named_children, *decl.named_children):
        if candidate.type == "decorator" and candidate.start_byte not in seen:
            seen.add(candidate.start_byte)
            names.append(_decorator_name(candidate, source))
    return names


def _decorator_name(decorator: TSNode, source: bytes) -> str:
    text = _text(decorator, source)
    return text.lstrip("@").split("(", 1)[0].strip()


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


register_adapter(TypeScriptAdapter())
