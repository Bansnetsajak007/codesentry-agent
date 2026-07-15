# 10 — Extending the System

A practical guide to the most likely changes a new contributor would make,
and the invariants to protect while making them. Per `CLAUDE.md`, any of these
that involve a real design trade-off (call resolution strategy, a new schema
concept, a new dependency, etc.) should be discussed before implementing, not
just built.

## Adding a sixth language

See `04-language-adapters.md`'s closing section for the mechanical steps. The
invariant to protect: **nothing outside `codesentry/languages/` may branch on
language**. If you find yourself writing `if language == "rust"` anywhere in
`graph/builder.py`, `retrieval/`, `agent/`, `review/`, or `cli.py`, that's a
sign the new concept needs to be normalized into the universal schema (or into
a node's `metadata` dict) instead.

Concretely:
1. Add the tree-sitter grammar package as a dependency — **ask first**, per the
   spec's "do not add dependencies without asking" guardrail.
2. `languages/<name>.py`: subclass `LanguageAdapter`, set `language_name` and
   `file_extensions`, implement `parse_file` following the shared pattern
   (FILE node → walk definitions → local edges only → stash cross-file hints in
   metadata), and call `register_adapter(<Name>Adapter())` at module scope.
3. Register the import in `languages/__init__.py` so it self-registers on
   package import.
4. `tests/fixtures/sample_<name>/`: 3–5 small files with at least one class, a
   cross-file call, an import, and one obvious bug (matching the pattern of
   the existing fixtures).
5. `tests/test_languages_<name>.py`: parse the fixture, assert the expected
   nodes/edges/metadata.

## Adding a tenth tool

1. Add a Pydantic input model to `agent/schemas.py` with descriptive
   `Field(description=...)`s — these become the model-facing schema text.
2. Add the tool function to `agent/tools.py`: `(ctx: ToolContext, params:
   YourInput) -> str`. Write the docstring carefully — it becomes the tool's
   LLM-facing description verbatim.
3. Register it in `TOOL_REGISTRY`.
4. Add a test in `test_tools.py` exercising it against a real built graph (no
   LLM needed for tool-logic tests).
5. If the tool changes what kinds of questions the agent should prefer it for,
   consider whether `agent/prompts.py`'s `QA_SYSTEM_PROMPT` needs an updated
   hint about when to use it.

## Adding a new graph concept (node type, edge type, or metadata field)

This is the highest-trade-off category, per the spec's guardrails — **stop and
ask before doing this**. A genuinely new universal concept (not expressible via
existing `NodeType`/`EdgeType` plus `metadata`) means touching
`graph/schema.py`, and probably every adapter, the builder's resolution logic,
and any tools/prompts that describe the schema to the model. Before proposing
one, check whether the concept can instead be represented as `metadata` on an
existing node/edge type (the pattern used for decorators, annotations,
generics, receiver types, visibility, etc.) — that's almost always the right
call and requires no schema change at all.

## Adding a new CLI command

Follow the existing pattern in `cli.py`: resolve and validate `repo_path`
first, check the graph exists (with the standard `[red]No graph found[/red]`
message pointing at `index`), check `OPENAI_API_KEY` up front if the command
needs the LLM (before doing any expensive work), wrap the actual operation in
a `console.status(...)` spinner, and report errors via `console.print("[red]...")`
+ `raise typer.Exit(code=1)` rather than letting exceptions propagate as raw
tracebacks for expected failure modes.

## Swapping the LLM provider

Everything outside `agent/llm.py` depends only on `LLMClient`'s two methods
(`chat_with_tools`, `parse_structured`) and the plain dataclasses/Pydantic
models they return (`ChatResponse`, `ToolCall`, `TokenUsage`,
`AnswerWithCitations`, `ReviewResult`). Swapping to Claude or a local model
means rewriting `agent/llm.py` to implement the same interface against a
different SDK — no other file should need to change. Preserve the "only file
that imports the provider SDK" rule for whatever the new provider is.

## Things that are explicitly out of scope right now

Per the spec's non-goals (Phase 1) — do not build these without asking first,
even if they'd be a natural-feeling next step: a desktop app, GitHub
integration, multi-agent orchestration, automated refactoring, documentation
generation, IDE extensions. These are candidates for Phase 2 and beyond, along
with the planner/executor/critic agent pattern and the SWE-bench evaluation
harness mentioned in the spec's closing section.
