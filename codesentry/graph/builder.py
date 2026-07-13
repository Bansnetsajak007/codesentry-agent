"""Repository indexer that walks a repo, respects .gitignore, dispatches each file
to its language adapter, merges the emitted nodes and edges into a single networkx
MultiDiGraph, and performs best-effort cross-file resolution of calls and imports."""
