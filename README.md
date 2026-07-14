# CodeSentry

CodeSentry is a **language-agnostic code-understanding assistant**. It builds a
unified graph model of a repository and uses an LLM agent to answer questions and
review code changes with **real grounding in the source** — every claim cites a
real file and line, rather than being hallucinated.

Phase 1 supports **Python, JavaScript, TypeScript, Go, and Java** in a single
graph. Adding a new language is a matter of writing one parser adapter; the graph,
retrieval, agent, and CLI never branch on language.

---

## Status

Phase 1 is feature-complete through the diff reviewer. The build proceeded in the
strict order defined in `docs/PHASE_1_SPEC.md`:

| # | Step | Status |
|---|------|--------|
| 1 | Scaffolding, tooling, test skeleton | ✅ |
| 2 | Universal graph schema (`Node`, `Edge`, enums) | ✅ |
| 3 | `LanguageAdapter` base class + registry | ✅ |
| 4 | Python adapter + minimal builder/store + `index`/`stats` | ✅ |
| 5 | JavaScript adapter (+ mixed-repo indexing) | ✅ |
| 6 | TypeScript adapter (interfaces, generics, `.tsx`) | ✅ |
| 7 | Go adapter (receiver methods, embedding) | ✅ |
| 8 | Java adapter (extends/implements, nested classes) | ✅ |
| 9 | Cross-file resolution (imports, calls, heritage) | ✅ |
| 10 | Retrieval layer (subgraph + snippets) | ✅ |
| 11 | Agent schemas, `LLMClient`, tools | ✅ |
| 12 | Agent loop + prompts + `ask` command | ✅ |
| 13 | Diff review + `review` command | ✅ |
| 14 | This README | ✅ |

**Quality gates:** `101 passing tests`, `mypy --strict` clean on `codesentry/`,
no `openai` import outside `agent/llm.py`. No test ever calls the real API.

> **Known gap:** the spec lists a `codesentry languages` command that is not yet
> implemented; the supported languages are documented in the table below instead.
> The `ask` and `review` commands require an `OPENAI_API_KEY` (see below).

---

## Supported languages

| Language | Extensions | Notes |
|----------|-----------|-------|
| Python | `.py`, `.pyi` | decorators, docstrings, intra-file calls |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` | classes, arrow/function-expression declarations, ESM + `require`, JSDoc |
| TypeScript | `.ts`, `.tsx`, `.mts`, `.cts` | interfaces & type aliases as `CLASS` nodes, `implements`, generics, `import type` |
| Go | `.go` | struct/interface types, receiver methods, embedding → `INHERITS`; interface satisfaction is **not** resolved in Phase 1 |
| Java | `.java` | `extends` → `INHERITS`, `implements` → `IMPLEMENTS`, nested classes, annotations |

---

## Architecture

```
 repository ──► LanguageAdapter (per file, tree-sitter)
                     │  emits universal Node/Edge objects
                     ▼
              graph/builder ──► networkx MultiDiGraph
                     │  cross-file resolution (imports, calls, heritage)
                     ▼
                graph/store  (pickle + JSON sidecar)
                     │
        ┌────────────┼───────────────┐
        ▼            ▼                ▼
   retrieval/    agent/loop       review/reviewer
  (subgraph,   (tools + LLM,     (diff → per-file
   snippets)   cited answers)     structured review)
```

Core ideas:

- **Universal graph schema** (`graph/schema.py`). Nodes are `FILE`, `MODULE`,
  `CLASS`, `FUNCTION`, `METHOD`, `FIELD`; edges are `CONTAINS`, `CALLS`,
  `IMPORTS`, `INHERITS`, `IMPLEMENTS`. Language-specific detail (decorators,
  annotations, generics, receiver types, ...) lives in a `metadata` dict.
- **Language adapters** (`languages/`) are the only code that knows a language.
  Each parses one language with tree-sitter and emits universal nodes/edges plus
  unresolved cross-file hints stashed in metadata.
- **Cross-file resolution** (`graph/builder.py`) connects the per-file graphs.
  The only language-specific step — mapping an import to a file — is delegated to
  each adapter's `resolve_import`; everything else is driven off universal
  metadata, so the builder never branches on language.
- **Retrieval** (`retrieval/`) turns the graph into LLM context: `extract_subgraph`
  (neighbors along any edge type, 1–2 hops) and `get_snippet` (exact source lines).
- **Agent** (`agent/`) runs a hand-rolled tool loop. The nine tools query the graph
  and files; the model must ground every claim in tool output and cite `file:line`.
  `agent/llm.py` is the *only* file that imports `openai`.
- **Review** (`review/`) parses a unified diff and, per changed file, gathers graph
  context and asks the model for structured, line-level comments.

### Project layout

```
codesentry/
├── config.py            # Settings + get_settings()
├── graph/               # schema, builder (+ cross-file resolution), store
├── languages/           # base (adapter + registry) + one file per language
├── retrieval/           # subgraph extraction, source snippets
├── agent/               # llm, schemas, tools, prompts, loop
├── review/              # diff parsing, reviewer
└── cli.py               # index / stats / ask / review
tests/                   # per-module tests + fixtures/ (one sample per language)
```

---

## Setup

Requires **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/).

```bash
# Install dependencies (creates .venv)
uv sync

# Configure (only needed for `ask` and `review`)
cp .env.example .env
# then edit .env and set OPENAI_API_KEY
```

Environment variables (see `.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | — | required for `ask` / `review` |
| `CODESENTRY_MODEL` | `gpt-4.1` | model id (use `gpt-4.1-mini` for cheap iteration) |
| `CODESENTRY_MAX_TOKENS` | `4096` | max tokens per response |
| `CODESENTRY_LOG_LEVEL` | `INFO` | log verbosity |
| `OPENAI_BASE_URL` | — | optional endpoint override |

---

## Usage

Run the CLI with `uv run codesentry <command>`.

### `index` — build the graph

```bash
uv run codesentry index /path/to/repo
```

```
Indexed 66 nodes, 81 edges -> /path/to/repo/.codesentry/graph.pkl
  Files per language
┏━━━━━━━━━━━━┳━━━━━━━┓
┃ Language   ┃ Files ┃
┡━━━━━━━━━━━━╇━━━━━━━┩
│ go         │     4 │
│ python     │     4 │
│ typescript │     4 │
└────────────┴───────┘
```

The graph is saved to `.codesentry/graph.pkl` inside the repo, with a JSON sidecar
of metadata (counts, per-language breakdown, git commit, resolution summary).

### `stats` — inspect an indexed repo

```bash
uv run codesentry stats /path/to/repo
```

```
Repository: /path/to/repo
Nodes: 66  Edges: 81
Unresolved calls: 16  Skipped files: 0  Parse errors: 0
  Files per language
┏━━━━━━━━━━━━┳━━━━━━━┓
┃ Language   ┃ Files ┃
┡━━━━━━━━━━━━╇━━━━━━━┩
│ go         │     4 │
│ python     │     4 │
│ typescript │     4 │
└────────────┴───────┘
```

### `ask` — question answering (requires `OPENAI_API_KEY`)

```bash
uv run codesentry ask /path/to/repo "Where is the user repository and what calls it?"
```

Runs the agent tool-loop and prints an answer whose every factual claim is backed
by a citation to a real `file:line`, followed by a citations table. Options:
`--max-iterations` (default 15), `--model`.

### `review` — review a diff (requires `OPENAI_API_KEY`)

```bash
# from a file
uv run codesentry review /path/to/repo --diff change.diff

# or from stdin
git diff | uv run codesentry review /path/to/repo
```

Prints line-level comments grouped by file (severity `INFO`/`WARNING`/`ERROR`),
focused on correctness, broken contracts, missing error handling, and obvious
performance problems — not style.

---

## How to test (Phase 1)

### 1. Automated tests and type checking

No test contacts the network; the LLM is always mocked.

```bash
uv run pytest              # 101 tests
uv run mypy                # strict, on codesentry/
```

### 2. Manual CLI walkthrough (no API key needed)

`index` and `stats` are fully exercisable offline. The repo ships small sample
projects under `tests/fixtures/` — index one (or point at any real repo):

```bash
uv run codesentry index tests/fixtures/sample_python
uv run codesentry stats tests/fixtures/sample_python
```

To see the language-agnostic graph merge several languages into one graph, copy a
few fixtures into one directory and index it:

```bash
mkdir /tmp/mixed
cp tests/fixtures/sample_python/*.py tests/fixtures/sample_go/*.go \
   tests/fixtures/sample_ts/*.ts /tmp/mixed/
uv run codesentry index /tmp/mixed
uv run codesentry stats /tmp/mixed
```

### 3. Manual `ask` / `review` (needs `OPENAI_API_KEY` in `.env`)

```bash
uv run codesentry index tests/fixtures/sample_python
uv run codesentry ask tests/fixtures/sample_python \
  "What does UserRepository.count return, and is it correct?"

# review the fixture's intentional off-by-one bug
printf '%s\n' \
'--- a/repository.py' \
'+++ b/repository.py' \
'@@ -18,3 +18,2 @@ class UserRepository:' \
'     def count(self) -> int:' \
'-        # BUG: off-by-one; should return len(self._users).' \
'-        return len(self._users) + 1' \
'+        return len(self._users)' \
| uv run codesentry review tests/fixtures/sample_python
```

Each `tests/fixtures/sample_*` project intentionally contains an obvious bug
(usually an off-by-one in a `count`/`Count` method) for exercising `review`.

---

## What's next (Phase 2)

Phase 2 (not started) covers a planner/executor/critic agent pattern, an
evaluation harness, and benchmark numbers against SWE-bench Lite and its
multi-language variants. See `docs/PHASE_1_SPEC.md` for the full Phase 1 spec.
