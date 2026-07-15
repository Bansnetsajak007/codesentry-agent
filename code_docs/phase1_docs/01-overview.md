# 01 — Project Overview

## What CodeSentry is

CodeSentry is a **language-agnostic code-understanding assistant**. Point it at a
repository and it will:

1. **Index** the repo into a single graph model that represents files, modules,
   classes/structs/interfaces, functions, and methods, connected by relationships
   like "calls", "imports", "inherits from", and "implements".
2. **Answer questions** about the repo through an LLM agent that navigates that
   graph with tools, and is required to cite a real `file:line` for every factual
   claim it makes — no hallucinated function names or line numbers.
3. **Review a git diff**, producing line-level comments about real defects
   (correctness bugs, broken contracts, missing error handling, obvious
   performance problems) — explicitly not style nitpicks — using the same graph
   for cross-file and cross-language context.

The distinguishing idea is the middle word: *language-agnostic*. The graph schema
doesn't have a "Python class" node type and a separate "Go struct" node type — it
has one `CLASS` node type that both normalize to, with the language-specific
detail (decorators, receiver types, annotations, generics, ...) preserved in a
free-form `metadata` dict. Everything downstream of parsing — the graph builder,
retrieval, the agent, the CLI — is written once and works identically regardless
of which language(s) are in the repo.

## Why it's built this way

Most "chat with your codebase" tools either (a) work on raw text/embeddings with
no structural understanding of calls or inheritance, or (b) are hard-wired to one
language's AST. CodeSentry's bet is that a small, universal graph schema captures
enough structure (who calls whom, who imports whom, what implements what) to
ground an LLM's answers in real code relationships, while staying cheap to extend
to new languages — adding language N+1 is "write one adapter file", not "rewrite
the graph, retrieval, agent, and CLI".

## Phase 1 scope

Phase 1 (this documentation's scope) delivers exactly:

- A CLI (`codesentry-agent`) with `index`, `stats`, `ask`, and `review` commands.
- Five language adapters: **Python, JavaScript, TypeScript, Go, Java**.
- A single unified graph per repo, persisted to `.codesentry/graph.pkl`.
- An LLM agent (OpenAI, via a swappable `LLMClient` abstraction) with nine tools
  for navigating the graph and reading source.
- A diff reviewer that reasons about cross-language boundaries (e.g. a TypeScript
  frontend calling a Go backend).

**Explicit non-goals for Phase 1** (per the spec — do not build these without
asking): desktop app, GitHub integration, multi-agent orchestration, automated
refactoring, documentation generation, IDE extensions.

## Definition of done (from the spec)

Phase 1 is considered complete when, against real medium-sized multi-language
repos:

1. `index` completes in under 90 seconds and produces >1000 nodes.
2. `ask` produces answers with ≥3 correct file:line citations, correctly
   identifying the language of each cited location.
3. `review` on a small diff produces at least one non-trivial comment a human
   reviewer would agree with.
4. `stats` shows a correct per-language breakdown.
5. All tests pass, `mypy --strict` is clean, and the README has a real quickstart
   with example output for at least two languages.

Per the repository's own README, all fourteen build-order steps are complete,
101 tests pass, and `mypy --strict` is clean on `codesentry/`.

## What comes after Phase 1

Phase 2 (not started, out of scope for this documentation) is planned to add a
planner/executor/critic agent pattern, an evaluation harness, and benchmark
numbers against SWE-bench Lite and its multi-language variants.
