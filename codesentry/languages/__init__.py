"""The languages subpackage isolates all language-specific parsing behind the
LanguageAdapter interface and a registry, so the rest of CodeSentry only ever sees
the universal graph and never branches on language."""
