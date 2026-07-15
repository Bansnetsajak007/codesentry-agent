# CodeSentry — Phase 1 Documentation

This is a developer-facing deep dive into CodeSentry as it stands at the end of
Phase 1. It is written for someone who has never seen the codebase before and
wants to get productive quickly — both "what is this project trying to do" and
"how does each file actually work."

The spec that drove this build is `docs/PHASE_1_SPEC.md` at the repo root; this
documentation explains what was actually built against that spec, in more
implementation detail than the spec itself contains.

## Reading order

1. **[01-overview.md](01-overview.md)** — What CodeSentry is, why it exists, what
   Phase 1 delivers, and the non-goals. Start here if you're new to the project.
2. **[02-architecture.md](02-architecture.md)** — The end-to-end data flow (index →
   graph → retrieval → agent/review), the core design principle (language-agnostic
   graph), and how the package is laid out on disk.
3. **[03-graph-module.md](03-graph-module.md)** — The universal graph schema
   (`graph/schema.py`), the indexer and cross-file resolution algorithm
   (`graph/builder.py`), and persistence (`graph/store.py`). This is the heart of
   the system — read it carefully.
4. **[04-language-adapters.md](04-language-adapters.md)** — The `LanguageAdapter`
   contract (`languages/base.py`) and how each of the five adapters (Python,
   JavaScript, TypeScript, Go, Java) turns tree-sitter parse trees into universal
   nodes and edges, including their language-specific quirks.
5. **[05-retrieval-module.md](05-retrieval-module.md)** — How a set of seed nodes
   becomes an LLM-friendly neighborhood (`retrieval/subgraph.py`) and how nodes
   become exact source-line snippets (`retrieval/snippets.py`).
6. **[06-agent-module.md](06-agent-module.md)** — The `LLMClient` abstraction
   (`agent/llm.py`), tool schemas and dispatch (`agent/tools.py`,
   `agent/schemas.py`), prompts (`agent/prompts.py`), and the hand-rolled tool-use
   loop (`agent/loop.py`) that powers `codesentry-agent ask`.
7. **[07-review-module.md](07-review-module.md)** — Unified-diff parsing
   (`review/diff.py`) and the per-file diff reviewer (`review/reviewer.py`) that
   powers `codesentry-agent review`.
8. **[08-cli-and-config.md](08-cli-and-config.md)** — The Typer CLI (`cli.py`) and
   environment-driven configuration (`config.py`), with example commands and
   output.
9. **[09-testing-and-running.md](09-testing-and-running.md)** — How the test suite
   is organized, what each fixture exercises, how to run everything locally
   (including the parts that need an API key and the parts that don't), and the
   quality gates (`pytest`, `mypy --strict`).
10. **[10-extending-the-system.md](10-extending-the-system.md)** — Concretely, what
    files you'd touch to add a sixth language, a tenth tool, or a new CLI command,
    and the invariants you must not break while doing so.

## The one-paragraph summary

CodeSentry indexes a repository — in any mix of Python, JavaScript, TypeScript,
Go, and Java — into a single `networkx` graph of files, classes, functions, and
methods connected by `CONTAINS`/`CALLS`/`IMPORTS`/`INHERITS`/`IMPLEMENTS` edges.
An OpenAI-backed agent then answers questions or reviews diffs by calling tools
that query that graph and read real source lines, so every claim it makes can be
traced back to an actual `file:line`. Language-specific parsing is fully isolated
in `codesentry/languages/`; everything else — the graph, retrieval, the agent, the
CLI — is written once and never branches on language.
