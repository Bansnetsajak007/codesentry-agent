"""LanguageAdapter for Java, using the tree-sitter-java grammar to extract classes
(including nested classes) and their methods, call sites, imports, extends as
INHERITS, and implements as IMPLEMENTS relations. Interfaces, enums, records, and
annotation types are captured as CLASS nodes tagged in metadata["kind"], and
annotations are recorded in metadata["annotations"]. Cross-file relations are
stashed in node metadata for the builder; only real in-file targets (CONTAINS,
INHERITS, IMPLEMENTS, intra-file CALLS) are emitted here."""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_java as tsjava
from tree_sitter import Language, Node as TSNode, Parser

from codesentry.graph.schema import Edge, EdgeType, Node, NodeType, make_node_id
from codesentry.languages.base import ImportIndex, LanguageAdapter, register_adapter

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tsjava.language())
_PARSER = Parser(_LANGUAGE)

_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
_KEYWORDS = {
    "class": "class",
    "interface": "interface",
    "enum": "enum",
    "record": "record",
    "annotation": "@interface",
}
_METHOD_DECLS = ("method_declaration", "constructor_declaration")
_VISIBILITY = ("public", "private", "protected")


class JavaAdapter(LanguageAdapter):
    """Parses Java source into universal graph nodes and edges."""

    language_name = "java"
    file_extensions = {".java"}
    package_level_visibility = True

    def resolve_import(self, module: str, importer: str, index: ImportIndex) -> str | None:
        parts = module.strip().split(".")
        if len(parts) >= 2:
            class_name = parts[-1]
            package = ".".join(parts[:-1])
            for candidate in index.files_by_package.get(package, []):
                if Path(candidate).stem == class_name:
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
                metadata={
                    "imports": _collect_imports(root, source),
                    "package": _package_name(root, source),
                    "parse_error": root.has_error,
                },
            )
            nodes.append(file_node)

            name_to_ids: dict[str, list[str]] = {}
            for child in root.named_children:
                if child.type in _TYPE_KINDS:
                    self._add_type(
                        child, source, file_path, "", file_node.id,
                        nodes, edges, name_to_ids,
                    )
            self._resolve_local_edges(nodes, edges, name_to_ids)
        except Exception:  # never crash the indexer on a single bad file
            logger.warning("Failed to parse %s; skipping", file_path, exc_info=True)
            return nodes, edges
        return nodes, edges

    def _add_type(
        self,
        decl: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        name = _field_text(decl, "name", source)
        if name is None:
            return
        kind = _TYPE_KINDS[decl.type]
        qualified = f"{prefix}{name}"
        node_id = make_node_id(file_path, qualified)
        extends, implements = _heritage(decl, kind, source)
        metadata: dict[str, object] = {
            "kind": kind,
            "annotations": _annotations(decl, source),
            "bases": extends,
            "implements": implements,
        }
        visibility = _visibility(decl, source)
        if visibility is not None:
            metadata["visibility"] = visibility
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
            start_line=decl.start_point[0] + 1,
            end_line=decl.end_point[0] + 1,
            signature=_type_signature(decl, kind, source),
            docstring=_javadoc_before(decl, source),
            metadata=metadata,
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )

        body = _body_of(decl)
        if body is not None:
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
        for member in body.named_children:
            if member.type in _METHOD_DECLS:
                self._add_method(
                    member, source, file_path, prefix, container_id,
                    nodes, edges, name_to_ids,
                )
            elif member.type in _TYPE_KINDS:
                self._add_type(
                    member, source, file_path, prefix, container_id,
                    nodes, edges, name_to_ids,
                )

    def _add_method(
        self,
        decl: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        name = _field_text(decl, "name", source)
        if name is None:
            return
        qualified = f"{prefix}{name}"
        node_id = make_node_id(file_path, qualified)
        node = Node(
            id=node_id,
            type=NodeType.METHOD,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=decl.start_point[0] + 1,
            end_line=decl.end_point[0] + 1,
            signature=_method_signature(decl, name, source),
            docstring=_javadoc_before(decl, source),
            metadata={
                "annotations": _annotations(decl, source),
                "calls": _collect_calls(decl, source),
            },
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
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
                for iface in node.metadata.get("implements", []):
                    target = unique_target(iface)
                    if target is not None:
                        edges.append(
                            Edge(source_id=node.id, target_id=target, type=EdgeType.IMPLEMENTS)
                        )
            if node.type is NodeType.METHOD:
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


def _child_of_type(node: TSNode, type_name: str) -> TSNode | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


def _body_of(decl: TSNode) -> TSNode | None:
    body = decl.child_by_field_name("body")
    if body is not None:
        return body
    for child in decl.named_children:
        if child.type.endswith("_body"):
            return child
    return None


def _bare_name(node: TSNode, source: bytes) -> str:
    text = _text(node, source).split("<", 1)[0].strip()
    return text.split(".")[-1]


def _type_list_names(container: TSNode, source: bytes) -> list[str]:
    type_list = _child_of_type(container, "type_list")
    target = type_list if type_list is not None else container
    return [
        _bare_name(child, source)
        for child in target.named_children
        if child.type in ("type_identifier", "scoped_type_identifier", "generic_type")
    ]


def _heritage(decl: TSNode, kind: str, source: bytes) -> tuple[list[str], list[str]]:
    extends: list[str] = []
    implements: list[str] = []
    superclass = _child_of_type(decl, "superclass")
    if superclass is not None:
        extends.extend(_type_list_names(superclass, source))
    extends_ifaces = _child_of_type(decl, "extends_interfaces")
    if extends_ifaces is not None:
        extends.extend(_type_list_names(extends_ifaces, source))
    super_ifaces = _child_of_type(decl, "super_interfaces")
    if super_ifaces is not None:
        implements.extend(_type_list_names(super_ifaces, source))
    return extends, implements


def _annotations(decl: TSNode, source: bytes) -> list[str]:
    modifiers = _child_of_type(decl, "modifiers")
    if modifiers is None:
        return []
    names: list[str] = []
    for child in modifiers.named_children:
        if child.type in ("marker_annotation", "annotation"):
            names.append(_text(child, source).lstrip("@").split("(", 1)[0].split(".")[-1].strip())
    return names


def _visibility(decl: TSNode, source: bytes) -> str | None:
    modifiers = _child_of_type(decl, "modifiers")
    if modifiers is None:
        return None
    tokens = _text(modifiers, source).split()
    for token in tokens:
        if token in _VISIBILITY:
            return token
    return None


def _type_signature(decl: TSNode, kind: str, source: bytes) -> str:
    keyword = _KEYWORDS[kind]
    name_node = decl.child_by_field_name("name")
    body = _body_of(decl)
    if name_node is None:
        return keyword
    end = body.start_byte if body is not None else decl.end_byte
    header = source[name_node.start_byte : end].decode("utf-8", "replace").strip()
    return f"{keyword} {header}".strip()


def _method_signature(decl: TSNode, name: str, source: bytes) -> str:
    params = decl.child_by_field_name("parameters")
    params_text = _text(params, source) if params is not None else "()"
    ret = decl.child_by_field_name("type")
    if ret is not None:
        return f"{_text(ret, source)} {name}{params_text}"
    return f"{name}{params_text}"


def _collect_calls(decl: TSNode, source: bytes) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for sub in _walk(decl):
        if sub.type == "method_invocation":
            name_node = sub.child_by_field_name("name")
        elif sub.type == "object_creation_expression":
            name_node = sub.child_by_field_name("type")
        else:
            continue
        if name_node is None:
            continue
        calls.append(
            {"name": _bare_name(name_node, source), "line": sub.start_point[0] + 1}
        )
    return calls


def _collect_imports(root: TSNode, source: bytes) -> list[dict[str, object]]:
    imports: list[dict[str, object]] = []
    for child in root.named_children:
        if child.type != "import_declaration":
            continue
        scoped = _child_of_type(child, "scoped_identifier")
        path = _text(scoped, source) if scoped is not None else ""
        is_wildcard = _child_of_type(child, "asterisk") is not None
        is_static = "static" in _text(child, source).split()
        names = ["*"] if is_wildcard else ([path.split(".")[-1]] if path else [])
        imports.append(
            {
                "modules": [path],
                "names": names,
                "line": child.start_point[0] + 1,
                "kind": "java",
                "static": is_static,
            }
        )
    return imports


def _package_name(root: TSNode, source: bytes) -> str | None:
    decl = _child_of_type(root, "package_declaration")
    if decl is None:
        return None
    scoped = _child_of_type(decl, "scoped_identifier") or _child_of_type(decl, "identifier")
    return _text(scoped, source) if scoped is not None else None


def _javadoc_before(node: TSNode, source: bytes) -> str | None:
    prev = node.prev_named_sibling
    if prev is None or prev.type != "block_comment":
        return None
    raw = _text(prev, source)
    if not raw.startswith("/**"):
        return None
    return _clean_javadoc(raw)


def _clean_javadoc(raw: str) -> str:
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


register_adapter(JavaAdapter())
