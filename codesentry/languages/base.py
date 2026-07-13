"""The LanguageAdapter abstract base class defining the parse_file contract that
every language implementation fulfils, together with the module-level adapter
registry and the get_adapter_for_file helper that dispatches by file extension."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from codesentry.graph.schema import Edge, Node


class LanguageAdapter(ABC):
    """Parses one language into universal graph nodes and edges.

    Concrete adapters set the ``language_name`` and ``file_extensions`` class
    attributes and implement :meth:`parse_file`. An adapter only produces local
    nodes and intra-file edges (CONTAINS plus intra-file CALLS/IMPORTS); cross-file
    resolution is done later by the builder."""

    language_name: ClassVar[str]
    file_extensions: ClassVar[set[str]]

    @abstractmethod
    def parse_file(self, path: Path, source: bytes) -> tuple[list[Node], list[Edge]]:
        """Parse ``source`` (the raw bytes of ``path``) and return the local nodes
        and local edges it defines."""


ADAPTERS: dict[str, LanguageAdapter] = {}


def register_adapter(adapter: LanguageAdapter) -> None:
    """Register ``adapter`` in the global registry, keyed by its language name.

    Raises ``ValueError`` if the language name is already registered or if any of
    the adapter's file extensions is already claimed by another adapter."""

    if adapter.language_name in ADAPTERS:
        raise ValueError(f"Language already registered: {adapter.language_name!r}")
    for ext in adapter.file_extensions:
        owner = _owner_of_extension(ext)
        if owner is not None:
            raise ValueError(
                f"Extension {ext!r} already claimed by adapter {owner!r}"
            )
    ADAPTERS[adapter.language_name] = adapter


def get_adapter_for_file(path: Path) -> LanguageAdapter | None:
    """Return the registered adapter that handles ``path`` by its file extension,
    or ``None`` if no adapter claims that extension."""

    suffix = path.suffix.lower()
    for adapter in ADAPTERS.values():
        if suffix in adapter.file_extensions:
            return adapter
    return None


def _owner_of_extension(ext: str) -> str | None:
    """Return the language name of the adapter that already claims ``ext``, if any."""

    for adapter in ADAPTERS.values():
        if ext in adapter.file_extensions:
            return adapter.language_name
    return None
