"""The languages subpackage isolates all language-specific parsing behind the
LanguageAdapter interface and a registry, so the rest of CodeSentry only ever sees
the universal graph and never branches on language. Importing this package imports
each concrete adapter module, which self-registers into the adapter registry."""

from codesentry.languages import python as _python  # noqa: F401

