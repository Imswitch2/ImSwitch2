# Task B — Smart widget→section helper in the ImSwitch Config editor

> Run this **after** Task A on the same checkout (sequential). Rebase onto Task A's
> changes; both tasks touch `utility_scripts/imswitch_config_editor.py`.

## Context

The standalone config editor is a single PyQt5 file:
`utility_scripts/imswitch_config_editor.py` (~3700 lines).

Some feature widgets only work if a matching top-level "system section" is also
configured. Today, when the user enables such a widget there is **no help**: the
section must be created separately, and the only feedback is a passive text
warning in the validation panel ("Widget X is enabled but no Y section is
configured"). The user then has to hunt for "＋ Add System Section" and fill it
in by hand. We want the editor to know what a widget needs and offer to fill it
in for them.

### Existing infra to reuse (read before editing)

- **Section schemas already declare their widget.** Each
  `builtin_templates/sections/*.json` may carry `"requires_widget": "<WidgetName>"`.
  Loaded by `_load_section_schemas()` into `SECTION_SCHEMAS` (keyed by section
  key). Current pairings:
  | widget     | section key      |
  |------------|------------------|
  | FocusLock  | focusLock        |
  | Autofocus  | autofocus        |
  | Tiling     | tiling           |
  | EtSTED     | etSTED           |
  | Scan       | scan             |
  | MotCorr    | microscopeStand  |
- **Hardcoded forward map** `_WIDGET_REQUIRES_SECTION` (~line 283) duplicates the
  above but is **missing the `MotCorr → microscopeStand` pairing** — an
  inconsistency to fix (see step 1).
- **`SectionEditorDialog`** (~line 2174) — a finished, schema-driven modal form
  editor for one section. Constructor: `SectionEditorDialog(key, schema, current, data, parent)`;
  on accept, `dlg.result_data` is the section dict. Already used by
  `_open_section_picker` (~line 1710) and `_make_system_row` editing (~line 1704).
- **`_build_default_section(schema)`** (~line 238) — pre-fills a section dict
  with schema defaults; used to seed the editor.
- **`_open_widget_picker()`** (~line 1544) — opens `WidgetPickerDialog`, then sets
  `self._data["availableWidgets"] = dlg.selected_widgets()`. This is where a
  widget gets enabled.
- **`_remove_widget(name)`** (~line 1553).
- **Validation panel** `validate(self, data)` (~line 2849) renders
  `_collect_xref_issues(data)` (~line 292) results into a single rich-text
  `QLabel` (`self._text`). The widget↔section warning is the block at ~line 418.

## What to build

1. **Derive the widget→section map from the schemas (single source of truth).**
   Replace the hardcoded `_WIDGET_REQUIRES_SECTION` with a map built at load time
   from `SECTION_SCHEMAS` using each schema's `requires_widget` field. This
   automatically includes `MotCorr → microscopeStand` and stays correct if new
   sections are added. Keep a module-level constant of the same name (other code
   reads it) but populate it from the schemas. If two sections claimed the same
   widget, last-wins is fine but log/ignore gracefully.

2. **Auto-offer the section editor when a widget is enabled.** In
   `_open_widget_picker()`, after computing the newly-enabled widgets (set
   difference of new vs. previous `availableWidgets`), for each newly-enabled
   widget that maps to a section which is **not yet configured**
   (`self._data.get(section_key) in (None, {}, [])`):
   - Pop a concise confirmation (`QMessageBox.question`) like:
     *"The 'Tiling' widget needs a 'tiling' section to initialize. Configure it
     now?"* with Yes/No/Skip-all semantics (a plain Yes/No per widget is
     acceptable; don't over-engineer).
   - On Yes, open `SectionEditorDialog(section_key, schema, _build_default_section(schema), self._data, self)`.
     On accept, store `self._data[section_key] = dlg.result_data`, then refresh
     the sections UI (`self._refresh_sections()`) and emit `self.sig_modified`.
   - On No, leave the widget enabled but unconfigured (the existing validation
     warning will then flag it — that's the intended safety net).
   Handle the case where several widgets are enabled at once (iterate; don't
   crash if the user cancels one). Reuse the exact same store/refresh pattern as
   `_open_section_picker` so behaviour is consistent.

3. **Make the validation warning actionable.** The widget↔section warning
   currently renders as static text. Make it a clickable affordance that opens
   the same `SectionEditorDialog` for the missing section. Approaches (pick one):
   - Render the warning with an HTML anchor whose href encodes the section key
     (e.g. `<a href="fixsection:tiling">Configure…</a>`), set the panel
     `QLabel.setOpenExternalLinks(False)`, and connect its `linkActivated`
     signal to a handler that resolves the key and opens the editor; **or**
   - Replace the single warning `QLabel` for these rows with small inline
     "Configure…" `QPushButton`s.

   The anchor/link approach is the smaller change. Whatever you choose, clicking
   it must open `SectionEditorDialog` for that section, store the result on accept,
   refresh sections + validation, and emit modified. Keep all other validation
   lines (errors/notes/DAQ conflicts) untouched.

4. **(Nice-to-have, keep optional and safe.)** When a widget that owns a section
   is removed via `_remove_widget` and that section is configured, ask whether to
   also remove the now-orphan section. Only do this if it's a clean, low-risk
   addition; never delete a section without an explicit Yes.

## Constraints

- Single file to change: `utility_scripts/imswitch_config_editor.py`.
- Reuse `SectionEditorDialog`, `_build_default_section`, and the existing
  store/refresh/emit pattern — do **not** write a parallel section editor.
- Importable without a display; no new dependencies.
- Don't change the saved-JSON shape of sections. A user who answers "No" must end
  up in exactly today's state (widget enabled, no section, validation warns).

## Tests (required)

Add `imswitch/imcontrol/_test/unit/test_config_editor_widget_section.py`.
Guard with `pytest.importorskip("PyQt5")`; import the script module by file path
(`importlib.util.spec_from_file_location`). Test the **pure** logic without
opening dialogs:
- the widget→section map built from `SECTION_SCHEMAS` contains all six pairings
  including `MotCorr → microscopeStand`, and matches every section schema that
  declares `requires_widget`;
- `_collect_xref_issues` still emits the "widget enabled but section missing"
  warning for a config with a requiring widget and no section, and does **not**
  emit it once the section is present;
- `_build_default_section` for each requiring section returns a dict containing
  that section's required field keys (so the auto-offered editor opens pre-seeded).

Run `python -m pytest imswitch/imcontrol/_test/unit/test_config_editor_widget_section.py -q`
and ensure it passes, and that the editor module still imports without error.

## Deliverable

A focused diff on `utility_scripts/imswitch_config_editor.py` (+ the new test),
with a short summary of: how the map is now derived, the enable-time auto-offer
flow, and the clickable-warning mechanism you chose.
