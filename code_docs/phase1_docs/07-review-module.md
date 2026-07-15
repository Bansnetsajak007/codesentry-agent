# 07 — The Review Module (`codesentry/review/`)

Powers `codesentry-agent review`: takes a unified diff and the indexed graph,
and returns line-level `ReviewComment`s grounded in real graph context —
including context that crosses language boundaries.

## `review/diff.py` — unified diff parsing

Built on the `unidiff` library (`PatchSet`), not a hand-rolled parser.

```python
@dataclass(frozen=True)
class DiffLine:
    line_number: int   # target-side for additions, source-side for removals
    content: str

@dataclass
class FileDiff:
    path: str
    added: list[DiffLine]
    removed: list[DiffLine]
    raw: str            # the raw unified-diff text for just this file

    @property
    def changed_line_numbers(self) -> set[int]:
        ...  # union of every added and removed line number
```

`parse_diff(diff_text: str) -> list[FileDiff]` feeds the whole diff text to
`unidiff.PatchSet`, then for each patched file walks every hunk and every line
in it, splitting into `added` (lines with `line.is_added`, keyed by
`target_line_no` — the line number *in the new version* of the file) and
`removed` (`line.is_removed`, keyed by `source_line_no` — the line number *in
the old version*). `raw` keeps `str(patched_file)` — the original diff text
scoped to just that one file, which gets sent to the LLM verbatim so it can see
the actual `+`/`-` diff formatting, not just a line-number summary. Malformed
diff input raises `unidiff.UnidiffParseError` (not caught here — it propagates
to the caller; `cli.py` doesn't currently special-case this, so a malformed diff
surfaces as an uncaught Typer traceback).

Diffs spanning multiple languages in one patch are expected and handled
naturally — `parse_diff` doesn't care what language a file is; that's
determined per-file downstream, one file at a time.

## `review/reviewer.py` — per-file structured review

### `review_diff(diff_text, graph, llm) -> list[ReviewComment]`

For each `FileDiff` returned by `parse_diff`:

1. **Detect the file's language** via `get_adapter_for_file(Path(file_diff.path))`
   — if no adapter claims that extension (e.g. a `.md` or `.json` file changed
   in the same PR), `language` is `None` and the LLM is told
   `"unknown language"` rather than the review being skipped — a change to an
   unindexed file type can still be reviewed on the diff text alone, just
   without graph context.
2. **Find changed symbols** (`_changed_symbols`): scan every `CLASS`/
   `FUNCTION`/`METHOD` node in the graph whose `file_path` matches this diff's
   file and whose `[start_line, end_line]` span overlaps *any* changed line
   number (added or removed) — i.e. "which real definitions did this diff
   actually touch", sorted by `start_line` for a stable, readable ordering.
3. **Build a context block** (`_context_block`): for each changed symbol,
   render its qualified name, language, location, signature, callers, and
   callees (via `_related`, a direct `CALLS`-edge filter — same idea as the
   agent's `_neighbors_by_edge` but self-contained here rather than reused, to
   keep the review module's dependencies minimal). Critically, it separately
   calls out **cross-language neighbors**: any caller or callee whose
   `.language` differs from the changed symbol's own language gets listed under
   a `"cross-language neighbors:"` line. This is the concrete mechanism that
   satisfies the spec's requirement to flag suspicious cross-language mismatches
   (e.g. a Go backend handler's signature changing in a way that would break a
   TypeScript frontend caller) — the LLM sees explicitly which of the symbol's
   neighbors live in another language and can reason about compatibility.
   If no indexed symbols overlap the changed lines at all (e.g. only comments
   or blank lines changed, or the file isn't indexed), the context block is
   just `"(no indexed symbols overlap the changed lines)"`.
4. **One structured LLM call per file**: builds messages
   `[{"role": "system", "content": REVIEW_SYSTEM_PROMPT}, {"role": "user",
   "content": _user_message(file_diff, language, context)}]` and calls
   `llm.parse_structured(messages, ReviewResult)`. The user message
   (`_user_message`) is a compact template: which file and language, the raw
   diff text, the repository context block, and an explicit instruction to
   report only real defects anchored to real lines in *this* file.
5. Collects `result.comments` from every file into one flat list, returned to
   the caller (`cli.py`, which groups them by file for display).

Note that review is **not** an iterative tool-calling loop like `ask` — it's
one shot per file: gather context deterministically from the graph, hand it
plus the diff to the model, get structured comments back. This is a simpler
and cheaper flow, appropriate since the reviewer's job (spot defects in a
bounded diff, with pre-gathered context) doesn't need open-ended graph
exploration the way answering an arbitrary question does.
