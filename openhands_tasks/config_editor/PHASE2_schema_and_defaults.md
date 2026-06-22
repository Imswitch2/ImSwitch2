# Phase 2 — Schema model + default builder for the config editor

## Goal

Give the config editor a **normalized field model** that merges two sources:

1. **JSON Schema** for `managerProperties`, declared by a registry/plugin
   contribution (`manager_properties_schema`) — the *authority* for which
   manager-properties exist and their types.
2. **Editor templates** under `utility_scripts/builtin_templates/` — kept as
   **UI/default overlays** (labels, groups, tooltips, default values).

And make device editing **round-trip safe**: unknown fields (top-level keys and
`managerProperties`, **including nested dicts**) must survive a load → edit →
apply cycle, never silently dropped.

This is Phase 2 of `docs/design/plans/config-editor-discovery-and-schema.md`.
Read its "Phase 2: Schema And Default Builder" section. Phase 1 (the catalog)
is already merged in-tree — build on it. Do **only** Phase 2: no shared
validation refactor (Phase 4), no template browser (Phase 3).

## What already exists (read these first)

- **Phase 1 catalog** — `imswitch/imcontrol/model/configeditor/catalog.py`:
  `build_catalog(...) -> ManagerCatalog`, `ManagerInfo` (frozen dataclass),
  `ManagerCatalog.get(name_or_alias)`, `.by_category()`, `.display_name()`,
  `.all_manager_names()`. `ManagerInfo` currently has: `manager_name`,
  `category`, `kind`, `display_name`, `aliases`, `plugin_name`,
  `source_package`, `docs_url`, `supported_platforms`, `setup_templates`,
  `is_builtin`, `from_registry`. It does **not** yet carry the JSON schema.
- **Schema loading helper** — `imswitch/imcontrol/model/plugins/validation.py`:
  `resolve_schema(contribution) -> dict | None` loads & parses the JSON schema
  from `contribution.source_package` + `contribution.manager_properties_schema`
  via `importlib.resources`, returning `None` gracefully if missing/invalid or
  if `jsonschema`/the package is absent. **Reuse it — do not reimplement.**
- **Template shape** — each `builtin_templates/<category>/<Manager>.json` has:
  `display`, `category`, and field lists `top` (top-level device keys), `props`
  (-> `managerProperties.<key>`), `nested` (`{nest_key: [fields]}` ->
  `managerProperties.<nest_key>.<key>`). A field is
  `{key,label,type,default,req,grp,tip,opts}` where `type` ∈
  `text|int|float|bool|select|path`.
- **Editor functions to touch** in `utility_scripts/imswitch_config_editor.py`:
  - `_build_default_device(manager_name)` (~line 408) — builds a new device
    dict from a template (or the category blank). Currently template-only.
  - `_do_apply()` (~line 1642) — rebuilds `new_device` from the form widgets on
    every apply. **This is where unknown fields are lost.**
  - `_collect_unknown(schema)` (~line 1615) — surfaces unknown fields into
    "Properties"/"Other" raw tabs, **but deliberately skips nested dicts**
    (`not isinstance(v, dict)` at ~line 1627). Those nested-dict unknowns are
    therefore dropped on apply today — Phase 2 must stop that loss.

## What to build

### 1. Surface the schema on the catalog (small Phase 1 extension)

In `catalog.py`, add a field to `ManagerInfo`:

```python
properties_schema: dict | None = None   # resolved managerProperties JSON Schema
```

(Add it last with a default so existing construction stays valid.) In
`build_catalog`, for each registry contribution, populate it by calling
`resolve_schema(contrib)` from `plugins.validation`. Import lazily / guard so a
missing `jsonschema` or unreadable resource just yields `None` (it already
does). Legacy-scanned and built-in managers without a schema keep `None`.
Update Phase 1 tests only if a test asserts the exact field set.

### 2. New Qt-free modules

```
imswitch/imcontrol/model/configeditor/schemas.py
imswitch/imcontrol/model/configeditor/defaults.py
```

No Qt imports. Suggested API (adjust names sensibly, keep documented + typed):

**`schemas.py`** — normalized field model:

```python
@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    type: str                 # text|int|float|bool|select|path
    default: object
    required: bool
    group: str                # UI group/tab, e.g. "Basic"
    tooltip: str
    options: tuple[str, ...]
    location: str             # "top" | "prop" | "nested"
    nested_key: str | None    # set when location == "nested"

def normalized_fields(
    *,
    template: dict | None,        # the builtin_templates JSON for this manager
    json_schema: dict | None,     # resolved managerProperties JSON Schema
) -> list[FieldSpec]:
    ...
```

Merge rules for `normalized_fields`:
- Start from the template's `top`/`props`/`nested` fields (UI/default authority).
- For each property in the JSON Schema's `properties` that the template does
  **not** already cover, add a `FieldSpec` (location `"prop"`) inferring `type`
  from the schema (`integer`->int, `number`->float, `boolean`->bool, `string`
  with `enum`->select, else text), `required` from the schema's `required`
  list, `default` from the schema's `default` if present, `options` from `enum`.
- Where both define a property, the **template wins** for label/group/tooltip;
  the schema may still mark it required if the template didn't.
- If `template` is None, derive fields entirely from the schema. If both are
  None, return `[]`.

**`defaults.py`** — Qt-free builders:

```python
def build_default_device(
    manager_name: str,
    *,
    template: dict | None,
    json_schema: dict | None,
) -> dict:
    """Return a new device dict {managerName, managerProperties, ...}."""
```
- Must reproduce today's template-driven defaults exactly (port the logic from
  the editor's `_build_default_device`: `top` keys at the device root, `props`
  and `nested` under `managerProperties`, with the same type coercion and the
  `"null"`/bool/empty-string handling).
- For schema-only properties (no template entry) put the schema `default` when
  present, else a type-appropriate empty value; keep `managerProperties` a dict.

```python
def merge_preserving_unknown(
    original: dict,
    edited: dict,
    *,
    schema_top_keys: set[str],
    schema_prop_keys: set[str],
) -> dict:
    """Return `edited`, re-adding any keys present in `original` but missing
    from `edited` that the form could not have produced — i.e. unknown
    top-level keys and unknown managerProperties (INCLUDING nested dicts)."""
```
- Top level: any key in `original` not in `edited` and not a schema/known top
  key is copied over.
- `managerProperties`: any key in `original["managerProperties"]` missing from
  `edited["managerProperties"]` is copied over verbatim — **this must include
  nested dict values**, which is the bug being fixed.
- Never overwrite a value the user actually edited; only fill what's missing.

### 3. Wire the editor (behavior-preserving + the fix)

In `utility_scripts/imswitch_config_editor.py`:
- `_build_default_device(manager_name)` delegates to
  `defaults.build_default_device`, passing the template (`SCHEMAS.get(name)`)
  and the catalog's `properties_schema` for that manager (look it up via the
  Phase 1 `_MANAGER_CATALOG.get(name)`; `None` when unavailable). Wrap in the
  same graceful fallback used in Phase 1 so the editor still works if the model
  package is unavailable. Default-device output for existing templated managers
  must be **byte-identical** to before (guard with a test).
- `_do_apply()` must call `merge_preserving_unknown(self._device, new_device,
  ...)` before emitting `sig_apply`, so unknown fields — especially unknown
  nested-dict `managerProperties` — are retained. The existing raw-tab handling
  for scalar unknowns should keep working; this closes the nested-dict gap.

### Hard constraints
- No Qt imports in `schemas.py`/`defaults.py`.
- Do not modify `builtin_templates/`.
- Do not require `jsonschema` — degrade to `None` schema gracefully.
- Do not import hardware manager classes.
- Scope = Phase 2 only.

## Tests (acceptance gate)

Add `imswitch/imcontrol/_test/unit/test_configeditor_schema_defaults.py`:

1. **Default-device parity.** For at least two real templated managers, assert
   `defaults.build_default_device(name, template=SCHEMAS_json, json_schema=None)`
   equals the device dict the current `_build_default_device` would produce
   (load the template JSON directly from `builtin_templates/`). This guards the
   port.
2. **Schema-only defaults.** Build a fake contribution carrying a small inline
   JSON schema (write the schema to a temp package dir and register it, or pass
   the parsed schema straight into `build_default_device`/`normalized_fields`).
   Assert schema-only properties appear with correct types/defaults.
3. **`normalized_fields` merge.** Given a template with one prop labelled
   "Exposure" and a schema declaring that prop plus a second prop, assert: the
   merged list keeps the template label for the shared prop and adds a
   `FieldSpec` for the schema-only prop with the inferred type.
4. **Round-trip preservation (the fix).** Construct an `original` device with:
   an unknown top-level key, an unknown scalar `managerProperties` entry, and an
   unknown **nested dict** `managerProperties` entry. Simulate a form rebuild
   that omits all three (as `_do_apply` would), call
   `merge_preserving_unknown`, and assert **all three survive**, the nested dict
   intact. Add a regression assertion that the nested dict specifically is not
   dropped.
5. **Catalog carries schema.** Assert `build_catalog` populates
   `ManagerInfo.properties_schema` for a fake contribution that declares one,
   and leaves it `None` for built-ins/legacy.

## How to verify

The repo's `python -m pytest` currently fails at COLLECTION under some envs
because the root `conftest.py` only stubs napari when it is *missing*, not when
it is *present-but-broken* (a pydantic/napari mismatch). `configeditor` and
`plugins` need no napari. If pytest collection fails for that reason, verify by
importing the test classes and running their methods directly with plain
`python` (this is how Phase 1 was verified). Otherwise:

```
python -m pytest imswitch/imcontrol/_test/unit/test_configeditor_schema_defaults.py -q
python -m pytest imswitch/imcontrol/_test/unit/test_configeditor_catalog.py -q
python -c "import ast; ast.parse(open('utility_scripts/imswitch_config_editor.py').read()); print('parse OK')"
```

All Phase 1 + Phase 2 tests must pass; the editor must still parse/import.

## Deliverables
- `configeditor/schemas.py`, `configeditor/defaults.py`
- `ManagerInfo.properties_schema` added + populated in `catalog.py`
- Edited `_build_default_device` + `_do_apply` in the editor (behavior-
  preserving except the nested-unknown fix)
- `imswitch/imcontrol/_test/unit/test_configeditor_schema_defaults.py`
- A short end-of-run summary of changes + test results.
