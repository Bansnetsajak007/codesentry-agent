"""LanguageAdapter for Go, using the tree-sitter-go grammar to extract functions,
structs and interfaces, and methods with receivers (recorded under their receiver
type via CONTAINS); interface satisfaction is not resolved statically in Phase 1."""
