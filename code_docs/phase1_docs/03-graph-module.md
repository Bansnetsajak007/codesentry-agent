# 03 — The Graph Module (`codesentry/graph/`)

This is the core of CodeSentry. If you understand this module, everything else
(retrieval, agent tools, review) is "just" reading from the structure this module
builds.

## `graph/schema.py` — the universal data model

Defines the vocabulary every language adapter must translate into.

```python
class NodeType(str, Enum):
    FILE = "file"
    MODULE = "module"
    CLASS = "class"       # also covers structs, interfaces, traits
    FUNCTION = "function"
    METHOD = "method"
    FIELD = "field"

class EdgeType(str, Enum):
    CONTAINS = "contains"
    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
```

`Node` (a Pydantic `BaseModel`, `extra="forbid"` so typos in field names fail
loudly) has these fields:

| Field | Meaning |
|---|---|
| `id` | Stable string `"<file_path>::<qualified_name>"`, built by `make_node_id()`. For FILE nodes, `qualified_name == file_path`, so the id is just the file path. |
| `type` | One of the six `NodeType` values. |
| `name` | The simple name (e.g. `"login"`). |
| `qualified_name` | The dotted/scoped name within the file (e.g. `"LoginHandler.login"`). |
| `file_path` | Repo-relative POSIX path. |
| `language` | `"python"`, `"javascript"`, `"typescript"`, `"go"`, or `"java"`. |
| `start_line`, `end_line` | 1-based, inclusive. |
| `signature` | The reconstructed header text (e.g. `"def login(self, request) -> Response"`), or `None`. |
| `docstring` | Docstring / JSDoc / godoc / Javadoc, normalized to plain text. |
| `metadata` | `dict[str, Any]` — everything language-specific lives here. |

`Edge` is simpler: `source_id`, `target_id`, `type: EdgeType`, and its own
`metadata` dict (e.g. `{"line": 42}` for a `CALLS` edge recording the call site).

`make_node_id(file_path, qualified_name)` is just
`f"{file_path}::{qualified_name}"` — but every adapter and the builder call this
one function, so the id format is guaranteed consistent everywhere.

## `languages/base.py` — the adapter contract

Every language plugs in by subclassing `LanguageAdapter` (abstract base class)
and implementing one method:

```python
class LanguageAdapter(ABC):
    language_name: ClassVar[str]
    file_extensions: ClassVar[set[str]]
    package_level_visibility: ClassVar[bool] = False

    @abstractmethod
    def parse_file(self, path: Path, source: bytes) -> tuple[list[Node], list[Edge]]:
        ...

    def resolve_import(self, module: str, importer: str, index: ImportIndex) -> str | None:
        ...  # default: match by unique filename stem; adapters override
```

Key contract details:

- `parse_file` returns only **local** nodes and edges: `CONTAINS` (file → its
  top-level definitions, class → its methods) and **intra-file** `CALLS`/
  `IMPORTS`/`INHERITS` — i.e. only edges whose target is a node the adapter can
  already see in the same file. Anything that might point outside the file (an
  imported symbol, a call to a function defined elsewhere, a base class in
  another file) is *not* emitted as an edge yet — it's stashed as raw data in
  `metadata` (`"imports"`, `"calls"`, `"bases"`, `"implements"`,
  `"receiver_type"`) for the builder to resolve in a second pass.
- `package_level_visibility` is `True` for Go and Java, where files in the same
  directory implicitly share scope (no explicit import needed to reference a
  sibling file's definitions). This flag changes how the builder computes each
  file's "visible" scope during call/heritage resolution (see below).
- `resolve_import` maps one language's import-string syntax to a repo-relative
  file path. The base implementation is a fallback: match the final path/dot
  component of the module string against a unique filename stem across the repo.
  Each adapter overrides this with real logic (Python dotted-path → file/package
  resolution, JS/TS relative-path + extension/`index` resolution, Java
  `package.ClassName` → file lookup by package).

`ImportIndex` is a small dataclass the builder constructs once and passes to
every adapter's `resolve_import` call — repo-wide lookup tables: `paths` (every
known file path), `by_stem` (filename stem → matching paths), `package_of` (file
→ its package/module, Go/Java), `files_by_package`.

The module-level `ADAPTERS: dict[str, LanguageAdapter]` registry and
`register_adapter()` / `get_adapter_for_file()` functions let the builder
dispatch a file to the right adapter purely by extension, without knowing
anything about specific languages. Each adapter module ends with
`register_adapter(PythonAdapter())` (etc.) so simply importing
`codesentry.languages` (which imports all five adapter modules) populates the
registry as a side effect — this is why `graph/builder.py` has the line
`import codesentry.languages  # noqa: F401  (imports register the adapters)`.

`register_adapter` also guards against two adapters claiming the same file
extension, raising `ValueError` if that happens.

## `graph/builder.py` — indexing and cross-file resolution

### `build_graph(repo_path: Path) -> nx.MultiDiGraph`

The top-level entry point (called by `codesentry-agent index`). Steps:

1. Resolve `repo_path` to an absolute path and load its `.gitignore` (if any) via
   `pathspec.PathSpec.from_lines("gitwildmatch", ...)`.
2. Walk the tree with `os.walk`, pruning directories in-place. Two layers of
   exclusion apply:
   - `_ALWAYS_IGNORE`: a hardcoded set of VCS/tool/build/dependency directory
     names (`.git`, `.codesentry`, `node_modules`, `vendor`, `dist`, `build`,
     `.venv`, `__pycache__`, `target`, `.gradle`, ... — see the file for the
     full list) that are skipped **unconditionally**, regardless of
     `.gitignore` content. This exists so a large repo doesn't get crawled into
     `node_modules` or vendored deps even if its `.gitignore` doesn't list them
     (or is missing/elsewhere).
   - The repo's actual `.gitignore` patterns, matched via `pathspec`.
3. For every remaining file, look up its adapter via `get_adapter_for_file`. No
   adapter → counted in `files_skipped` and ignored (not an error). Otherwise
   read the file's bytes and call `adapter.parse_file(rel_path, source)`, then
   merge the returned nodes/edges into the graph (`_merge`).
4. Call `_resolve_cross_file(graph)` to add the edges that span files (see next
   section), attach the resulting summary dict to `graph.graph["summary"]`
   (this is `networkx`'s per-graph attribute dict, distinct from per-node
   attributes), log it, and return the graph.

Note that `_iter_source_files` returns a **sorted** list of paths — this makes
indexing deterministic and reproducible across runs (important for tests and for
diffing graph output).

### Cross-file resolution — `_resolve_cross_file`

This is the most algorithmically interesting part of the codebase. It runs
*after* every file has been parsed independently, and stitches the per-file
graphs together into one connected graph. It works in four numbered passes:

**Setup.** Build `all_nodes` (id → `Node`), `file_nodes` (all `FILE` nodes),
`defs_by_name` (definition name → list of node ids, across the whole repo — used
for name resolution), and per-file lookup tables (`files_by_dir`, `by_stem`,
`package_of`, `files_by_package`) that become the `ImportIndex` passed to every
adapter's `resolve_import`.

**Pass 1 — IMPORTS edges.** For every file node, for every import entry in its
`metadata["imports"]`, for every module string in that entry, call
`adapter.resolve_import(module, file_path, index)`. If it resolves to a real,
different file in the graph, add an `IMPORTS` edge and record it in
`imported_files[file_path]` (used by pass 3 to compute what each file can "see").

**Name resolution helpers** (`scope_files`, `resolve_name`, `resolve_call`):

- `scope_files(file_path)` returns the set of files whose definitions
  `file_path` is allowed to reference: itself, plus everything it imports
  (from pass 1), plus — **only if** the file's adapter has
  `package_level_visibility = True` (Go, Java) — every other file in the same
  directory (approximating "same package").
- `resolve_name(name, file_path, self_id, kinds)` looks up all definitions named
  `name` of the given `NodeType`s (excluding the node itself), then narrows to
  those whose file is in `file_path`'s scope. **The resolution is deliberately
  conservative**: if exactly one candidate is in scope, return it. If none are
  in scope but there is exactly one candidate anywhere in the whole repo, return
  that (a weaker fallback for cases where scope computation misses something,
  e.g. missing import metadata). Otherwise — zero candidates, or more than one
  ambiguous candidate — return `None` and **drop the edge rather than guess**.
  This matches the spec's explicit requirement: *"If ambiguous, drop the edge —
  do not guess."*
- `resolve_call(name, file_path, self_id)` tries `resolve_name` against
  `(FUNCTION, METHOD)` first, then against `(CLASS,)` as a fallback (so a call
  to a class name — i.e. a constructor call like `User(...)` — still resolves,
  without ever confusing a function and a same-named class as ambiguous with
  each other, since they're tried as separate, sequential lookups rather than
  one combined candidate pool).

**Pass 2 — Go receiver methods.** A Go method like `func (u *User) Save() error`
is parsed by the Go adapter with `metadata["receiver_type"] = "User"`. If the
method wasn't already attached under a local `User` struct node during parsing
(i.e., the struct lives in a *different* file of the same package — legal in
Go), this pass resolves `receiver_type` to the real struct node (search scoped
to `NodeType.CLASS`), detaches the method from its temporary file-level
`CONTAINS` parent (`_detach_file_container`), and re-parents it under the struct
via a new `CONTAINS` edge. `_has_class_parent` checks whether re-parenting is
even needed (skip if the method already has a class-typed `CONTAINS` parent from
the adapter itself, which happens when the struct is in the same file).

**Pass 3 — cross-file CALLS.** For every `FUNCTION`/`METHOD` node, for every
call recorded in its `metadata["calls"]` (each a `{"name": ..., "line": ...}`
dict emitted by the adapter), try `resolve_call`. On success, add a `CALLS` edge
carrying the call-site line number in its own metadata. On failure, increment
`unresolved_calls` (reported in `stats`/logs — this is expected and normal; not
every call resolves, e.g. calls to third-party library functions never will,
since those aren't indexed).

**Pass 4 — cross-file INHERITS/IMPLEMENTS.** For every `CLASS` node, resolve
each name in `metadata["bases"]` to a `CLASS` node → `INHERITS` edge, and each
name in `metadata["implements"]` → `IMPLEMENTS` edge (TypeScript `implements`,
Java `implements`; Go structural interfaces are not resolved at all, per the
spec — see the language-adapters doc).

**Deduplication.** `_add_edge` is the single choke point for adding any
resolved edge; it keeps a running `existing: set[(source, target, type)]` set
(seeded from edges already present from the per-file parse) so the same logical
edge is never added twice, even if e.g. a name is both a local-parse hit and a
cross-file resolution hit.

**Summary.** The function returns a dict with `files_indexed`,
`files_with_parse_errors` (files where `metadata["parse_error"]` was set by the
adapter — see below), `nodes`, `edges`, `unresolved_calls`. `build_graph` adds
`files_skipped` to this dict before storing it, and it becomes both the log line
and the payload shown by `codesentry-agent stats`.

### Resilience to bad input

Every adapter's `parse_file` is wrapped in a broad `try/except Exception` (see
each language file) — a single malformed file logs a warning and yields
whatever partial nodes/edges were extracted before the exception, rather than
aborting the whole index run. Separately, if tree-sitter itself reports parse
errors (`root.has_error`) but doesn't raise, the adapter still emits whatever it
could extract and marks `file_node.metadata["parse_error"] = True`, which
surfaces in the `files_with_parse_errors` stat.

## `graph/store.py` — persistence

Two file formats, always written together:

- **The graph itself**: `pickle.dump(graph, ...)` to the path you give it (by
  convention `.codesentry/graph.pkl` inside the target repo — see `cli.py`).
  Pickle was chosen (per the spec) over a custom serialization for Phase 1
  simplicity; it round-trips the `networkx.MultiDiGraph` (including the
  attached `Node`/`Edge` Pydantic objects) exactly.
- **A JSON sidecar**, at `<path>.meta.json` (`_meta_path` just appends
  `.meta.json` to the pickle's suffix), containing:
  `codesentry_version`, `indexed_at` (UTC ISO timestamp), `repo_path`,
  `git_commit` (via `git rev-parse HEAD`, best-effort — `None` if not a git repo
  or `git` isn't available), `node_count`, `edge_count`, `files_per_language`,
  and `resolution` (the summary dict from the builder, if present).

`save_graph(graph, path, *, repo_path=None)` creates the parent directory if
needed, writes both files. `load_graph(path)` just unpickles. `load_metadata(path)`
reads and parses the JSON sidecar independently — this is what lets
`codesentry-agent stats` show metadata without needing to unpickle (though in
practice it currently loads both).

`per_language_file_counts(graph)` is a small standalone helper (also used
directly by `cli.py` right after indexing, before the sidecar is even
consulted) that counts `FILE` nodes grouped by `.language`, sorted
alphabetically.
