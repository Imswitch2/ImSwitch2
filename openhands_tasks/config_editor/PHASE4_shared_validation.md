# Phase 4 — Shared validation service (one diagnostic model)

## Goal

Today validation lives in two places:
- **Model layer** — `imswitch/imcontrol/model/plugins/validation.py` validates
  manager resolution + `managerProperties` schemas (used by the CLI).
- **Editor UI** — `utility_scripts/imswitch_config_editor.py` separately checks
  DAQ-channel conflicts and section⇄device cross-references, emitting HTML.

Phase 4 unifies them into **one** model-layer service that returns **structured
diagnostics (data, not HTML)**, consumed by the CLI, the editor, and tests.

This is Phase 4 of `docs/design/plans/config-editor-discovery-and-schema.md`.
**Read its "Validation Contract" and "Phase 4" sections — they contain a hard
rule you must follow.** Phases 1–3 are in-tree. Do only Phase 4 (no module
split — that's Phase 5).

## ⚠️ THE HARD RULE (do not violate)

**Extend the EXISTING model. Do NOT create a second, parallel diagnostic type.**
There is already `DeviceValidationResult` and `ValidationReport` in
`plugins/validation.py`. You will **rename/generalize `DeviceValidationResult`
into a single `SetupDiagnostic`** and migrate every call site in the same
change. After this task there must be exactly **one** diagnostic dataclass in the
codebase. If you find yourself adding `SetupDiagnostic` *next to* a still-living
`DeviceValidationResult`, stop — that is the mistake this phase exists to avoid.

## What exists today (read first)

In `imswitch/imcontrol/model/plugins/validation.py`:
- `validate_setup_file(path, registry) -> ValidationReport` — opens the JSON file
  and iterates `SETUP_SECTION_TO_KIND` (detectors→detector, lasers→laser,
  positioners→positioner, rotators→rotator, rs232devices→rs232, slms→slm,
  flipMirrors→flip_mirror, microscopeStand→stand), resolving each device's
  manager via the registry / legacy fallback / stand-mock, and collecting
  `managerProperties` schema warnings.
- `DeviceValidationResult(section, device_name, manager_name, resolved_via,
  schema_warnings)` and `ValidationReport(path, devices, jsonschema_available)`
  with `.has_errors` (any `resolved_via == "UNRESOLVED"`) and `.format()`.
- Helpers to reuse as-is: `resolve_schema`, `validate_manager_properties`,
  `legacy_manager_exists`, `load_jsonschema_validator`.

In `utility_scripts/imswitch_config_editor.py` (logic to MOVE into the model):
- `_collect_daq_channels(data) -> {channel: [device_name,...]}` (~line 492) — a
  channel used by >1 device is a **DAQ conflict** (error).
- `_collect_xref_issues(data) -> list[(severity, html_message)]` (~line 518) —
  cross-reference checks with severities `"error"|"warning"|"note"`. Covers:
  focusLock⇄forFocusLock detector + focusLock.camera/.positioner refs;
  autofocus.camera/.positioner; tiling.xy/zPositioner/camera; scan⇄forScanning;
  etSTED⇄scan; processing.reconstructors/processors id validity;
  microscopeStand.rs232device⇄rs232devices; availableWidgets⇄required section;
  legacy notes for `pulseStreamer` and singular `slm`. One check emits a fix
  link: `<a href='fixsection:{section_key}'>Configure…</a>`.
- These use editor-only inputs: `_WIDGET_REQUIRES_SECTION` (built from the
  editor's section schemas) and the improcess known-id sets via
  `_section_option_values(...)`. **Do not relocate the editor's section-schema
  machinery** — parameterize those inputs (see ValidationContext below).

Call sites that MUST be migrated in this task:
- `imswitch/imcontrol/model/plugins/__main__.py` (~lines 149, 159) — the
  `validate-setup` CLI command uses `validate_setup_file` + `report.has_errors`.
- `imswitch/imcontrol/_test/unit/test_device_plugin_diagnostics.py` — asserts
  `report.devices`, `device.resolved_via`, `report.has_errors`.

## What to build

### 1. The single diagnostic model (in `plugins/validation.py`)

```python
@dataclass(frozen=True)
class SetupFix:
    action: str              # e.g. "configure_section"
    target: str | None = None

@dataclass(frozen=True)
class SetupDiagnostic:       # THE diagnostic type (generalized DeviceValidationResult)
    severity: str            # "error" | "warning" | "note"
    code: str                # stable, e.g. "manager.unresolved", "daq.conflict"
    message: str             # PLAIN TEXT (no HTML/markup)
    path: tuple              # JSON path, e.g. ("focusLock","camera") or ("detectors","Cam","managerName")
    fix: SetupFix | None = None
```

`ValidationReport` becomes:
```python
@dataclass
class ValidationReport:
    path: str | None
    diagnostics: list[SetupDiagnostic]
    jsonschema_available: bool
    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.diagnostics)
    def format(self) -> str: ...   # plain-text, grouped by severity, shows code+path+message
```

### 2. Dict-based core + file wrapper

The editor holds an in-memory `data` dict; the CLI has a file. Provide:

```python
@dataclass(frozen=True)
class ValidationContext:
    # Optional editor-supplied inputs for checks that need them.
    widget_requires_section: dict | None = None     # widget -> section key
    known_reconstructor_ids: tuple | None = None
    known_processor_ids: tuple | None = None

def validate_setup_data(
    data: dict,
    registry,
    *,
    context: ValidationContext | None = None,
    source_path: str | None = None,
) -> ValidationReport: ...

def validate_setup_file(path, registry, *, context=None) -> ValidationReport:
    # load JSON, then delegate to validate_setup_data(..., source_path=str(path))
```

`validate_setup_data` runs, in order:
1. **Manager resolution** (port the existing `_validate_setup_device` logic over
   `SETUP_SECTION_TO_KIND`): unresolved → `SetupDiagnostic(severity="error",
   code="manager.unresolved", path=(section, device_name, "managerName"))`;
   resolved only via legacy/legacy-mock → `severity="note",
   code="manager.legacy-fallback"` (this is the plan's "valid only because of
   legacy fallback" signal); each schema warning → `severity="warning",
   code="manager.schema"` with `path=(section, device_name, "managerProperties", <field>)`
   when derivable. Registry-resolved with no warnings produces no diagnostic.
2. **DAQ conflicts** (port `_collect_daq_channels`): channel used by >1 device →
   `severity="error", code="daq.conflict", path=("<daq>", channel)`, message
   listing the device names.
3. **Cross-references** (port `_collect_xref_issues`): one diagnostic per check
   with the **same severity** as today, a **stable descriptive code** (e.g.
   `xref.focuslock.missing-section`, `xref.focuslock.camera-undefined`,
   `xref.autofocus.positioner-undefined`, `xref.tiling.zpositioner-undefined`,
   `xref.scan.no-scanning-positioner`, `xref.etsted.no-scan`,
   `xref.processing.unknown-reconstructor`, `xref.microscopestand.rs232-undefined`,
   `widget.missing-section`, `legacy.pulsestreamer`, `legacy.slm-singular`), a
   `path` tuple, and **plain-text** message (move the `<b>`/`<a>` markup out).
   - The availableWidgets→section check requires
     `context.widget_requires_section`; skip it when context/that map is None.
   - The processing reconstructor/processor checks use
     `context.known_*_ids` when provided; when None, you MAY import the
     improcess id helpers directly (they are core, not editor) — but guard the
     import so validation still works if improcess is unavailable.
   - The "Configure…" check sets `fix=SetupFix("configure_section", section_key)`
     instead of embedding an `<a>` tag.

### 3. CLI (`plugins/__main__.py`) — keep working, now richer

- `validate-setup` still loads the file and prints a report; exit code stays
  `1 if report.has_errors else 0`. It now also surfaces DAQ/xref diagnostics
  (no editor context, so widget-section checks are skipped — that's fine).
- Update it to the new `ValidationReport`/`format()`.

### 4. Editor — render diagnostics, stop owning validation

- `ValidationPanel.validate(data)` (~line 3247) calls `validate_setup_data(data,
  registry, context=...)` where context supplies the editor's
  `_WIDGET_REQUIRES_SECTION` and improcess id lists. Guard with try/except so the
  panel degrades gracefully if the model import fails.
- Render the returned diagnostics to the existing HTML (group by severity, same
  colors). Build the "Configure…" link from `fix` metadata
  (`fix.action == "configure_section"` → `<a href='fixsection:{fix.target}'>`),
  not from the message. The existing `_on_link_activated`/`sig_fix_section`
  wiring stays.
- You MAY keep the thin editor helpers as wrappers that call the model, or
  delete them and inline the call — but the **validation logic** must live in the
  model now, not the editor.

### Hard constraints
- Exactly one diagnostic dataclass after this task (the rename, not a sibling).
- Diagnostic messages are plain text; HTML/links are the editor's job, built
  from `fix`/severity.
- Preserve every existing check's condition and severity.
- CLI `validate-setup` keeps its behavior + exit codes.
- `jsonschema`/improcess optional — degrade gracefully.
- No Qt in `validation.py`. Don't relocate editor section-schema machinery.
- Scope = Phase 4 only.

## Tests (acceptance gate)

Migrate `test_device_plugin_diagnostics.py` to the new model: replace
`report.devices[*].resolved_via` assertions with assertions on
`report.diagnostics` codes/severities (e.g. a registry-mock resolution yields no
`manager.unresolved` error; an unresolved manager yields one;
`report.has_errors` reflects error-severity diagnostics). Keep its existing
scenarios (mock resolution, legacy resolution) meaningful.

Add `imswitch/imcontrol/_test/unit/test_setup_validation_xref.py` covering, via
`validate_setup_data(dict, registry, context=...)`:
1. DAQ conflict → one `daq.conflict` error naming both devices.
2. focusLock present but no `forFocusLock` detector → the matching error code.
3. `focusLock.camera` referencing an undefined detector → error with
   `path == ("focusLock","camera")`.
4. `tiling.zPositioner` undefined → error code, correct path.
5. availableWidgets check: with a `context.widget_requires_section` mapping a
   widget to a missing section → `widget.missing-section` warning carrying
   `fix == SetupFix("configure_section", <section>)`; and assert the same call
   **without** context does not emit it (graceful skip).
6. Legacy singular `slm` → `legacy.slm-singular` note.
7. A clean config → no error-severity diagnostics, `has_errors is False`.

## How to verify

`python -m pytest` may fail at COLLECTION under some envs (root conftest stubs
napari only when missing, not when present-but-broken). validation/plugins need
no napari. If collection fails for that reason, pre-stub it: run a small Python
snippet that inserts genuine stub modules / blocks napari import, OR run the
test classes' methods directly. Required checks:

```
python -m pytest imswitch/imcontrol/_test/unit/test_setup_validation_xref.py \
    imswitch/imcontrol/_test/unit/test_device_plugin_diagnostics.py -q
python -m imswitch.imcontrol.model.plugins validate-setup <some setup json>   # still runs, sane exit code
python -c "import ast; ast.parse(open('utility_scripts/imswitch_config_editor.py').read()); print('parse OK')"
```

Also confirm there is exactly one diagnostic dataclass:
`grep -rn "class DeviceValidationResult" imswitch` must return nothing.

## Deliverables
- `plugins/validation.py`: `SetupDiagnostic`/`SetupFix`, new `ValidationReport`,
  `validate_setup_data` + `ValidationContext`, file wrapper, ported DAQ/xref
  checks, plain-text messages.
- `plugins/__main__.py`: migrated CLI.
- Editor `ValidationPanel`: renders model diagnostics, builds links from `fix`.
- Migrated `test_device_plugin_diagnostics.py` + new `test_setup_validation_xref.py`.
- End-of-run summary of changes + test results, and confirmation that
  `DeviceValidationResult` no longer exists.
