# 02 — Architecture

## End-to-end data flow

```
 repository ──► LanguageAdapter (per file, tree-sitter)
                     │  emits universal Node/Edge objects
                     ▼
              graph/builder ──► networkx MultiDiGraph
                     │  cross-file resolution (imports, calls, heritage)
                     ▼
                graph/store   (pickle + JSON sidecar, .codesentry/graph.pkl)
                     │
        ┌────────────┼───────────────┐
        ▼            ▼                ▼
   retrieval/    agent/loop       review/reviewer
  (subgraph,   (tools + LLM,     (diff → per-file
   snippets)   cited answers)     structured review)
```

Walking this left to right:

1. **`codesentry-agent index <repo>`** walks the repository (`graph/builder.py`),
   skips ignored directories, and for every file with a registered adapter calls
   that adapter's `parse_file`. Each adapter returns a list of `Node`s and `Edge`s
   local to that one file (`languages/*.py`).
2. The builder merges every file's nodes/edges into one `networkx.MultiDiGraph`,
   then performs **cross-file resolution**: turning import strings into `IMPORTS`
   edges to real file nodes, turning bare call names into `CALLS` edges to real
   function/method nodes (only when unambiguous), and turning base-class /
   interface names into `INHERITS`/`IMPLEMENTS` edges.
3. The finished graph is pickled to `.codesentry/graph.pkl` inside the target
   repo, with a JSON sidecar of metadata (`graph/store.py`).
4. **`codesentry-agent ask <repo> "<question>"`** loads that graph and runs the
   agent loop (`agent/loop.py`). The agent has nine tools (`agent/tools.py`) that
   query the graph (find a symbol, list callers/callees, get a neighborhood) and
   read exact source lines (`retrieval/snippets.py`). It keeps calling tools until
   it's ready to answer, then returns a structured `AnswerWithCitations`.
5. **`codesentry-agent review <repo> --diff <file>`** parses a unified diff
   (`review/diff.py`), maps each changed file's changed lines onto graph nodes
   whose span overlaps them, gathers each changed symbol's callers/callees
   (including ones in a *different* language, which get flagged explicitly as
   cross-language neighbors), and asks the LLM for a structured `ReviewResult`
   per file (`review/reviewer.py`).
6. **`codesentry-agent stats <repo>`** just loads the persisted graph/metadata and
   prints counts — no LLM call.

## Core design principle: the universal graph

The single most important architectural decision in this codebase is that
`graph/schema.py` defines node and edge types that exist in *every* supported
language, and nothing outside `codesentry/languages/` is allowed to branch on
language. Concretely:

- `NodeType` has exactly six values: `FILE`, `MODULE`, `CLASS` (also covers
  structs, interfaces, traits, enums, type aliases...), `FUNCTION`, `METHOD`,
  `FIELD`.
- `EdgeType` has exactly five values: `CONTAINS`, `CALLS`, `IMPORTS`, `INHERITS`,
  `IMPLEMENTS`.
- Anything that doesn't fit — Python decorators, Go receiver types, TypeScript
  `kind: "interface" | "type" | "enum"`, Java annotations and visibility
  modifiers — goes into the node's free-form `metadata: dict[str, Any]`.

This means `graph/builder.py`, `retrieval/`, `agent/`, `review/`, and `cli.py`
never need an `if language == "go"` anywhere. The *only* language-specific hook
outside `languages/` is that the builder calls each adapter's
`resolve_import(module, importer, index)` method to turn one language's import
syntax into a file path — everything else in cross-file resolution is driven
purely off the universal `metadata` fields (`imports`, `calls`, `bases`,
`implements`, `receiver_type`).

## Package layout

```
codesentry/
├── __init__.py           # __version__
├── cli.py                # Typer app: index / stats / ask / review
├── config.py             # Settings + get_settings() (env-driven)
├── graph/
│   ├── schema.py          # NodeType, EdgeType, Node, Edge, make_node_id
│   ├── builder.py         # build_graph(): walk repo, merge, resolve cross-file
│   └── store.py           # save_graph/load_graph + JSON metadata sidecar
├── languages/
│   ├── base.py             # LanguageAdapter ABC, ImportIndex, registry
│   ├── python.py, javascript.py, typescript.py, go.py, java.py
├── retrieval/
│   ├── subgraph.py         # extract_subgraph(): N-hop neighborhood
│   └── snippets.py         # get_snippet(), find_nodes_by_name(), get_node()
├── agent/
│   ├── llm.py               # LLMClient — the ONLY file that imports openai
│   ├── schemas.py           # Pydantic tool I/O + AnswerWithCitations/ReviewResult
│   ├── tools.py              # the 9 tools + TOOL_REGISTRY + OpenAI schema export
│   ├── prompts.py            # QA and review system prompts
│   └── loop.py                # run_agent(): the hand-rolled tool-use loop
└── review/
    ├── diff.py              # unified diff parsing (unidiff)
    └── reviewer.py            # review_diff(): per-file structured review

tests/
├── fixtures/               # sample_python, sample_js (js/ts), sample_go, sample_java, sample_mixed
└── test_*.py               # one test module roughly per source module
```

## Why a `MultiDiGraph`

`networkx.MultiDiGraph` is a directed graph that allows multiple edges between
the same pair of nodes (needed because, e.g., a function can both `CALLS` and
appear unrelated-otherwise to another node — and in principle a `CALLS` edge and
an `IMPORTS`-derived relation could coexist between two file-level nodes). Nodes
store their full `Node` Pydantic object under the `"node"` attribute key; edges
store their `Edge` object under `"edge"` and a plain string `"type"` (the
`EdgeType.value`) for fast filtering without touching Pydantic. This dual storage
(`type` as plain string + full `Edge` object) is a deliberate performance choice:
hot paths like `_neighbors_by_edge` and `extract_subgraph` filter on
`data["type"]` directly rather than deserializing/comparing enum members.

## Where the OpenAI dependency lives

Per a hard project rule (`agent/llm.py` is the *only* file allowed to `import
openai`), the entire rest of the codebase talks to `LLMClient`, which exposes two
provider-agnostic methods: `chat_with_tools(messages, tools)` for the agent loop,
and `parse_structured(messages, schema)` for structured outputs (used for the
final cited answer and for review comments). Swapping providers later (Claude, a
local model) is scoped to rewriting this one file.
