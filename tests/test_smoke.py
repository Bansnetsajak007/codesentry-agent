"""Smoke test asserting the package is importable and exposes its version, so the
test suite runs green from the scaffolding step onward. Per-module test files are
added in their respective build-order steps."""

import codesentry


def test_package_imports_and_has_version() -> None:
    assert codesentry.__version__ == "0.1.0"
