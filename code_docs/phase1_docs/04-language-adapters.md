# 04 — Language Adapters (`codesentry/languages/`)

All five adapters follow the same shape, built on [tree-sitter](https://tree-sitter.github.io/tree-sitter/)
grammars. If you've read one, the others will feel familiar — this doc covers
the shared pattern once, then each language's specific behavior and quirks.

## The shared pattern

Every adapter (`PythonAdapter`, `JavaScriptAdapter`, `TypeScriptAdapter`,
`GoAdapter`, `JavaAdapter`) does roughly this in `parse_file`:

1. Parse `source` bytes with a module-level `tree_sitter.Parser` built once from
   the language's grammar package (e.g. `tree_sitter_python.language()`). The
   parser instances are created at import time, not per-call, for performance.
2. Build a `FILE` node for the whole file (id = the file's own path, since
   `qualified_name == file_path` for files), with `metadata["imports"]` (raw
   import records) and `metadata["parse_error"]` (`root.has_error`).
3. Recursively walk the tree collecting class/function/method definitions,
   emitting a `Node` for each and a `CONTAINS` `Edge` from its container (file
   or enclosing class) to it. A local `name_to_ids: dict[str, list[str]]` map is
   built along the way — every name defined *in this file*, and every node id
   that defines it.
4. Call a private `_resolve_local_edges` pass that, using `name_to_ids`, emits
   `INHERITS`/`IMPLEMENTS`/`CALLS` edges for any base-class or call-site name
   that resolves to **exactly one** definition *within this same file*. Ambiguous
   or cross-file references are left as raw metadata for the builder's
   cross-file resolution pass (see `03-graph-module.md`).
5. The whole body is wrapped in `try/except Exception` — any parse failure logs
   a warning and returns whatever was collected so far (possibly empty lists),
   never crashing the indexer.

Shared metadata conventions across all languages:

- `"calls"`: `list[{"name": str, "line": int}]` — every call-expression name
  seen inside a function/method body, whether or not it resolves.
- `"bases"`: `list[str]` — base class / superclass / embedded-type names (feeds
  `INHERITS`).
- `"implements"`: `list[str]` — interface names (feeds `IMPLEMENTS`; Go leaves
  this empty everywhere, TS and Java populate it).
- `"imports"`: `list[{"modules": [...], "names": [...], "line": int, ...}]` on
  the FILE node — raw import records for the builder's `resolve_import` pass.

A small helper `_walk(node)` (redefined per file — deliberately not shared, to
keep adapters standalone) does a depth-first generator walk of a tree-sitter
subtree, used to find every `call_expression` etc. inside a function body
regardless of nesting depth.

## Python (`languages/python.py`)

- Grammar: `tree_sitter_python`.
- File extensions: `.py`, `.pyi`.
- **Module + definition docstrings**: the first statement of a module or
  function/class body, if it's a bare string expression, is treated as a
  docstring. `_clean_docstring` strips string prefixes (`r`, `b`, `f`, `u`,
  case-insensitive) and quote characters (`"""`, `'''`, `"`, `'`), then
  `textwrap.dedent`s the result.
- **Decorators**: `@decorator` on a function or class becomes
  `metadata["decorators"]`, a list of decorator names with any call arguments
  stripped (e.g. `@app.route("/x")` → `"app.route"`).
- **Signature reconstruction**: `_signature` slices the source from the
  definition's start to the end of its most specific header field (return type
  → parameters → name for functions; superclasses → name for classes) — this
  means comments or blank lines between the header and `:` never leak into the
  signature text.
- **Imports**: both `import x.y` and `from x.y import a, b` are captured,
  recording the full module string(s) and any imported names (or `"*"` for
  wildcard imports).
- **`resolve_import` override**: turns a dotted module path (`a.b.models`) into
  candidate file paths — `a/b/models.py` or `a/b/models/__init__.py` — tried
  both relative to the repo root and relative to the importing file's own
  directory (to support Python's implicit relative-ish resolution within a
  package), falling back to the base class's by-stem matching if neither hits.

## JavaScript (`languages/javascript.py`)

- Grammar: `tree_sitter_javascript`.
- File extensions: `.js`, `.jsx`, `.mjs`, `.cjs`.
- Captures three ways a function/class can be declared: `function_declaration`,
  `class_declaration`, and **variable-assigned** arrow functions / function
  expressions / class expressions (`const f = () => {...}`, `const C = class
  {...}`) — the variable's name becomes the node's name. This matters because a
  large fraction of real-world JS code defines things this way rather than with
  bare `function`/`class` keywords.
- **JSDoc**: a `/** ... */` comment immediately preceding a declaration
  (`prev_named_sibling`) is captured as the docstring, with `_clean_jsdoc`
  stripping the `/**`/`*/` delimiters and leading `*` on each line.
- **Imports**: handles both ESM `import ... from "..."` and CommonJS
  `const x = require("...")`, recording which is which via `"kind": "esm" |
  "require"`.
- **`resolve_import` override** (`_resolve_relative`): only handles *relative*
  imports (`./x`, `../x`) — bare package names (`"react"`) are left to the base
  class's stem-matching fallback, since they're not part of the repo. Resolution
  tries the bare path, then each of `.js/.jsx/.mjs/.cjs` appended directly, then
  the same extensions under an `/index` suffix (mimicking Node's module
  resolution algorithm).

## TypeScript (`languages/typescript.py`)

- Grammar: `tree_sitter_typescript`, which ships **two** grammars — one for
  plain `.ts`/`.mts`/`.cts` and one for `.tsx` (JSX syntax) — so this adapter
  keeps two `Parser` instances and picks one based on the file's suffix.
- File extensions: `.ts`, `.mts`, `.cts`, `.tsx`.
- **This adapter intentionally does not share code with the JavaScript
  adapter**, even though both parse ECMAScript-family syntax — a documented
  design choice (see the file's module docstring) to keep the two languages
  independent rather than coupling them through shared internals.
- **TypeScript-specific node kinds**: in addition to classes and functions,
  `interface_declaration`, `type_alias_declaration`, and `enum_declaration` are
  all captured as `CLASS` nodes, distinguished by `metadata["kind"] = "class" |
  "interface" | "type" | "enum"` — this is the concrete example of "normalize to
  the closest universal concept, keep the detail in metadata" from the spec.
  Interface members become `METHOD` nodes (`type` and `enum` bodies are not
  descended into).
- **Heritage**: `extends` on a class → `bases` (→ `INHERITS`); `implements` on a
  class → `implements` (→ `IMPLEMENTS`); `extends` on an *interface* also maps
  to `bases` (interface-extends-interface is modeled as `INHERITS`, matching
  "closest universal concept").
- **Generics**: a class/interface's type parameters (`<T>`) are captured
  verbatim as `metadata["type_parameters"]`, not modeled structurally.
- **Decorators**: class-level and method-level decorators are captured
  (`_collect_decorators` / `_decorator_name`), same normalization as Python.
- **`import type` detection**: `_is_type_import` inspects the tokens between the
  statement start and the import clause for the `type` keyword, recording it as
  `metadata["imports"][i]["type"]` — useful for a reviewer reasoning about
  runtime vs. type-only dependencies.
- **`resolve_import` override**: same relative-path + extension/`index`
  resolution strategy as JavaScript, using the TS-specific extension list.

## Go (`languages/go.py`)

- Grammar: `tree_sitter_go`.
- File extensions: `.go`.
- `package_level_visibility = True` — files in the same directory form a Go
  package and can reference each other's exported *and* unexported identifiers
  without an explicit import; this is what lets the builder's `scope_files`
  treat sibling files in the same directory as visible to each other for name
  resolution.
- **Two-pass parse within a file**: pass 1 collects type declarations (structs,
  interfaces) and top-level functions; pass 2 collects methods (functions with
  a receiver). This ordering matters because a method's receiver struct must
  already exist in `name_to_ids` for the method to be attached under it
  directly (rather than falling back to file-level containment, later corrected
  by the builder's cross-file pass 2 if the struct turns out to live in another
  file — see `03-graph-module.md`).
- **Structs and interfaces** both become `CLASS` nodes with
  `metadata["kind"] = "struct" | "interface"`. Interface method signatures
  become `METHOD` nodes with `metadata["calls"] = []` (an interface method has
  no body to call anything from).
- **Embedding, not classical inheritance**: Go has no `extends`/`implements`
  keywords. Struct/interface **embedding** (an anonymous field, e.g. `struct {
  Base; Name string }`) is the closest analog and is modeled as `INHERITS`,
  with the embedded type names also kept separately in `metadata["embeds"]` (in
  addition to being copied into `metadata["bases"]` for the shared resolution
  path).
- **Interface satisfaction is explicitly NOT resolved** — this is a documented,
  intentional gap, not a bug. Go interfaces are satisfied *structurally* (any
  type with the right method set implicitly satisfies an interface, with no
  keyword linking them), which cannot be determined without full type-checking.
  The module docstring says so explicitly, and the adapter never emits
  `IMPLEMENTS` edges. `IMPLEMENTS` in the graph is populated only by the
  TypeScript and Java adapters.
- **Receiver methods**: `_receiver_type` extracts the receiver's bare type name
  (stripping a leading `*` for pointer receivers) and whether it's a pointer
  receiver (`metadata["receiver_pointer"]`). The method's qualified name is
  `"<ReceiverType>.<MethodName>"`.
- **Exported vs. unexported**: every function, method, and type records
  `metadata["exported"] = name[:1].isupper()`, mirroring Go's own
  capitalization-based visibility convention.
- **Godoc comments**: `_godoc_before` walks backward through contiguous
  `//`/`/* */` comment lines immediately above a declaration (stopping at the
  first gap or non-comment sibling) and joins them as the docstring — this is
  how Go's convention of doc comments directly above a declaration (no special
  delimiter syntax) gets captured.
- No `resolve_import` override — Go's import paths (e.g.
  `"example.com/x/models"`) don't map cleanly to a generic strategy in Phase 1,
  so it relies on the base class's by-stem fallback (matching the last path
  component against a unique file stem in the repo). This is a known,
  accepted limitation for external/module-path imports; it works well for
  imports of files within the same simple repo layout.

## Java (`languages/java.py`)

- Grammar: `tree_sitter_java`.
- File extensions: `.java`.
- `package_level_visibility = True` — same rationale as Go: files in the same
  package can reference each other's package-private members without an
  import.
- **Five type kinds**, all normalized to `CLASS` nodes with
  `metadata["kind"]`: `class`, `interface`, `enum`, `record`, `annotation`
  (`@interface`) — see `_TYPE_KINDS`.
- **Nested classes**: `_add_members` recurses into a type's body and, for any
  member that is itself a type declaration, calls `_add_type` again with a
  deeper `prefix` (dotted qualified name) — nested classes become `CLASS` nodes
  contained (via `CONTAINS`) by their enclosing class, arbitrarily deep.
- **Heritage**: `extends` on a class → `bases` (`INHERITS`); `extends` on an
  *interface* also → `bases` (interface-extends-interface, same "closest
  universal concept" treatment as TypeScript); `implements` → `implements`
  (`IMPLEMENTS`).
- **Annotations**: `@Override`, `@Deprecated`, custom annotations, etc. on a
  class or method are captured in `metadata["annotations"]` (name only, args
  stripped, package-qualification stripped to the simple name).
- **Visibility**: `public`/`private`/`protected` modifiers on a type are
  recorded in `metadata["visibility"]` when present (methods don't currently
  record this).
- **Constructors**: `constructor_declaration` is treated the same as
  `method_declaration` for extraction purposes (`_METHOD_DECLS`).
- **Call detection**: both `method_invocation` (regular calls) and
  `object_creation_expression` (`new Foo(...)`) contribute to `metadata["calls"]`,
  so constructor calls resolve the same way instance method calls do.
- **Javadoc**: only `/** ... */` block comments (not `//` line comments)
  immediately preceding a declaration are treated as Javadoc, cleaned the same
  way as JSDoc.
- **`resolve_import` override**: Java's `import a.b.ClassName;` syntax splits
  cleanly into a package (`a.b`) and a class name; the adapter looks up
  `files_by_package[package]` from the shared `ImportIndex` and returns the
  file whose stem matches the class name, falling back to the base class's
  stem-matching if that fails (e.g. static imports, wildcard imports).

## Adding a sixth language

Because of the contract in `languages/base.py`, adding a new language means:

1. Add the grammar package as a dependency (ask first — the spec forbids adding
   dependencies without asking).
2. Write `languages/<name>.py` implementing `LanguageAdapter.parse_file`,
   following the shared pattern above, and call `register_adapter(<Name>Adapter())`
   at module scope.
3. Import the new module from `languages/__init__.py` (so it self-registers).
4. Add a fixture directory under `tests/fixtures/sample_<name>/` and a
   `test_languages_<name>.py`.

Nothing in `graph/builder.py`, `retrieval/`, `agent/`, `review/`, or `cli.py`
needs to change — this is the architectural promise the whole system is built
around.
