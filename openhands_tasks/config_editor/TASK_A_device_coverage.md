# Task A — Universal device-category coverage + auto-discovered managers in the ImSwitch Config editor

## Context

The standalone config editor is a single PyQt5 file:
`utility_scripts/imswitch_config_editor.py` (~3700 lines).

It builds its device "manager picker" entirely from JSON templates found under
`utility_scripts/builtin_templates/<category>/*.json`. Each template (e.g.
`builtin_templates/detectors/TISManager.json`) has the shape:

```json
{
  "display": "TIS Camera",
  "category": "detectors",
  "top":   [ {"key": "...", "label": "...", "type": "text|int|float|bool|select|path", "default": ..., "req": false, "grp": "Basic", "tip": "", "opts": []}, ... ],
  "props": [ ... ],                    // -> managerProperties.<key>
  "nested": { "tis": [ ... ] }         // -> managerProperties.tis.<key>
}
```

Key existing code (read these before editing):
- `_load_schemas()` (~line 33) — loads every `<category>/*.json` into `SCHEMAS` keyed by file stem (the manager class name).
- `SCHEMAS`, `DEVICE_CATS`, `CAT_COLOR`, `CAT_LABEL`, `CAT_MANAGERS` (~lines 163-180) — all derived **only** from the templates that happen to exist.
- `_build_default_device(manager_name)` (~line 212) — builds a new device dict from a schema; returns `{"managerName": ..., "managerProperties": {}}` with no fields when the schema is unknown.
- `DeviceCanvas.load()` / `_add_section()` / `_request_add()` (~lines 758, 776, 846) — renders one section per category and the "Add <Category>" picker. `_request_add` offers `CAT_MANAGERS.get(cat)` only; the "others" catch-all offers `["[Free-form / Custom]"] + sorted(SCHEMAS.keys())`.
- `MainWindow` add path keyed on the `"__ADD__"` / `"__custom__"` sentinels (~lines 3182-3260).

## The problem

Many managers that exist in the codebase have **no** JSON template, so they can
never be added through the picker. Whole categories with zero templates
(`flipMirrors`, `pulsegen`, `stands`) don't even appear as sections. The real
managers live at:

```
imswitch/imcontrol/model/managers/<dir>/<Something>Manager.py
```

with these directory→category mappings (note name mismatches):

| manager dir   | editor category name |
|---------------|----------------------|
| detectors     | detectors            |
| lasers        | lasers               |
| positioners   | positioners          |
| rotators      | rotators             |
| rs232         | rs232devices         |
| slms          | slms                 |
| flipMirrors   | flipMirrors          |
| pulsegen      | pulsegen             |
| stands        | stands               |

(The editor file lives at `utility_scripts/`; the managers tree is at
`<repo-root>/imswitch/imcontrol/model/managers/`, i.e. two levels up from the
script.)

## What to build

1. **Authoritative category registry.** Introduce a single source of truth for
   the device categories the editor supports — canonical category name, display
   label, palette colour, and the manager-directory name it maps to. Every
   category in the table above must always be present in `DEVICE_CATS` /
   `CAT_LABEL` / `CAT_COLOR` and always render as a (possibly empty) section in
   `DeviceCanvas`, even when it has zero templates and zero configured devices.
   Keep the existing template-derived categories working; merge, don't replace.
   Preserve the existing `_LABEL_OVERRIDES` ("RS232 Devices", "SLMs") and the
   permanent trailing "others" catch-all.

2. **Per-category generic blank schema.** Provide a generic blank scaffold per
   category, used whenever a specific manager has no template. The blank must
   carry the category-appropriate common top-level fields so the resulting
   device is usable, matching how existing templates in that category look:
   - detectors: `analogChannel` (text, default `"null"`), `digitalLine` (text,
     default `"null"`), `forAcquisition` (bool, default `true`), `forFocusLock`
     (bool, default `false`), plus empty `managerProperties`.
   - lasers: `analogChannel`, `digitalLine`, `wavelength` (int), `valueRangeMin`
     (int), `valueRangeMax` (int) — inspect a couple of existing laser
     templates (`builtin_templates/lasers/*.json`) and match their `top`/`props`.
   - positioners: `forScanning` (bool), `forPositioning` (bool) where present;
     plus an `axes` property — match existing positioner templates.
   - rotators / rs232devices / slms / flipMirrors / pulsegen / stands: at minimum
     `managerName` + empty `managerProperties`; add any obviously-common top
     fields you can confirm from an existing template in that category.

   Implement these as real JSON files so the design stays data-driven — e.g.
   `builtin_templates/<category>/_blank.json` with a sentinel/`"blank": true`
   marker — OR as a code-level fallback builder. Either is fine; prefer the JSON
   approach to match the existing pattern. A blank schema must **not** appear as
   a selectable "manager type" by its own name; it is only the fallback used
   when a chosen manager has no dedicated template.

3. **Auto-discover managers (runtime, graceful).** At startup, scan
   `<repo-root>/imswitch/imcontrol/model/managers/<dir>/*Manager.py` (resolve the
   path relative to the script: `Path(__file__).resolve().parents[1]`) and build,
   per category, the union of:
   - manager class names that have a dedicated template (existing `CAT_MANAGERS`), and
   - manager class names discovered on disk that do **not** have a template.

   Skip base classes (`DetectorManager`, `LaserManager`, `PositionerManager`,
   `RotatorManager`, etc. — i.e. files whose stem equals the category's base
   manager) and `__init__`/private files. Use the file stem as the manager name
   (e.g. `BaslerManager`). **Do not import imswitch** — discovery must be pure
   filesystem globbing so the editor still launches if the managers tree is
   absent (standalone shipping). If the tree isn't found, fall back to the
   template-only list exactly as today.

4. **Wire discovery into the picker.** In `DeviceCanvas._request_add(cat)`, the
   manager list must be: discovered-managers-with-templates first (current
   behaviour), then discovered-managers-without-templates (clearly grouped or
   suffixed, e.g. `"BaslerManager  (no template — blank)"`), then always a
   `"[Free-form / Custom]"` entry. Selecting a manager that has no template must
   create the device from that category's generic blank with `managerName` set to
   the chosen class name (reuse/extend `_build_default_device` so an unknown
   manager name resolves the category blank instead of returning an empty dict).
   The existing `"__ADD__"`/`"__custom__"` MainWindow add path must keep working.

## Constraints

- Single file to change: `utility_scripts/imswitch_config_editor.py`, plus any
  new `builtin_templates/<category>/_blank.json` files you add.
- Pure-Python, importable without a display: module-level constants
  (`SCHEMAS`, `DEVICE_CATS`, etc.) load at import. Do not require a running
  `QApplication` for discovery/blank logic.
- No new third-party dependencies. No `import imswitch`.
- Don't break existing behaviour for categories/managers that already have
  templates, and don't change the saved-JSON output shape for existing devices.

## Tests (required)

Add `imswitch/imcontrol/_test/unit/test_config_editor_device_coverage.py`.
Guard the import with `pytest.importorskip("PyQt5")` and import the module by
file path (it's a script, not a package) — e.g. via
`importlib.util.spec_from_file_location`. Test the **pure** helpers only (no
dialogs / no QApplication):
- every category in the registry resolves a non-`None` blank schema;
- `flipMirrors`, `pulsegen`, `stands` are present in `DEVICE_CATS`;
- auto-discovery finds at least the templated managers, and finds known
  template-less managers when the managers tree exists (e.g. `BaslerManager`
  under detectors), and degrades to template-only without raising when the tree
  is absent;
- building a device for a template-less manager yields
  `managerName == "<Chosen>Manager"` and the category's common top-level fields.

Run `python -m pytest imswitch/imcontrol/_test/unit/test_config_editor_device_coverage.py -q`
and make sure it passes. Also confirm `python -c "import importlib.util, pathlib; ..."`-style
import of the editor module still succeeds (no import-time crash).

## Deliverable

A focused diff on `utility_scripts/imswitch_config_editor.py` (+ any
`_blank.json` files + the new test), with a short summary of: the category
registry, how blanks are defined, how discovery resolves paths and degrades, and
the exact new picker UX.
