# CodeSentry — Agent Memory

> **Single source of durable context across chats.** This is the only file in
> `AgentMemory/`. It is written for an AI agent starting a new phase in a *fresh
> chat with zero prior context*. Read this first, then `docs/PHASE_1_SPEC.md`.

---

## 0. Memory protocol (read this first)

- **One file, updated in place.** Do **not** create additional memory files.
- **Update after each phase completes** (not after every small change): append a
  new `Phase N` entry to §3, then refresh §2 (Status), §7 (Gaps), and §8 (Next).
- **Record only what actually happened.** No speculation, no aspirational claims.
  If something is unverified, mark it **UNVERIFIED**.
- **Attribute decisions.** Note whether the *maintainer* decided it or the agent
  chose a default — the maintainer's decisions are binding and must not be
  silently reversed.

**Last updated:** end of Phase 1 (build order steps 1–14 complete).

---

## 1. Project identity

| | |
|---|---|
| **What** | CodeSentry — a language-agnostic code-understanding assistant. Builds a graph of a repo, then an LLM agent answers questions and reviews diffs **with real `file:line` citations** (not hallucinated). |
| **Local path** | `/mnt/elements/internshipProject/codesentry` |
| **GitHub** | `git@github.com:Bansnetsajak007/codesentry-agent.git` (branch `main`) |
| **CLI command** | `codesentry-agent` (NOT `codesentry` — see §6 gotcha) |
| **Spec** | `docs/PHASE_1_SPEC.md` — authoritative. `CLAUDE.md` — working rules. |
| **Python** | requires `>=3.11`; pinned to **3.12** via `.python-version` |
| **Deps** | uv + `pyproject.toml`. tree-sitter (+5 grammars), networkx, openai, pydantic v2, typer, rich, pathspec, unidiff, python-dotenv. Dev: pytest, pytest-mock, mypy. **No LangChain/LlamaIndex/CrewAI** — agent loop is hand-rolled. |

---

## 2. Status (as of Phase 1 completion)

- **Phase 1 build order: steps 1–14 all complete.**
- **107 tests passing**; `mypy --strict` clean on 26 source files.
- **22 commits**, all pushed to `origin/main`.
- **CI**: `.github/workflows/ci.yml` runs `uv sync` → `mypy --strict` → `pytest`
  on push to `main` and on PRs.
- Working CLI commands: **`index`**, **`stats`**, **`ask`**, **`review`**.

---

## 3. Phase log

### Phase 1 — COMPLETE

Delivered, in strict spec build order (one focused commit per step):

1. Scaffolding + tooling + test skeleton
2. Universal graph schema (`Node`, `Edge`, enums)
3. `LanguageAdapter` base class + registry
4. Python adapter + minimal builder/store + `index`/`stats`
5. JavaScript adapter (+ mixed `.py`/`.js` repo test)
6. TypeScript adapter (interfaces, generics, `.tsx`)
7. Go adapter (receiver methods, embedding)
8. Java adapter (extends/implements, nested classes)
9. Cross-file resolution (imports, calls, heritage, Go receivers)
10. Retrieval layer (subgraph + snippets)
11. Agent schemas, `LLMClient`, 9 tools
12. Agent loop + prompts + `config.py` + `ask`
13. Diff review + `review`
14. README

Post-Phase-1 additions: CLI rename, vendor-dir skipping, CI, provider-resilience
fixes (see §3 "maintainer's own commits" below).

**Maintainer's own commits** (written by the maintainer, not the agent — do not
revert without asking):
- `d62e8a2` gracefully handle providers without strict structured outputs
- `6f832a7` config: read API key from **`MODEL_API_KEY`**
- `636ffb5` fall back when structured output hits the length limit

---

## 4. Architecture (self-contained)

### The core thesis
Not vector RAG. **No embeddings, no vector store.** Chunks are semantic
(functions/classes with exact line spans), the index is a **typed graph**, and
retrieval is **traversal chosen by the LLM via tools** — not cosine similarity.
Trade-off: loses fuzzy semantic search, gains precision + provable grounding.

**The LLM does NOT build the graph.** `index` is 100% offline (tree-sitter only,
no API key, no network). The LLM only *reads* the finished graph at query time.
This is exactly why citations are trustworthy: line numbers are *measured* by a
parser, not recalled by a model.

### Indexing pipeline (`codesentry-agent index .`)
1. **Walk & filter** (`graph/builder.py`) — `os.walk`, pruned by the repo's root
   `.gitignore` (via `pathspec`) **and** a hardcoded `_ALWAYS_IGNORE` set
   (`node_modules`, `dist`, `.venv`, `vendor`, `target`, …).
2. **Dispatch** (`languages/base.py`) — `get_adapter_for_file(path)` maps suffix →
   adapter. No adapter → file skipped. **Only place language selection happens.**
3. **Parse** — tree-sitter grammar → concrete syntax tree. Parse errors are
   tolerated: emit what parsed, log a warning, set `metadata["parse_error"]`,
   never crash the indexer.
4. **Extract universal nodes/edges** — each adapter emits the same shapes:
   - Nodes: `FILE`, `MODULE`, `CLASS`, `FUNCTION`, `METHOD`, `FIELD`
   - Edges: `CONTAINS`, `CALLS`, `IMPORTS`, `INHERITS`, `IMPLEMENTS`
   - Node id format: `<file_path>::<qualified_name>` (FILE node id = file path)
   - Language specifics go in `metadata` (Python `decorators`, Go
     `receiver_type`, TS `kind: "interface"|"type"|"enum"`, …)
   - **Crucially:** a per-file parse can't resolve `add(...)`, so adapters
     **stash unresolved facts in metadata** (`calls`, `imports`, `bases`,
     `implements`) and emit **only edges with real in-file targets**.
5. **Merge** — all nodes/edges into one `networkx.MultiDiGraph`. Still an
   archipelago at this point.
6. **Cross-file resolution** — cashes in the breadcrumbs (see §5 decisions).
7. **Persist** — pickle → `.codesentry/graph.pkl` + JSON sidecar (counts, git
   commit, per-language files, resolution summary).

### Query time
`agent/loop.py` `run_agent()` — hand-rolled loop: chat → append assistant msg →
dispatch each tool call (Pydantic-validated) → append tool results → repeat until
the model stops → final `parse_structured` into `AnswerWithCitations`. Raises
`AgentIterationLimitError` on overflow. Malformed tool args are fed **back to the
model** as error text (it self-corrects) rather than crashing.

The 9 tools (`agent/tools.py`): `list_files`, `read_file`, `find_symbol`,
`get_definition`, `get_callers`, `get_callees`, `get_neighbors`, `grep`,
`list_languages`. Each takes a bound `ToolContext(graph, repo_root)` + a Pydantic
input, returns LLM-readable text with node ids and `file:line`.

### Module map
```
codesentry/
├── config.py            Settings + cached get_settings()
├── cli.py               index / stats / ask / review  (Typer app object = `app`)
├── graph/
│   ├── schema.py        Node, Edge, NodeType, EdgeType, make_node_id
│   ├── builder.py       build_graph() = walk + parse + merge + cross-file resolve
│   └── store.py         save_graph/load_graph/load_metadata + per_language_file_counts
├── languages/
│   ├── base.py          LanguageAdapter ABC, ADAPTERS registry, register_adapter,
│   │                    get_adapter_for_file, ImportIndex, resolve_import default,
│   │                    package_level_visibility flag
│   ├── python.py  javascript.py  typescript.py  go.py  java.py
├── retrieval/
│   ├── subgraph.py      extract_subgraph (1–2 hops, both directions), subgraph_nodes
│   └── snippets.py      get_snippet, find_nodes_by_name, get_node
├── agent/
│   ├── llm.py           LLMClient — THE ONLY FILE THAT IMPORTS openai
│   ├── schemas.py       tool inputs + Citation/AnswerWithCitations/ReviewComment/ReviewResult
│   ├── tools.py         the 9 tools, TOOL_REGISTRY, openai_tool_schemas()
│   ├── prompts.py       QA_SYSTEM_PROMPT (2 few-shot: Python+Go), REVIEW_SYSTEM_PROMPT
│   └── loop.py          run_agent, AgentIterationLimitError
└── review/
    ├── diff.py          parse_diff (unidiff) → FileDiff/DiffLine
    └── reviewer.py      review_diff — one structured call per changed file
```
Tests: 13 files in `tests/`; fixtures: `sample_python`, `sample_js`, `sample_ts`,
`sample_go`, `sample_java`, `sample_mixed` (Python+TS cross-language).
Each `sample_*` fixture intentionally contains an obvious bug (usually an
off-by-one in `count`/`Count`) for exercising `review`.

---

## 5. Design decisions & rationale (LOAD-BEARING — do not silently reverse)

**Decided by the MAINTAINER** (binding):

1. **Python 3.11+, pinned 3.12.** The scaffold defaulted to 3.14; tree-sitter
   grammar wheels may not exist there. All 5 grammars resolve cleanly on 3.12.
2. **Unresolved calls/imports → stash in metadata** (not placeholder edges with
   fake `target_id`). Invariant: *every edge target is a real node at every
   stage*. The builder later reads `metadata["calls"]` / `["imports"]`.
3. **JS and TS adapters are standalone** — TypeScript deliberately duplicates the
   ECMAScript traversal rather than sharing a `_ecmascript.py` module. Rationale:
   each adapter stays independent; "one language = one file"; no coupling.
4. **Go embedding → `INHERITS` edge** (plus `metadata["embeds"]`). It's the
   closest universal analog since embedding promotes methods/fields.
5. **Adapters own module→file resolution.** `LanguageAdapter.resolve_import()`
   (generic stem-match default in base; Python dotted-path, JS/TS relative-path,
   Java FQN+package overrides). Keeps the builder free of `node.language`
   branching — it reacts only to *metadata keys*.
6. **Call/heritage linking = import-scoped + global-unique.** Look in imported
   (+ own) files first; Go/Java also see same-directory files via
   `package_level_visibility`; else fall back to a name defined exactly **once**
   repo-wide. **Ambiguous → drop the edge. Never guess.**
7. **CLI renamed `codesentry` → `codesentry-agent`** (distribution name too).
   The importable package stays `codesentry`. See §6.
8. **This memory file**: committed+pushed, fully self-contained, includes the
   working agreement.

**Agent defaults (flagged at the time, changeable):**

- `FIELD` is in `NodeType` for schema stability but **no adapter emits it**.
- `register_adapter()` guards against duplicate languages / conflicting extensions.
- **Go emits ZERO `IMPLEMENTS` edges** — mandated by spec (interface satisfaction
  is structural/implicit; can't be resolved statically in Phase 1). Asserted by a
  test. Java's `implements` *is* declared, so Java DOES emit `IMPLEMENTS`.
- `run_agent()` takes an **extra `repo_root` param** (spec's signature omitted it,
  but tools need it to read source) and an optional `system_prompt` so `review`
  can reuse the loop.
- `review_diff(diff_text, graph, llm)` keeps the **exact spec signature** by
  sourcing context from node attributes (signatures/callers/callees) — no file
  reads, so no `repo_root` needed. **One `parse_structured` call per changed
  file** (spec-mandated; NOT the full tool loop).
- `read_file` prefixes line numbers (for citation accuracy); `grep` caps at 200
  matches.
- `_ALWAYS_IGNORE` hardcodes vendor/build/cache dirs (see §6 gotcha).

---

## 6. Gotchas & hard-won lessons (each cost real debugging time)

**tree-sitter**
- v0.26: `Query` has **no** `.captures()`. Use `QueryCursor(query).captures(node)`
  → `dict[str, list[Node]]`, or `.matches()` → `list[tuple[int, dict]]`.
  **In practice all 5 adapters use manual recursive tree walks**, not queries —
  better control over nesting, decorators, docstrings, call attribution.
- **Node identity fails.** `child_by_field_name()` returns *fresh wrapper objects*,
  so `node_a is node_b` is False for the same underlying node. **Compare
  `(start_byte, end_byte)` spans instead.** (This bug made `from models import
  User` list `models` as an imported name.)
- **Don't slice signatures to `body.start_byte`** — it swallows intervening
  comments. Reconstruct from header fields (name/parameters/return_type).

**Resolution**
- **Java constructors are named after their class**, so `User` matched both the
  CLASS and the constructor METHOD → ambiguity → dropped edges. Fixed with
  **two-tier call resolution**: try FUNCTION/METHOD first, then CLASS.
  Heritage resolution is restricted to CLASS only.
- Werkzeug shows ~2,361 "unresolved calls" — that's *correct*: stdlib/3rd-party
  symbols were never indexed. Precision over recall by design.

**Providers / LLM**
- The maintainer uses **`MODEL_API_KEY`** (not `OPENAI_API_KEY`) — see `config.py`.
  Their endpoint is **not** strict-OpenAI, so it does **not enforce structured
  output schemas**. Consequences:
  - `parse_structured` raises `StructuredOutputError` on non-conforming JSON.
  - `ask` falls back to `complete()` + regex citation extraction (`loop._finalize`).
  - `review` falls back to `_lenient_review()` — plain completion, JSON extracted
    (tolerating ```json fences), fields coerced (severity defaults to `warning`,
    message pulled from `message`/`comment`/`issue`/`description`/`text`/
    **`suggestion`**). This was a real crash the maintainer hit.
- **Never call the real API in tests.** All LLM tests use scripted fakes.

**Tooling / environment**
- **`uv pip install -e .` puts the launcher in the project's `.venv/bin`**, which
  is NOT on PATH. Use **`uv tool install --editable .`** → installs to
  `~/.local/bin` (on PATH). Editable = source changes take effect immediately.
- An **unrelated `codesentry` tool exists on the maintainer's PATH** — that's why
  the command was renamed to `codesentry-agent`. Both coexist. Do not remove theirs.
- **`node_modules` was being crawled** on a real repo because the root `.gitignore`
  didn't cover it at that level (monorepo `backend/` subdir). Hence `_ALWAYS_IGNORE`.
- **networkx and unidiff ship no type stubs** → `[[tool.mypy.overrides]]` with
  `ignore_missing_imports` in `pyproject.toml` (chosen over adding stub deps,
  which the spec doesn't list).
- **unidiff hunk headers must be exact** — hand-written diffs with wrong line
  counts raise `UnidiffParseError`. Let `git diff` generate diffs for tests/demos.

---

## 7. Verified evidence & known gaps

### Verified with real runs
| Check | Result |
|---|---|
| index <90s, >1000 nodes | ✅ **Werkzeug**: 139 files → 2,222 nodes / 4,725 edges in ~29s. **Flask**: 83 files → 977 nodes / 2,108 edges in ~28s |
| Multi-language merge | ✅ 5 fixtures in one dir → single graph, correct per-language counts. Werkzeug also picked up 1 stray `.js` |
| `ask` grounded citations | ✅ On a real FastAPI/PGVector codebase: full architecture overview, accurate `file:line` (e.g. `main.py:25`, `services/answer_service.py:151`), ~95k tokens |
| Tests / types / CI | ✅ 107 tests, `mypy --strict` clean, GitHub Actions added |

### Known gaps (honest list)
- **⚠️ ENV VAR INCONSISTENCY (landmine — verified, still open).** `config.py`
  reads **`MODEL_API_KEY`** (maintainer's commit `6f832a7`), but three other
  places still say `OPENAI_API_KEY`:
  - `.env.example` line 4
  - `README.md` (4 places: lines ~41, 122, 129, 182, 192)
  - `cli.py` error text: `"OPENAI_API_KEY is not set."` (lines ~112 and ~160)

  Following the docs sets the wrong variable **and the error message then tells
  you to set the wrong variable**. Not fixed because `config.py` is the
  maintainer's deliberate change — **ask them** whether to (a) accept both vars,
  or (b) update docs + error message to `MODEL_API_KEY`.
- **`codesentry-agent languages` command NOT implemented** — it's in the spec's
  CLI list. Everything needed exists (`ADAPTERS` registry). ~15 lines.
- **`review` live run UNVERIFIED after the fix** (`db91fee`). The maintainer hit a
  crash; the lenient fallback is committed + unit-tested but they have not re-run
  it live yet. **Confirm this early in Phase 2.**
- **`ask` citations table is empty** when the model writes citations inline in prose
  but leaves the structured `citations` list empty → no table renders. Fix offered
  (backfill via `loop._extract_citations` on the success path), **not implemented**.
- **Cosmetic**: the `Thinking...` spinner and the dim per-tool-call log interleave
  on the same terminal line, looking glitchy.
- **Only the root `.gitignore`** is honored; nested `.gitignore` files are not.
- **~28s index floor** is mostly fixed startup overhead (importing the openai SDK,
  loading 5 tree-sitter grammars), not parsing. Lazy imports would help.
- **No semantic search** (no embeddings) — finding code relies on names/grep.
- **Dynamic dispatch is invisible** (`getattr`, DI containers, decorators that
  rewrite behavior).

---

## 8. Next: Phase 2 (not started)

Per `docs/PHASE_1_SPEC.md`, Phase 2 covers:
- **Planner / executor / critic** agent pattern
- **Evaluation harness**
- **Benchmark numbers vs SWE-bench Lite** (and its multi-language variants)

Read the Phase 2 spec (when written) before doing anything. Confirm the Phase 1
gaps in §7 with the maintainer first — some may be in scope for Phase 2.

---

## 9. Working agreement (how to work with this maintainer)

**Cadence — follow this exactly:**
1. **Propose** the step (scope, design, flagged defaults) — then **WAIT**.
2. **Wait for explicit approval** ("let's go" / "go ahead"). Do not start on a
   design-question answer alone if they asked you to wait for approval.
3. **Implement.**
4. **Verify**: `uv run pytest` + `uv run mypy` **and** a real manual CLI check —
   not just green tests.
5. **Report** honestly (what worked, what's flagged, deviations).
6. **Commit only when they say so**; conventional commits; one build-order step =
   one focused commit. Push when asked.

**Hard rules (from `CLAUDE.md` + spec guardrails):**
- Follow the spec's build order **strictly**. Do not skip ahead.
- **Ask before design decisions with real trade-offs** — use a question tool with
  a clear recommendation. (Examples that warranted asking: call-resolution
  strategy, Go interface satisfaction, JS/TS code sharing.)
- **Do not add dependencies** not listed in the spec without asking.
- **Do not import `openai` outside `agent/llm.py`.**
- **Never branch on language outside `languages/`.**
- **Never call the real API in tests.**
- `mypy --strict` must pass on `codesentry/`. Type hints everywhere.
- Every module gets a one-paragraph top-of-file docstring.
- Prefer **boring, obvious code** over clever code.
- Cost: model is configurable (`CODESENTRY_MODEL`); use a cheap model for
  iteration; never hardcode a model.

**Style preferences observed:**
- They want **honesty over hype** — flag gaps plainly, don't overclaim. They
  actively push back with a DoD checklist and expect **evidence, not claims**.
- They value **real runs on real repos** as proof.
- They edit the code themselves between sessions — **check `git log` and the
  working tree before assuming you wrote everything**. Commit surgically (only
  your files); don't sweep their in-progress work into your commits.
- They ask "explain simply" often — plain-language explanations land well.

---

## 10. Quick start for a fresh agent

```bash
cd /mnt/elements/internshipProject/codesentry
uv sync
uv run pytest                       # expect 107 passing
uv run mypy                         # expect clean
uv run codesentry-agent --help      # index / stats / ask / review

# offline smoke test (no API key needed)
uv run codesentry-agent index tests/fixtures/sample_python
uv run codesentry-agent stats tests/fixtures/sample_python
```
`ask`/`review` need **`MODEL_API_KEY`** set — read it from `config.py`, **not**
from `.env.example`/README, which still say `OPENAI_API_KEY` (see the landmine
in §7).
The global `codesentry-agent` on this machine is an **editable** uv tool install —
source edits take effect immediately, no reinstall.
