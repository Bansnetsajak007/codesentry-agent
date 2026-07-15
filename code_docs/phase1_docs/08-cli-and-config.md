# 08 — CLI and Configuration

## `codesentry/config.py` — environment-driven settings

A single Pydantic model plus a cached singleton loader:

```python
class Settings(BaseModel):
    openai_api_key: str | None = None
    model: str = "gpt-4.1"
    max_tokens: int = 4096
    log_level: str = "INFO"
    openai_base_url: str | None = None

@lru_cache(maxsize=1)
def get_settings() -> Settings: ...
```

`get_settings()` calls `load_dotenv()` (reads a `.env` file in the working
directory if present — via `python-dotenv`, harmless no-op if absent) and then
reads five environment variables, applying defaults for anything unset:

| Env var | Settings field | Default |
|---|---|---|
| `OPENAI_API_KEY` | `openai_api_key` | `None` (required for `ask`/`review`) |
| `CODESENTRY_MODEL` | `model` | `"gpt-4.1"` |
| `CODESENTRY_MAX_TOKENS` | `max_tokens` | `4096` |
| `CODESENTRY_LOG_LEVEL` | `log_level` | `"INFO"` |
| `OPENAI_BASE_URL` | `openai_base_url` | `None` |

`@lru_cache(maxsize=1)` means settings are loaded from the environment exactly
once per process — every subsequent call to `get_settings()` returns the same
cached `Settings` instance. This is a deliberate simplicity choice for a CLI
tool (no need to support runtime reconfiguration mid-process); if you're
writing a test that needs different settings, you'd need to work around or
clear this cache rather than expect a second call to re-read the environment.

Model choice is never hardcoded anywhere else in the codebase — `cli.py`'s
`_build_llm` always goes through `settings.model` (with an optional per-command
`--model` override), which is what lets a developer set
`CODESENTRY_MODEL=gpt-4.1-mini` for cheap iteration and reserve the full model
for real runs, per the spec's cost-awareness guidance.

## `codesentry/cli.py` — the Typer app

A `typer.Typer()` app with five commands wired up (four implemented in Phase 1:
`index`, `stats`, `ask`, `review` — a `languages` command from the spec is
noted in the README as not yet implemented, with the same information
available via the language table in the README and via `list_languages`
metadata already surfaced in `stats`).

All commands share two conventions:

- The graph always lives at `<repo_path>/.codesentry/graph.pkl`
  (`_graph_path`, built from the module-level constant
  `_GRAPH_RELATIVE_PATH = Path(".codesentry") / "graph.pkl"`).
- Errors are reported with `console.print("[red]...[/red]")` followed by
  `raise typer.Exit(code=1)` — never a bare exception bubbling up as a Python
  traceback for expected failure modes (missing graph, missing API key, empty
  diff, non-directory repo path).

### `index REPO_PATH`

Resolves the path, verifies it's a directory, and — inside a `console.status`
spinner — calls `build_graph(repo_path)` then `save_graph(graph, graph_path,
repo_path=repo_path)`. Prints the resulting node/edge counts and the graph's
output path, then a `rich.table.Table` of per-language file counts
(`_print_language_table`, shared with `stats`).

### `stats REPO_PATH`

Requires a graph to already exist (errors out with a hint to run `index` first
if not). Loads both the pickle (`load_graph`) and the JSON sidecar
(`load_metadata`), and prints: repo path, indexed-at timestamp, git commit,
node/edge counts, and — if the sidecar has a `"resolution"` dict (it always
does, post-build) — unresolved call count, skipped file count, and parse-error
count, followed by the same per-language table. This command never touches the
LLM or needs an API key — it's purely reading persisted state, which is why the
README calls it out as fully exercisable offline.

### `ask REPO_PATH QUESTION [--max-iterations N] [--model NAME]`

Requires an existing graph *and* `OPENAI_API_KEY` to be set (both checked and
reported as friendly errors before doing any work). Loads the graph, builds an
`LLMClient` via `_build_llm`, and — inside a `console.status("Thinking...")`
spinner — calls `run_agent(question, graph, llm, repo_root=repo_path,
max_iterations=max_iterations)`. Prints the answer text, then (if any
citations were returned) a `rich.table.Table` titled "Citations" with File and
Lines columns.

Note: `run_agent` can raise `AgentIterationLimitError` if the model never
settles within `max_iterations` — this command does not currently catch that
exception, so it will surface as an uncaught traceback rather than a clean
`[red]...[/red]` error message. Worth knowing if you're debugging a run that
exhausted its iteration budget.

### `review REPO_PATH [--diff PATH] [--model NAME]`

Requires an existing graph and an API key, same checks as `ask`. Reads the diff
either from `--diff PATH` or, if omitted, from stdin (`sys.stdin.read()`) —
this is what makes `git diff | codesentry-agent review /path/to/repo` work.
Errors if the diff text is empty/whitespace-only. Calls `review_diff(diff_text,
graph, llm)` inside a spinner, then groups the returned comments by file
(`defaultdict(list)`), and for each file (sorted) prints each comment (sorted
by line number) with a severity-colored tag (`error` → red, `warning` →
yellow, `info` → cyan, via `_SEVERITY_COLORS`) and an optional dim "suggestion:"
line. If there are no comments at all, prints a green "No issues found."

### `_build_llm(settings, model) -> LLMClient`

A tiny shared helper: asserts `settings.openai_api_key` is already verified
non-`None` by the caller (both `ask` and `review` check this before calling
it), then constructs `LLMClient(api_key=..., model=model or settings.model,
base_url=settings.openai_base_url, max_tokens=settings.max_tokens)` — the
per-command `--model` flag, when given, takes priority over the configured
default.

## Example output (from the README)

```
$ uv run codesentry-agent index /path/to/repo
Indexed 66 nodes, 81 edges -> /path/to/repo/.codesentry/graph.pkl
  Files per language
┏━━━━━━━━━━━━┳━━━━━━━┓
┃ Language   ┃ Files ┃
┡━━━━━━━━━━━━╇━━━━━━━┩
│ go         │     4 │
│ python     │     4 │
│ typescript │     4 │
└────────────┴───────┘

$ uv run codesentry-agent stats /path/to/repo
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

`ask` and `review` require `OPENAI_API_KEY` in `.env` — see
`09-testing-and-running.md` for a full walkthrough including the fixture that
ships a deliberate off-by-one bug for exercising `review`.
