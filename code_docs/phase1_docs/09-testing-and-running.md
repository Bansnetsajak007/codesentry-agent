# 09 — Testing and Running Locally

## Quality gates

Per the project's working agreement, every build-order step ends with tests
green, a manual CLI check, and a commit. The two automated gates are:

```bash
uv run pytest              # tests, no network access ever
uv run mypy                # mypy --strict on codesentry/ (see pyproject.toml)
```

`mypy` is configured in `pyproject.toml` (`[tool.mypy]`) with `strict = true`,
scoped to `files = ["codesentry"]` (tests aren't held to the same strictness).
`networkx` and `unidiff` ship no type stubs, so they're explicitly marked
`ignore_missing_imports = true` rather than pulling in an out-of-spec stub
dependency.

**No test ever calls the real OpenAI API.** Everywhere an `LLMClient` is
needed, tests substitute a hand-written fake object implementing the same
`chat_with_tools`/`parse_structured` interface (duck-typed — Python doesn't
enforce the `LLMClient` type at the call site, so a fake with matching method
signatures works transparently). This is what makes the whole suite runnable
offline and in CI without secrets.

## Test module map

| Test file | Covers |
|---|---|
| `test_smoke.py` | Package imports and has `__version__` — the "scaffolding works" check. |
| `test_graph.py` | `graph/schema.py` — `Node`/`Edge` validation, `make_node_id`, enum values. |
| `test_languages_base.py` | `LanguageAdapter` registry mechanics: registration, duplicate-extension rejection, `get_adapter_for_file` dispatch, using a minimal stub adapter. |
| `test_languages_python.py` | `PythonAdapter` against `fixtures/sample_python/`: functions, classes, methods, decorators, docstrings, intra-file calls/inheritance. |
| `test_languages_javascript.py` | `JavaScriptAdapter` against `fixtures/sample_js/`: declarations, arrow/function-expression forms, ESM + `require`, JSDoc. |
| `test_languages_typescript.py` | `TypeScriptAdapter` against `fixtures/sample_ts/` (and `.tsx`): interfaces/type aliases as `CLASS`, `implements`, generics. |
| `test_languages_go.py` | `GoAdapter` against `fixtures/sample_go/`: structs, interfaces, receiver methods, embedding, no `IMPLEMENTS` edges. |
| `test_languages_java.py` | `JavaAdapter` against `fixtures/sample_java/`: `extends`/`implements`, nested classes, annotations. |
| `test_builder.py` | `graph/builder.py` end-to-end: `.gitignore` handling, cross-file `IMPORTS`/`CALLS`/`INHERITS`/`IMPLEMENTS` resolution, ambiguous-name edges correctly dropped, the mixed-language fixture merging correctly into one graph. |
| `test_retrieval.py` | `retrieval/subgraph.py` and `retrieval/snippets.py`: hop-depth clamping, edge-type filtering, snippet margins, symbol lookup. |
| `test_tools.py` | Every non-LLM agent tool in `agent/tools.py` against a real built graph (no LLM involved — these are the tools' own logic, independent of the loop). |
| `test_agent.py` | `agent/loop.py` with a scripted `FakeLLM`: happy path, multi-tool-call path, `AgentIterationLimitError` on non-convergence, malformed tool arguments recovering gracefully. |
| `test_review.py` | `review/diff.py` and `review/reviewer.py`: diff parsing, changed-symbol matching, context-block construction (including cross-language neighbor detection), with a fake `LLMClient`. |

Total: **101 passing tests** at the end of Phase 1 (per the README), covering
roughly the ~70% non-agent-code coverage target from the spec.

## Fixtures (`tests/fixtures/`)

Each language gets its own small, realistic sample project — enough to exercise
classes, cross-file calls, imports, and one obvious bug:

```
sample_python/  models.py, repository.py, service.py, utils.py
sample_js/      models.js, repository.js, service.js, utils.js
sample_ts/      models.ts, repository.ts, service.ts, utils.ts, widget.tsx
sample_go/      models.go, repository.go, service.go, utils.go
sample_java/    User.java, AdminUser.java, Named.java, UserRepository.java, UserService.java, Utils.java
sample_mixed/   server.py, client.ts   — a Python backend + TS frontend, used to test
                                          that the merged graph correctly combines two
                                          languages in one index
```

Per the README, each `sample_*` fixture intentionally contains one obvious bug
(commonly an off-by-one in a `count`/`Count` method) — this is what the manual
`review` walkthrough below exercises, and what `test_review.py`/language tests
implicitly validate parsing around.

## Running things manually, end to end

### 1. Fully offline (`index` / `stats`) — no API key needed

```bash
uv run codesentry-agent index tests/fixtures/sample_python
uv run codesentry-agent stats tests/fixtures/sample_python
```

To see the language-agnostic merge in action — several languages combined into
one graph:

```bash
mkdir /tmp/mixed
cp tests/fixtures/sample_python/*.py tests/fixtures/sample_go/*.go \
   tests/fixtures/sample_ts/*.ts /tmp/mixed/
uv run codesentry-agent index /tmp/mixed
uv run codesentry-agent stats /tmp/mixed
```

### 2. `ask` / `review` — needs `OPENAI_API_KEY`

```bash
cp .env.example .env   # then edit .env and set OPENAI_API_KEY
```

`.env.example` documents all five settings (`OPENAI_API_KEY`,
`CODESENTRY_MODEL` — defaults to `gpt-4.1`, use `gpt-4.1-mini` for cheap
iteration — `CODESENTRY_MAX_TOKENS`, `CODESENTRY_LOG_LEVEL`,
`OPENAI_BASE_URL`).

```bash
uv run codesentry-agent index tests/fixtures/sample_python
uv run codesentry-agent ask tests/fixtures/sample_python \
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
| uv run codesentry-agent review tests/fixtures/sample_python
```

## Installing and running the CLI

```bash
uv sync                              # installs deps into .venv, Python 3.11+
uv run codesentry-agent <command>    # run any CLI command via uv
```

The CLI entry point is named `codesentry-agent` (declared in
`pyproject.toml`'s `[project.scripts]`, pointing at `codesentry.cli:app`) —
note this is *not* `codesentry`, a deliberate rename (see the repo's commit
history) to avoid colliding with another `codesentry` binary that might already
be on a developer's `PATH`.
