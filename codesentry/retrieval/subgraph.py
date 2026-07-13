"""Subgraph extraction that, given seed node IDs, returns those nodes plus their
neighbors along CALLS, IMPORTS, INHERITS, IMPLEMENTS, and CONTAINS edges up to a
configurable hop depth (default 1, capped at 2 for Phase 1)."""
