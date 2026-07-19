"""LanguageAdapter for Go, using the tree-sitter-go grammar to extract functions,
structs and interfaces, and methods with receivers (recorded under their receiver
type via CONTAINS); interface satisfaction is not resolved statically in Phase 1.

Go interfaces are satisfied implicitly and structurally, which cannot be determined
without full type resolution, so this adapter emits NO IMPLEMENTS edges. Struct and
interface embedding is modeled as INHERITS (the closest universal analog), with the
embedded type names also kept in metadata["embeds"]. Cross-file relations (imports,
calls to names defined elsewhere, and methods whose receiver type lives in another
file of the package) are stashed in node metadata for the builder to resolve."""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_go as tsgo
from tree_sitter import Language, Node as TSNode, Parser

from codesentry.graph.schema import Edge, EdgeType, Node, NodeType, make_node_id
from codesentry.languages.base import LanguageAdapter, register_adapter

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tsgo.language())
_PARSER = Parser(_LANGUAGE)


class GoAdapter(LanguageAdapter):
    """Parses Go source into universal graph nodes and edges."""

    language_name = "go"
    file_extensions = {".go"}
    package_level_visibility = True

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
            # Pass 1: types and top-level functions (so receiver types exist for pass 2).
            for child in root.named_children:
                if child.type == "type_declaration":
                    self._add_types(
                        child, source, file_path, file_node.id, nodes, edges, name_to_ids
                    )
                elif child.type == "function_declaration":
                    self._add_function(
                        child, source, file_path, "", file_node.id, nodes, edges, name_to_ids
                    )
            # Pass 2: methods, linked under their receiver type when it is local.
            for child in root.named_children:
                if child.type == "method_declaration":
                    self._add_method(
                        child, source, file_path, file_node.id, nodes, edges, name_to_ids
                    )
            self._resolve_local_edges(nodes, edges, name_to_ids)
        except Exception:  # never crash the indexer on a single bad file
            logger.warning("Failed to parse %s; skipping", file_path, exc_info=True)
            return nodes, edges
        return nodes, edges

    def _add_types(
        self,
        type_decl: TSNode,
        source: bytes,
        file_path: str,
        container_id: str,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        for spec in type_decl.named_children:
            if spec.type != "type_spec":
                continue
            name = _field_text(spec, "name", source)
            type_node = spec.child_by_field_name("type")
            if name is None or type_node is None:
                continue
            kind = {
                "struct_type": "struct",
                "interface_type": "interface",
            }.get(type_node.type, "type")
            node_id = make_node_id(file_path, name)
            embeds = _embedded_types(type_node, source)
            metadata: dict[str, object] = {
                "kind": kind,
                "embeds": embeds,
                "bases": embeds,
                "exported": name[:1].isupper(),
            }
            type_params = spec.child_by_field_name("type_parameters")
            if type_params is not None:
                metadata["type_parameters"] = _text(type_params, source)
            node = Node(
                id=node_id,
                type=NodeType.CLASS,
                name=name,
                qualified_name=name,
                file_path=file_path,
                language=self.language_name,
                start_line=type_decl.start_point[0] + 1,
                end_line=spec.end_point[0] + 1,
                signature=_type_signature(type_decl, type_node, source),
                docstring=_godoc_before(type_decl, source),
                metadata=metadata,
            )
            nodes.append(node)
            name_to_ids.setdefault(name, []).append(node_id)
            edges.append(
                Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
            )

            if type_node.type == "interface_type":
                self._add_interface_methods(
                    type_node, source, file_path, f"{name}.", node_id,
                    nodes, edges, name_to_ids,
                )

    def _add_interface_methods(
        self,
        interface_node: TSNode,
        source: bytes,
        file_path: str,
        prefix: str,
        container_id: str,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        for member in interface_node.named_children:
            if member.type != "method_elem":
                continue
            name = _field_text(member, "name", source)
            if name is None:
                continue
            qualified = f"{prefix}{name}"
            node_id = make_node_id(file_path, qualified)
            node = Node(
                id=node_id,
                type=NodeType.METHOD,
                name=name,
                qualified_name=qualified,
                file_path=file_path,
                language=self.language_name,
                start_line=member.start_point[0] + 1,
                end_line=member.end_point[0] + 1,
                signature=_text(member, source).strip(),
                docstring=_godoc_before(member, source),
                metadata={"calls": [], "exported": name[:1].isupper()},
            )
            nodes.append(node)
            name_to_ids.setdefault(name, []).append(node_id)
            edges.append(
                Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
            )

    def _add_function(
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
            type=NodeType.METHOD if prefix else NodeType.FUNCTION,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=decl.start_point[0] + 1,
            end_line=decl.end_point[0] + 1,
            signature=_function_signature(decl, source),
            docstring=_godoc_before(decl, source),
            metadata={"calls": _collect_calls(decl, source), "exported": name[:1].isupper()},
        )
        nodes.append(node)
        name_to_ids.setdefault(name, []).append(node_id)
        edges.append(
            Edge(source_id=container_id, target_id=node_id, type=EdgeType.CONTAINS)
        )

    def _add_method(
        self,
        decl: TSNode,
        source: bytes,
        file_path: str,
        file_id: str,
        nodes: list[Node],
        edges: list[Edge],
        name_to_ids: dict[str, list[str]],
    ) -> None:
        name = _field_text(decl, "name", source)
        receiver_type, is_pointer = _receiver_type(decl, source)
        if name is None or receiver_type is None:
            return
        qualified = f"{receiver_type}.{name}"
        node_id = make_node_id(file_path, qualified)
        # Prefer to contain the method under its receiver struct if it is local;
        # otherwise fall back to the file and let the builder re-parent in step 9.
        local_struct = name_to_ids.get(receiver_type, [])
        container_id = local_struct[0] if len(local_struct) == 1 else file_id
        node = Node(
            id=node_id,
            type=NodeType.METHOD,
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=self.language_name,
            start_line=decl.start_point[0] + 1,
            end_line=decl.end_point[0] + 1,
            signature=_function_signature(decl, source),
            docstring=_godoc_before(decl, source),
            metadata={
                "calls": _collect_calls(decl, source),
                "receiver_type": receiver_type,
                "receiver_pointer": is_pointer,
                "exported": name[:1].isupper(),
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
        types_by_id = {node.id: node.type for node in nodes}

        def unique_target(name: str) -> str | None:
            ids = name_to_ids.get(name, [])
            return ids[0] if len(ids) == 1 else None

        def unique_call_target(call: dict[str, object]) -> str | None:
            # Selector calls (x.Method()) may only bind to methods or types;
            # binding them to a same-named package-level function would be wrong.
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


def _package_name(root: TSNode, source: bytes) -> str | None:
    clause = _child_of_type(root, "package_clause")
    if clause is None:
        return None
    ident = _child_of_type(clause, "package_identifier")
    return _text(ident, source) if ident is not None else None


def _function_signature(decl: TSNode, source: bytes) -> str | None:
    end = decl.child_by_field_name("result") or decl.child_by_field_name("parameters")
    if end is None:
        return None
    return source[decl.start_byte : end.end_byte].decode("utf-8", "replace").strip()


def _type_signature(type_decl: TSNode, type_node: TSNode, source: bytes) -> str:
    body = _child_of_type(type_node, "field_declaration_list")
    if body is None and type_node.type == "interface_type":
        # interface_type has no single 'body' node; slice up to its first member.
        first = next(iter(type_node.named_children), None)
        end = first.start_byte if first is not None else type_node.end_byte
        header = source[type_decl.start_byte : end].decode("utf-8", "replace")
        return header.rstrip(" \t\n{")
    if body is not None:
        return source[type_decl.start_byte : body.start_byte].decode("utf-8", "replace").strip()
    return _text(type_decl, source).strip()


def _embedded_types(type_node: TSNode, source: bytes) -> list[str]:
    embeds: list[str] = []
    if type_node.type == "struct_type":
        body = _child_of_type(type_node, "field_declaration_list")
        if body is None:
            return embeds
        for field in body.named_children:
            if field.type != "field_declaration":
                continue
            has_name = any(c.type == "field_identifier" for c in field.named_children)
            if has_name:
                continue
            type_child = field.child_by_field_name("type")
            if type_child is not None:
                embeds.append(_bare_type_name(type_child, source))
    elif type_node.type == "interface_type":
        for member in type_node.named_children:
            if member.type == "type_elem":
                embeds.append(_bare_type_name(member, source))
    return embeds


def _bare_type_name(node: TSNode, source: bytes) -> str:
    text = _text(node, source).strip().lstrip("*")
    # Reduce a qualified/generic name to its final identifier component.
    text = text.split("[", 1)[0]
    return text.split(".")[-1]


def _receiver_type(decl: TSNode, source: bytes) -> tuple[str | None, bool]:
    receiver = decl.child_by_field_name("receiver")
    if receiver is None:
        return None, False
    param = _child_of_type(receiver, "parameter_declaration")
    if param is None:
        return None, False
    type_node = param.child_by_field_name("type")
    if type_node is None:
        return None, False
    is_pointer = type_node.type == "pointer_type"
    return _bare_type_name(type_node, source), is_pointer


def _collect_calls(node: TSNode, source: bytes) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for sub in _walk(node):
        if sub.type != "call_expression":
            continue
        func = sub.child_by_field_name("function")
        if func is None:
            continue
        if func.type == "identifier":
            calls.append({"name": _text(func, source), "line": sub.start_point[0] + 1})
        elif func.type == "selector_expression":
            field = func.child_by_field_name("field")
            if field is None:
                continue
            entry: dict[str, object] = {
                "name": _text(field, source),
                "line": sub.start_point[0] + 1,
                "member": True,
            }
            operand = func.child_by_field_name("operand")
            if operand is not None and operand.type == "identifier":
                entry["recv"] = _text(operand, source)
            calls.append(entry)
    return calls


def _collect_imports(root: TSNode, source: bytes) -> list[dict[str, object]]:
    imports: list[dict[str, object]] = []
    for child in root.named_children:
        if child.type != "import_declaration":
            continue
        for spec_holder in child.named_children:
            specs = (
                spec_holder.named_children
                if spec_holder.type == "import_spec_list"
                else [spec_holder]
            )
            for spec in specs:
                if spec.type != "import_spec":
                    continue
                path_node = spec.child_by_field_name("path")
                if path_node is None:
                    continue
                alias = _field_text(spec, "name", source)
                imports.append(
                    {
                        "modules": [_string_value(path_node, source)],
                        "names": [alias] if alias else [],
                        "line": spec.start_point[0] + 1,
                        "kind": "go",
                    }
                )
    return imports


def _string_value(node: TSNode, source: bytes) -> str:
    text = _text(node, source)
    if len(text) >= 2 and text[0] in "\"'`":
        return text[1:-1]
    return text


def _godoc_before(node: TSNode, source: bytes) -> str | None:
    """Collect the contiguous block of // (or /* */) comments immediately above a
    declaration, normalized to plain text."""

    lines: list[str] = []
    current = node
    while True:
        prev = current.prev_named_sibling
        if (
            prev is None
            or prev.type != "comment"
            or prev.end_point[0] + 1 != current.start_point[0]
        ):
            break
        lines.append(_clean_comment(_text(prev, source)))
        current = prev
    if not lines:
        return None
    return "\n".join(reversed(lines)).strip()


def _clean_comment(raw: str) -> str:
    text = raw.strip()
    if text.startswith("//"):
        return text[2:].strip()
    if text.startswith("/*"):
        text = text[2:]
        if text.endswith("*/"):
            text = text[:-2]
        return text.strip()
    return text


register_adapter(GoAdapter())
