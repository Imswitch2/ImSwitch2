# ImSwitch2 Architecture — Dependency Map

## Overview

ImSwitch follows a strict **Model-View-Presenter (MVP)** architecture organized into four independent modules, each loadable as a plugin:

| Module | Purpose | Package |
|---|---|---|
| **imcontrol** | Hardware control (lasers, stages, DAQ, detectors) | `imswitch.imcontrol` |
| **imreconstruct** | SIM image reconstruction | `imswitch.imreconstruct` |
| **imscripting** | Python scripting console & editor | `imswitch.imscripting` |
| **imcommon** | Shared framework, signals, utilities | `imswitch.imcommon` |

Each module implements `getMainViewAndController()` and sets `__imswitch_module__ = True` for dynamic discovery. The top-level `__main__.py` orchestrates multi-module loading via `MultiModuleWindow`.

---

## Application Startup Flow

```
imswitch/__main__.py
  → modulesconfigtools.getEnabledModuleIds()        # reads ~/ImSwitchConfig/config/modules.json
  → for each module: importlib(module)
  → MultiModuleWindow + MultiModuleWindowController
  → for each module: module.getMainViewAndController()
      → imcontrol.__init__:
          → loadOptions()                            # reads imcontrol_options.json
          → loadSetupInfo(setupFileName)             # reads hardware JSON config
          → PickSetupDialog (if not configured)
          → ImConMainView(setupInfo)                 # creates all widgets
          → ImConMainController(setupInfo, mainView)
              → CommunicationChannel()               # inter-controller signal bus
              → MasterController(setupInfo)           # instantiates all managers
              → ImConWidgetControllerFactory          # creates one controller per widget
              → generateAPI()                         # harvests @APIExport methods
              → ImSwitchServer (if pyroServerInfo.active)  # FastAPI + Pyro5
  → launchApp()                                      # Qt event loop
```

---

## Framework Layer (imcommon/framework)

A dependency-injection shim with abstract base classes (`base.py`) and Qt5 concrete implementations (`qt.py`):

| Class | Purpose |
|---|---|
| `Signal` | Qt Signal wrapper |
| `SignalInterface` | Base for any class that emits signals (QObject under the hood) |
| `Thread` | Thread wrapper with sip-safety guards |
| `Worker` | Runs in a Thread, emits signals |
| `Timer` | Periodic timer |
| `Mutex` | Mutual exclusion lock |

All application code imports from `framework` (which re-exports `qt.*`), so the backend could theoretically be swapped.

---

## Manager Layer — Hardware Abstraction

### Architecture Pattern

```
MultiManager(ABC)  ←  DetectorsManager, LasersManager, PositionersManager, ...
    │                        │
    │                  uses importlib to load
    │                        │
    ▼                        ▼
AbstractBaseManager(ABC)  ←  ConcreteDeviceManager (per hardware)
    (e.g. DetectorManager)       (e.g. HamamatsuManager)
```

`MultiManager` dynamically loads sub-managers by class name string from the JSON configuration. Hardware libraries are generally imported lazily (try/except) to allow graceful mock fallback.

### MasterController — Manager Instantiation

`MasterController` creates and owns all managers:

| Attribute | Manager Class | Always Present |
|---|---|---|
| `nidaqManager` | `NidaqManager` | Yes (if NI-DAQ configured) |
| `rs232sManager` | `RS232sManager` | Yes |
| `detectorsManager` | `DetectorsManager` | Yes |
| `lasersManager` | `LasersManager` | Yes |
| `positionersManager` | `PositionersManager` | Yes |
| `rotatorsManager` | `RotatorsManager` | Yes |
| `recordingManager` | `RecordingManager` | Yes |
| `slmsManager` | `SLMsManager` | Yes |
| `standManager` | `StandManager` | Conditional (if `setupInfo.microscopeStand`) |
| `scanManager` | `ScanManagerBase/PointScan/MoNaLISA/Advanced` | Conditional (by `scan.scanWidgetType`) |

### Detector Managers

| Manager | Hardware | Interface |
|---|---|---|
| `APDManager` | Avalanche Photodiode (NI-DAQ counter) | Via injected `nidaqManager` |
| `PMTManager` | Photomultiplier Tube (NI-DAQ analog) | Via injected `nidaqManager` |
| `HamamatsuManager` | Hamamatsu sCMOS (DCAM API) | `ctypes` DLL via bundled interface |
| `BaslerManager` | Basler cameras | `pypylon` (lazy) |
| `ThorcamManager` | Thorlabs cameras | Bundled interface |
| `PhotometricsManager` | Photometrics sCMOS/CCD | `pyvcam` (lazy) |
| `GXPIPYManager` | Daheng Imaging cameras | `gxipy` (lazy) |
| `TISManager` | The Imaging Source cameras | `ctypes`/`tisgrabber` via bundled interface |
| `SwabianTimeTaggerManager` | Swabian Time Tagger (FLIM) | `TimeTagger` (lazy) |
| `AVManager` | Generic video (webcam) | OpenCV |
| `JetsonCamManager` | NVIDIA Jetson cameras | Bundled interface |
| `PiCamManager` | Raspberry Pi Camera | `picamera` (lazy) |
| `ESP32CamManager` | ESP32-CAM (network) | Bundled interface |

### Laser Managers

| Manager | Hardware | Interface |
|---|---|---|
| `NidaqLaserManager` | NI-DAQ analog output | Via injected `nidaqManager` |
| `Cobolt0601LaserManager` | Cobolt 06-01 (Lantz) | `lantz` framework |
| `Cobolt0601NewLaserManager` | Cobolt 06-01 (direct serial) | Vendored Cobolt driver → `pyserial` |
| `CoboltLaserManager` | Generic Cobolt lasers | Vendored Cobolt driver |
| `LantzLaserManager` | Any Lantz-compatible laser | `lantz.messagebased` |
| `PyCoboltManager` | Cobolt lasers (Python serial) | `pyserial` direct |
| `AAAOTFLaserManager` | AAA AOTF | Via injected low-level manager |
| `MPBLaserManager` | MPB Communications fiber lasers | RS-232 |
| `CoolLEDLaserManager` | CoolLED illumination | Via injected low-level manager |
| `PulseStreamerLaserManager` | Swabian Pulse Streamer | Via injected `PulseStreamerManager` |
| `PyMicroscopeLaserManager` | python-microscopy compatible | `importlib` dynamic load |
| `ESP32LEDLaserManager` | SQUID/ESP32 LED | Via injected `SQUIDManager` |
| `LEDMatrixManager` | LED matrix control | Generic GPIO/serial |

### Positioner Managers

| Manager | Hardware | Interface |
|---|---|---|
| `NidaqPositionerManager` | NI-DAQ analog output (piezo/galvo) | Via injected `nidaqManager` |
| `PIStageManager` | Physik Instrumente stages | Bundled `pipython` (GCS2) |
| `BSC203StageManager` | Thorlabs BSC203 servo controller | `thorlabs_apt_device` |
| `PiezoconceptZManager` | Piezoconcept Z-axis | Via RS232 manager |
| `PiezoconceptZManager2` | Piezoconcept Z-axis (alternate) | Via RS232 manager |
| `LeicaDMIManager` | Leica DMI stand | Via RS232 manager |
| `MHXYStageManager` | Märzhäuser SCAN XY | Via RS232 manager |
| `SQUIDStageManager` | SQUID open-hardware stage | Via injected `SQUIDManager` |
| `SmarACTPositionerManager` | SmarACT piezo-motor stages | `ctypes` DLL |
| `MockPositionerManager` | Software mock | None |

### Rotator Managers

| Manager | Hardware | Interface |
|---|---|---|
| `StandaRotatorManager` | Standa rotation stages | Vendor SDK (lazy import) |

### RS232 / Board Managers

| Manager | Purpose |
|---|---|
| `RS232Manager` | Generic serial interface via vendored RS232Driver |
| `ESP32Manager` | UC2 ESP32 REST/serial (`uc2rest`) |
| `GRBLManager` | GRBL CNC G-code controller (`pyserial`) |
| `SQUIDManager` | SQUID open-hardware board (`pyserial`) |

### Other Managers

| Manager | Purpose |
|---|---|
| `NidaqManager` | Direct NI-DAQ interface (`nidaqmx`) — scan signal output, counter/analog input |
| `PulseStreamerManager` | Swabian Pulse Streamer digital output (`pulsestreamer`) |
| `RecordingManager` | Data recording to HDF5/TIFF/Zarr (`h5py`, `tifffile`, `zarr`) |
| `SLMManager` | Single SLM pattern generation (NumPy/PIL/SciPy) |
| `ScanManagerBase/Advanced/MoNaLISA/PointScan` | Scan waveform orchestration via `SignalDesignerFactory` |

---

## Signal Designers — Scan Waveform Generation

| Designer | Type | Purpose |
|---|---|---|
| `GalvoScanDesigner` | `ScanDesigner` | Galvo mirror analog waveforms (BPoly spline interpolation) |
| `BetaScanDesigner` | `ScanDesigner` | Physical stage step-scan waveforms |
| `BetaTTLCycleDesigner` | `TTLCycleDesigner` | Simple frame-based TTL pulses |
| `AdvancedScanTTLCycleDesigner` | `TTLCycleDesigner` | Per-pixel intra-pulse timing with linestep gating |
| `PointScanTTLCycleDesigner` | `TTLCycleDesigner` | Line-based TTL sequences for point scanning |

Loaded dynamically by `SignalDesignerFactory` using class name strings from JSON config.

---

## Controller Layer — Widget Controllers

### Controller Hierarchy

```
WidgetController (imcommon)
  └─ ImConWidgetController
       ├─ LiveUpdatedController (frame-driven: AlignAverage, AlignXY, FFT, Image, View)
       └─ SuperScanController (scan-driven: ScanControllerBase/PointScan/MoNaLISA/Advanced)
```

### Controller → Manager Dependency Matrix

| Controller | detectors | lasers | positioners | rotators | recording | nidaq | scan | slm | slms | stand | rs232 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| ImageController | X | | | | | | | | | | |
| SettingsController | X | | | | | | | | | | |
| ViewController | X | | | | | | | | | | |
| ULensesController | X | | | | | | | | | | |
| LaserController | | X | | | | | | | | | X |
| PositionerController | | | X | | | | | | | | |
| RotatorController | | | | X | | | | | | | |
| RecordingController | X | X | | | X | | | | | | |
| FocusLockController | X | | X | | | | | | | | |
| AutofocusController | X | | X | | | | | | | | |
| ScanControllers (×4) | | | X | | | X | X | | | | |
| SLMController | | | | | | | | X | | | |
| SLMsController | | | | | | | | | X | | |
| EtSTED/EtMonalisa | X | X | | | | X | | | | X | |
| RotationScanController | X | | | X | | | | | | | |
| BFTimelapseController | X | | | | | | | | | X | |
| LeicaStandController | | | | | | | | | | X | |
| BeadRecController | X | | | | | | | | | | |

### CommunicationChannel — Inter-Controller Signal Bus

Key signals (selected): `sigUpdateImage`, `sigAcquisitionStarted/Stopped`, `sigDetectorSwitched`, `sigRunScan`, `sigScanStarting/Built/Started/Done/Ended`, `sigRecordingStarted/Ended`, `sigSnapImg`, `sigSLMMaskUpdated`, `sigSetXYPosition`, `sigSetZPosition`.

### API Exposure

`@APIExport` decorators on controller methods are harvested by `generateAPI()` and exposed via dual protocols:
- **FastAPI** (HTTP REST) at `/{module}/{function_name}`
- **Pyro5** (RPC) with msgpack serialization

Large arrays (images) are passed via POSIX shared memory through `SerNDArray` to avoid copies.

---

## View Layer — Widgets

28 widget files in `imcontrol/view/widgets/`, all inheriting from `Widget` or `NapariHybridWidget`. Key widgets include:

| Widget | Purpose |
|---|---|
| `ImageWidget` | Main napari viewer with live acquisition display |
| `SettingsWidget` | Detector configuration (exposure, ROI, binning) |
| `LaserWidget` | Laser power control and presets |
| `PositionerWidget` | XYZ stage control |
| `RecordingWidget` | Data acquisition to HDF5/TIFF/Zarr |
| `FLIMHistWidget` | FLIM histogram display (Swabian Time Tagger) |
| `TilingWidget` | Multi-position tiled acquisition |
| `ViewerToolsWidget` | Napari tool mode selection (pan, ROI, line) |
| `LineProfileWidget` | Line profile analysis |
| `FocusLockWidget` | Autofocus/focus-lock control |
| `AutofocusWidget` | Autofocus algorithms |
| `AlignAverageWidget`, `AlignXYWidget` | Image alignment tools |
| `FFTWidget` | Fourier transform display |
| `BeadRecWidget`, `MotCorrWidget` | Bead reconstruction, motion correction |
| `EtSTEDWidget`, `EtMonalisaWidget` | STED/MoNaLISA microscopy |
| `SLMWidget`, `SLMsWidget` | SLM pattern control |
| `RotationScanWidget`, `RotatorWidget` | Rotation stage control |
| `ULensesWidget` | Microlens array analysis |
| `BFTimelapseWidget` | Brightfield timelapse |
| `LeicaStandWidget` | Leica stand control |
| `ConsoleWidget`, `WatcherWidget` | Scripting and monitoring |
| `ViewWidget` | Additional viewer controls |
| `AlignmentLineWidget` | Line alignment overlay |

Key architecture patterns:
- `pyqtgraph.dockarea.DockArea` for flexible panel layout
- `pyqtgraph.ParameterTree` for structured settings
- `EmbeddedNapari` for live image display
- Qt Signals following `sigVerbNoun` / `sigNounChanged` convention

---

## Subsystems Added During ImSwitch2 Work

### ViewerToolManager (2026-05-12)
**Location:** `imswitch/imcommon/view/guitools/naparitools.py` (lines 1153-1342)

A napari Shape layer-based viewer interaction tool manager:
- Lazy initialization (layer only created when first used)
- Provides simplified API: `set_mode('rectangle')`, `set_mode('line')`, `set_mode('pan')`
- Qt signals for integration: `sigShapesChanged`, `sigModeChanged`
- Helper methods: `get_rectangle_bounds()`, `get_line_endpoints()`, `get_shapes_data()`
- Enables napari-native ROI drawing, line profiles, and annotation tools
- Fully backward compatible with existing Vispy overlays

**Integration:** Used by `ImageWidget`, exposed via `ViewerToolsWidget` toolbar (disabled by default).

### Widget State Persistence Framework (2026-05-14)
**Location:** `imswitch/imcontrol/model/WidgetStatePersistence.py`

A centralized service for saving/loading widget controller states:
- Controllers opt-in via `getWidgetState()` / `setWidgetState()` interface
- States stored in `~/ImSwitchConfig/imcontrol_widget_states/` (JSON format)
- **Safety-first:** Never includes hardware-active states (laser on, acquisition running)
- Only restores passive configuration (values, settings, UI state)
- Per-property error handling with graceful degradation
- Accessible via File menu: "Save Widget States…" / "Load Widget States…" (Ctrl+Shift+S / Ctrl+Shift+L)

**Implemented controllers:** `LaserController`, `SettingsController`, `ScanControllerBase`

### N-dimensional ImageWidget Support
**Location:** `imswitch/imcontrol/view/widgets/ImageWidget.py`

Extended napari integration to support N-dimensional acquisitions:
- Automatic layer dimensionality detection from image shape
- Multi-channel layer support with proper channel axis handling
- Z-stack and time-series layer management
- Preserves backward compatibility with 2D live-view workflows

### TilingController
**Location:** `imswitch/imcontrol/controller/controllers/TilingController.py`

Multi-position tiled acquisition manager:
- Grid-based position list generation
- Integration with positioner and recording managers
- Automated stage movement and acquisition sequencing

### FLIMHistWidget / FLIMHistController
**Location:** `imswitch/imcontrol/view/widgets/FLIMHistWidget.py`

Swabian Time Tagger FLIM histogram display:
- Real-time FLIM histogram plotting
- Integration with Time Tagger hardware
- Lifetime analysis tools

---

## Hardware Library Dependencies

| Library | Used By | Protocol |
|---|---|---|
| `nidaqmx` | NidaqManager (direct) | PCIe/USB DAQ |
| `pulsestreamer` | PulseStreamerManager | Swabian digital output |
| `TimeTagger` | SwabianTimeTaggerManager | FLIM/TCSPC |
| `pyserial` | PyCoboltManager, SQUID, GRBL, various RS232 | Serial/USB |
| `thorlabs_apt_device` | BSC203StageManager | Thorlabs servo |
| `uc2rest` | ESP32Manager | UC2 REST API |
| `lantz` | LantzLaserManager, RS232Driver | Instrument framework |
| `ctypes` | Hamamatsu SLM, SmarACT, TIS, Hamamatsu camera | Vendor C DLLs |
| `pyvcam` | PhotometricsManager | Photometrics SDK |
| `pypylon` | BaslerManager (via interface) | Basler SDK |
| `gxipy` | GXPIPYManager (via interface) | Daheng SDK |
| `h5py` | RecordingManager | HDF5 storage |
| `zarr` | RecordingManager | Zarr storage |
| `tifffile` | RecordingManager | TIFF storage |

---

## Configuration System

```
~/ImSwitchConfig/
  ├── config/
  │   ├── modules.json              # which modules to load
  │   └── imcontrol_options.json    # recording folder, setup filename
  └── imcontrol_setups/
      └── <setup_name>.json         # full hardware config (SetupInfo)
```

`SetupInfo` is a frozen dataclass tree with `dataclasses_json` serialization. It defines all hardware devices (detectors, lasers, positioners, RS232, SLMs), scan parameters, and optional subsystems (focus lock, etSTED, Pyro server).

---

## Known Issues

**Last updated:** 2026-05-17

Most issues identified in the initial codebase analysis have been resolved (see ROADMAP.md Milestone 3). Remaining open items:

### Interface/Mock Separation
`imswitch/imcontrol/model/interfaces/` mixes real hardware drivers (`hamamatsu.py`, `squid.py`, `lantzlasers.py`) with mock implementations (`hamamatsu_mock.py`, `RS232Driver_mock.py`, `piezoPiezoconceptZ_mock.py`) in the same directory with no clear organizational pattern.

**Proposed fix:** Separate into `interfaces/drivers/` and `interfaces/mocks/` subdirectories (ROADMAP Milestone 3).

### Resolved Issues (2026-05 ImSwitch2 Work)
- ✅ **EtSTEDInfo duplication** — Fixed by renaming first definition to `EtSTEDDeviceInfo` (Milestone 3)
- ✅ **SQUIDLaserManager naming** — File renamed to `ESP32LEDLaserManager.py` to match class (Milestone 3)
- ✅ **Hard-pinned dependencies** — All version pins relaxed in `setup.cfg` (Milestone 2)
- ✅ **Legacy SLM dualism** — Old `SLMController`/`slmManager` removed from codebase (Milestone 3)
- ✅ **Test file location** — `__test_Manager.py` moved to `_test/` directory (Milestone 3)
