"""Tests for the language-adapter base layer: abstractness of LanguageAdapter,
extension-based dispatch via get_adapter_for_file, and the registration guards
against duplicate languages and conflicting file extensions. Uses a fixture to
snapshot and restore the global ADAPTERS registry so tests do not leak state."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from codesentry.graph.schema import Edge, Node
from codesentry.languages import base
from codesentry.languages.base import (
    ADAPTERS,
    LanguageAdapter,
    get_adapter_for_file,
    register_adapter,
)


class StubAdapter(LanguageAdapter):
    language_name = "stub"
    file_extensions = {".stub"}

    def parse_file(
        self, path: Path, source: bytes
    ) -> tuple[list[Node], list[Edge]]:
        return [], []


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    saved = dict(ADAPTERS)
    ADAPTERS.clear()
    try:
        yield
    finally:
        ADAPTERS.clear()
        ADAPTERS.update(saved)


def test_base_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        LanguageAdapter()  # type: ignore[abstract]


def test_register_and_dispatch_by_extension() -> None:
    adapter = StubAdapter()
    register_adapter(adapter)
    assert base.ADAPTERS["stub"] is adapter
    assert get_adapter_for_file(Path("some/file.stub")) is adapter


def test_dispatch_is_case_insensitive() -> None:
    adapter = StubAdapter()
    register_adapter(adapter)
    assert get_adapter_for_file(Path("SOME/FILE.STUB")) is adapter


def test_dispatch_returns_none_for_unknown_extension() -> None:
    register_adapter(StubAdapter())
    assert get_adapter_for_file(Path("file.unknown")) is None


def test_register_rejects_duplicate_language() -> None:
    register_adapter(StubAdapter())
    with pytest.raises(ValueError, match="already registered"):
        register_adapter(StubAdapter())


def test_register_rejects_conflicting_extension() -> None:
    class OtherAdapter(LanguageAdapter):
        language_name = "other"
        file_extensions = {".stub"}

        def parse_file(
            self, path: Path, source: bytes
        ) -> tuple[list[Node], list[Edge]]:
            return [], []

    register_adapter(StubAdapter())
    with pytest.raises(ValueError, match="already claimed"):
        register_adapter(OtherAdapter())
