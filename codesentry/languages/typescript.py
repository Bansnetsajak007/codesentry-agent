"""LanguageAdapter for TypeScript, using the tree-sitter-typescript grammar to
extract classes and functions plus type aliases and interfaces as CLASS nodes
tagged in metadata, alongside call sites and import/export statements."""
