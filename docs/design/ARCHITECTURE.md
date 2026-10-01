# ImSwitch2 Architecture — Dependency Map

## Overview

ImSwitch follows a strict **Model-View-Presenter (MVP)** architecture organized into four independent modules, each loadable as a plugin:

| Module | Purpose | Package | Standalone Launch |
|---|---|---|---|
| **imcontrol** | Hardware control (lasers, stages, DAQ, detectors) | `imswitch.imcontrol` | ❌ (requires hardware config) |
| **improcess** | Image processing and reconstruction | `imswitch.improcess` | ✅ `python -m imswitch.improcess` |
| **imscripting** | Python scripting console & editor | `imswitch.imscripting` | ❌ |
| **imcommon** | Shared framework, signals, utilities | `imswitch.imcommon` | N/A (library) |

Each module implements `getMainViewAndController()` and sets `__imswitch_module__ = True` for dynamic discovery. The top-level `__main__.py` orchestrates multi-module loading via `MultiModuleWindow`. **ImProcess** can also run standalone for offline processing of recorded data.

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
| `pulseGeneratorManager` | `TeensyPulseManager` (or other `PulseGeneratorManager` subclass) | Conditional (if `setupInfo.teensyPulse`) — see [Pulse Generator Subsystem](#pulse-generator-subsystem) |
| `detectorsManager` | `DetectorsManager` | Yes |
| `lasersManager` | `LasersManager` | Yes |
| `positionersManager` | `PositionersManager` | Yes |
| `rotatorsManager` | `RotatorsManager` | Yes |
| `recordingManager` | `RecordingManager` | Yes |
| `slmsManager` | `SLMsManager` | Yes |
| `standManager` | `StandManager` | Conditional (if `setupInfo.microscopeStand`) |
| `scanManager` | `ScanManagerBase/PointScan/MoNaLISA/Advanced` | Conditional (by `scan.scanWidgetType`) |

Singleton "low-level" managers (NI-DAQ, RS232s, PulseGenerator) are injected into per-device managers via the `lowLevelManagers` keyword-argument dict. Per-device managers look up the dependency by key:

```python
class PulseGeneratorLaserManager(LaserManager):
    def __init__(self, laserInfo, name, **lowLevelManagers):
        self._pulseGen = lowLevelManagers.get('pulseGeneratorManager')
        ...
```

Missing dependencies (`None` or absent key) trigger a documented mock-mode fallback rather than a hard error — see e.g. `PulseGeneratorLaserManager` for the canonical pattern.

### Detector Managers

| Manager | Hardware | Interface |
|---|---|---|
| `APDManager` | Avalanche Photodiode (NI-DAQ counter) | Via injected `nidaqManager` |
| `PMTManager` | Photomultiplier Tube (NI-DAQ analog) | Via injected `nidaqManager` |
| `HamamatsuManager` | Hamamatsu sCMOS (DCAM API) | `ctypes` DLL via bundled interface |
| `ThorCamTSIManager` | Thorlabs scientific cameras (Zelux/Kiralux/Quantalux) | `thorlabs_tsi_sdk` (lazy) + bundled DLL bootstrap; in-process mock fallback |
| `PhotometricsManager` | Photometrics sCMOS/CCD | `pyvcam` (lazy) |
| `TISManager` | The Imaging Source cameras | `ctypes`/`tisgrabber` via bundled interface |
| `SwabianTimeTaggerManager` | Swabian Time Tagger (FLIM) | `TimeTagger` (lazy) |
| `AVManager` | Synthetic test camera (no video driver bundled) | `MockCameraTIS` |
| `PiCamManager` | Raspberry Pi Camera | `picamera` (lazy) |

### Laser Managers

| Manager | Hardware | Interface |
|---|---|---|
| `NidaqLaserManager` | NI-DAQ analog output | Via injected `nidaqManager` |
| `Cobolt0601LaserManager` | Cobolt 06-01 (Lantz) | `lantz` framework |
| `Cobolt0601NewLaserManager` | Cobolt 06-01 (direct serial) | Vendored Cobolt driver → `pyserial` |
| `CoboltLaserManager` | Generic Cobolt lasers | Vendored Cobolt driver |
| `LantzLaserManager` | Any Lantz-compatible laser | `lantz.messagebased` |
| `PyCoboltManager` *(not a LaserManager — driver module)* | Vendored pyserial driver consumed by `Cobolt0601NewLaserManager` | — |
| `AAAOTFLaserManager` | AAA AOTF | Via injected low-level manager |
| `MPBLaserManager` | MPB Communications fiber lasers | RS-232 |
| `CoolLEDLaserManager` | CoolLED illumination | Via injected low-level manager |
| `PulseStreamerLaserManager` | Swabian Pulse Streamer (legacy, single-backend) | Via injected `PulseStreamerManager` |
| `PulseGeneratorLaserManager` | **Backend-agnostic** — drives any `PulseGeneratorManager` (Teensy, PulseStreamer once migrated, future NI/FPGA backends) | Via injected `pulseGeneratorManager` |
| `PyMicroscopeLaserManager` | python-microscopy compatible | `importlib` dynamic load |
| `ESP32LEDLaserManager` | SQUID/ESP32 LED | Via injected `SQUIDManager` |
| `LEDMatrixManager` | LED matrix control | Generic GPIO/serial |

### Positioner Managers

| Manager | Hardware | Interface |
|---|---|---|
| `NidaqPositionerManager` | NI-DAQ analog output (piezo/galvo) | Via injected `nidaqManager` |
| `PIStageManager` | Physik Instrumente stages | Bundled `pipython` (GCS2) |
| `BSC203StageManager` | Thorlabs BSC203 servo controller | `thorlabs_apt_device` |
| `KinesisStageManager` | Thorlabs MLS203 XY (Kinesis) | `pylablib.devices.Thorlabs.KinesisMotor` (lazy) + in-process mock fallback; implements jog API |
| `PiezoconceptZManager` | Piezoconcept Z-axis | Via RS232 manager |
| `PiezoconceptZManager2` | Piezoconcept Z-axis (alternate) | Via RS232 manager |
| `JenaPiezoZManager` | Jena piezo Z-stage | Via RS232 manager; closed-loop settle polling with retry/timeout |
| `LeicaDMIManager` | Leica DMI stand | Via RS232 manager |
| `MHXYStageManager` | Märzhäuser SCAN XY | Via RS232 manager |
| `SQUIDStageManager` | SQUID open-hardware stage | Via injected `SQUIDManager` |
| `SmarACTPositionerManager` | SmarACT piezo-motor stages | `ctypes` DLL |
| `MockPositionerManager` | Software mock | None |

Continuous-motion support is added per-backend via `jog_start(axis, sign)` / `jog_stop(axis)` methods on the concrete manager (currently only `KinesisStageManager` defines them). The abstract `PositionerManager` does **not** declare these — consumers must feature-detect via `hasattr` before calling. Adding default no-op stubs to the base is a planned cleanup so consumers can call uniformly without exception handling.

### Rotator Managers

| Manager | Hardware | Interface |
|---|---|---|
| `StandaRotatorManager` | Standa rotation stages | Vendor SDK (lazy import) |
| `KinesisRotatorManager` | Thorlabs K10CR1 (Kinesis) | `pylablib.devices.Thorlabs.KinesisMotor` (lazy) + in-process mock fallback |
| `ElliptecRotatorManager` | Thorlabs ELL14/ELL14K (Elliptec multidrop) | `pylablib.devices.Thorlabs.ElliptecMotor` via a refcounted per-COM-port shared-bus singleton (multiple rotators on one bus) |

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
| `RecordingManager` | Data recording to HDF5/TIFF/Zarr (`h5py`, `tifffile`, `zarr`) |
| `SLMManager` | Single SLM pattern generation (NumPy/PIL/SciPy) |
| `ScanManagerBase/Advanced/MoNaLISA/PointScan` | Scan waveform orchestration via `SignalDesignerFactory` |

### Pulse Generator Subsystem

**Location:** `imswitch/imcontrol/model/managers/pulsegen/`

Abstract base + per-backend implementations for digital pulse generators. Consumers (e.g. `PulseGeneratorLaserManager`, snap actions, scan controllers) talk to the ABC and don't care which backend is wired:

```
PulseGeneratorManager (ABC)
   │
   ├── PulseStreamerManager   — Swabian Pulse Streamer 8/2 (ns jitter, hw trigger, analog)
   └── TeensyPulseManager     — Teensy / Arduino on serial (~µs jitter, digital-only)
                                 backed by TeensyPulseDriver or MockTeensyPulseDriver
```

The ABC defines explicit **capability properties** so consumers can introspect what they're getting rather than assuming all backends are equivalent:

| Property | Meaning |
|---|---|
| `jitter_ns` | Honest worst-case transition jitter (PulseStreamer: 1, Teensy: 1000) |
| `min_pulse_width_ns` | Shortest step duration the backend can resolve |
| `n_digital_channels` | Logical channel count (0..n-1) |
| `supports_hw_trigger_in` | Whether `run()` can wait for an external trigger |
| `supports_analog` | Whether `setAnalog()` works |
| `connected` | True if real hardware is responding (False = in mock fallback) |

Core API surface (full contract in [`PulseGeneratorManager.py`](../../imswitch/imcontrol/model/managers/pulsegen/PulseGeneratorManager.py)):

| Method | Purpose |
|---|---|
| `setDigital(channel, enable)` | Constant pin level |
| `setAnalog(channel, voltage)` | Constant analog output (raises if unsupported) |
| `program_sequence(list[PulseStep])` | Upload an arbitrary timeline (per-step full channel state) |
| `run(n_reps, blocking)` | Execute the uploaded sequence; emits `sigSequenceStarted` / `sigSequenceDone` |
| `stop()` | Abort, then idle |
| `snap(channels, width_ns)` | One-shot HIGH→LOW pulse (ABC default = 2-step program+run; backends may override for lower latency) |

#### Teensy backend

The Teensy stack has three layers:

1. **Firmware** ([`teensy/arduino_code_teensy4p1_v4.txt`](../../../WidefieldStarss/teensy/arduino_code_teensy4p1_v4.txt) in the WidefieldStarss repo) — implements v4 wire protocol with v3 backwards compat.
2. **`TeensyPulseDriver`** (`interfaces/teensypulse.py`) — line-based ASCII over `pyserial`; auto-detects v3/v4 via `*IDN?` handshake. Has a paired **`MockTeensyPulseDriver`** with identical public API that simulates pin state + a `timeline` of virtual-time transitions for hardware-free tests.
3. **`TeensyPulseManager`** — bridges the driver to `PulseGeneratorManager`. Falls back to the mock when the port can't be opened (configurable).

The v4 wire protocol adds:
- `*IDN?` → `IMSWITCH_TEENSY,<ver>,<nch>,<min_us>,<max_steps>` capability handshake.
- `PIN,<n>,<0|1>` — set pin.
- `SEQ,<n_steps>,<n_reps>` + step lines (`<dur_us>,<bitmask_hex>`) — upload buffered sequence.
- `START` → `STARTED` ack, then `DONE` / `STOPPED` on completion.
- `STOP` — abort (checked between steps; latency = one step duration, chunked at 16 ms for long delays).

The legacy v3 commands (`Parameters`, `Snap`, `PinHigh`, `PinLow`) keep working unchanged. The driver auto-falls-back to v3 mode if the IDN handshake times out.

Hardware-free testability is enforced at every layer: `MockTeensyPulseDriver` mirrors the real driver's public API; a wire-protocol cross-check (`_FakeSerial` + `_V4FirmwareSim` in `test_teensypulse_driver.py`) lets the real `TeensyPulseDriver` be exercised against a simulated firmware to catch wire-format bugs the mock can't.

See [ws-integration.md](plans/ws-integration.md) for the integration plan and design rationale.

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

`CommunicationChannel` remains the compatibility signal bus for existing
controllers and external API consumers. Signal names and signatures are
protected by a contract-test snapshot.

Key signals (selected): `sigUpdateImage`, `sigAcquisitionStarted/Stopped`,
`sigDetectorSwitched`, `sigRunScan`,
`sigScanStarting/Built/Started/Done/Ended`, `sigRecordingStarted/Ended`,
`sigSnapImg`, `sigSLMMaskUpdated`, `sigSetXYPosition`, `sigSetZPosition`.

New code should prefer the narrower workflow/event surfaces exposed by the
channel:

- `scanWorkflow`: scan parameter requests, scan-start notifications,
  recording-triggered scan coordination, axis-center updates.
- `beadRecWorkflow`: MoNaLISA/bead-recognition center-query and axial-list
  coordination.
- Read-only event groups such as `scanEvents`, `recordingEvents`,
  `eventTriggeredEvents`, and `beadRecEvents` provide domain aliases for the
  legacy `sigX` attributes.

### API Exposure

`@APIExport` decorators on controller methods are harvested by `generateAPI()` and exposed via dual protocols:
- **FastAPI** (HTTP REST) at `/{module}/{function_name}`
- **Pyro5** (RPC) with msgpack serialization

Large arrays (images) are passed via POSIX shared memory through `SerNDArray` to avoid copies.

---

## View Layer — Widgets

50 widget files in `imcontrol/view/widgets/`, all inheriting from `Widget` or `NapariHybridWidget`. Key widgets include:

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
| `EtSTEDWidget`, `EtMonalisaWidget`, `EtSnoutyWidget` | STED/MoNaLISA/Snouty microscopy |
| `SLMWidget`, `SLMsWidget` | SLM pattern control |
| `RotationScanWidget`, `RotatorWidget` | Rotation stage control |
| `ULensesWidget` | Microlens array analysis |
| `BFTimelapseWidget` | Brightfield timelapse |
| `LeicaStandWidget` | Leica stand control |
| `ConsoleWidget`, `WatcherWidget` | Scripting and monitoring |
| `ViewWidget` | Additional viewer controls |
| `AlignmentLineWidget` | Line alignment overlay |
| `SetupModesWidget`, `SetupStatusWidget` | Smart microscopy mode switching, setup status |
| `WellPlateWidget` | Multi-well plate scanning |
| `FlipMirrorWidget` | Flip mirror control |
| `BSC203Widget` | Thorlabs BSC203 servo controller |
| `SegmentationParamsWidget` | Segmentation parameter tuning |
| `LightSheetMulticolorWidget` | Lightsheet multicolor control |
| `TriggerScope*Widgets` | Family of 6 TriggerScope-synchronized lightsheet scan widgets (GalvoDetection, LSXYR, PLSR, PLSRMulticolor, Raster, Scan) |

Key architecture patterns:
- `pyqtgraph.dockarea.DockArea` for flexible panel layout
- `pyqtgraph.ParameterTree` for structured settings
- `EmbeddedNapari` for live image display
- Qt Signals following `sigVerbNoun` / `sigNounChanged` convention

---

## Device Plugin Architecture

Device support can live in external pip-installable packages (napari-style), so setups can name managers that ship outside the core repo. Design: [DEVICE_PLUGINS.md](DEVICE_PLUGINS.md); user guide: `docs/devices/plugins.rst`.

| Piece | Location | Role |
|---|---|---|
| Public authoring API | `imswitch/pluginapi/` | The stable surface plugin authors import: device info dataclasses + abstract managers (`DetectorManager`, `LaserManager`, `PositionerManager`, `RotatorManager`, parameter/action types) |
| Manifest model | `imcontrol/model/plugins/manifest.py` | JSON manifest parsing → `DeviceManagerContribution` (kind, manager class path, optional managerProperties JSON-Schema) |
| Discovery | `imcontrol/model/plugins/discovery.py` | Finds plugins via `imswitch.manifest` entry points |
| Registry | `imcontrol/model/plugins/registry.py` | `DevicePluginRegistry` — merges an explicit built-in table (`builtins.py`) with discovered external contributions (`external.py`) |
| Validation | `imcontrol/model/plugins/validation.py` | Best-effort managerProperties JSON-Schema validation |
| Diagnostics CLI | `python -m imswitch.imcontrol.model.plugins list \| inspect \| validate-setup` | Inspect what is registered and validate a setup file against it |

**Resolution order:** `MultiManager` resolves a setup's `managerName` through the registry first, then the legacy internal import path — existing setups boot unchanged. An unresolved registry-backed kind raises an actionable diagnostic; known-extracted manager names map to a "pip install <package>" hint. In-tree example plugins: `examples/plugins/imswitch-zhinst-devices` (new device) and `examples/plugins/imswitch-device-thorlabs` (first Phase-8 extraction).

---

## Recording Data Path

`RecordingManager` (`imcontrol/model/managers/RecordingManager.py`) owns the acquisition→disk pipeline. See [recording_dataflow_plan.md](../recording_dataflow_plan.md) for the full design.

```
DetectorsManager frames
   → RecordingManager (dtype contract: DetectorManager.dtype/bitDepth is authoritative;
                       storers warn loudly on mismatch instead of silently casting)
   → WriterThread (off-acquisition-thread disk I/O + compression,
                   bounded backpressure queue, batched multi-frame chunks)
   → Storer (ABC) ── ZarrStorer | HDF5Storer | TiffStorer
```

- **Abort semantics:** `abortRecording()` stops the recording and discards the partial output (`Storer.abortStream` deletes the file/store).
- **Live-consumer barrier:** streaming storers publish a `recording:frames_committed` attribute *after* each data flush, so mid-recording readers (the ImProcess live pipeline below) never read unwritten chunks; a stream-complete marker signals finalization.
- **In progress:** OME standardization of all three formats (OME-TIFF, OME-NGFF 0.5, HDF5 + OME-XML) via a shared `OmeImageMeta` — see `docs/recording_ome_standardization_plan.md`.

---

## ImProcess Module Architecture

The post-processing module (Milestone 12) is a plugin shell around two contracts defined in `improcess/reconstructors/base.py` and registered in a `PluginRegistry` (`reconstructors/registry.py`, populated from the setup's `processing:` block or standalone defaults):

| Contract | Role | Built-ins |
|---|---|---|
| `Reconstructor` | raw `DataObj` → `ProcessingResult` (one per dataset) | `monalisa`, `view-only`, `snouty`, `snouty-projections`, `widefield-starss`, `smlm-localizer` |
| `Processor` | `ProcessingResult` → new result(s), stackable | `drift_correct`, `frc`, `projection`, `segmentation`, `psf_resolution`, `colocalization`, `denoise`, `smlm_render`, `channel_merge`/`channel_split`, `make_composite`/`make_rgb`, `multicolor_*`, `stack_split`/`stack_subset` |

**Result types** (`improcess/model/`): the image/array-centric `ProcessingResult` family (`array_result.py`, per-modality subclasses) plus the table-backed `LocalizationResult` (`localization_result.py` / `localization_schema.py`) whose payload is an SMLM coordinate recarray with a lazy histogram preview as viewable data.

**Controller decomposition:** `ImProcessMainViewController` is a thin coordinator owning sub-controllers (`DataFrameController`, `ReconstructionViewController`, `ReconstructorManagerController`, `MoNaLISAController`, `FileIOController`, `WatcherFrameController`, `LiveModeController`, `MemoryLiveController`, `WidefieldStarssBatchController`, …), wired over the ImProcess `CommunicationChannel`.

**View split & source of truth:** the raw-data pane (`DataFrame`, pyqtgraph; hosts the MoNaLISA pattern overlay and the SMLM detection-preview scatter) is separate from the napari-based `ReconstructionView`, whose results list is the registry of produced results; selecting a result re-targets, renames and re-activates the main napari layer, so tool panels always operate on the selected result (contract documented in `docs/improcess.rst`, "Results list vs. napari layers").

---

## Live Reconstruction Pipeline

Milestone 10's streaming path lets any registered reconstructor consume frames while a recording is still being written (`improcess/live/`):

```
imcontrol recording (Zarr/HDF5 + recording:frames_committed barrier)
   → LiveSource (ABC, sources.py): ZarrLiveSource | ZarrLapseSource |
     ZarrMultiFileLapseSource | Hdf5LiveSource | Hdf5MultiFileLapseSource
     (selected by make_live_source(), source_factory.py)
   → LiveStreamWorker / LiveProcessWorker (workers.py, Qt worker objects)
   → StreamingReconstructor.make_session() → StreamingSession
     (reconstructors/base.py) consumes chunks incrementally
   → sigLiveResultUpdated → ReconstructionViewController updates the viewer
```

The first streaming consumer is the MoNaLISA fast-Gauss path (`reconstructors/monalisa/live_session.py`). Completion is gated on the writer's stream-complete marker, with progressive updates for timelapse (`scan{N}`) stores. Remaining known gap: a crashed-writer stall fallback (tracked in ROADMAP M10).

---

## Workflow Scripting Layer

Headless, testable acquisition workflows live in `imcontrol/model/workflows/` and talk to a narrow facade instead of managers/controllers:

- **Facades** (`facade.py`): `MicroscopeFacade` (+ `build_facade_from_master`), `ScanWorkflowFacade.run_once()` for triggering the configured scan, `TimeResolvedDetectorFacade` wrapping the time-resolved detector contract (`imcontrol/model/timeresolved/`); `MockMicroscopeFacade` (`mock_facade.py`) for hardware-free tests.
- **Workflows:** WidefieldSTARSS family (`WidefieldStarssWorkflow`, `ZStackWorkflow`, `CWSTARSSWorkflow`, `CalibrationWorkflow`, composites `DefocusScan`/`SerialCWSTARSS`/`MultiWellTiling`), time-resolved workflows (`time_resolved.py`: binned photon-arrival cubes, gated STED, tau-STED), plus event-probe and target-timelapse workflows.
- **Entry point:** `api.imcontrol.buildWorkflowFacade(...)` (exposed by `WorkflowFacadeController`).

Cookbooks: `docs/scripting-wfs-workflows.rst` (general pattern, WFS worked example) and `docs/scripting-time-resolved-workflows.rst` (detector contract + TimeTagger worked example).

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
- Stitched-overview cell detection with passive GUI marker overlay
- Explicit automated cell-target iteration for API/workflow callbacks

### FLIMHistWidget / FLIMHistController
**Location:** `imswitch/imcontrol/view/widgets/FLIMHistWidget.py`

Swabian Time Tagger FLIM histogram display:
- Real-time FLIM histogram plotting
- Integration with Time Tagger hardware
- Lifetime analysis tools

### WidefieldStarss Hardware Ports (2026-05-23)

Five device managers ported from the WidefieldStarss project — see [ws-integration.md](plans/ws-integration.md):

- **`KinesisRotatorManager`** — Thorlabs K10CR1.
- **`ElliptecRotatorManager`** — Thorlabs ELL14/ELL14K, with a refcounted shared-bus singleton so multiple addresses on the same COM port can coexist.
- **`JenaPiezoZManager`** — Jena piezo Z-stage with closed-loop settle polling.
- **`KinesisStageManager`** — Thorlabs MLS203 XY; first manager to expose `jog_start`/`jog_stop` (the abstract base does not declare these; consumers feature-detect).
- **`ThorCamTSIManager`** — Thorlabs scientific cameras (Zelux/Kiralux/Quantalux) via `thorlabs_tsi_sdk`, replacing the prior orphaned `ThorcamManager.py` stub.

All five follow the pattern: optional `pylablib`/vendor-SDK import, in-process mock fallback, no application policy in the driver.

### Pulse Generator Abstraction + Teensy Backend (2026-05-23/24)

A new abstraction layer for digital pulse generators — see the [Pulse Generator Subsystem](#pulse-generator-subsystem) section above for the design and [ws-integration.md](plans/ws-integration.md) for the phase-by-phase plan.

Components added across five phases:

| Phase | Deliverable |
|---|---|
| 1 | `PulseGeneratorManager` ABC + `PulseStep` dataclass; `PulseStreamerManager` refactored to inherit |
| 2 | `TeensyPulseDriver` (real, `pyserial`) + `MockTeensyPulseDriver` (in-process, with virtual-time `timeline` for tests) |
| 3 | `TeensyPulseManager` — bridges driver to ABC; non-blocking-run worker thread emits Qt signals on completion |
| 4 | Teensy firmware v4 sketch + hardware smoke-test checklist (`teensy/arduino_code_teensy4p1_v4.txt`, `teensy/v4_smoke_test_checklist.md`) |
| 5 | `TeensyPulseInfo` lifted into `SetupInfo`; `MasterController` constructs the manager when configured; new `PulseGeneratorLaserManager` consumes it as the first real consumer |

The architecture is fully verified against the in-process mock (75 tests across four files, plus a wire-protocol cross-check using a fake serial transport against a simulated firmware). Real-hardware verification awaits a user walk-through of the smoke-test checklist.

---

## Hardware Library Dependencies

| Library | Used By | Protocol |
|---|---|---|
| `nidaqmx` | NidaqManager (direct) | PCIe/USB DAQ |
| `pulsestreamer` | PulseStreamerManager | Swabian digital output |
| `TimeTagger` | SwabianTimeTaggerManager | FLIM/TCSPC |
| `pyserial` | PyCoboltManager, SQUID, GRBL, JenaPiezoZ, **TeensyPulseDriver**, various RS232 | Serial/USB |
| `pylablib` | KinesisRotatorManager, ElliptecRotatorManager, KinesisStageManager | Thorlabs Kinesis / Elliptec |
| `thorlabs_apt_device` | BSC203StageManager | Thorlabs servo (legacy APT) |
| `thorlabs_tsi_sdk` | ThorCamTSIManager | Thorlabs scientific cameras |
| `uc2rest` | ESP32Manager | UC2 REST API |
| `lantz` | LantzLaserManager, RS232Driver | Instrument framework |
| `ctypes` | Hamamatsu SLM, SmarACT, TIS, Hamamatsu camera | Vendor C DLLs |
| `pyvcam` | PhotometricsManager | Photometrics SDK |
| `h5py` | RecordingManager | HDF5 storage |
| `zarr` | RecordingManager | Zarr storage |
| `tifffile` | RecordingManager | TIFF storage |

All third-party hardware libraries are imported lazily (try/except at module level) so missing SDKs degrade to documented mock-mode fallbacks rather than blocking application startup.

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

**Last updated:** 2026-05-24

Most issues identified in the initial codebase analysis have been resolved (see ROADMAP.md Milestone 3). Remaining open items:

### Interface/Mock Separation
`imswitch/imcontrol/model/interfaces/` mixes real hardware drivers (`hamamatsu.py`, `squid.py`, `lantzlasers.py`) with mock implementations (`hamamatsu_mock.py`, `RS232Driver_mock.py`, `tiscamera_mock.py`) in the same directory with no clear organizational pattern.

**Proposed fix:** Separate into `interfaces/drivers/` and `interfaces/mocks/` subdirectories (ROADMAP Milestone 3).

### Resolved Issues (2026-05 ImSwitch2 Work)
- ✅ **EtSTEDInfo duplication** — Fixed by renaming first definition to `EtSTEDDeviceInfo` (Milestone 3)
- ✅ **SQUIDLaserManager naming** — File renamed to `ESP32LEDLaserManager.py` to match class (Milestone 3)
- ✅ **Hard-pinned dependencies** — All version pins relaxed in `setup.cfg` (Milestone 2)
- ✅ **Legacy SLM dualism** — Old `SLMController`/`slmManager` removed from codebase (Milestone 3)
- ✅ **Test file location** — `__test_Manager.py` moved to `_test/` directory (Milestone 3)
- ✅ **Orphaned `ThorcamManager.py`** — Unused duplicate of `AVManager` deleted; replaced by genuine `ThorCamTSIManager` (2026-05-23)
- ✅ **`PulseStreamerManager` vendor-lock-in** — Refactored under a `PulseGeneratorManager` ABC so consumers don't bind to a specific backend (2026-05-23/24)
- ✅ **No abort path for in-flight pulse sequences** — Added via v4 Teensy firmware's STOP command, with a fire-and-forget driver design that survives concurrent stop+wait_done from different threads (2026-05-24)
