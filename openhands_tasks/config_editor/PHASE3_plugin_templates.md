# Phase 3 — Plugin setup templates in the config editor

## Goal

Let installed plugins ship ready-made device templates that show up in the
editor's "Add from Template…" browser, **without** adding files to the core
repo. The browser should present three clearly-labeled sources: **Built-in**
(core), **Plugin** (per plugin/package), and **My Templates** (user). Template
files that fail to load must surface as **visible errors**, never silent skips.

This is Phase 3 of `docs/design/plans/config-editor-discovery-and-schema.md`
("Phase 3: Plugin Setup Templates"). Phases 1 & 2 are already in-tree — build on
them. Do **only** Phase 3: no shared-validation refactor (Phase 4), no module
split (Phase 5).

## What already exists (read first)

- **Catalog** — `imswitch/imcontrol/model/configeditor/catalog.py`:
  `build_catalog() -> ManagerCatalog`, `ManagerCatalog.managers() ->
  list[ManagerInfo]`. Each `ManagerInfo` carries `manager_name`, `category`
  (editor plural, e.g. `"positioners"`), `kind`, `display_name`, `plugin_name`,
  **`source_package`** (the importable package for resolving resources, or
  None), and **`setup_templates: tuple[str, ...]`** (resource paths relative to
  `source_package`, e.g. `("templates/superstage-usb.json",)`). Built-in and
  legacy managers have `setup_templates == ()` / `source_package is None`.
- **Resource-loading precedent** — `imswitch/imcontrol/model/plugins/validation.py`
  `resolve_schema()` already loads a contribution resource via
  `importlib.resources.files(contribution.source_package) / relative_path` then
  `.read_text(...)`, returning None gracefully on any failure. Mirror this
  pattern (but for Phase 3 you must *report* failures, not swallow them).
- **Editor template browser** — `utility_scripts/imswitch_config_editor.py`:
  - `TemplateStore` (~line 2766) = user templates (file-backed). Leave as-is.
  - `LeftPanel._refresh_tmpl_tree()` (~line 3000) builds the QTreeWidget. It adds
    a **"Built-in"** top-level item (categories → managers, payload
    `("builtin", cat, mgr)`) and a **"My Templates"** item (payload
    `("user", cat, tname, tdata)`).
  - `LeftPanel._on_tmpl_double_click()` (~line 3064) dispatches on payload[0]:
    `"builtin"` emits `sig_tmpl_add.emit(cat, mgr)` (a manager-name string),
    `"user"` emits `sig_tmpl_add.emit(cat, copy.deepcopy(tdata))` (a device
    dict). The receiver `MainWindow._add_from_template` (~line 3561) accepts
    either a manager-name string or a device dict.

## Template content contract

Treat each `setup_templates` resource as a **single device dict** (the same
shape the editor adds: at minimum a `dict` containing `"managerName"`, usually
with `"managerProperties"` and other top-level keys) — i.e. the same thing a
"My Templates" entry holds. A resource that parses but is not a dict with a
`managerName` is a **load error**, not a valid template.

## What to build

### 1. Qt-free loader module

Create `imswitch/imcontrol/model/configeditor/templates.py` (no Qt imports).
Suggested API:

```python
@dataclass(frozen=True)
class PluginTemplate:
    name: str            # display name (file stem, or a "name" field if present)
    category: str        # editor category from the owning ManagerInfo
    manager_name: str    # owning manager id
    plugin_name: str     # source plugin label, e.g. "vendor-superstage"
    source_package: str
    resource: str        # the relative resource path
    device: dict         # parsed device dict

@dataclass(frozen=True)
class PluginTemplateError:
    manager_name: str
    plugin_name: str | None
    source_package: str | None
    resource: str
    message: str         # human-readable reason (file missing, bad JSON, not a device)

def load_plugin_templates(
    catalog: "ManagerCatalog",
) -> tuple[list[PluginTemplate], list[PluginTemplateError]]:
    """Load all setup_templates declared by catalog managers via
    importlib.resources. Never raises for a bad template — collects errors."""
```

Behavior:
- Iterate `catalog.managers()`; skip those with no `setup_templates` or no
  `source_package`.
- For each declared resource, resolve via
  `importlib.resources.files(source_package) / resource`, read text, `json.loads`.
- On success and a valid device dict → `PluginTemplate`. Use a top-level
  `"name"` field if the JSON has one, else the file stem as the display name.
- On any failure (package not importable, resource missing, JSON parse error,
  not a dict, missing `managerName`) → append a `PluginTemplateError` with a
  clear `message`. Do **not** raise, do **not** silently drop.
- Group/sort deterministically (e.g. by plugin_name, then category, then name).

### 2. Wire into the editor browser

In `LeftPanel`:
- Build plugin templates once (e.g. in `__init__` via `build_catalog()` +
  `load_plugin_templates(...)`), guarded by try/except so the editor still
  launches if the model package is unavailable (mirror Phase 1's bootstrap).
- In `_refresh_tmpl_tree()`, after "Built-in" and before/after "My Templates",
  add a **"Plugin Templates"** top-level item when any plugin templates or
  errors exist. Under it, group by **plugin name**, then category, then
  template. Each leaf payload: `("plugin", cat, name, device_dict)`.
- In `_on_tmpl_double_click()`, handle `"plugin"` exactly like `"user"`:
  `sig_tmpl_add.emit(cat, copy.deepcopy(device_dict))`.
- **Surface load errors visibly**: add an "Errors" child under "Plugin
  Templates" listing each `PluginTemplateError` (greyed/red leaf, tooltip =
  message, non-actionable payload). Do not pop a blocking dialog. (Structured
  diagnostics come in Phase 4; here a visible tree entry is enough.)

### Hard constraints
- No Qt imports in `templates.py`.
- Do not modify `builtin_templates/` or the user `TemplateStore`.
- Do not import hardware manager classes.
- Editor must still launch if the model package / catalog is unavailable.
- A malformed plugin template must never crash the browser or the loader.
- Scope = Phase 3 only.

## Tests (acceptance gate)

Add `imswitch/imcontrol/_test/unit/test_configeditor_plugin_templates.py`:

Use a temp importable package on `sys.path` (create a tmp dir with an
`__init__.py` and a `templates/` subdir holding JSON files), register a fake
contribution that points `source_package` at it and lists the resources in
`setup_templates`, build a catalog from that registry, then call
`load_plugin_templates`. Cover:

1. **Happy path.** A well-formed device template (`{"managerName": ...,
   "managerProperties": {...}}`) loads into a `PluginTemplate` with the right
   `category` (from the owning manager's kind), `plugin_name`, and `device`.
2. **Multiple templates / multiple managers** load and are grouped/sorted
   deterministically.
3. **Bad JSON** → exactly one `PluginTemplateError` with a message mentioning
   the parse failure; loader returns normally and still yields the other valid
   templates.
4. **Not-a-device** (valid JSON but no `managerName`, or not a dict) →
   `PluginTemplateError`, not a `PluginTemplate`.
5. **Missing resource** (declared in `setup_templates` but file absent) →
   `PluginTemplateError`.
6. **No plugins** (catalog with only built-ins) → empty lists, no error.

Clean up `sys.path`/`sys.modules` and the temp dir in teardown.

## How to verify

The repo's `python -m pytest` may fail at COLLECTION under some envs (root
`conftest.py` only stubs napari when missing, not when present-but-broken).
`configeditor` needs no napari — if collection fails for that reason, run the
test class methods directly with plain `python` (that's how Phases 1–2 were
verified). Otherwise:

```
python -m pytest imswitch/imcontrol/_test/unit/test_configeditor_plugin_templates.py -q
python -m pytest imswitch/imcontrol/_test/unit/test_configeditor_catalog.py imswitch/imcontrol/_test/unit/test_configeditor_schema_defaults.py -q
python -c "import ast; ast.parse(open('utility_scripts/imswitch_config_editor.py').read()); print('parse OK')"
```

All Phase 1–3 tests must pass; the editor must still parse/import.

## Deliverables
- `imswitch/imcontrol/model/configeditor/templates.py`
- `LeftPanel` changes: load plugin templates + render the "Plugin Templates"
  section (with a visible "Errors" group) + handle the `"plugin"` payload
- `imswitch/imcontrol/_test/unit/test_configeditor_plugin_templates.py`
- A short end-of-run summary of changes + test results.
