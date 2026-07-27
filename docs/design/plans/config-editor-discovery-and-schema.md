# Config Editor Discovery And Schema Plan

Date: 2026-06-22 (plan), status verified 2026-07-21

## Status

Phases 1–4 shipped in `99444fd8` ("Config editor: consume plugin registry,
schemas, templates & shared validation"). Phase 5 landed partially; Phase 6 has
not been started.

| Phase | Status |
| --- | --- |
| 1. Catalog service | ✅ Done — exit criterion asserted by `test_configeditor_catalog.py::test_fake_plugin_contribution` |
| 2. Schema and default builder | ✅ Done — `configeditor/schemas.py`, `configeditor/defaults.py` |
| 3. Plugin setup templates | ✅ Done — `configeditor/templates.py` |
| 4. Shared validation service | ⚠️ Mostly done — model layer complete, CLI still under-reports (see below) |
| 5. Editor module split | ⚠️ Partial — core editor-resource loading never moved out of the Qt script |
| 6. UX cleanup | ❌ Not started |

Remaining work is rendered as the [Final Delivery Plan](#final-delivery-plan).

## Verification Snapshot (2026-07-21)

The status above was checked against the current checkout, rather than inferred
from the implementation commit message.

- `build_default_registry(discover=True)` currently yields **9** contributions.
  The editor catalog contains **64** managers: 9 registry-backed and 55 added by
  its legacy filename scanner. The registry is therefore the correct extension
  mechanism, but it is not yet complete for core managers.
- **0 of the 9** registered contributions currently declares a
  `manager_properties_schema`. The editor supports schema-driven fields, but
  core managers do not yet exercise that contract.
- The editor supplies `ValidationContext`; the `validate-setup` CLI command
  does not. Widget-to-section diagnostics are consequently absent from CLI
  output for the same setup file.
- Core editor schema resources remain under `utility_scripts/builtin_templates`
  and are loaded by the Qt script. That location is not package data in
  `MANIFEST.in`, so moving the loader without moving the resources would not
  make the standalone editor reliably installable.

## Summary

The config editor should become a UI over the same extension contracts that the
runtime uses. Today it works, but it still owns a parallel view of the world:
local category definitions, JSON templates, filename-based manager discovery,
handwritten defaults, and handwritten cross-reference validation. That makes it
useful for core setups but fragile for newly added managers, renamed managers,
and external plugins.

The target architecture is:

- runtime/plugin registry is the source of truth for available managers;
- manager schemas describe expected top-level fields and `managerProperties`;
- setup templates are examples, not the schema authority;
- validation is shared between CLI, tests, and editor;
- the editor remains tolerant of unknown fields so existing setup files are not
  damaged by round-tripping.

## Current State

> This section and *Problems To Fix* describe the **2026-06-22 baseline**, before
> any phase landed. They are kept for rationale. For what is true today, see
> [Status](#status) and the [Final Delivery Plan](#final-delivery-plan).

The editor lives in `utility_scripts/imswitch_config_editor.py`. It currently
does several jobs in one PyQt module:

- loads built-in editor templates from
  `utility_scripts/builtin_templates/{category}/*.json`;
- keeps a private category registry for detectors, lasers, positioners,
  rotators, RS232 devices, SLMs, flip mirrors, pulse generators, and stands;
- discovers in-tree managers by scanning manager filenames ending in
  `*Manager.py`;
- builds default devices from editor-template fields;
- validates DAQ conflicts and selected cross-references directly in UI code;
- stores user templates in a file-backed directory next to the script.

This has improved recently: non-templated discovered managers now get fallback
display names and blank category templates, so managers such as `AVManager` no
longer crash the template tree. Still, the editor has not yet caught up with the
device plugin architecture.

## Problems To Fix

### 1. Duplicate Manager Discovery

Runtime manager loading is increasingly registry-backed, but the editor still
discovers managers by walking the source tree. This misses installed plugins and
can expose implementation files that are not valid user-facing managers.

Desired behavior:

- build the available-manager list from `DevicePluginRegistry`;
- include built-ins, aliases, display names, plugin names, docs URLs, supported
  platforms, and setup templates;
- keep a legacy fallback scanner only for local development and old unregistered
  in-tree managers.

### 2. Templates Are Doing Schema Work

The current `builtin_templates/*.json` files mix three meanings:

- default values for creating a new device;
- UI layout hints such as labels, groups, and tooltips;
- implicit knowledge of what a manager accepts.

That is too much responsibility for examples. It also means a plugin manager can
be discovered but still has weak editing support unless a matching editor
template is added to the core repository.

Desired behavior:

- manager expectations come from a schema contract;
- templates provide ready-made example instances;
- blank category templates remain as a safe fallback for unknown managers.

### 3. Plugin Managers Are Not First-Class In The Editor

The plugin manifest already supports:

- `id`;
- `kind`;
- `display_name`;
- `python_name`;
- `manager_name_aliases`;
- `manager_properties_schema`;
- `setup_templates`;
- `docs_url`;
- `supported_platforms`.

The editor should consume that information directly. A plugin should be able to
install a manager and immediately appear in the editor without modifying
`utility_scripts/builtin_templates`.

### 4. Validation Is Split

The plugin CLI can validate manager resolution and manager-properties schema.
The editor separately validates DAQ conflicts and some section/device
cross-references. These are useful checks, but they are currently UI-local.

Desired behavior:

- one model-layer validation service returns structured diagnostics;
- the CLI, editor, and tests all call that service;
- diagnostics have stable severity, code, message, JSON path, and optional fix
  action;
- editor-specific links such as "Configure scan" are created from fix metadata,
  not embedded in validation strings.

### 5. The Editor Is Hard To Test Incrementally

The current script contains discovery, schema handling, validation, model
mutation, and widgets in one file. That makes every improvement a GUI change,
even when the logic is pure data transformation.

Desired behavior:

- move pure editor services into importable modules;
- keep Qt widgets thin;
- cover schema loading, default-device creation, round-trip preservation, and
  validation with unit tests.

## Proposed Architecture

Introduce a small config-editor service layer under the model or utility
package. The exact package can be decided during implementation, but the API
should be independent of Qt.

```text
imswitch/imcontrol/model/configeditor/
  __init__.py
  catalog.py
  schemas.py
  templates.py
  defaults.py
  coercion.py
  io.py
```

Responsibilities:

- `catalog.py`: builds a manager catalog from `DevicePluginRegistry` plus
  explicit legacy fallbacks.
- `schemas.py`: resolves manager-property JSON schemas and optional UI schema
  hints.
- `templates.py`: loads plugin setup templates. Core editor-resource loading
  remains in the Qt script (see the Final Delivery Plan).
- `defaults.py`: creates new setup entries without requiring Qt.
- `coercion.py`, `io.py`: JSON↔display value coercion and setup file load/save.
  Added during implementation; not anticipated by the original plan.

There is deliberately **no** `configeditor/validation.py`: diagnostics live in
`imswitch/imcontrol/model/plugins/validation.py`, which already served the CLI.
See [Validation Contract](#validation-contract) and Phase 4.

The PyQt script can then become a consumer of this service layer. It does not
need to be rewritten at once; the current functions can be replaced one at a
time.

## Manager Metadata Contract

For external plugins, the existing manifest fields should remain the primary
contract. A plugin contribution should be enough for the editor to show:

- manager ID and display name;
- kind/category;
- plugin/package source;
- aliases accepted by old setup files;
- docs link;
- bundled setup templates;
- `managerProperties` schema.

For fields outside `managerProperties`, ImSwitch should provide a shared setup
section schema per kind. For example, detectors share fields such as
`managerName`, `analogChannel`, `digitalLine`, `forAcquisition`, and related
flags. The plugin should not need to restate those unless it has a specific
override.

### Can Managers Tell Us What They Expect?

Yes, but the clean path is declarative metadata, not importing hardware manager
classes and asking them at runtime.

Preferred:

- plugin/core contribution points to a JSON Schema for `managerProperties`;
- optional UI hints describe labels, grouping, enum labels, units, secret/path
  fields, and references to other setup sections;
- setup templates provide concrete examples.

Avoid as the default:

- importing the manager class to introspect `__init__`, because many managers
  import vendor SDKs or touch hardware-adjacent modules;
- requiring live hardware discovery before the editor can show a form.

Optional later (defer until a concrete manager needs it — YAGNI):

- a static `describe_config()` classmethod for managers that can be imported
  safely;
- a plugin manifest flag declaring that metadata import is side-effect free.

The JSON-Schema-in-manifest path plus the blank-category fallback already meets
the stated goal (plugins self-describe without core PRs), so the introspection
path should not be built speculatively.

## Plugin Template Loading

A plugin should be able to declare setup templates in its manifest:

```json
{
  "contributions": {
    "device_managers": [
      {
        "id": "vendor.superstage",
        "kind": "positioner",
        "display_name": "Vendor SuperStage",
        "python_name": "vendor_plugin.stage:SuperStageManager",
        "manager_properties_schema": "schemas/superstage.schema.json",
        "setup_templates": [
          "templates/superstage-usb.json",
          "templates/superstage-ethernet.json"
        ]
      }
    ]
  }
}
```

The editor should load those resources through `importlib.resources` using the
contribution's `source_package`. It should show them alongside core templates,
clearly labeled by plugin.

## Validation Contract

Validation should produce data, not HTML.

A structured validation layer already exists in
`imswitch/imcontrol/model/plugins/validation.py`: `validate_setup_file()`
returns a `ValidationReport` built from `DeviceValidationResult` entries, plus
`resolve_schema()` and `validate_manager_properties()` for `managerProperties`.
The CLI already consumes it. **Do not introduce a second, parallel diagnostic
model** — that would recreate the exact "parallel view of the world" this plan
exists to remove. Instead, evolve the existing types: extend
`DeviceValidationResult` (or a renamed `SetupDiagnostic` that *replaces* it) to
carry `path` and `fix` metadata, and absorb the editor's xref/DAQ checks into
the same report.

The target shape:

```python
@dataclass(frozen=True)
class SetupDiagnostic:  # evolution of DeviceValidationResult, not a new sibling
    severity: Literal["error", "warning", "note"]
    code: str
    message: str
    path: tuple[str | int, ...]
    fix: SetupFix | None = None
```

If `DeviceValidationResult` is renamed, update its current call sites (CLI,
tests) in the same change so only one diagnostic type survives.

Useful first diagnostic families:

- unresolved manager names;
- plugin discovery errors;
- manager-properties schema errors;
- stale `availableWidgets` entries;
- widget enabled but required setup section missing;
- `widgetLayout` references to absent widgets;
- invalid references between sections, cameras, positioners, RS232 devices, and
  DAQ channels;
- deprecated sections such as singular `slm`;
- setup entries that are valid only because of legacy fallback.

The editor can render diagnostics with links. The CLI can render plain text.
Tests can assert diagnostic codes.

## Phased Implementation

### Phase 1: Catalog Service — ✅ Done

- Add a pure-Python manager catalog built from `build_default_registry()`.
- Map plugin kinds to setup sections using the existing plugin validation maps.
- Preserve the editor's current filename scanner as a legacy fallback.
- Replace `_discover_managers()`, `_all_known_managers()`, and display-name
  lookup in the editor with catalog calls.
- Add tests for built-in managers, aliases, non-templated managers, and an
  injected fake plugin contribution.

**Exit criterion (proves the duplication is broken):** an injected fake plugin
contribution appears in the editor's manager list with its display name,
accepted aliases, and docs URL — with **zero** changes to
`utility_scripts/builtin_templates/`. This is asserted by a pytest, not just
checked by hand.

*Met by `test_configeditor_catalog.py::test_fake_plugin_contribution`, including
the assertion that `builtin_templates/` is untouched. Note that
`_discover_managers()` and `_all_known_managers()` were not deleted — they are
now catalog-first with the filename scanner retained as the intended legacy
fallback.*

### Phase 2: Schema And Default Builder — ✅ Done

- Introduce a normalized internal field model for top-level common fields and
  `managerProperties`.
- Read JSON Schema from registry contributions where available.
- Keep current `builtin_templates` as UI/default overlays during migration.
- Ensure unknown fields are preserved during round-trip editing.
- Add tests for default device creation and unknown-field preservation.

### Phase 3: Plugin Setup Templates — ✅ Done

- Load `setup_templates` resources from registry contributions.
- Show core templates, plugin templates, and user templates in one template
  browser.
- Label templates by source: core, plugin name, or user.
- Validate template JSON before showing it and report plugin template errors as
  diagnostics.

### Phase 4: Shared Validation Service — ⚠️ Mostly done

This phase **extends the existing** `imswitch.imcontrol.model.plugins.validation`
module rather than building a new one beside it (see Validation Contract).

- Evolve `DeviceValidationResult`/`ValidationReport` to carry `path` and `fix`
  metadata (or rename to `SetupDiagnostic`, migrating all call sites at once).
- Move `_collect_xref_issues()` and DAQ conflict checks out of the editor and
  into that model-layer module, emitting the same diagnostic type.
- Keep manager-resolution/schema validation (`validate_setup_file`,
  `resolve_schema`, `validate_manager_properties`) as the existing core; do not
  duplicate it.
- The CLI already consumes `validate_setup_file` — keep it working; add the new
  xref/DAQ diagnostics to its output rather than forking a new entry point.
- Update the editor validation panel to render diagnostics instead of owning
  validation logic.

*`SetupDiagnostic`/`SetupFix`/`ValidationContext` and the xref + DAQ checks all
live in `plugins/validation.py`; the editor renders them and keeps
`_collect_xref_issues()` only as a thin compatibility wrapper. Outstanding: the
CLI passes no `ValidationContext`, so it silently emits a weaker report than the
editor (see the Final Delivery Plan).*

### Phase 5: Editor Module Split — ⚠️ Partial

This phase is the **payoff** of Phases 1–4, not a separate refactor: by extracting
catalog, schema, templates, and validation as importable services in the earlier
phases, the seams are already cut. Phase 5 is mostly relocating the now-isolated
logic into `imswitch/imcontrol/model/configeditor/` and deleting the dead code
left in the script. Do it last, after the services are test-covered, because the
4,100-line Qt module has wiring that no unit test guards.

- Move pure logic out of `utility_scripts/imswitch_config_editor.py`.
- Keep the script as the entry point and Qt composition layer.
- Add focused unit tests for catalog, schema resolution, defaults, templates,
  validation, save/load round trips, and active-config handling.
- Manually confirm the editor still launches and round-trips a real setup file.

**Exit criterion (not yet met):** no `builtin_templates` filesystem access
remains in `utility_scripts/imswitch_config_editor.py`; core editor resources,
plugin setup templates, and user templates are each supplied by a Qt-free model
service, and the script's resource-loading paths are covered by non-Qt tests.

*Unlike Phase 1, this phase shipped without a falsifiable exit criterion, which
is why its shortfall went unnoticed. The script grew from ~3,900 to 4,126 lines.*

### Phase 6: UX Cleanup After The Contract Is Stable — ❌ Not started

- Add a manager detail panel showing source plugin, accepted aliases, docs URL,
  and schema status.
- Add "create from connected hardware" only for managers that expose a safe
  discovery API.
- Add quick fixes for common validation failures.
- Add a compatibility report before save so users can see which entries rely on
  legacy fallback.

## Migration Rules

- Do not remove support for current setup files.
- Preserve unknown sections and unknown fields by default.
- Do not import hardware managers just to build the editor UI.
- Prefer plugin/core metadata over filename scanning.
- Treat template JSON parse failures and plugin discovery failures as visible
  diagnostics, not silent skips.
- Keep editing possible even when optional packages such as `jsonschema`,
  vendor SDKs, or plugin packages are missing.

## Final Delivery Plan

The remaining work should be completed in this order. Each stage has an exit
criterion so a partial refactor cannot be mistaken for a finished contract.

**Implementation progress:** Stage A's shared metadata contract and coverage
audit are in place. Its migration exit criterion remains open: the audit
currently records 53 runtime-plugin-capable core managers that still need
registry contributions, plus two intentional legacy-only pulse generators.

### A. Make registry metadata complete and authoritative

Create one model-layer setup-metadata definition for the mappings currently
duplicated between `configeditor/catalog.py`, `plugins/validation.py`, and the
Qt category registry. It must declare each kind's setup section, editor category,
legacy scan directory, and whether the runtime can resolve third-party plugins.
The editor may retain labels and colours as presentation-only data.

Inventory every legacy-scanned core manager and classify it as one of:

- a registry contribution with maintained ID/aliases and an optional schema;
- explicitly legacy-only, with a documented reason; or
- not a selectable device manager, and therefore excluded from scanning.

Add a registry-completeness test that fails when a runtime-loadable core manager
is silently discovered only by filename. `pulse_generator` must remain visibly
legacy-only until its bespoke runtime loader becomes registry-backed; it must not
be presented as a working external-plugin contract.

**Exit criterion:** the normal catalog contains no unexplained legacy-scanned
manager, and all kind/category/section mappings have one model-layer owner.

### B. Move core editor resources out of Qt and into package data

`builtin_templates` are editor schema/default overlays, not plugin setup-instance
templates. Keep `templates.py` focused on plugin-declared setup instances; add a
Qt-free `configeditor/resources.py` (or equivalent) to load the core overlays,
blank category forms, and singleton-section forms as a typed resource bundle.

Move those JSON files beneath an installable `imswitch` package-data directory
and update `MANIFEST.in`/packaging metadata. Resolve them with
`importlib.resources`, with an injectable resource root for tests. Move
`TemplateStore` to the model layer separately: user-writable templates are a
different concern from immutable packaged resources.

**Exit criterion:** `utility_scripts/imswitch_config_editor.py` contains no
`builtin_templates` path, `glob()` or JSON-loading code; a wheel-installed editor
loads the resource bundle; and non-Qt tests cover a malformed resource, a blank
schema, a section schema, and a user-template round trip.

### C. Give CLI and editor identical validation inputs

Move widget-to-section requirements and the default ImProcess ID providers out
of the Qt script. Supply a standard `ValidationContext` factory to both
`validate-setup` and the editor. Keep UI actions such as a clickable
“Configure…” link as rendering of `SetupFix`, never as validator HTML.

Add paired tests that validate the same fixture through the model, CLI, and
editor adapter and assert the same diagnostic codes (including
`widget.missing-section`, DAQ conflicts, and ImProcess plugin IDs).

**Exit criterion:** the CLI and editor report the same diagnostics for the same
setup data, apart from explicitly documented UI-only presentation.

### D. Complete schema-driven editing and compatibility reporting

For every registry contribution with a JSON Schema, render schema-only fields
without a core template and preserve unsupported saved values/unknown nested
members on round trip. Extend the supported schema-to-widget mapping only where
there is a lossless representation; use the JSON editor for arrays, objects,
unions, and unsupported extensions.

Add optional `x-imswitch-*` JSON-Schema UI annotations for group, label, help,
units, path picker, secret masking, and device-reference category. These are
declarative plugin metadata; do not import manager classes or probe hardware to
construct a form.

Before saving, generate a compatibility report for registry aliases,
legacy-scanned managers, unknown fields retained verbatim, and unavailable
plugins. Saving must remain possible unless a validation error makes the setup
invalid.

**Exit criterion:** an injected plugin with only manifest + JSON Schema is
editable, validates through CLI and editor, round-trips values unknown to the
current form, and needs no file in the core resource package.

### E. Deliver the UX on top of the settled contract

Add a manager detail panel showing source, version, aliases, documentation URL,
schema/resource status, and compatibility state. Surface plugin discovery and
template/resource errors in the same diagnostic panel. Add safe quick fixes only
for diagnostics carrying a `SetupFix`.

Defer connected-hardware discovery until a manager declares a separately safe,
side-effect-free discovery capability. It must never be inferred by importing a
manager class.

**Exit criterion:** the UI exposes why every selectable manager is available or
legacy-only, and all diagnostics that can be fixed mechanically offer a scoped
action.

### Delivery safeguards

- Preserve existing setup files, unknown sections, aliases, and unknown values
  throughout every stage.
- Never use manager-class introspection as normal metadata discovery: vendor SDK
  imports and hardware-adjacent side effects make it unsuitable for an editor.
- Land stages independently with unit tests plus one offscreen Qt smoke test;
  build and inspect a wheel in stage B to verify package-data delivery.
