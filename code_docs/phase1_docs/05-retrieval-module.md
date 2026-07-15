# 05 — The Retrieval Module (`codesentry/retrieval/`)

This module turns the graph into things an LLM can actually consume: bounded
neighborhoods around a node, and exact source-code text. It's small — two files
— but it's what every agent tool ultimately calls into.

## `retrieval/subgraph.py` — neighborhood extraction

### `extract_subgraph(graph, seed_ids, hops=1, edge_types=None) -> nx.MultiDiGraph`

Given one or more "seed" node ids, returns the induced subgraph of those seeds
plus everything reachable from them within `hops` steps, walking edges in
**both directions** (so a seed pulls in both what it calls and what calls it,
both its container and its members, both its parents and its subtypes).

Implementation:

- `hops` is clamped to `[1, 2]` via `max(1, min(hops, _MAX_HOPS))` — Phase 1
  caps hop depth at 2 regardless of what's requested, to keep the LLM context
  bounded (a 2-hop neighborhood in a densely-connected graph can already be
  large).
- `edge_types` defaults to all five types (`CONTAINS`, `CALLS`, `IMPORTS`,
  `INHERITS`, `IMPLEMENTS`) if not given, letting a caller narrow the walk to
  e.g. only `CALLS` if it wants a pure call graph.
- The walk is a straightforward BFS: start with `visited = frontier = seed_ids
  present in the graph`; each round, for every node in the frontier, collect
  both `out_edges` and `in_edges` whose `data["type"]` is in the allowed set and
  whose other endpoint isn't already visited; add those to `visited` and make
  them the next frontier; stop early if a round adds nothing new.
- Returns `graph.subgraph(visited).copy()` — a real, independent
  `MultiDiGraph` (the `.copy()` matters: `networkx.subgraph()` alone returns a
  live *view* that shares state with the original graph, which would be
  dangerous to mutate or hold onto across calls).
- Seed ids that aren't in the graph are silently ignored (not an error) — if
  none of the seeds exist, you get back an empty graph rather than a crash.

### `subgraph_nodes(graph) -> list[Node]`

Pulls the `Node` object off every graph node's `"node"` attribute and returns
them **sorted by id** — the sorting is what makes tool output (and therefore
LLM prompts, and therefore test assertions) deterministic across runs.

## `retrieval/snippets.py` — exact source text

### `get_snippet(node, repo_root, margin=2) -> str`

Reads `node.file_path` (resolved relative to `repo_root`) and returns exactly
the lines from `node.start_line - margin` to `node.end_line + margin`
(clamped to the file's actual bounds), joined with newlines. The 2-line margin
on each side gives the LLM (and a human reading tool output) a little visual
context — e.g. a blank line or the preceding `@decorator` — without inflating
the snippet. If the file can't be read (moved, deleted, permissions), it
returns a short marker string (`"# <source unavailable for {path}>"`) rather
than raising, so a single missing file doesn't blow up a tool call mid-agent-run.

This is the function that makes CodeSentry's answers *grounded*: every time the
agent calls `get_definition`, this is what supplies the actual text — never a
paraphrase or a remembered version of the code.

### `find_nodes_by_name(graph, name, language=None) -> list[Node]`

Symbol lookup: returns every non-`FILE` node whose `name` (simple) or
`qualified_name` (dotted/scoped) exactly equals `name`, optionally restricted to
one `language`. `FILE` nodes are excluded because "find a symbol named X"
conceptually means a definition, not a file. Results are sorted by node id for
determinism — important because `find_symbol` (the agent tool built on this) can
legitimately return multiple matches (e.g. a `login` method in two different
classes), and the agent needs consistent ordering to reason about which one is
which across a conversation.

### `get_node(graph, node_id) -> Node | None`

The simplest possible node lookup by exact id — returns `None` rather than
raising if the id doesn't exist, which is the pattern every tool follows so the
agent gets a clear "not found" string back instead of a stack trace.

## How this feeds the agent

None of the agent's nine tools (`agent/tools.py`) touch the graph or filesystem
directly — they all go through this module: `find_symbol` → `find_nodes_by_name`,
`get_definition` → `get_node` + `get_snippet`, `get_neighbors` → `extract_subgraph`
+ `subgraph_nodes`, `get_callers`/`get_callees` → direct edge filtering (these
two don't use `extract_subgraph` since they need one specific edge type and
direction, not a general neighborhood — see `06-agent-module.md`).
