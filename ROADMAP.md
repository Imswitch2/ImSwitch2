# ImSwitch2 Migration Roadmap

This roadmap tracks the major milestones for the ImSwitch2 migration. Each milestone should be created as a GitHub Milestone with associated issues.

## Milestone 1: Stabilization ✅

**Goal:** Establish a stable, tested baseline from the imported ImSwitch code.

- ✅ Import existing ImSwitch codebase and tag baseline
- ✅ Set up CI pipeline (linting, tests, build)
- ✅ Critical lint errors resolved (`E9/F63/F7/F82` clean)
- ✅ First unit tests passing in CI (`test_stores.py`)
- ⬜ Ensure the application launches without errors on a clean install

## Milestone 2: Packaging Cleanup 🔄

**Goal:** Clean up the project structure and packaging for easy installation.

- ✅ Modernize `setup.cfg`: `python_requires = >=3.10`, relaxed version pins
- ✅ Split hardware packages into optional extras (`[hardware]`, `[full]`)
- ✅ Core install (`pip install imswitch`) no longer requires NI-DAQ, Lantz, napari
- ✅ Consolidate or remove `setup.py` in favour of `pyproject.toml` only (deleted in Milestone 3)
- ⬜ Remove dead code and unused imports (29 bare `except:` blocks remain in `imcontrol/`)
- ⬜ Establish clear package boundaries

## Milestone 3: Code-Level Bug Fixes

**Goal:** Resolve known structural bugs and naming inconsistencies found during codebase analysis.

- ✅ Fix duplicate `EtSTEDInfo` class in `imswitch/imcontrol/model/SetupInfo.py` (renamed first to `EtSTEDDeviceInfo`)
- ✅ Rename `SQUIDLaserManager.py` → `ESP32LEDLaserManager.py` to match the class it contains
- ✅ Delete redundant `setup.py` shim (build metadata fully covered by `setup.cfg` + `pyproject.toml`)
- ✅ Remove dead `SLMController`/`slmManager` layer (verified gone from `MasterController`; superseded by `SLMsController`/`slmsManager`)
- ✅ Move `__test_Manager.py` from `imcontrol/` root into `_test/` (now at `imcontrol/_test/__test_Manager.py`; still requires live hardware, still not part of CI)
- ⬜ Separate driver mocks from real interfaces in `model/interfaces/` (currently mixed with no clear pattern)
- ⬜ Remove bare `except:` blocks and blank imports (deferred from Milestone 2 cleanup)

## Milestone 4: Hardware Abstraction Cleanup

**Goal:** Improve the hardware abstraction layer for clarity and safety.

- Document all hardware interfaces
- Standardize manager/controller patterns
- Add type hints to hardware interfaces
- Improve error handling in hardware communication
- Add timeout mechanisms where missing

## Milestone 5: Detector Manager Refactor

**Goal:** Modernize and simplify detector management.

- Map current detector manager dependencies
- Define clean detector interface
- Refactor with backward compatibility
- Add comprehensive tests
- Document the new architecture

## Milestone 6: DAQ Safety Layer

**Goal:** Add a safety layer around all DAQ operations.

- Implement voltage limit enforcement
- Add scan-parameter validation in `SetupInfo` validators (config-level limits, e.g. galvo `maxVolt` range, dwell-time bounds)
- Implement graceful error recovery for DAQ failures
- Add logging for all DAQ operations
- Create integration tests with mock hardware

## Milestone 7: Documentation Improvements

**Goal:** Comprehensive documentation for developers, agents, and users.

- ✅ Architecture map (`docs/ARCHITECTURE.md` + SVG) — manager inventory, controller→manager matrix, startup flow
- ⬜ Document current state and known issues (moved from Milestone 1)
- ⬜ Document all configuration options in `SetupInfo`
- ⬜ Write developer onboarding guide
- ⬜ Ship the microscope-KB building guide (schema + prompts, [ScopeAId](https://github.com/LREIN663/ScopeAId)-based) under `docs/microscope-kb/`; users build their own KB locally and feed it to an external LLM project (Claude Project / Custom GPT / etc.) — ImSwitch ships no KB content and no in-app LLM
- ⬜ Create agent task templates for common operations

## Milestone 8: ImControl UI & Workflow Enhancements ✅

**Goal:** Modernize ImControl UI with persistent state, improved configuration editing, and advanced imaging workflows.

- ✅ Widget state persistence framework (`WidgetStatePersistence.py` + `docs/WIDGET_STATE_PERSISTENCE.md`)
  - Save/load widget controller states (laser power, scan params, detector settings) to JSON
  - UI integration: File menu actions (Ctrl+Shift+S/L) with file dialogs
  - Implemented for: Laser, Settings (detector), Scan, Positioner, Recording, Rotator controllers
- ✅ Configuration editor improvements (`utility_scripts/imswitch_config_editor.py`)
  - Widget layout JSON editor with drag-drop visual widget picker
  - COM port quoting bugfix, chip/panel UI fixes
  - macOS window positioning fix
- ✅ FLIM (Fluorescence Lifetime Imaging) support
  - `FLIMHistWidget.py` (histogram display), `FLIMHistController.py`
  - Optimized pipeline with cached scan-invariant tables
  - `SwabianTimeTaggerManager` fixes (live preview, signal duplication, init=True per frame)
- ✅ Tiling/stitching workflow
  - `TilingController.py` + `TilingWidget.py` (replaced stub implementation)
  - Spiral scan pattern (`spiral_moves` utility)
  - `StitchedImage` in-memory tile stitcher (workflows)
  - `TilingInfo` config dataclass
- ✅ Napari viewer enhancements
  - `ViewerToolManager` (ROI, line, pan tools via napari Shapes layer)
  - Zoom-stable edge widths (shape edges in screen pixels)
  - 3D/2D dimension transition handling (layer padding to ndisplay dims)
  - Manual contrast range inputs, scale bar unit set to µm
- ✅ Detector improvements
  - Hamamatsu advanced subarray property editing (`docs/HAMAMATSU_ADVANCED_PROPERTIES.md`)
  - TIS camera IndexError fix
  - APD tuple mutation fix
  - Detector pixel size float step fix
  - Frame sync after advanced property changes
- ⬜ Dynamic napari layer lifecycle (still planned)
  - Currently: all `forAcquisition` detectors get permanent layers at startup
  - Plan: create/remove layers dynamically based on active detector
  - Design doc: `docs/dynamic_layer_lifecycle_plan.md`

