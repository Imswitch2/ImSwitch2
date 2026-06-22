# Phase 5 — Consolidate duplication + extract pure helpers

## Goal

Phases 1–4 moved discovery, schema, defaults, templates, and validation into
`imswitch/imcontrol/model/configeditor/` and `plugins/validation.py`. Phase 5
finishes the job by **removing the duplication those phases left behind** and
extracting the last data-dir-independent pure helpers, so the editor script
becomes a thinner Qt-composition layer. This is Phase 5 of
`docs/design/plans/config-editor-discovery-and-schema.md` ("Phase 5: Editor
Module Split"). Phases 1–4 are in-tree.

**This is a refactor: behavior must not change.** Keep it conservative and
parity-preserving. Do NOT attempt a wholesale rewrite of the 3,900-line script.

## Hard scope boundaries (read carefully)

- **Do NOT move the schema loaders** (`_load_schemas`, `_load_blank_schemas`,
  `_load_section_schemas`, `_load_widget_registry`) out of the editor. They read
  `utility_scripts/builtin_templates/`, which is editor-local UI data; relocating
  them into the `imswitch` package would create a backwards dependency. Leave
  them in the script.
- **Do NOT move Qt widget classes** (anything subclassing `QWidget`/`QDialog`/
  `QFrame`/etc.) out of the script. Qt composition stays in the script.
- **Do NOT change saved-file format or any validation behavior.**
- Scope = Phase 5 only.

## Three concrete tasks

### 1. Remove the Phase-4 validation duplication (DRY)

`utility_scripts/imswitch_config_editor.py` still contains the *old* validation
logic, now duplicated by the model:
- `_collect_daq_channels(data)` (~line 492)
- `_collect_xref_issues(data)` (~line 518) — ~150 lines
- `ValidationPanel._validate_legacy(data)` (~line 3334) — the fallback that calls
  the two functions above.

The primary `ValidationPanel.validate()` already uses the model
(`validate_setup_data`). Remove the duplicate logic:
- Delete `_collect_xref_issues` and `_collect_daq_channels` from the editor
  (confirm nothing else references them — `grep`).
- Replace `_validate_legacy` so the except-fallback does **not** re-implement
  validation. Simplest acceptable behavior: on model-import failure, show a
  short, non-crashing message in the panel (e.g. "Validation unavailable
  (model import failed)") instead of running a second copy of the checks.
- Keep `_build_widget_requires_section_map` / `_WIDGET_REQUIRES_SECTION` and
  `_section_option_values` — those are still passed into the model via
  `ValidationContext` by `validate()`.

### 2. Consolidate the duplicated type-coercion helpers

`_display_to_json` / `_json_to_display` exist in the editor (~lines 282 / 273),
**and Phase 2 copied `_display_to_json` into**
`imswitch/imcontrol/model/configeditor/defaults.py`. That's two copies of the
same logic. Consolidate into one Qt-free home:
- Create `imswitch/imcontrol/model/configeditor/coercion.py` with
  `display_to_json(value_str, field_type)` and `json_to_display(value, field_type)`,
  byte-for-byte matching the editor's CURRENT behavior (the editor's
  `_display_to_json`: strips text; `"null"`→None; int/float parse-failure
  returns the original text; everything else, including bool, falls through as
  text).
- `defaults.py` imports `display_to_json` from `coercion` and drops its private
  `_display_to_json`.
- The editor's `_display_to_json`/`_json_to_display` become thin wrappers that
  call `coercion` (or import the names directly), guarded so the editor still
  works if the import fails (mirror the Phase 1 catalog bootstrap pattern).

### 3. Extract save/load helpers + add round-trip tests

The file I/O is in `MainWindow._load_file` (~3539) and `_write_file` (~3575).
The only non-trivial transform on save is stripping an empty `"others"` dict.
Extract the pure parts into `imswitch/imcontrol/model/configeditor/io.py`:
- `load_config_file(path) -> dict` — open + `json.load`.
- `prepare_for_save(data: dict) -> dict` — deep-copy, strip empty `"others"`,
  return; **must not** drop any other key (unknown sections/fields preserved).
- The editor's `_load_file`/`_write_file` keep all the Qt parts (dialogs,
  status, error `QMessageBox`, window title) but delegate the pure read/transform
  to these helpers.

## Tests (acceptance gate)

Add `imswitch/imcontrol/_test/unit/test_configeditor_io_coercion.py`:
1. **Coercion parity.** `display_to_json` matches the documented editor behavior
   for: a `"null"` string → None; an int field with `"42"` → 42 and with
   `"abc"` → `"abc"`; a float field similarly; a bool field with `"False"` →
   the string `"False"` (current behavior); a plain text field.
2. **`defaults.build_default_device` still byte-identical** after the import
   change — rebuild a couple real templated managers and compare to the expected
   dicts (this guards that consolidation didn't shift behavior).
3. **Round-trip `prepare_for_save`.** A config dict with an unknown top-level
   section, unknown device fields, and a populated `"others"` survives
   `prepare_for_save` unchanged except an *empty* `"others"` is removed; a
   non-empty `"others"` is kept. Load→prepare round-trips the document.

(If `python -m pytest` fails at COLLECTION due to the repo's broken-napari env,
run the test methods directly with plain `python`, as in earlier phases.)

## How to verify (REQUIRED — this phase touches the GUI)

The editor must still construct and load a config headlessly. Run:

```
QT_QPA_PLATFORM=offscreen python - <<'PY'
import sys, importlib.util, glob
spec = importlib.util.spec_from_file_location("ce","utility_scripts/imswitch_config_editor.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from PyQt5.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)
w = m.MainWindow()
cfg = glob.glob("imswitch/_data/user_defaults/imcontrol_setups/*.json")[0]
w._load_file(cfg)                      # exercises load + model validation
w._val_panel.validate(w._data)         # exercises the validation panel path
print("OFFSCREEN SMOKE OK")
PY
```

This must print `OFFSCREEN SMOKE OK` with no traceback. Also run:
```
python -c "import ast; ast.parse(open('utility_scripts/imswitch_config_editor.py').read()); print('parse OK')"
grep -n "_collect_xref_issues\|_collect_daq_channels" utility_scripts/imswitch_config_editor.py   # expect no defs left
```
And confirm Phases 1–4 tests still pass (catalog, schema_defaults, plugin_templates,
device_plugin_diagnostics, setup_validation_xref).

## Deliverables
- `configeditor/coercion.py`, `configeditor/io.py`
- `defaults.py` using shared `coercion`
- Editor: validation duplication removed; `_display_to_json`/`_json_to_display`
  and `_load_file`/`_write_file` delegating to the shared helpers; Qt unchanged
- `test_configeditor_io_coercion.py`
- End-of-run summary + confirmation the offscreen smoke test passed.
