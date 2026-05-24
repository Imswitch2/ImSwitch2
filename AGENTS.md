# AGENTS.md — AI Agent Coding Rules for ImSwitch2

This document defines the rules, constraints, and boundaries for all AI agents working on ImSwitch2. Every agent (OpenHands, Claude, Microsoft Agent Framework agents, or any future agent) MUST follow these rules.

## Core Principles

1. **Human-in-the-loop is mandatory.** No agent may merge code, push to main, or deploy without human review and approval.
2. **Safety first.** ImSwitch2 controls real microscope hardware. Incorrect changes can damage equipment or create unsafe conditions.
3. **Bounded tasks only.** Agents work on narrowly scoped, well-defined tasks — never open-ended refactors.

## Coding Rules

### Mandatory for All Changes

- **No API-breaking changes** without explicit maintainer approval.
- **No hardware timing modifications** unless explicitly requested and approved by someone with physical hardware access.
- **All changes require tests.** No PR will be accepted without corresponding test coverage.
- **No direct hardware execution.** Agents must never trigger physical hardware actions (laser firing, stage movement, DAQ acquisition).
- **All PRs require risk explanations.** Every pull request must include a description of what could go wrong.

### Code Quality

- Follow PEP 8 and existing project conventions.
- Add type hints to all new functions and methods.
- Add docstrings to all public classes and functions.
- Keep changes small and focused — one concern per PR.
- Run linting (`ruff`) before submitting.

### Branch Discipline

- Agents work ONLY on isolated feature branches, never on `main`.
- Branch naming: `agent/<agent-type>/<short-description>` (e.g., `agent/openhands/cleanup-imports`).
- Agents must never force-push or rewrite history.

## Red-Zone Files

The following code areas are considered **red-zone** — they require **mandatory human review** and **explicit maintainer approval** before any modification. Agents should avoid touching these unless the task specifically requires it.

### Hardware Control
- **DAQ timing** — Any code controlling data acquisition timing, synchronization, or triggering.
- **Laser control** — Laser power, enable/disable, modulation, safety interlocks.
- **Galvo scan generation** — Scan patterns, voltage generation, waveform construction.
- **TTL generation** — Digital pulse timing, trigger sequences, synchronization signals.
- **Stage movement** — Positioning commands, velocity, acceleration, limit handling.
- **Hardware initialization** — Device discovery, connection, configuration, calibration.

### Identification Patterns

Red-zone files typically include (but are not limited to):
- Files containing `DAQ`, `NI`, `nidaq` in their names or imports
- Files in `laser`, `positioner`, `stage`, `scanner`, `galvo` directories
- Files with `TTL`, `trigger`, `pulse`, `waveform` in their names
- Hardware manager initialization code
- Any file importing `nidaqmx`, `pyvisa`, or direct serial communication libraries

### What Agents Must Do with Red-Zone Files

1. **Flag the file** as red-zone in the PR description.
2. **Explain the risk** of every change in detail.
3. **Never modify timing constants** or hardware parameters without explicit values provided by a maintainer.
4. **Request review** from someone with physical access to the relevant hardware.

## Agent Workflow

The expected workflow for agent-generated changes:

```
GitHub Issue (scoped task)
  → Agent plans approach
  → Agent creates isolated branch
  → Agent implements changes
  → Agent runs tests
  → Agent opens PR with risk explanation
  → Human reviews PR
  → Human merges or rejects
```

Automatic merges are **never allowed**.

## Cost Tracking

Each agent task should track:
- Estimated cost before starting
- Actual cost after completion
- Whether the output was useful

Budget limits (configured in `.env`):
- Monthly budget: ~$100
- Per-task budget: $2–10
- Daily soft limit: $10–20

## Recent Additions

### ViewerToolManager (2026-05-12)

A new `ViewerToolManager` class was added to `imswitch/imcommon/view/guitools/naparitools.py` to provide napari Shape layer-based viewer interaction tools.

**What it does:**
- Manages a napari Shapes layer for ROI drawing, line drawing, and other annotation tools
- Provides simplified API: `set_mode('rectangle')`, `set_mode('line')`, `set_mode('pan')`
- Lazy initialization (layer only created when first used)
- Qt signals for integration: `sigShapesChanged`, `sigModeChanged`
- Helper methods: `get_rectangle_bounds()`, `get_line_endpoints()`, `get_shapes_data()`

**Why it was added:**
- Enable modern napari-native interaction tools
- Provide foundation for future line profiles, multi-ROI analysis, etc.
- Completely backward compatible (Vispy overlays unchanged)

**Location:**
- Implementation: `imswitch/imcommon/view/guitools/naparitools.py` (lines 981-1190)
- Usage docs: `VIEWER_TOOL_MANAGER_USAGE.md`
- Architecture analysis: `NAPARI_VIEWER_TOOLS_ARCHITECTURE.md`
- Example: `examples/viewer_tool_manager_demo.py`

**Status:** Fully implemented and integrated into ImSwitch. Safe to use, no risk to existing functionality.

### ViewerToolsWidget (2026-05-12)

A new UI widget was added to provide toolbar buttons for switching napari viewer modes.

**What it does:**
- Provides 4 mutually exclusive buttons: Pan, Select, Rectangle ROI, Line
- Emits `sigToolSelected(str)` signal with mode name: 'pan', 'select', 'rectangle', 'line'
- API methods: `setActiveTool(mode)`, `getActiveTool()`
- Follows standard ImSwitch widget conventions (inherits from `Widget`)

**Why it was added:**
- User interface component for ViewerToolManager
- Enables manual mode switching via toolbar
- Self-contained, minimal design

**Location:**
- Implementation: `imswitch/imcontrol/view/widgets/ViewerToolsWidget.py`
- Documentation: `VIEWER_TOOLS_WIDGET_README.md`

**Status:** Fully implemented and wired. Available but disabled by default.

### ViewerToolsWidget Wiring (2026-05-12)

The ViewerToolsWidget is now fully wired to the ImageWidget.toolManager through ViewerToolsController.

**Architecture:**
```
ViewerToolsWidget.sigToolSelected(str)
    ↓
ViewerToolsController.__init__
    ↓ connects signal to
ImageWidget.toolManager.set_mode(str)
    ↓
napari Shapes layer mode change
```

**Implementation:**
- `ViewerToolsController` (new): Minimal controller that connects widget signals to toolManager
- `ImConMainView`: Passes `ImageWidget.toolManager` to factory as `imageToolManager` argument
- `ImConMainView`: Registers ViewerTools dock at yPosition=2 (left panel)
- All existing Vispy overlays remain unchanged

**Files modified:**
1. `imswitch/imcontrol/controller/controllers/ViewerToolsController.py` - **NEW**
2. `imswitch/imcontrol/controller/controllers/__init__.py` - Export ViewerToolsController
3. `imswitch/imcontrol/view/widgets/__init__.py` - Export ViewerToolsWidget
4. `imswitch/imcontrol/view/ImConMainView.py` - Register dock and pass toolManager

**Configuration:**
- Disabled by default (not in example configs)
- Enable by adding to `availableWidgets`: `"ViewerTools"`
- No breaking changes to existing setups

**Documentation:**
- Wiring summary: `VIEWER_TOOLS_WIRING_SUMMARY.md`
- Wiring diagram: `VIEWER_TOOLS_WIRING_DIAGRAM.txt`
- Widget API: `VIEWER_TOOLS_WIDGET_README.md`
- ToolManager API: `VIEWER_TOOL_MANAGER_USAGE.md`

**Status:** Complete and ready for testing. Zero risk to existing functionality.

### Widget State Persistence Framework (2026-05-14)

A generalized, reusable framework for saving and loading widget controller states to persistent storage.

**What it does:**
- Centralized service for widget state save/load operations
- Controllers opt-in by implementing `getWidgetState()` and `setWidgetState()` methods
- States stored in `~/ImSwitchConfig/imcontrol_widget_states/`
- JSON format with metadata (controller name, schema version, widget type)
- Robust error handling (missing widgets, corrupted files, version mismatches)
- **Safety-first**: Never includes hardware-active states (laser on/off, acquisition running, etc.)

**Why it was added:**
- Enable persistent UI preferences across sessions
- Provide programmatic state management via API
- Replace ad-hoc preset systems with unified framework
- Support future features (cloud sync, team configurations, state history)

**Architecture:**
```
WidgetStatePersistence (singleton service)
    ↓ register()
Controllers implementing:
    - getWidgetState() -> dict (passive config only, NO hardware-active states)
    - setWidgetState(state: dict) -> None (restore config, NO hardware actions)
    - getStateSchemaVersion() -> int (optional)
```

**Location:**
- Service: `imswitch/imcontrol/model/WidgetStatePersistence.py`
- Documentation: `docs/design/WIDGET_STATE_PERSISTENCE.md`
- Demo: `examples/widget_state_persistence_demo.py`

**Reference implementation:**
- `LaserController` now implements state persistence
- Includes laser power values, modulation settings, selected preset
- **Does NOT include** laser enable states (safety requirement)
- Coexists with existing laser preset system (backward compatible)

**Safety guarantees:**
1. **Never restores hardware-active states** (laser on, acquisition running, motor moving)
2. **Only restores passive configuration** (values, settings, UI state)
3. **Graceful degradation** (missing keys, unknown hardware, corrupted files)
4. **Per-property error handling** (one failing property doesn't break others)

**Usage pattern:**
```python
# In controller __init__:
from imswitch.imcontrol.model import getWidgetStatePersistence
getWidgetStatePersistence().register('MyController', self)

# Implement interface:
def getWidgetState(self) -> Dict[str, Any]:
    return {'setting1': value1, 'setting2': value2}

def setWidgetState(self, state: Dict[str, Any]) -> None:
    self._widget.setSetting1(state.get('setting1', default))
    self._widget.setSetting2(state.get('setting2', default))
```

**Storage structure:**
```
~/ImSwitchConfig/imcontrol_widget_states/
├── LaserController/
│   ├── default.json
│   ├── high_power.json
│   └── scanning.json
└── <OtherController>/
    └── default.json
```

**Integration with existing systems:**
- **LaserController**: Both legacy presets (in setup JSON) and new state persistence work independently
- **Future controllers**: Can use either system or both
- **No breaking changes**: All existing functionality preserved

**Status:** Fully implemented, documented, and tested. Safe for production use. See `docs/design/WIDGET_STATE_PERSISTENCE.md` for detailed usage guide.

### Widget State Persistence: Detector and Scan Controllers + UI Integration (2026-05-14)

Extended the widget state persistence framework to detector and scan controllers, and added UI integration with convenient save/load menu actions.

**Controllers with persistence:**
- **SettingsController**: Detector settings (exposure, ROI, binning, frame mode, detector-specific parameters)
- **ScanControllerBase**: Scan parameters (size, step, center, TTL settings, dwell time)
- **LaserController**: Laser settings (power, modulation, presets) — already implemented

**UI Integration:**
- Menu items: File > "Save Widget States…", "Load Widget States…"
- Keyboard shortcuts: Ctrl+Shift+S (save), Ctrl+Shift+L (load)
- File dialogs for user-friendly state file selection
- Success/error messages via QMessageBox
- Graceful handling of missing controllers

**What gets persisted:**

*SettingsController:*
- Exposure time
- ROI (X0, Y0, X1, Y1)
- Binning (horizontal, vertical)
- Frame mode (Continuous, Fixed-length)
- Detector-specific parameters (Gain, Offset, etc.)

*ScanControllerBase:*
- Scan size (X, Y, Z)
- Step size (X, Y, Z)
- Center position (X, Y, Z)
- TTL settings (start trigger, each, sequence)
- Dwell time
- **Note**: Only safe parameters, no hardware-active states

**Safety:**
- No automatic hardware actions on restore
- Per-property error handling (one failure doesn't break all)
- Only passive configuration persisted (no "scan running", "acquisition on", etc.)
- Graceful degradation on missing/invalid properties
- Schema versioning for compatibility

**Files modified:**
1. `imswitch/imcontrol/controller/controllers/SettingsController.py` (+178 lines)
   - getWidgetState(), restoreWidgetState()
   - Registration with WidgetStatePersistence
   
2. `imswitch/imcontrol/controller/controllers/ScanControllerBase.py` (+157 lines)
   - getWidgetState(), restoreWidgetState()
   - Registration with WidgetStatePersistence
   
3. `imswitch/imcontrol/view/ImConMainView.py` (+14 lines)
   - sigSaveWidgetState, sigLoadWidgetState signals
   - Menu items and keyboard shortcuts
   
4. `imswitch/imcontrol/controller/ImConMainController.py` (+64 lines)
   - saveWidgetState(), loadWidgetState() handlers
   - File dialogs and error handling

**Usage:**
```python
# Programmatic usage:
from imswitch.imcontrol.model import getWidgetStatePersistence
persistence = getWidgetStatePersistence()

# Save all widget states
persistence.saveAllStates('my_experiment_config')

# Load all widget states
persistence.loadAllStates('my_experiment_config')

# Or via UI:
# File > Save Widget States… (Ctrl+Shift+S)
# File > Load Widget States… (Ctrl+Shift+L)
```

**Documentation:**
- Implementation summary: `PERSISTENCE_UI_INTEGRATION_SUMMARY.md`
- Usage demo: `examples/widget_state_save_load_demo.py`
- Framework docs: `docs/design/WIDGET_STATE_PERSISTENCE.md`

**Status:** Complete and committed (commit 340bfb13). Fully backward compatible, zero risk to existing functionality. Ready for user testing.

### Laser-Scan Emission Ownership (2026-05-22)

The scan module's TTL device list (`ttlDeviceList`) is now the single source of truth for which lasers emit during a scan.

**What changed:**
- **Sole authority**: The scan's TTL cycle (linestep_enable signal) determines per-laser emission. Only lasers in the scan's device list participate.
- **Force-disable non-participants**: `LaserController.setScanModeActive()` now explicitly disables all lasers not in the scan's device list before entering scan mode.
- **Dead parameter removed**: The unused `enabled` parameter was removed from `setScanModeActive()` (always `True` at all call sites).
- **Dead code removed**: A permanently disabled `if False:` branch referencing a legacy `scanManager` was removed from `ScanControllerAdvanced.runScanAdvanced()`.

**Why this matters:**
- Eliminates ambiguity about which module controls laser emission during scans
- Prevents stale laser state from previous scans interfering with current scan
- Clarifies control flow: scan module owns emission authority, LaserController enforces it
- Removes confusion from dead/unused code paths

**Files modified (red-zone):**
1. `imswitch/imcontrol/controller/controllers/LaserController.py`
   - `setScanModeActive()`: Removed `enabled` param, added force-disable of non-participants
2. `imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py`
   - `runScanAdvanced()`: Removed dead `scanManager` branch, added clarifying comment

**Safety notes:**
- **Red-zone files**: Both modified files involve laser control (hardware safety critical)
- **No behavior change**: Only removes dead code and clarifies existing ownership model
- **No timing changes**: TTL generation and signal timing unchanged
- **No API breaking changes**: `setScanModeActive()` call sites already passed no explicit `enabled` value

**Status:** Committed (commits cb64772e). No functional changes to working code paths. Documentation change only.

