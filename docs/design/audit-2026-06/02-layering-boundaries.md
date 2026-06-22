# Audit Report 2 — Layering & Boundaries

## Summary

- **Critical MVC violations**: Model managers directly import controller classes (NidaqLaserManager, WidgetStatePersistence), breaking unidirectional dependency flow; one controller imports view widgets (TilingController).
- **Widespread private API access**: Controllers systematically bypass manager public interfaces to access `._subManager`, `._subManagers`, `._position`, and `._laserInfo` private attributes (6+ instances).
- **Bidirectional cross-module coupling**: `imcontrol` ↔ `improcess` form a circular dependency via shared segmentation code and configuration utilities, preventing independent deployment.
- **View bypasses controller layer**: Multiple widgets (EtSnoutyWidget, SegmentationParamsWidget, ShortcutEditorDialog) directly import model classes and configuration tools instead of delegating through controllers.
- **God-objects present**: SLMsWidget (2212 LOC), ScanWidgetAdvanced (1972 LOC), RecordingManager (1632 LOC), and SLMsController (1333 LOC) exceed reasonable complexity; third-party interface files reach 6874 LOC but are out of scope.
- **`imcommon` is well-structured**: Serves as a legitimate shared utility layer (framework, logging, GUI tools) with no evidence of dumping-ground abuse; 49 focused files across model/view/controller patterns.

---

## Findings

### [HIGH] Model→Controller: NidaqLaserManager imports CommunicationChannel

**Site**: `imswitch/imcontrol/model/managers/lasers/NidaqLaserManager.py:6`
```python
from imswitch.imcontrol.controller import CommunicationChannel
```

**Why it breaks layering**: Model (business logic/device drivers) should never depend on controller layer. This creates inverted dependency—model becomes untestable without controller infrastructure, and violates MVC's unidirectional flow (Model ← Controller → View).

**Recommendation**: Move `CommunicationChannel` to `imswitch.imcommon.controller` (shared infra) or `imswitch.imcontrol.model.signaling` if it's truly a model-level pub/sub primitive. If it contains controller-specific logic, refactor NidaqLaserManager to accept a signal emitter via dependency injection rather than importing it.

---

### [HIGH] Model→Controller: WidgetStatePersistence lazy-imports ComponentStateApplyMode

**Site**: `imswitch/imcontrol/model/WidgetStatePersistence.py:210`
```python
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
```

**Why it breaks layering**: Model layer (state persistence registry) imports an enum from controller layer. While lazy-import at function scope avoids import-time circular dependency, it still creates runtime dependency on controller internals. Model should define its own contracts.

**Recommendation**: Move `ComponentStateApplyMode` enum to `imswitch.imcontrol.model` (e.g., `model/state_contracts.py`) since it defines a data contract for state application semantics, not controller behavior. Controller's `basecontrollers.py` should re-export it for convenience, but the canonical definition belongs in model.

---

### [HIGH] Controller→View: TilingController imports SegmentationParamsWidget

**Site**: `imswitch/imcontrol/controller/controllers/TilingController.py:249-251`
```python
from imswitch.imcontrol.view.widgets.SegmentationParamsWidget import (
    SegmentationParamsWidget,
)
```

**Why it breaks layering**: Controllers should interact with views via abstract interfaces or signals, not import concrete widget classes. This creates tight coupling—controller logic becomes bound to PyQt widget implementation details, making it impossible to test without GUI framework or swap UI layers.

**Recommendation**:
1. Extract segmentation parameter logic from `SegmentationParamsWidget` into a model-layer class (e.g., `SegmentationParams` in `model/workflows/`).
2. Controller should call `self._widget.getSegmentationParams()` which returns the model object, not import the widget class directly.
3. If the controller needs to access widget state for UI coordination, define an abstract interface (e.g., `ISegmentationParamsProvider`) that the widget implements.

---

### [HIGH] Cross-module circular dependency: imcontrol ↔ improcess

**Sites**:
- **imcontrol → improcess**: `imswitch/imcontrol/model/workflows/segmentation.py:38-42`
  ```python
  from imswitch.improcess.analysis.segmentation import (
      otsu_threshold,
      prepare_segmentation_image,
      segment_image,
  )
  ```
- **improcess → imcontrol** (3 locations):
  - `imswitch/improcess/view/WatcherFrame.py:3`: `from imswitch.imcontrol.view import guitools`
  - `imswitch/improcess/controller/ImProcessMainController.py:54,232`: `from imswitch.imcontrol.model import getWidgetStatePersistence`
  - `imswitch/improcess/model/processing_config.py:9-10`: `from imswitch.imcontrol.model import configfiletools, SetupInfo`

**Why it breaks layering**: Bidirectional dependency prevents either module from being deployed, tested, or evolved independently. Creates fragile coupling where changes in one module can break the other through transitive imports.

**Recommendation**:
1. **Break the cycle via shared abstraction**: Move `improcess.analysis.segmentation` algorithms to `imcommon.algorithms.segmentation` (shared analysis library) so both modules depend on common, not each other.
2. **Invert improcess→imcontrol dependencies**:
   - Move `guitools` that `WatcherFrame` needs to `imcommon.view.guitools` (already exists—verify if duplication).
   - Replace `processing_config.py`'s direct import of `configfiletools` with dependency injection: `ImProcessMainController` should receive config dict from caller, not reach into imcontrol internals.
   - Replace `getWidgetStatePersistence` import with an adapter pattern: ImProcess defines its own persistence interface; imcontrol provides an implementation.
3. **Verify separation**: After refactor, `improcess` should be runnable standalone without `imcontrol` on Python path.

---

### [MED] View→Model: EtSnoutyWidget imports model utility

**Site**: `imswitch/imcontrol/view/widgets/EtSnoutyWidget.py:9`
```python
from imswitch.imcontrol.model.EtSnoutyPaths import getEtSnoutyPath
```

**Why it breaks layering**: View directly accesses model-layer file path utilities, bypassing controller. While less severe than importing managers, it prevents path logic from being centralized/mocked and couples widget to filesystem structure.

**Recommendation**: Controller should provide path via `self._controller.getEtSnoutyPath()` or expose it as a property. If path is static config, move `getEtSnoutyPath` to `imcommon.model.dirtools` (shared utilities) rather than model layer.

---

### [MED] View→Model: SegmentationParamsWidget imports Segmenter class

**Sites**:
- `imswitch/imcontrol/view/widgets/SegmentationParamsWidget.py:90`
- `imswitch/imcontrol/view/widgets/SegmentationParamsWidget.py:262`

```python
from imswitch.imcontrol.model.workflows.segmentation import Segmenter
self._segmenter = Segmenter()
```

**Why it breaks layering**: Widget instantiates and runs segmentation algorithms directly (model business logic) instead of delegating to controller. View becomes untestable without model dependencies and mixes presentation with computation.

**Recommendation**:
1. Controller should own the `Segmenter` instance and expose methods like `controller.runSegmentation(params)`.
2. Widget should only render parameters UI and emit signals/callbacks when user requests segmentation.
3. If preview computation must run in widget for performance (e.g., live slider feedback), define a thin adapter interface that controller provides.

---

### [MED] View→Model: ShortcutEditorDialog imports configfiletools

**Site**: `imswitch/imcontrol/view/widgets/ShortcutEditorDialog.py:378`
```python
from imswitch.imcontrol.model import configfiletools
options = configfiletools.loadOptions()
setupInfo = configfiletools.loadSetupInfo(options, options.setupFileName)
```

**Why it breaks layering**: Widget directly manipulates application config files (model layer responsibility). Creates tight coupling to file I/O and prevents config source from being abstracted (e.g., database, remote API).

**Recommendation**: Controller should provide `saveShortcuts(shortcutsMap)` method that delegates to model layer. Widget should only collect user input and call controller method. Bonus: enables undo/redo and validation logic in controller.

---

### [MED] Controllers access manager private `._subManager` / `._subManagers`

**Sites**:
- `imswitch/imcontrol/controller/controllers/EtMonalisaController.py:90,92,95`
  ```python
  self._master.standManager._subManager.setFLUO()
  self._master.standManager._subManager.setILshutter(1)
  self._master.standManager._subManager.setCS()
  ```
- `imswitch/imcontrol/controller/controllers/EtSnoutyController.py:215,661,721`
  ```python
  self._master.detectorsManager._subManagers[self.detectorFast]
  ```

**Why it breaks layering**: Bypasses manager's public API to reach internal device handles. Breaks encapsulation—if manager refactors internal structure (e.g., renames `_subManager` or changes composite pattern), controllers break. Indicates missing public API methods.

**Proper API (recommended)**:
```python
# In standManager:
def setFLUO(self): self._subManager.setFLUO()
def setILshutter(self, state): self._subManager.setILshutter(state)
def setCS(self): self._subManager.setCS()

# In detectorsManager:
def getDetector(self, name): return self._subManagers.get(name)
```

**Recommendation**: Add public methods to `standManager` and `detectorsManager` that delegate to internal sub-managers. Controllers should call `standManager.setFLUO()` instead of reaching through `._subManager`. If sub-manager access is truly unavoidable, document it as public API and remove leading underscore.

---

### [MED] Controllers access manager private `._position`

**Site**: `imswitch/imcontrol/controller/controllers/BSC203Controller.py:147-149`
```python
# Keep manager._position in sync with hardware
self._stageManager._position['X'] = x * 1000
self._stageManager._position['Y'] = y * 1000
self._stageManager._position['Z'] = z * 1000
```

**Why it breaks layering**: Direct mutation of manager's internal state dictionary bypasses invariants/validation. If manager later adds position caching, coordinate transforms, or bounds checking, this code will silently break them. Comment reveals the symptom: controller is "syncing" because public API doesn't exist.

**Proper API (recommended)**:
```python
# In stageManager:
def updatePosition(self, axis: str, value_um: float):
    """Update tracked position after hardware move. Internal use only."""
    self._position[axis] = value_um
    self.sigPositionChanged.emit(axis, value_um)

# Or batch update:
def updatePositions(self, positions: dict[str, float]):
    self._position.update(positions)
    self.sigPositionsChanged.emit(positions)
```

**Recommendation**: Add `stageManager.updatePosition(axis, value)` or `updatePositions(dict)` method. Controller calls it after hardware move. If this reveals that BSC203Controller is doing position tracking that belongs in manager, refactor hardware abstraction layer instead.

---

### [MED] Controllers access manager private `._laserInfo`

**Site**: `imswitch/imcontrol/controller/controllers/LaserController.py:37`
```python
for lName, lManager in self._master.lasersManager:
    if "calibCsvPath" in lManager._laserInfo.managerProperties:
```

**Why it breaks layering**: Directly inspects manager's internal metadata object. If `_laserInfo` structure changes (e.g., rename `managerProperties` or move to external config), controller breaks. Indicates missing public API for querying laser capabilities.

**Proper API (recommended)**:
```python
# In LaserManager base class:
def hasProperty(self, key: str) -> bool:
    return key in self._laserInfo.managerProperties

def getProperty(self, key: str, default=None):
    return self._laserInfo.managerProperties.get(key, default)
```

**Recommendation**: Add `lManager.hasProperty("calibCsvPath")` and `lManager.getProperty("calibCsvPath")` methods. Controller calls these instead of reaching into `._laserInfo`. Encapsulates metadata access and allows managers to override property lookup logic.

---

### [LOW] improcess→imcontrol test dependency (acceptable in test code)

**Site**: `imswitch/improcess/_test/test_data_obj_io.py:8`
```python
from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
```

**Why it's low severity**: Test code often imports from other modules to verify integration. This is acceptable as long as production `improcess` code doesn't depend on `imcontrol`.

**Recommendation**: Consider moving `ZarrStorer` to `imcommon.model.io` if it's a reusable data storage abstraction (not RecordingManager-specific). Otherwise, document that this test verifies improcess-imcontrol data format compatibility.

---

## God-object / Size Table

**Top 15 files by line count** (excluding third-party interfaces: `pipython/`, `pyicic/`):

| Rank | LOC  | Path | Layer | Notes |
|------|------|------|-------|-------|
| 1 | 2212 | `imcontrol/view/widgets/SLMsWidget.py` | View | ⚠️ Excessive UI logic |
| 2 | 1972 | `imcontrol/view/widgets/ScanWidgetAdvanced.py` | View | ⚠️ Complex scan UI |
| 3 | 1704 | `imcommon/view/guitools/naparitools.py` | View | Napari integration helpers |
| 4 | 1632 | `imcontrol/model/managers/RecordingManager.py` | Model | ⚠️ God-object manager |
| 5 | 1333 | `imcontrol/controller/controllers/SLMsController.py` | Controller | ⚠️ Matches large widget |
| 6 | 1208 | `imcontrol/controller/controllers/EventTriggeredBaseController.py` | Controller | Base class complexity |
| 7 | 1150 | `imcontrol/model/interfaces/SmarACT.py` | Model | Device driver |
| 8 | 1115 | `improcess/view/ImProcessMainView.py` | View | Main window UI |
| 9 | 1089 | `imcontrol/controller/controllers/BeadRecController.py` | Controller | Feature controller |
| 10 | 1087 | `imcontrol/_test/unit/test_recording.py` | Test | Comprehensive tests |
| 11 | 1079 | `imcontrol/controller/controllers/SetupModesController.py` | Controller | Complex state logic |
| 12 | 1060 | `imcontrol/controller/controllers/ScanControllerAdvanced.py` | Controller | ⚠️ Matches scan widget |
| 13 | 1059 | `imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py` | Model | Device driver |
| 14 | 1055 | `imcontrol/_test/unit/test_tiling_workflow.py` | Test | Workflow tests |
| 15 | 1039 | `imcontrol/model/interfaces/hamamatsu.py` | Model | Device driver |

**Analysis**:
- **View god-objects**: `SLMsWidget` (2212) and `ScanWidgetAdvanced` (1972) suggest feature bloat. Likely candidates for splitting into sub-widgets (e.g., SLMParamsWidget + SLMPatternsWidget + SLMPreviewWidget).
- **Model god-object**: `RecordingManager` (1632 LOC) probably conflates recording orchestration, format management, and device coordination. Recommend splitting into `RecordingCoordinator`, `FormatRegistry`, and individual format handlers.
- **Controller bloat**: `SLMsController` (1333) and `ScanControllerAdvanced` (1060) mirror their widgets' complexity. After splitting widgets, controllers should shrink naturally.
- **Third-party interfaces excluded**: `gcsbasecommands.py` (6874 LOC) and `IC_GrabberDLL.py` (2252 LOC) are vendor-provided bindings, out of scope for refactoring.

---

## Severity Table

| Severity | Count | Impact |
|----------|-------|--------|
| **HIGH** | 4 | Architectural violations that prevent module independence, testing, or create circular dependencies |
| **MED** | 7 | API encapsulation breaks or layer-skipping that increases coupling and maintenance cost |
| **LOW** | 1 | Test-code dependency, acceptable for integration testing |
| **Total** | 12 | Verified findings with line-number citations |

---

## What's Cleanly Layered

Despite violations, several areas demonstrate good architectural discipline:

1. **`imcommon` is well-factored**: 49 files organized into `model/`, `view/`, `controller/` with focused utilities (logging, dirtools, Qt framework abstractions, napari tools). No evidence of dumping-ground anti-pattern; all modules serve legitimate cross-cutting concerns.

2. **`imscripting` is independent**: Zero imports to/from `imcontrol` or `improcess` (verified by user context). Clean module boundary enables standalone scripting engine.

3. **Most managers have clean APIs**: The majority of managers in `imcontrol/model/managers/` are imported and used via public methods. Private API violations are localized to 3 controllers (EtMonalisa, EtSnouty, BSC203, Laser) out of dozens.

4. **Controller→Model flow is mostly unidirectional**: Controllers typically call manager methods correctly. The 2 model→controller violations (NidaqLaserManager, WidgetStatePersistence) are exceptions, not systemic issues.

5. **TYPE_CHECKING guards exist**: `CommunicationChannel.py` and `SLMsWidget.py` use `if TYPE_CHECKING:` for type hints, showing awareness of import discipline (though adoption is minimal: 2 instances found).

6. **No import-time side effects detected**: Grep analysis found no top-level calls to expensive operations (database connections, file I/O, hardware init) at module import. Lazy imports in SegmentationParamsWidget and WidgetStatePersistence avoid circular import crashes (though they mask the underlying architectural problem).

7. **Test isolation**: Test files (`_test/`) correctly import from production code without polluting production imports. Test-only dependencies stay in test scope.

---

**End of Report**
