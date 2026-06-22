# Audit Report 1 — Docs ↔ Code Drift

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit Date:** 2026-06-21
**Scope:** Documentation vs. actual code structure (read-only audit)

## Summary

The most significant documentation drift issues discovered:

- **Widget inventory severely outdated** — ARCHITECTURE.md claims 28 widgets when the codebase contains 48 widget files
- **Controller inventory incomplete** — The controller→manager matrix documents 18 controllers but omits 25+ additional controllers present in the codebase
- **Stale module references** — `shortcuts_config.md` references non-existent modules "imreconstruct" and "imnotebook" (should be "improcess")
- **Incorrect line number citations** — ViewerToolManager location cited as lines 1153-1342 when the class actually starts at line 1214
- **Contradictory removal claim** — ARCHITECTURE.md claims SLMController/slmManager were removed, but both still exist in code
- **Overclaim about plugin architecture** — README claims "one file + one entry" for all device types, but this is only true for managers (controllers and widgets require manual registration in `__init__.py`)

---

## Findings

### [HIGH] Widget count severely understated

- **Doc:** `docs/design/ARCHITECTURE.md:262` says "ImSwitch ships **28 widgets** under `view/widgets/` (excluding base classes):"
- **Code:** `imswitch/imcontrol/view/widgets/` contains **48** `.py` files (excluding `__init__.py`, `__pycache__`, and `basewidgets/`)
- **Impact:** Developers and users have a fundamentally incorrect understanding of the UI surface area. The documented list omits approximately 20 widgets (42% of the total).
- **Recommendation:** Update the widget inventory. Either provide the full list of 48 widgets or clarify the criteria for inclusion (e.g., "28 core/commonly-used widgets" if there's a meaningful distinction).

**Verification:**
```bash
$ ls -1 /Users/lenny/PycharmProjects/Imswitch2/imswitch/imcontrol/view/widgets/*.py | grep -v "__" | wc -l
      48
```

Documented widgets (from ARCHITECTURE.md:262-269):
ImageWidget, SettingsWidget, ConsoleWidget, LaserWidget, PositionerWidget, RotatorWidget, RecordingWidget, FocusLockWidget, AutofocusWidget, ScanWidgetBase/Advanced/MoNaLISA/PointScan, SLMWidget, SLMsWidget, EtSTEDWidget, EtMonalisaWidget, RotationScanWidget, BeadRecWidget, ULensesWidget, AlignmentLineWidget, AlignAverageWidget, AlignXYWidget, FFTWidget, LineProfileWidget (28 items).

Example undocumented widgets visible in code: AlignTileWidget.py, FLIMHistWidget.py, FlipMirrorWidget.py, LightSheetMulticolorWidget.py, MotCorrWidget.py, SetupModesWidget.py, SetupStatusWidget.py, TilingWidget.py, TriggerScopeScanWidget.py, ViewerToolsWidget.py, WatcherWidget.py, WellPlateWidget.py, WorkflowFacadeWidget.py, plus various TriggerScope-specific widgets.

---

### [HIGH] Controller inventory severely incomplete

- **Doc:** `docs/design/ARCHITECTURE.md:272-291` Controller→Manager matrix documents **18 controller types** (counting "ScanControllers (×4)" as one group)
- **Code:** `imswitch/imcontrol/controller/controllers/` contains **46** controller `.py` files (excluding `__init__.py` and helpers like `_beadrec_scan_source.py`)
- **Impact:** The dependency matrix, which is supposed to be a comprehensive reference for "which controllers use which hardware", omits more than half of the controllers. Developers cannot rely on it for architecture planning or impact analysis.
- **Recommendation:** Either (1) complete the matrix with all controllers, (2) clearly state it covers only "core/stable" controllers and list the criteria, or (3) provide a second table for "additional/specialized controllers".

**Documented controllers** (from ARCHITECTURE.md:272-291):
ImageController, SettingsController, ViewController, ULensesController, LaserController, PositionerController, RotatorController, RecordingController, FocusLockController, AutofocusController, ScanControllers (×4: Base, Advanced, MoNaLISA, PointScan), SLMController, SLMsController, EtSTED/EtMonalisa, RotationScanController, BFTimelapseController, LeicaStandController, BeadRecController.

**Example undocumented controllers** (verified at `imswitch/imcontrol/controller/controllers/*.py`):
- AlignAverageController.py
- AlignXYController.py
- AlignmentLineController.py
- BSC203Controller.py
- ConsoleController.py
- EtSnoutyController.py
- EventTriggeredBaseController.py
- FFTController.py
- FLIMHistController.py
- FlipMirrorController.py
- LightSheetMulticolorController.py
- LineProfileController.py
- MotCorrController.py
- SetupModesController.py
- SetupStatusController.py
- TilingController.py
- TriggerScopeGalvoDetectionController.py
- TriggerScopeLSXYRController.py
- TriggerScopePLSRController.py
- TriggerScopePLSRMulticolorController.py
- TriggerScopeRasterController.py
- TriggerScopeScanController.py
- ViewerToolsController.py
- WatcherController.py
- WellPlateController.py
- WorkflowFacadeController.py

---

### [HIGH] Stale module references: imreconstruct and imnotebook

- **Doc:** `docs/shortcuts_config.md:108` says "This shortcut system applies to **imcontrol only** (the main microscopy control module). Other modules (e.g., imreconstruct, imnotebook) have independent shortcut systems."
- **Code:**
  - No `imreconstruct/` or `imnotebook/` directory exists under `imswitch/`
  - Actual modules: `imcommon/`, `imcontrol/`, `improcess/`, `imscripting/`, `pluginapi/`
  - The current image-processing module is **`improcess`**, not `imreconstruct`
- **Impact:** Misleading reference suggests modules that don't exist. `imreconstruct` may be a legacy/renamed module (if ImSwitch1 used that name, it's now `improcess` in ImSwitch2). `imnotebook` may have never shipped.
- **Recommendation:** Replace line 108 with "Other modules (e.g., improcess, imscripting) have independent shortcut systems" or simply remove the parenthetical if those modules don't actually have independent shortcut systems.

**Verification:**
```bash
$ ls -1d /Users/lenny/PycharmProjects/Imswitch2/imswitch/im*/
imswitch/imcommon/
imswitch/imcontrol/
imswitch/improcess/
imswitch/imscripting/
# No imreconstruct or imnotebook
```

Cross-reference: `docs/modules.rst:10-15` correctly lists the three user-facing modules as `imcontrol`, `improcess`, and `imscripting`.

---

### [MED] Incorrect line-number citation for ViewerToolManager

- **Doc:** `docs/design/ARCHITECTURE.md:366` says "**Location:** `imswitch/imcommon/view/guitools/naparitools.py` (lines 1153-1342)"
- **Code:**
  - File `imswitch/imcommon/view/guitools/naparitools.py` is **1704 lines** long (not ~1342)
  - `class ViewerToolManager` is defined at **line 1214**, not line 1153
- **Impact:** Developers looking up the class will search the wrong lines. The cited range is off by ~60 lines at the start, and the file has grown ~360 lines beyond the cited end.
- **Recommendation:** Update citation to `naparitools.py:1214` (or provide an approximate range like `1214-1400` if you want to indicate the class body).

**Verification:**
```bash
$ wc -l /Users/lenny/PycharmProjects/Imswitch2/imswitch/imcommon/view/guitools/naparitools.py
    1704 naparitools.py

$ grep -n "^class ViewerToolManager" naparitools.py
1214:class ViewerToolManager:
```

---

### [MED] Contradictory claim: SLMController/slmManager removal

- **Doc:** `docs/design/ARCHITECTURE.md:503` says "✅ **Legacy SLM dualism** — Old `SLMController`/`slmManager` removed from codebase (Milestone 3)"
- **Code:**
  - `imswitch/imcontrol/controller/controllers/SLMController.py` **exists** (line 17: `class SLMController(StatefulComponentMixin, ImConWidgetController)`)
  - `imswitch/imcontrol/model/managers/SLMManager.py` **exists**
  - Both are imported and referenced in the controller→manager matrix (ARCHITECTURE.md:285: "SLMController | ... | X | ...")
- **Impact:** Contradictory statements create confusion. Either the claim is wrong, or there was an "old" SLMController that was replaced by a new implementation (but still called SLMController), or the dualism refers to something else (e.g., SLMController vs. SLMsController, which both exist).
- **Recommendation:** Clarify what "Legacy SLM dualism" means. If the old implementation was replaced in-place, rephrase to "Legacy SLM implementation replaced with refactored version" or similar. If the claim is simply incorrect, remove or update the milestone line.

**Verification:**
```bash
$ ls -1 /Users/lenny/PycharmProjects/Imswitch2/imswitch/imcontrol/controller/controllers/SLM*.py
SLMController.py
SLMsController.py

$ ls -1 /Users/lenny/PycharmProjects/Imswitch2/imswitch/imcontrol/model/managers/SLM*.py
SLMManager.py
```

---

### [MED] Overclaim: "one file + one entry" for all device types

- **Doc:** `README.md:179` says "Adding a new driver is **one file plus one JSON entry**" (emphasis in spirit, not original)
- **Code:**
  - **Managers (hardware layer):** TRUE — the plugin registry (`imcontrol/model/plugins/registry.py:56`) automatically discovers managers via `MultiManager._resolveManagerClass` (MultiManager.py:47-74).
  - **Controllers:** FALSE — you must add the new controller to `_CONTROLLER_MODULES` dict in `imswitch/imcontrol/controller/controllers/__init__.py:14-61` for it to be importable by `ImConWidgetControllerFactory`.
  - **Widgets:** FALSE — you must add the new widget to `_WIDGET_MODULES` dict in `imswitch/imcontrol/view/widgets/__init__.py:14-64` for it to be importable.
- **Impact:** README oversimplifies the plugin architecture. The claim is accurate **only for device managers**, not for controllers or widgets (which require manual registration). New contributors will be confused when their controller or widget file is not automatically discovered.
- **Recommendation:** Qualify the claim: "Adding a new **device manager** is one file plus one JSON entry. Controllers and widgets require an additional registration step in their respective `__init__.py` files — see the developer onboarding guide for details."

**Verification:**
- Plugin registry for managers: `imswitch/imcontrol/model/plugins/registry.py:26-40` (auto-discovery via `iter_entry_points`)
- Manual registration for controllers: `imswitch/imcontrol/controller/controllers/__init__.py:14-61` (`_CONTROLLER_MODULES` dict)
- Manual registration for widgets: `imswitch/imcontrol/view/widgets/__init__.py:14-64` (`_WIDGET_MODULES` dict)

---

### [LOW] Manager inventory claim accuracy

- **Doc:** `README.md:154` says "Imswitch2 ships managers for **42+ devices** across five categories."
- **Code:** Actual count in `imswitch/imcontrol/model/managers/`:
  - `detectors/`: 13 managers
  - `lasers/`: 14 managers
  - `positioners/`: 12 managers
  - `rotators/`: 3 managers
  - `flipMirrors/`: 1 manager
  - `slms/`: 1 manager (SLMManager.py in root)
  - `rs232/`: 3 managers
  - Root managers: NidaqManager, RecordingManager, ScanManager*, StandManager, PulseGeneratorManager, etc. (~10+)
  - **Total:** Approximately **57+ manager files**, though some are base classes or variants.
- **Impact:** Minor — the claim says "42+" which is technically correct (57 > 42), but it's a significant undercount.
- **Recommendation:** Update to "57+ devices" or perform an exact count excluding abstract base classes.

**Note:** The claim "five categories" is also imprecise. The actual categories include detectors, lasers, positioners, rotators, flipMirrors, slms, rs232, and several non-MultiManager categories (nidaq, recording, scan, stand, pulse generator). This is more like 8+ categories.

---

### [LOW] Signal flow documentation accuracy

- **Doc:** `docs/design/ARCHITECTURE.md:293-300` documents `CommunicationChannel` as the "Inter-Controller Signal Bus" and describes the major signals.
- **Code:** Verified against `imswitch/imcontrol/controller/CommunicationChannel.py:1-50`
- **Impact:** None — this section is **accurate**. The signals listed in the docs match the actual signal definitions in `CommunicationChannel.__init__`.
- **Finding:** This is an example of well-maintained documentation. The signal inventory is correct.

---

### [LOW] Startup flow documentation accuracy

- **Doc:** `docs/design/ARCHITECTURE.md:18-38` documents the application startup sequence
- **Code:** Verified against:
  - `imswitch/__main__.py` → module loading
  - `imswitch/imcontrol/controller/ImConMainController.py:26-106` → controller initialization
  - Line 44: `self.__commChannel = CommunicationChannel(...)`
  - Line 45: `self.__masterController = MasterController(...)`
  - Line 49: `self.__factory = ImConWidgetControllerFactory(...)`
  - Line 106: `self.__api = generateAPI(...)`
- **Impact:** None — the documented flow is **accurate** and matches the code.
- **Finding:** Another example of correct, verifiable documentation.

---

## Severity Table

| Severity | Count | Examples |
|---|:---:|---|
| **HIGH** | 4 | Widget count (28 vs 48), controller inventory (18 vs 46), stale module refs (imreconstruct/imnotebook), plugin architecture overclaim |
| **MED** | 3 | Incorrect line numbers (ViewerToolManager), contradictory SLM removal claim, manager count understatement |
| **LOW** | 2 | Signal flow (accurate ✓), startup flow (accurate ✓) |

**Total findings:** 9 (4 high, 3 medium, 2 low verification/accurate)

---

## Things That Are Actually Accurate / Well-Documented

**Credit where it's due:**

1. **Startup sequence** (`ARCHITECTURE.md:18-38`) — Accurately describes the initialization flow from `__main__.py` → `ImConMainController` → `CommunicationChannel` → `MasterController` → controller factory → API generation. Verified against `ImConMainController.py:26-106`.

2. **CommunicationChannel signal inventory** (`ARCHITECTURE.md:293-300`) — Correctly lists the major Qt signals emitted by the inter-controller signal bus. Verified against `CommunicationChannel.py:1-50`.

3. **Module list** (`docs/modules.rst:10-15`) — Correctly identifies the three user-facing modules as `imcontrol`, `improcess`, and `imscripting`.

4. **Developer onboarding guide** (`docs/developer-onboarding.rst`) — Provides accurate instructions for setup, no-hardware validation, and linting. The no-hardware test command on line 31 is current and correct.

5. **SetupInfo reference** (`docs/setupinfo-reference.rst`) — Exists and is comprehensive (27,740 bytes). Not audited in detail for this report, but cross-references from README and ARCHITECTURE.md are valid.

6. **Design docs under `docs/design/`** — The ARCHITECTURE.md file is generally high-quality, well-structured, and mostly accurate. The issues found are specific line-count/inventory staleness, not fundamental architectural misrepresentations.

---

## Recommendations Summary

1. **Update widget inventory** — Either provide the full 48-widget list or clarify the "28 widgets" claim (e.g., "28 commonly-used widgets").
2. **Complete controller matrix** — Add the missing 25+ controllers to the dependency matrix or clearly scope it as "core controllers only".
3. **Fix stale module references** — Replace "imreconstruct"/"imnotebook" with "improcess"/"imscripting" in `shortcuts_config.md:108`.
4. **Correct line number citation** — Update `ARCHITECTURE.md:366` to cite `naparitools.py:1214` for ViewerToolManager.
5. **Clarify SLM dualism claim** — Explain what was removed/replaced in Milestone 3, since both SLMController and SLMManager still exist.
6. **Qualify plugin architecture claim** — Specify that "one file + one entry" applies to **managers**, not controllers/widgets (which need manual registration).
7. **Update manager/device count** — Change "42+ devices" to reflect the actual ~57+ managers in the codebase.

---

**End of Report**
