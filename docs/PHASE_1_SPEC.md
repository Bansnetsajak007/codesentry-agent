# CodeSentry — Phase 1 Specification

## Project overview

CodeSentry is a language-agnostic code-understanding assistant that builds a graph model of a repository and uses an LLM agent to answer questions and review code with real repo grounding (not hallucinated).

Phase 1 goal: a working CLI that can (a) index a repository (any supported language) into a unified graph, (b) answer questions about the repo with citations to real files and line numbers, and (c) review a git diff and produce line-level comments. Everything else comes later.

Languages supported in Phase 1: **Python, JavaScript, TypeScript, Go, Java**. The architecture must make adding a new language a matter of adding one parser adapter, not touching the graph, retrieval, agent, or CLI code.

Non-goals for Phase 1: desktop app, GitHub integration, multi-agent orchestration, refactoring, doc generation, IDE extensions. Do not build these. If you're tempted to, stop and ask.

## Tech stack (use exactly these — do not substitute)

- Python 3.11+ (this is the implementation language of CodeSentry itself; the *target* codebases can be any supported language)
- `uv` for dependency management, `pyproject.toml` for config
- `tree-sitter` for parsing, with per-language grammar packages:
  - `tree-sitter-python`
  - `tree-sitter-javascript`
  - `tree-sitter-typescript`
  - `tree-sitter-go`
  - `tree-sitter-java`
- `networkx` for the graph (in-memory, persisted as pickle + JSON metadata)
- `openai` SDK (>=1.50), model `gpt-4.1` (configurable via env var). Use the Chat Completions API with function calling. Structured outputs via `client.beta.chat.completions.parse()` with Pydantic models.
- `pydantic` v2 for all structured data and tool schemas
- `typer` + `rich` for the CLI
- `pathspec` for `.gitignore` handling
- `unidiff` for parsing git diffs
- `pytest` + `pytest-mock` for tests
- `python-dotenv` for config
- `mypy` (dev dependency) for type checking
- No LangChain, no LlamaIndex, no CrewAI, no LangGraph. Roll the agent loop by hand.

Wrap the OpenAI client in a small `LLMClient` abstraction (in `agent/llm.py`) with methods `chat_with_tools(messages, tools)` and `parse_structured(messages, schema)`. Nothing outside `agent/llm.py` may import `openai` directly. This makes provider swaps (Claude, local models) a one-file change later.

## Core design principle: language-agnostic graph

The graph schema is **universal**. Nodes and edges represent concepts that exist in nearly every language: files, modules/packages, classes/structs/interfaces, functions/methods, calls, imports, inheritance. Language-specific concepts (e.g. Go interfaces, TS type aliases, Java annotations) get normalized to the closest universal concept, with the language-specific detail stored in a `metadata: dict` field on the node.

Language support is added via a `LanguageAdapter` interface. Each adapter knows how to parse one language and emit universal `Node` and `Edge` objects. The rest of the system never branches on language.

## Repository structure

Create exactly this layout:

```
codesentry/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── codesentry/
│   ├── __init__.py
│   ├── config.py
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── schema.py
│   │   ├── builder.py
│   │   └── store.py
│   ├── languages/
│   │   ├── __init__.py
│   │   ├── base.py           # LanguageAdapter abstract class + registry
│   │   ├── python.py
│   │   ├── javascript.py
│   │   ├── typescript.py
│   │   ├── go.py
│   │   └── java.py
│   ├── retrieval/
│   │   ├── __init__.py
│   │   ├── subgraph.py
│   │   └── snippets.py
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── llm.py
│   │   ├── schemas.py
│   │   ├── tools.py
│   │   ├── prompts.py
│   │   └── loop.py
│   ├── review/
│   │   ├── __init__.py
│   │   ├── diff.py
│   │   └── reviewer.py
│   └── cli.py
└── tests/
    ├── fixtures/
    │   ├── sample_python/     # small Python project
    │   ├── sample_js/         # small JS project
    │   ├── sample_ts/         # small TS project
    │   ├── sample_go/         # small Go project
    │   └── sample_java/       # small Java project
    ├── test_graph.py
    ├── test_languages_python.py
    ├── test_languages_javascript.py
    ├── test_languages_typescript.py
    ├── test_languages_go.py
    ├── test_languages_java.py
    ├── test_retrieval.py
    ├── test_agent.py
    └── test_review.py
```

## Module specifications

### `graph/schema.py`
Pydantic models, language-agnostic.

`NodeType` enum: FILE, MODULE, CLASS (also covers structs, interfaces, traits), FUNCTION, METHOD, FIELD (optional for Phase 1 if it adds noise).

`EdgeType` enum: CONTAINS, CALLS, IMPORTS, INHERITS, IMPLEMENTS (for interface implementation in Go/Java/TS).

`Node` fields:
- `id` — stable string like `<file_path>::<qualified_name>` (e.g. `src/auth.py::LoginHandler.login`, `pkg/user/user.go::User.Save`)
- `type: NodeType`
- `name: str`
- `qualified_name: str` — dotted/scoped name within the file
- `file_path: str`
- `language: str` — "python", "javascript", "typescript", "go", "java"
- `start_line: int`, `end_line: int`
- `signature: str | None`
- `docstring: str | None` — docstring, JSDoc, godoc, or Javadoc, normalized to plain text
- `metadata: dict[str, Any]` — language-specific extras (decorators, annotations, visibility modifiers, generics, etc.)

`Edge` fields: `source_id`, `target_id`, `type`, `metadata: dict[str, Any]` (e.g. call site line number).

### `languages/base.py`
Abstract base class `LanguageAdapter` with:
- `language_name: str` (class attribute)
- `file_extensions: set[str]` (class attribute, e.g. `{".py"}` or `{".ts", ".tsx"}`)
- `parse_file(self, path: Path, source: bytes) -> tuple[list[Node], list[Edge]]` — returns local nodes and local edges (CONTAINS + intra-file CALLS/IMPORTS). Cross-file resolution is done later in the builder.

Registry: a module-level dict `ADAPTERS: dict[str, LanguageAdapter]` mapping language name to adapter instance, plus a helper `get_adapter_for_file(path) -> LanguageAdapter | None` that dispatches by file extension.

### `languages/python.py`, `languages/javascript.py`, `languages/typescript.py`, `languages/go.py`, `languages/java.py`
Each implements `LanguageAdapter` using the corresponding tree-sitter grammar. Each defines the tree-sitter query patterns needed to extract:
- Top-level functions
- Classes/structs/interfaces and their methods
- Function/method call sites (name only; resolution is later)
- Import/require statements
- Inheritance/implementation relations

Language-specific notes:
- **Python**: extract decorators into `metadata["decorators"]`.
- **JavaScript/TypeScript**: handle both `class` and top-level function declarations, plus `export`/`import`/`require`. TS additionally captures type aliases and interfaces as CLASS nodes with `metadata["kind"] = "interface" | "type"`.
- **Go**: methods are functions with a receiver; capture the receiver type and record the method under the receiver's CLASS node via CONTAINS. Interface satisfaction is not resolved statically in Phase 1 — leave IMPLEMENTS edges empty for Go and note this in the adapter docstring.
- **Java**: capture `extends` as INHERITS, `implements` as IMPLEMENTS. Handle nested classes.

Every adapter must be resilient to parse errors — if tree-sitter reports errors, emit whatever nodes were successfully parsed, log a warning, and move on. Never crash the indexer on a single bad file.

### `graph/builder.py`
`build_graph(repo_path: Path) -> nx.MultiDiGraph`. Walks the repo, respects `.gitignore` (use `pathspec`), dispatches each file to the appropriate adapter via `get_adapter_for_file`, and skips files with no adapter. Merges all nodes and edges into a single `MultiDiGraph`.

Cross-file resolution (best-effort, per language):
- Resolve CALLS edges by matching the callee name against known FUNCTION/METHOD nodes reachable via IMPORTS from the source file. If ambiguous, drop the edge — do not guess.
- Resolve IMPORTS by matching import paths to FILE/MODULE nodes.

Log summary at the end: files indexed per language, total nodes, total edges, unresolved calls, files skipped (unsupported language), files with parse errors.

### `graph/store.py`
`save_graph(graph, path)` and `load_graph(path)`. Pickle for the graph, sidecar JSON with: repo path, indexed_at, node/edge counts, per-language file counts, git commit if available, CodeSentry version.

### `retrieval/subgraph.py`
Given a set of seed node IDs, return a subgraph with those nodes plus their 1-hop neighbors along CALLS, IMPORTS, INHERITS, IMPLEMENTS, CONTAINS edges. Configurable hop depth (default 1, max 2 for Phase 1).

### `retrieval/snippets.py`
`get_snippet(node) -> str` reads the file and returns exact source lines for a node with 2 lines of margin. `find_nodes_by_name(graph, name, language: str | None = None) -> list[Node]` for symbol lookup, optionally scoped to a language.

### `agent/llm.py`
Thin abstraction over the OpenAI SDK. Class `LLMClient` with:
- `__init__(self, api_key, model, base_url=None, max_tokens=4096)`
- `chat_with_tools(self, messages, tools) -> ChatResponse` — wraps `client.chat.completions.create()` with `tools=[...]` and `tool_choice="auto"`. Returns a normalized response with `.content`, `.tool_calls` (list of `{id, name, arguments}`), `.finish_reason`, `.usage`.
- `parse_structured(self, messages, schema: type[BaseModel]) -> BaseModel` — wraps `client.beta.chat.completions.parse()` with `response_format=<PydanticModel>`.

Keep this file provider-specific.

### `agent/schemas.py`
Pydantic models for tool inputs/outputs. Define one class per tool. Also define `ReviewComment` (file, line, severity: info/warning/error, message, suggestion optional), `Citation` (file, start_line, end_line), `AnswerWithCitations` (answer, citations: list[Citation]), `ReviewResult` (comments: list[ReviewComment]).

### `agent/tools.py`
Tools take a Pydantic input and return a Pydantic output or plain string. Every tool docstring becomes the LLM-facing description.

Tools:
1. `list_files(pattern: str | None, language: str | None)` — lists indexed files, optional glob and language filter.
2. `read_file(path: str, start_line: int | None, end_line: int | None)` — reads a range or whole file.
3. `find_symbol(name: str, language: str | None)` — returns matching nodes with file:line and language.
4. `get_definition(node_id: str)` — returns node source.
5. `get_callers(node_id: str)` — reverse CALLS edges.
6. `get_callees(node_id: str)` — forward CALLS edges.
7. `get_neighbors(node_id: str, hops: int = 1)` — subgraph around a node.
8. `grep(pattern: str, path_glob: str | None)` — regex search across all indexed files.
9. `list_languages()` — returns which languages are present in the indexed repo and file counts per language.

Expose `openai_tool_schemas() -> list[dict]` returning OpenAI's function-calling format. Generate `parameters` schemas from Pydantic models via `.model_json_schema()`. Provide a `TOOL_REGISTRY: dict[str, Callable]` for name-based dispatch in the loop.

### `agent/prompts.py`
System prompt for the agent. Emphasize: ground every claim in tools, cite file:line for every factual statement, never invent function names or file paths, if uncertain say so, and always note the language when it's relevant (e.g. "In `auth.go:42` (Go), ..."). Include 2 few-shot examples across at least two different languages.

Separate system prompt for review mode focused on correctness bugs, broken contracts, missing error handling, obvious performance issues — explicitly not style. Review prompt must instruct the agent to reason about cross-language boundaries where they exist (e.g. a TS frontend calling a Go backend endpoint — flag suspicious mismatches).

### `agent/loop.py`
`run_agent(query: str, graph, llm: LLMClient, max_iterations: int = 15) -> AnswerWithCitations`.

Flow:
1. Build initial messages: system prompt + user query.
2. Loop up to `max_iterations`:
   a. Call `llm.chat_with_tools(messages, tools=openai_tool_schemas())`.
   b. Append the assistant message (with `tool_calls`) to `messages`.
   c. For each `tool_call`: look up in `TOOL_REGISTRY`, validate arguments against the Pydantic input model, execute, append a `role: "tool"` message with JSON-serialized result and matching `tool_call_id`.
   d. If no `tool_calls` and `finish_reason == "stop"`: break.
3. Final step: call `llm.parse_structured(messages + [final instruction], AnswerWithCitations)`.
4. On limit exceeded: raise `AgentIterationLimitError`.

Track cumulative token usage. Log every tool call (name, arg summary, result size) via `rich`.

### `review/diff.py`
Parse unified diffs using `unidiff`. Return hunks with file path, old/new ranges, added/removed lines and their line numbers. Diffs may touch multiple languages in one PR — that's expected.

### `review/reviewer.py`
`review_diff(diff_text: str, graph, llm: LLMClient) -> list[ReviewComment]`. For each changed function/file: detect its language from extension, gather graph context (callers, callees, related symbols across languages if IMPORTS/CALLS cross the boundary), invoke the agent with the review system prompt using `llm.parse_structured(..., ReviewResult)`.

### `cli.py`
Typer app:
- `codesentry index <repo_path>` — builds graph, saves to `.codesentry/graph.pkl` in the repo. Prints per-language file counts.
- `codesentry ask <repo_path> "<question>"` — runs agent, prints answer with citations.
- `codesentry review <repo_path> --diff <path_or_stdin>` — reviews diff, prints comments grouped by file.
- `codesentry stats <repo_path>` — prints graph stats including per-language breakdown.
- `codesentry languages` — prints the list of supported languages and their file extensions.

Rich for output, spinners for long ops.

### `config.py`
Loads from env via `python-dotenv`: `OPENAI_API_KEY`, `CODESENTRY_MODEL` (default `gpt-4.1`), `CODESENTRY_MAX_TOKENS` (default 4096), `CODESENTRY_LOG_LEVEL` (default INFO), `OPENAI_BASE_URL` (optional). Expose a `Settings` Pydantic model and `get_settings()` singleton.

## Testing requirements

Each language gets its own fixture directory with 3–5 small files including at least one class, cross-file calls, imports, and one obvious bug. Fixtures should be minimal but realistic.

Per-language tests: parse the fixture, assert expected nodes and edges exist, assert language-specific metadata is captured.

Graph builder test: build against a mixed-language fixture (create one that combines a Python backend and a TS frontend calling it via a shared naming convention) and assert the merged graph is correct.

Retrieval, agent, and review tests as before, with mocked `LLMClient`. Agent tests must cover: happy path, multi-tool-call path, iteration-limit error, malformed tool arguments.

Aim for ~70% coverage on non-agent code.

## Build order (do NOT skip ahead)

1. Scaffolding: `pyproject.toml`, `.env.example`, `.gitignore`, empty modules, `pytest` runs green.
2. `graph/schema.py` + tests.
3. `languages/base.py` (abstract adapter + registry) + tests with a stub adapter.
4. `languages/python.py` + fixture + tests. `codesentry index` and `codesentry stats` work end-to-end on a pure-Python repo.
5. `languages/javascript.py` + fixture + tests. Verify indexer handles mixed .py + .js repos.
6. `languages/typescript.py` + fixture + tests.
7. `languages/go.py` + fixture + tests.
8. `languages/java.py` + fixture + tests.
9. `graph/builder.py` cross-file resolution + `graph/store.py` + tests.
10. `retrieval/` + tests.
11. `agent/schemas.py`, `agent/llm.py`, `agent/tools.py` + non-LLM tool tests.
12. `agent/prompts.py` + `agent/loop.py` + mocked tests. `codesentry ask` works.
13. `review/` + `codesentry review` command.
14. README with quickstart, architecture diagram, supported-language table, example commands and output for at least two different languages.

After each step: tests green, manual CLI check, commit with a clear message. Do not proceed until current step is green.

## Guardrails

- If a design decision has real trade-offs (call resolution strategy, Go interface satisfaction, LLM response caching, whether a language needs a concept the schema doesn't cover), STOP and ask.
- Do not invent features not in this spec.
- Do not add dependencies not listed above without asking.
- Do not import `openai` outside `agent/llm.py`.
- If a new language grammar has an incompatible tree-sitter binding version, ask before pinning workarounds.
- Prefer boring code over clever code.
- Every module has a top-of-file docstring in one paragraph.
- Type hints everywhere. `mypy --strict` passes on `codesentry/`.
- Never branch on language outside `languages/`. The rest of the code sees only the universal graph.

## Cost awareness

- Model configurable via `CODESENTRY_MODEL`. Use `gpt-4.1-mini` for iteration if the user requests, reserve `gpt-4.1` for benchmark and demo runs. Never hardcode.
- Never call the real API in tests.
- Log token usage after each agent run.
- `max_iterations` default 15, expose as CLI flag.

## Definition of done for Phase 1

Against real medium-sized repos in at least two different languages (suggested: Flask for Python, Express or Zod for TS, Gin for Go — you pick):
1. `codesentry index` completes in under 90 seconds on each and produces a graph with >1000 nodes.
2. `codesentry ask <repo> "<question>"` produces answers with ≥3 correct file:line citations, correctly identifying the language of each cited location.
3. `codesentry review` on a small diff produces at least one non-trivial comment a human reviewer would agree with.
4. `codesentry stats` shows a correct per-language breakdown.
5. All tests pass, `mypy --strict` clean, README has quickstart with real example output for at least two languages.

When all five are true, Phase 1 is done. Phase 2 covers planner/executor/critic pattern, evaluation harness, and benchmark numbers against SWE-bench Lite (and its multi-language variants).
