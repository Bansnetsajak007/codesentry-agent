"""Language-agnostic Pydantic models for the code graph: the NodeType and EdgeType
enums plus the Node and Edge structures whose fields capture concepts common to
every supported language, with language-specific detail relegated to a metadata dict."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeType(str, Enum):
    """Universal kinds of code entity. CLASS also covers structs, interfaces, and
    traits; language-specific distinctions are recorded in ``Node.metadata``."""

    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    FIELD = "field"


class EdgeType(str, Enum):
    """Universal relations between code entities. IMPLEMENTS is used for interface
    implementation in Go, Java, and TypeScript."""

    CONTAINS = "contains"
    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"


class Node(BaseModel):
    """A single code entity in the universal graph.

    ``id`` is a stable ``<file_path>::<qualified_name>`` string (see
    :func:`make_node_id`). ``metadata`` holds language-specific extras such as
    decorators, annotations, visibility modifiers, or generics."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: NodeType
    name: str
    qualified_name: str
    file_path: str
    language: str
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    """A directed relation between two nodes, identified by their ``id`` strings.

    ``metadata`` holds relation-specific extras such as the line number of a call
    site."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    target_id: str
    type: EdgeType
    metadata: dict[str, Any] = Field(default_factory=dict)


def make_node_id(file_path: str, qualified_name: str) -> str:
    """Build the stable node id for an entity from its file path and the dotted or
    scoped qualified name it has within that file."""

    return f"{file_path}::{qualified_name}"
