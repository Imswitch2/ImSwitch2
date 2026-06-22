# Phase 1 — Registry-backed manager catalog for the config editor

## Goal

Make the standalone ImSwitch config editor build its list of available device
managers from the **device plugin registry** (`DevicePluginRegistry`) instead of
scanning the source tree for `*Manager.py` files. Installed plugins must appear
in the editor with their display name, aliases, and docs URL **without** adding
anything to `utility_scripts/builtin_templates/`.

This is the first phase of the plan in
`docs/design/plans/config-editor-discovery-and-schema.md`. Read that plan's
"Phase 1: Catalog Service" section before starting. Do **only** Phase 1.

## Background — the two worlds today

**Runtime / plugin world** (source of truth going forward):
`imswitch/imcontrol/model/plugins/registry.py`

- `build_default_registry(*, discover=True) -> DevicePluginRegistry` builds a
  registry with built-ins and discovered plugins. Use `discover=False` in tests.
- `registry.list_contributions(kind=None) -> list[DeviceManagerContribution]`
  returns all contributions (optionally filtered by kind).
- A `DeviceManagerContribution` (see `plugins/manifest.py`) has:
  `id`, `kind`, `display_name`, `python_name`, `plugin_name`, `source_package`,
  `manager_name_aliases: tuple[str, ...]`, `manager_properties_schema`,
  `setup_templates: tuple[str, ...]`, `docs_url`,
  `supported_platforms: tuple[str, ...]`.
- The contribution **`id` is the manager class name** used in setup files'
  `managerName` field (e.g. `"HamamatsuManager"`, `"AVManager"`). Aliases are
  alternative accepted names. See `plugins/builtins.py` for the 9 built-ins.
- Registry **`kind` values are singular**: `detector`, `laser`, `positioner`,
  `rotator`, `rs232`, `slm`, `flip_mirror`, `stand`, `pulse_generator`
  (see `ALL_VALID_KINDS` / `REGISTRY_BACKED_KINDS` in `plugins/manifest.py`).

**Editor world** (`utility_scripts/imswitch_config_editor.py`, ~3900 lines):

- Editor **category names are plural / camelCase** and live in
  `_CATEGORY_REGISTRY` (~line 34): `detectors`, `lasers`, `positioners`,
  `rotators`, `rs232devices`, `slms`, `flipMirrors`, `pulsegen`, `stands`.
- `_discover_managers()` (~line 100) walks
  `imswitch/imcontrol/model/managers/<dir>/*Manager.py` per category to build
  `category -> [manager_name, ...]`. This is the filename scanner to be
  superseded (but kept as a fallback — see below).
- `_manager_display_name(name)` (~line 313) returns the template `display` or
  the bare name.
- `_get_category_for_manager(name)` (~line 300) returns the editor category for
  a manager (from templates, then from discovered).
- `_all_known_managers()` (~line 321) returns all templated + discovered names.
- Module globals derived at import: `SCHEMAS`, `DISCOVERED_MANAGERS`,
  `CAT_MANAGERS` (category -> manager names). These feed the "Add device" picker.

## Kind → editor-category mapping (authoritative for this task)

| registry kind     | editor category |
|-------------------|-----------------|
| detector          | detectors       |
| laser             | lasers          |
| positioner        | positioners     |
| rotator           | rotators        |
| rs232             | rs232devices    |
| slm               | slms            |
| flip_mirror       | flipMirrors     |
| stand             | stands          |
| pulse_generator   | pulsegen        |

## What to build

### 1. New pure-Python module (no Qt import anywhere in it)

Create the target service package and the catalog module:

```
imswitch/imcontrol/model/configeditor/__init__.py
imswitch/imcontrol/model/configeditor/catalog.py
```

`catalog.py` must NOT import PyQt or any Qt module, and must NOT import hardware
manager classes. It only reads registry metadata + optionally does the legacy
filename scan.

Suggested API (adjust names if you find something cleaner, but keep it Qt-free
and documented):

```python
@dataclass(frozen=True)
class ManagerInfo:
    manager_name: str            # contribution id == setup-file managerName
    category: str                # editor category (plural), e.g. "detectors"
    kind: str                    # registry kind (singular), e.g. "detector"
    display_name: str
    aliases: tuple[str, ...]
    plugin_name: str | None
    source_package: str | None
    docs_url: str | None
    supported_platforms: tuple[str, ...]
    setup_templates: tuple[str, ...]
    is_builtin: bool
    from_registry: bool          # True from registry, False from legacy scan

class ManagerCatalog:
    def managers(self) -> list[ManagerInfo]: ...
    def by_category(self) -> dict[str, list[ManagerInfo]]: ...   # category -> infos
    def get(self, manager_name: str) -> ManagerInfo | None: ...  # match id OR alias
    def category_for(self, manager_name: str) -> str | None: ...
    def display_name(self, manager_name: str) -> str: ...        # falls back to name
    def all_manager_names(self) -> list[str]: ...                # sorted ids

def build_catalog(
    registry=None,                  # default: build_default_registry(discover=True)
    *,
    include_legacy_scan: bool = True,
    managers_root: Path | None = None,  # for tests; default resolves from repo
) -> ManagerCatalog: ...
```

Behavior requirements for `build_catalog`:

- If `registry is None`, call `build_default_registry(discover=True)`.
- Map every contribution to a `ManagerInfo` via the kind→category table above.
  Skip (don't crash on) kinds not in the table, but log/ignore gracefully.
- `is_builtin`: derive from `plugin_name == "imswitch-core"` (the built-ins use
  that plugin name). `from_registry=True` for all registry entries.
- When `include_legacy_scan` is True, also run the existing filename scan and
  add any manager class names **not already present** from the registry, marked
  `from_registry=False`, `is_builtin=False`, with `display_name == manager_name`
  and empty aliases/metadata. This preserves today's behavior for in-tree
  managers that aren't registered yet (e.g. `TISManager`).
- `get()` resolves by `manager_name` (id) first, then by any alias.

### 2. Wire the editor to the catalog (minimal, behavior-preserving)

In `utility_scripts/imswitch_config_editor.py`:

- Build a module-level catalog once at import, e.g.
  `MANAGER_CATALOG = build_catalog()` wrapped in a try/except that falls back to
  the old path if the import fails (so the editor still launches if the model
  package is unavailable). Import as:
  `from imswitch.imcontrol.model.configeditor.catalog import build_catalog`.
  The editor already resolves the repo on `sys.path`; if not, add the repo root
  (two levels up from the script) to `sys.path` before importing, guarded.
- Replace the **bodies** of `_discover_managers()`, `_all_known_managers()`,
  `_manager_display_name()`, and `_get_category_for_manager()` so they consult
  `MANAGER_CATALOG` when it is available, and fall back to the existing logic
  when it is not. Keep the same signatures and return types so the rest of the
  GUI is untouched.
  - `_discover_managers()` should return `category -> [names]` exactly as before
    (now sourced from the catalog union).
  - `_manager_display_name()` should prefer a template `display` (so existing
    rich templates win), then the catalog display name, then the bare name.
- `CAT_MANAGERS`, the picker, and template lookups must keep working. Templated
  managers must still show their template display names and fields.

### Hard constraints

- Do **not** modify anything under `utility_scripts/builtin_templates/`.
- Do **not** import PyQt in `catalog.py`.
- Do **not** import hardware manager classes to read metadata.
- Do **not** remove the legacy filename scanner — it becomes the fallback.
- Keep the editor importable/launchable even if the registry import fails.
- Scope: Phase 1 only. No schema editing (Phase 2), no template loading
  (Phase 3), no validation refactor (Phase 4). Don't touch those.

## Tests (this is the acceptance gate)

Add `imswitch/imcontrol/_test/unit/test_configeditor_catalog.py` (match the
existing unit-test location/style — check `imswitch/imcontrol/_test/`). Cover:

1. **Built-ins present.** `build_catalog(registry=build_default_registry(discover=False))`
   yields the 9 built-in managers, each in the correct editor category with the
   right `display_name` (e.g. `HamamatsuManager` -> category `detectors`,
   display `"Hamamatsu ORCA camera"`).
2. **Alias resolution.** `catalog.get("hamamatsu.orca")` resolves to the
   `HamamatsuManager` info; `catalog.get("ThorlabsMFF")` resolves to
   `ThorlabsMFFManager`.
3. **Non-templated / legacy managers.** With `include_legacy_scan=True` against
   the real tree, a known in-tree-but-unregistered manager (e.g. `TISManager`,
   verify it exists under `managers/detectors/`) appears with
   `from_registry=False`. (If unavailable in the env, simulate via a temp
   `managers_root`.)
4. **EXIT CRITERION — injected fake plugin contribution.** Build a fresh
   `DevicePluginRegistry`, register a fake contribution (e.g.
   `id="vendor.superstage"`, `kind="positioner"`,
   `display_name="Vendor SuperStage"`,
   `python_name="vendor_plugin.stage:SuperStageManager"`,
   `manager_name_aliases=("superstage.legacy",)`,
   `docs_url="https://example.com/superstage"`), then
   `build_catalog(registry=that_registry, include_legacy_scan=False)`. Assert:
   - it appears under category `positioners`,
   - `display_name == "Vendor SuperStage"`,
   - `catalog.get("superstage.legacy")` resolves to it,
   - `docs_url` is preserved,
   - and that **no file under `utility_scripts/builtin_templates/` was created
     or modified** by the test run (the manager is discoverable purely from the
     contribution).

## How to verify

Run the new tests and the existing plugin/registry tests:

```
python -m pytest imswitch/imcontrol/_test/unit/test_configeditor_catalog.py -q
python -m pytest imswitch/imcontrol/_test -k "plugin or registry or catalog" -q
```

All must pass. Also confirm the editor module still imports:

```
python -c "import ast; ast.parse(open('utility_scripts/imswitch_config_editor.py').read())"
```

(A full GUI launch needs a display; a clean import/parse plus passing unit tests
is sufficient for this phase.)

## Deliverables

- `imswitch/imcontrol/model/configeditor/{__init__,catalog}.py`
- Edited `utility_scripts/imswitch_config_editor.py` (4 function bodies + catalog
  bootstrap, behavior-preserving)
- `imswitch/imcontrol/_test/unit/test_configeditor_catalog.py`
- A short note at the end of the run summarizing what changed and test results.
