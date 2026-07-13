"""The LanguageAdapter abstract base class defining the parse_file contract that
every language implementation fulfils, together with the module-level adapter
registry and the get_adapter_for_file helper that dispatches by file extension."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from codesentry.graph.schema import Edge, Node


@dataclass
class ImportIndex:
    """Repo-wide lookup tables the builder passes to adapters during cross-file
    import resolution. All paths are repo-relative POSIX strings (FILE node ids)."""

    paths: set[str] = field(default_factory=set)
    by_stem: dict[str, list[str]] = field(default_factory=dict)
    package_of: dict[str, str | None] = field(default_factory=dict)
    files_by_package: dict[str, list[str]] = field(default_factory=dict)


def _module_basename(module: str) -> str:
    """Reduce an import module string to its final path/name component, splitting on
    both path separators and dots (handles JS './models', Python 'a.b.models', Go
    'ex.com/x/models', and Java 'com.example.User')."""

    cleaned = module.strip().strip("\"'`")
    parts = [p for p in re.split(r"[./\\]", cleaned) if p and p != ".."]
    return parts[-1] if parts else ""


class LanguageAdapter(ABC):
    """Parses one language into universal graph nodes and edges.

    Concrete adapters set the ``language_name`` and ``file_extensions`` class
    attributes and implement :meth:`parse_file`. An adapter only produces local
    nodes and intra-file edges (CONTAINS plus intra-file CALLS/IMPORTS); cross-file
    resolution is done later by the builder."""

    language_name: ClassVar[str]
    file_extensions: ClassVar[set[str]]
    # True for languages where files in the same directory form a package and can
    # reference each other's definitions without an explicit import (Go, Java).
    package_level_visibility: ClassVar[bool] = False

    @abstractmethod
    def parse_file(self, path: Path, source: bytes) -> tuple[list[Node], list[Edge]]:
        """Parse ``source`` (the raw bytes of ``path``) and return the local nodes
        and local edges it defines."""

    def resolve_import(self, module: str, importer: str, index: ImportIndex) -> str | None:
        """Map an import ``module`` string (from ``importer``) to a repo-relative
        file path, or return None. The default matches by unique filename stem;
        adapters override for language-specific module syntax."""

        matches = index.by_stem.get(_module_basename(module), [])
        return matches[0] if len(matches) == 1 else None


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
