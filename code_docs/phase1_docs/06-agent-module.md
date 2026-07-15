# 06 — The Agent Module (`codesentry/agent/`)

This module is what powers `codesentry-agent ask`. It's a hand-rolled
tool-calling loop against the OpenAI Chat Completions API — no LangChain, no
LangGraph, per the project's explicit "roll it by hand" rule.

## `agent/llm.py` — the only file that imports `openai`

`LLMClient` wraps the OpenAI SDK behind exactly two methods, and this is the
**sole** file in the entire codebase permitted to `import openai` (a rule
enforced by convention/review, not by tooling, so respect it when editing).

```python
class LLMClient:
    def __init__(self, api_key, model, base_url=None, max_tokens=4096): ...
    def chat_with_tools(self, messages, tools) -> ChatResponse: ...
    def parse_structured(self, messages, schema: type[T]) -> T: ...
```

- **`chat_with_tools`** calls `client.chat.completions.create(..., tools=tools,
  tool_choice="auto")` and normalizes the raw SDK response into a small
  dataclass, `ChatResponse`, with `.content`, `.tool_calls` (a list of
  `ToolCall(id, name, arguments)` — arguments already JSON-parsed to a `dict`
  via `_parse_arguments`, defaulting to `{}` on any parse failure rather than
  raising), `.finish_reason`, and `.usage` (a `TokenUsage` dataclass:
  `prompt_tokens`, `completion_tokens`, `total_tokens`). Custom (non-function)
  tool call types from the SDK are filtered out — CodeSentry only uses function
  tools.
- **`parse_structured`** calls the beta `client.beta.chat.completions.parse()`
  endpoint with `response_format=<a Pydantic model class>`, and returns the
  parsed Pydantic instance directly. This is what both the final agent answer
  (`AnswerWithCitations`) and every review call (`ReviewResult`) use — it
  guarantees the LLM's output conforms to the schema rather than requiring the
  caller to hand-parse free text.
- If the SDK returns no parsed object (`response.choices[0].message.parsed is
  None`), `parse_structured` raises `ValueError` rather than silently returning
  something wrong.

Everything else in the codebase depends only on `ChatResponse`, `ToolCall`,
`TokenUsage`, and `LLMClient` — never on any `openai.*` type directly. This is
what makes a future provider swap (Claude, a local model) a one-file rewrite:
implement the same three-method interface against a different SDK.

## `agent/schemas.py` — the shared vocabulary

Pure Pydantic models, no logic. Two groups:

**Tool inputs** — one class per tool (`ListFilesInput`, `ReadFileInput`,
`FindSymbolInput`, `GetDefinitionInput`, `GetCallersInput`, `GetCalleesInput`,
`GetNeighborsInput`, `GrepInput`, `ListLanguagesInput`). Every field has a
`Field(description=...)` — these descriptions are not just documentation, they
become part of the JSON schema sent to the LLM (via `.model_json_schema()` in
`openai_tool_schemas()`), so the model literally reads them to decide how to
call the tool. Keep them accurate and specific if you ever add a tool.

**Structured results:**

- `Citation(file, start_line, end_line)` — one grounded reference.
- `AnswerWithCitations(answer, citations: list[Citation])` — what `ask` returns.
- `ReviewComment(file, line, severity: "info"|"warning"|"error", message,
  suggestion: str | None)` — one review finding.
- `ReviewResult(comments: list[ReviewComment])` — what `review` returns per file.

## `agent/tools.py` — the nine tools

Every tool is a plain function `(ctx: ToolContext, params: <InputModel>) ->
str`. `ToolContext` is just `{graph: nx.MultiDiGraph, repo_root: Path}` — the
two pieces of state every tool needs, bound once per agent run and threaded
through explicitly rather than via globals. Tools always return a **string**
(never raise for "not found" cases — they return a descriptive string instead),
because that string becomes the content of a `role: "tool"` chat message the
model reads directly.

| Tool | What it does | Built on |
|---|---|---|
| `list_files` | Lists indexed files, optional glob (`fnmatch`) and/or language filter | `_file_nodes` (filters graph for `NodeType.FILE`) |
| `read_file` | Reads a file or a 1-based inclusive line range, with line-number prefixes for easy citation | direct file read via `repo_root` |
| `find_symbol` | Finds nodes by simple or qualified name, optional language filter | `retrieval.snippets.find_nodes_by_name` |
| `get_definition` | Returns a node's exact source text by id | `retrieval.snippets.get_node` + `get_snippet` |
| `get_callers` | Reverse `CALLS` edges (who calls this node) | `_neighbors_by_edge(..., incoming=True)` |
| `get_callees` | Forward `CALLS` edges (what this node calls) | `_neighbors_by_edge(..., incoming=False)` |
| `get_neighbors` | Full N-hop neighborhood (1 or 2 hops, all edge types) | `retrieval.subgraph.extract_subgraph` + `subgraph_nodes` |
| `grep` | Regex search across indexed files' text, optional path glob, capped at 200 matches | `re.compile` + per-file line scan |
| `list_languages` | Per-language file counts in the indexed repo | `_file_nodes` grouped by `.language` |

Every tool's docstring literally *is* its LLM-facing description — the registry
extracts `func.__doc__.strip()` when building each `ToolDef`. There's no
separate prose to keep in sync; if you rewrite a tool's docstring, you've
rewritten what the model sees.

`_ref(node)` is the shared one-line formatting helper used by most tools:
`"<id> [<type>] <file_path>:<start_line> (<language>)"` — consistently
including the language tag is a direct requirement of the QA system prompt
("always note the language of a cited location").

`_neighbors_by_edge(ctx, node_id, edge_type, incoming)` is a focused variant of
subgraph extraction: unlike `extract_subgraph` (general N-hop, all edge types,
bidirectional), this filters `in_edges`/`out_edges` directly for exactly one
edge type in exactly one direction, deduplicates by target/source id, and
returns nodes sorted by id — used only by `get_callers`/`get_callees`, which
need precisely "one hop, one direction, one edge type" rather than a general
neighborhood.

`TOOL_REGISTRY: dict[str, ToolDef]` maps tool name → `ToolDef(name, description,
input_model, func)`. This is what `agent/loop.py` uses for name-based dispatch
when the model requests a tool call, and `openai_tool_schemas()` is what turns
the whole registry into the `tools=[...]` array the OpenAI API expects,
generating each tool's JSON parameter schema straight from its Pydantic input
model via `.model_json_schema()` — so a tool's input model **is** its schema;
there's no second definition to keep in sync.

## `agent/prompts.py` — the two system prompts

**`QA_SYSTEM_PROMPT`** (used by `ask`) establishes the grounding rules the whole
project is built around:

- Ground every factual claim in tool output — no relying on prior/general
  knowledge of "what a login handler usually does".
- Never invent function names, file paths, or line numbers; if a tool doesn't
  return something, say so rather than guessing.
- Cite `file:line` for every factual statement.
- Note the language of a cited location when relevant.
- Prefer the graph-navigation tools (`find_symbol`, `get_definition`,
  `get_callers`/`get_callees`, `get_neighbors`) over `grep`/`read_file`, which
  are for confirming details rather than primary navigation.
- Stop calling tools once there's enough grounded information — be concise.

It includes two worked few-shot examples, deliberately in **two different
languages** (Python and Go), per the spec's requirement, showing the
tool-call → answer pattern including the citation and language-tag format.

**`REVIEW_SYSTEM_PROMPT`** (used by `review`) scopes the reviewer narrowly:

- Focus **only** on correctness bugs, broken contracts, missing error handling,
  and obvious performance problems.
- Explicitly **not** style, formatting, naming, or subjective preferences.
- When a change touches one side of a language boundary (e.g. a TypeScript
  frontend calling a Go/Python backend), reason about the other side and flag
  suspicious mismatches (argument counts, field names, types) — this is the
  concrete mechanism behind the spec's cross-language review requirement.
- Every comment must reference a real file/line, explain what's wrong and why
  it matters, and may include a fix suggestion.
- If there are no real defects, return **no** comments rather than inventing
  nitpicks — an explicit instruction against reviewer noise.

**`FINAL_ANSWER_INSTRUCTION`** is a short standalone string appended as one
final user message once the agent stops calling tools, instructing the model to
produce its structured final answer using only what it already learned, with
every claim cited — used by `agent/loop.py`'s `_finalize`.

## `agent/loop.py` — the tool-use loop

### `run_agent(query, graph, llm, repo_root, max_iterations=15, system_prompt=QA_SYSTEM_PROMPT) -> AnswerWithCitations`

The whole loop, spelled out:

1. Build a `ToolContext(graph, repo_root)` and fetch `openai_tool_schemas()`
   once (tool definitions never change mid-run).
2. Seed `messages` with `[{"role": "system", "content": system_prompt},
   {"role": "user", "content": query}]`.
3. Loop up to `max_iterations` times:
   a. Call `llm.chat_with_tools(messages, tool_schemas)`.
   b. Accumulate token usage (`_accumulate`) and append the assistant's message
      to `messages` (`_assistant_message` — re-serializes `tool_calls` back into
      the OpenAI wire format, since the SDK needs the *next* request to include
      the assistant's own prior tool-call message for context).
   c. **If the model made no tool calls**, it's done reasoning — call
      `_finalize` (which appends `FINAL_ANSWER_INSTRUCTION` as a final user
      turn and calls `llm.parse_structured(..., AnswerWithCitations)`), log
      total token usage, and return.
   d. **Otherwise**, for each requested tool call, `_dispatch` it and append a
      `{"role": "tool", "tool_call_id": call.id, "content": result}` message
      for each — this is the standard OpenAI function-calling message shape,
      and the loop then goes back to step (a) with the tool results now in
      context.
4. If the loop exhausts `max_iterations` without the model ever stopping,
   log usage and raise `AgentIterationLimitError`.

### `_dispatch(ctx, call) -> str`

Looks up the tool by name in `TOOL_REGISTRY`; if unknown, returns an error
string (not an exception) so the *model* sees "unknown tool" and can recover
(e.g. by trying a different tool) rather than the whole run crashing. If found,
validates `call.arguments` against the tool's Pydantic input model via
`.model_validate()` — a `ValidationError` here (e.g. the model hallucinated a
wrong parameter name or type) also becomes a returned error string, not a
raised exception. Finally, executes the tool inside its own `try/except
Exception`, so even a bug inside a specific tool's implementation degrades to
an error message the model can see rather than aborting the whole agent run.
This three-layer defensiveness (unknown tool / bad args / execution error, all
becoming strings) is exactly what the spec's "Agent tests must cover: happy
path, multi-tool-call path, iteration-limit error, malformed tool arguments"
requirement is testing.

### Logging

Every tool call is logged via `rich` to stderr (`_log_tool_call`): the tool
name, a compact `key=value` rendering of its arguments, and the length of the
result — enough to follow an agent run live without printing potentially huge
tool output. Final cumulative token usage (`prompt`/`completion`/`total`) is
logged once at the end of every run, win or lose — this satisfies the spec's
"log token usage after each agent run" cost-awareness requirement.

### `AgentIterationLimitError`

A plain `RuntimeError` subclass — its existence as a distinct type (rather than
a bare `RuntimeError`) is what lets `cli.py` (if it ever needs to) or callers
distinguish "the agent ran out of budget" from other failure modes.
