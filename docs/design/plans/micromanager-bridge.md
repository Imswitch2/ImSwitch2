# Micro-Manager device bridge

Status: Active — P0, P1 and P2 IMPLEMENTED 2026-10-24 on `feat/micromanager-bridge`
(616 plugin tests on the real MMCore with the demo adapters, plus N-position
flip mirrors and the config-editor import/label picker; see §5 and §10);
P3 rig and P4 publish open. Design text below is revision 3 (two independent
reviews the same day); implementation deviations are recorded in §10.
Documentation: `docs/devices/micromanager.rst` (user reference, quick start,
troubleshooting), changelog entries, the plugin README; polished f029fd1ab.
Branch: `feat/micromanager-bridge` (worktree `../Imswitch2-micromanager`), one
branch for the whole effort.

## 1. Goal

Make every Micro-Manager device adapter usable from an ImSwitch2 setup file
without touching the ImSwitch2 core: a camera, stage, shutter or two-position
switch that Micro-Manager can drive becomes an ordinary ImSwitch2 detector,
positioner, laser or flip mirror. The bridge ships as **one device plugin**,
`imswitch-device-micromanager`, built on `pymmcore-plus` exactly the way the
Thorlabs and TIS plugins are built on their vendor SDKs.

"Seamless" means three things, in priority order:

1. A Micro-Manager user keeps their existing hardware configuration file
   (`*.cfg`) and points ImSwitch2 at it. Hubs, peripherals, pre-init
   properties and config groups load as Micro-Manager loads them.
2. A converter turns that `.cfg` into an ImSwitch2 setup-file skeleton, so the
   user does not hand-write one device entry per Micro-Manager device.
3. Mock and CI runs go through the **real** MMCore with the **real**
   `DemoCamera` adapters (pip-installable, see §3), not through a hand-written
   fake core. The mock that does not match hardware was the lesson of the TIS
   plugin; here the mock *is* the hardware path minus the hardware.

Out of scope for this plan: driving ImSwitch2 hardware *from* Micro-Manager
(§9 lists it as a stretch), `useq-schema` as an acquisition format, colour
cameras (§4.4), multi-position State devices such as filter wheels, turrets
and light paths (§4.4: ImSwitch2 has no selector kind yet), and any change to
how ImSwitch2 scans (NI-DAQ / TriggerScope
waveforms stay native).

## 2. What was decided before this document

- 2026-10-03, comparison essay for Lenny's meeting with the Micro-Manager
  group: the two projects are "neighbours in one stack, not rivals". Four
  proposals, the first being *an MMCore device bridge: an ImSwitch2 device
  plugin backed by pymmcore-plus*. Tone rules: say "presented as a camera",
  never "posing"; avoid "neglect"/"rigid".
- 2026-10-10, open-source survey: pymmcore-plus is "absolutely yes", shape =
  one ImSwitch2 device plugin wrapping pymmcore-plus, **but only after the
  Micro-Manager meetings**. Lenny started this effort on 2026-10-24, so the
  gate is open; any commitments made in those meetings should be folded into
  §9 (see Open questions).
- 2026-10-18/19: the smart-microscopy review sessions treated the bridge as
  "done elsewhere" and did not depend on it. The essay memory gained the Liu
  and Tsuchida chapters (OpenScan, pymmcore-openscan, data-driven MDA,
  libtcspc).
- Upstream ImSwitch had a prototype in 2023–24 (`upstream/pymmcore-llm`,
  Jacopo Abramo): `PyMMCoreManager` low-level manager + camera / positioner /
  laser managers, loading devices one by one from `module` + `device`
  manager properties, exposure as a named property, stage "single"/"double".
  It never shipped. What it got right: a shared core object, properties
  mapped to `DetectorListParameter`/`DetectorNumberParameter`, popping the
  circular buffer in `getLatestFrame`. What it got wrong, and this plan
  changes: it lived in the core tree (we have plugins now), it needed a new
  `lowLevelManagers` key wired into `MasterController` (§4.2 avoids that), it
  had no `.cfg` path, no mock, no tests, one camera only, and `getChunk`
  returned a single frame instead of draining the buffer.

## 3. Facts checked on 2026-10-24

Rows marked *probe* were verified in a throwaway venv with pymmcore-plus
0.18.1 / pymmcore 12.5.1.75.0 / mm-test-adapters 75.20261014.post1.

| Fact | Value | Consequence |
|---|---|---|
| pymmcore-plus | 0.18.1 (2026-04-02), BSD-3, `requires-python >= 3.11`, deps: pymmcore ≥ 11.10.0.74, useq-schema ≥ 0.9.2, psygnal, ome-types, ome-writers[tensorstore], typer, rich, platformdirs | Pulls ome-writers + tensorstore; acceptable for an optional plugin, not for the core |
| pymmcore | 12.5.1.75.0 (2026-10-15), LGPL-2.1, **Python ≥ 3.11**, wheels for win64 / linux x64 / macOS x64 **and arm64** | **The plugin cannot support Python 3.10.** ImSwitch2 CI runs 3.10 (`ci.yml`, `imswitch-test.yml`) and the installers bundle 3.10 (`imswitch-bundle.yml`) |
| Device interface version | 75 (the fourth component of the pymmcore version) | Adapters must be built for DIV 75; `mmcore install` picks the matching nightly |
| Micro-Manager nightlies | Windows x64 and **macOS Intel only**. No arm64, no Linux | Real adapters only on Windows (= the rigs) and Intel Macs |
| `mm-test-adapters` | PyPI 75.20261014.post1; DemoCamera, Utilities, SequenceTester, NotificationTester; wheels for Linux x64, macOS arm64/x64, Windows x64; `device_adapter_path()`. *Probe:* ships `MMConfig_demo.cfg` in that directory; `find_micromanager()` returns it when no full install exists; `loadSystemConfiguration` resolves a relative name against cwd, then the adapter path | **Hardware-free real-MMCore path on every platform incl. Apple Silicon and GitHub Actions**, demo config included |
| Demo config | *Probe:* pixel-size config `Res10x` = 1.0 µm; `autoShutter` on by default; devices Camera, Dichroic, Emission, Excitation, Objective, Z, Path, XY, White Light Shutter, Autofocus, LED, LED Shutter, DHub | The bundled setup template maps Camera, Z, XY, shutters and wheels straight from it |
| pymmcore-plus events | psygnal `CMMCoreSignaler` or Qt `QCoreSignaler`; `PYMM_SIGNALS_BACKEND` ∈ {`qt`, `psygnal`, `auto`}, default `auto` = **Qt whenever `QApplication.instance()` exists** (`_util.signals_backend`). Signals incl. `propertyChanged`, `stagePositionChanged`, `XYStagePositionChanged`, `sequenceAcquisitionStarted/Stopped`, `roiSet`, `exposureChanged` | Inside ImSwitch2 a QApplication always exists, so by default every MMCore callback would become a Qt signal on a QObject the PyQt5 atexit walker then has to handle. The hub sets `PYMM_SIGNALS_BACKEND=psygnal` before constructing the core and asserts the backend it got |
| `CMMCorePlus.instance()` | process-wide weak singleton; any other package in the process (napari-micromanager) may have created it first, with the Qt signaler | The hub constructs its **own** `CMMCorePlus()`; adopting the singleton is an explicit opt-in |
| Sequence acquisition | *Probe:* `startSequenceAcquisition(label, numImages, intervalMs, stopOnOverflow)` takes a C `long` — `float('inf')` raises `TypeError`, `2**31-1` is accepted; `startContinuousSequenceAcquisition` has no per-label overload | Use `startSequenceAcquisition(label, 2**31-1, 0, True)` (see Overrun) |
| Mixed frame sizes | *Probe:* two DemoCameras 512² + 256² both start **without error**; only the first camera's frames arrive, the second's are **dropped silently**. `setROI` is **accepted mid-sequence** (same effect on one camera) | The host must refuse the second start and must stop/restart around every ROI change |
| One buffer, cleared on start | *Probe:* `startSequenceAcquisition` on a second camera **discards every unread frame** in the buffer (10 + 10 frames → 10 remain; 5 unread + 3 new → 3). `clearCircularBuffer` flushes all cameras | The buffer has one owner (§4.5): the host drains it into per-camera queues before any start/stop/ROI call; no manager calls `clearCircularBuffer` while another streams |
| Buffer also cleared on `setROI`; counters reset | *P1a tests:* `setROI` on **any** camera, even an idle one, clears the whole buffer (a running camera's unread frames vanish), and every clear/start resets every camera's `ImageNumber` to 0 (A at 33 → 0 when B starts). `setExposure`, `Binning`, `PixelType`, CCD-size properties do not clear | The host drains before `setROI` as before a start, tracks `ImageNumber` per epoch (expected 0 after each start/ROI), and treats a counter going backwards as the buffer having been cleared behind its back (fault) |
| Pop shapes by the *current* camera | *P1a tests:* `popNextImageAndMD` wraps the slot with the width/height/pixel type of MMCore's **currently selected** camera, not the frame's own; a streaming camera that differs → read past the slot (**segfault** observed with a 16-bit and an 8-bit camera). MMCore refuses `setCameraDevice` while a sequence runs | The host selects the first streaming camera as current, restores the selection when the last stream stops, and `normalize_frame` cross-checks the array against the frame's own `Width`/`Height`/`PixelType` tags |
| Camera identity | *Probe:* `popNextTaggedImage()` **relabels** `tags["Camera"]` with the currently selected camera (5 Cam2 frames all tagged `Camera`); `popNextImageAndMD()` metadata keeps the true `Camera` | Demultiplex from `popNextImageAndMD` metadata only; a test checks identity, not just counts |
| Overrun | *Probe:* 1 MB buffer, 20 frames: `stopOnOverflow=False` keeps only frames 18–19 and `isBufferOverflowed()` is **False** (silent loss); `stopOnOverflow=True` stops the sequence, keeps 0–1, `isBufferOverflowed()` True, **no exception raised** | Always `stopOnOverflow=True`; the drain thread checks `isBufferOverflowed()` and `ImageNumber` continuity and turns loss into an acquisition error (§4.5) |
| Buffer size | *Probe:* default circular buffer 250 MB (MMCore default) | Kept as the source of the number; hub property `circularBufferMB` overrides it |
| Image metadata | *Probe, corrected by the P0 probe:* `popNextImageAndMD` metadata carries exactly `Binning`, `Camera`, `ElapsedTime-ms`, `Height`, `ImageNumber`, `PixelType`, `ROI-X-start`, `ROI-Y-start`, `TimeReceivedByCore` (wall-clock string, µs), `Width`. **No `BitDepth` and no `ROI` tag**; `PixelType` is `GRAY16` (not the property value `16bit`) | dtype from `PixelType`/bytes-per-pixel (`getBytesPerPixel`), map `GRAY8`/`GRAY16` by name; bit depth only from the camera property; ROI origin from `ROI-X-start`/`ROI-Y-start`; `ImageNumber` for gap detection |
| Adapter metadata without hardware | *Probe:* `getAvailableDevices(library)` + `getAvailableDeviceTypes(library)` classify every device in an adapter library (DemoCamera: DCam=Camera, DWheel/DStateDevice/DObjective/DLightPath=State, DStage=Stage, DXYStage=XYStage, DShutter=Shutter, DAutoFocus, D-DA=SignalIO, DGalvo, pumps, image processors, DHub) with **only `Core` loaded** | The converter classifies `.cfg` devices this way; it needs the adapters installed but initialises nothing |
| Homing | *Probe:* `home("XY")` works on DemoXYStage; `home("Z")` raises "Unsupported device command (11)". MMCore has no query for home support | Homing is a physical operation: never probed at startup; offered only when the setup says `homeable: true` |
| `fixImage` | only rewrites 4-component (BGRA) frames to `(h, w, 3)`; mono passes through | Mono-only in this plan; see §4.4 |
| UniMMCore | experimental; `loadPyDevice(label, obj)`; base classes `CameraDevice`, `StageDevice`, `XYStageDevice`, `ShutterDevice`, `StateDevice`, `SLMDevice`, `GenericDevice`, hub; Python and C++ devices coexist | Enables the reverse bridge (§9) later; not used in Phases 0–4 |
| MM multi-camera buffer | PR #936 ("proper support for multi-channel cameras", Tsuchida) still **open, draft**, last touched 2026-05-07 | The equal-size limit stays; re-check at P4 |
| Frame rate and drain latency (P0) | *P0 probe*, DemoCamera 512² 16-bit, `popNextImageAndMD` spin loop for 5 s, macOS, Python 3.12: **1 ms exposure: 628 fps (one camera); 615 + 614 fps (two cameras at once)**; **5 ms: 170 fps; 161 + 161 fps**. The rate is the camera's own (≈ exposure + 0.6–0.8 ms per frame), not the pop loop's: backlog never above 4 frames, no overflow on the 250 MB buffer. Age of a frame when popped: **median 0.2–0.4 ms, p95 ≤ 0.6 ms**, measured two ways that agree (`ElapsedTime-ms` against wall clock taken just before that camera's start call, and the `TimeReceivedByCore` stamp against wall clock at pop). `ImageNumber`: **0 gaps, strictly increasing, per camera in all 6 camera runs (11 748 frames)**. `startSequenceAcquisition` returns in ≤ 0.1 ms; first frame 1.7 ms (1 ms exposure) to 6 ms (5 ms) later; the very first start in a process took 62 ms. Run-to-run spread about 3 % | One pop thread keeps up with ≈ 1230 fps / ≈ 640 MB/s of 512² frames, so the single-drain-thread design of §4.5 has headroom on this adapter; the per-camera deque will not fill at these rates unless the consumer stalls. DemoCamera frames are cheap to produce; real cameras and larger frames re-measure at P3. `startSequenceAcquisition` is synchronous and cheap, so the host can call it under its lock |
| Slow consumer, `stopOnOverflow=True` (P0) | *P0 probe*, 1 MB buffer (= 2 frames of 512 KB), 1 ms exposure, consumer sleeps 50 ms per pop: the **sequence stopped itself before the first 50 ms poll**; from then on `isSequenceRunning(label)` is **False** and `isBufferOverflowed()` **True**; **2 frames delivered** (`ImageNumber` 0, 1: contiguous, **gap size 0**). **No exception from any call** (state queries, pop, and `stopSequenceAcquisition` after the self-stop all return normally); a pop on the emptied buffer raises `IndexError: Circular buffer is empty.` The frames already in the buffer stay poppable after the stop. The flag stays True after the stop and is **cleared by `clearCircularBuffer()` and by the next `startSequenceAcquisition`** (False right after a restart without clearing); restoring `setCircularBufferMemoryFootprint(250)` works | With `stopOnOverflow=True` MMCore never skips frames, it ends the sequence, so **`ImageNumber` continuity cannot detect overflow**; the only detectors are `isSequenceRunning(label)` turning False without a requested stop and `isBufferOverflowed()`. The drain thread must read both every cycle and **before** any start or clear that resets the flag, drain what is left, then record `frames_lost` / fault. Gap checking stays as defence in depth only. Per-camera attribution of the flag with two cameras: not measured (single-camera test); treat it as buffer-wide |
| Signal backend under Qt (P0) | *P0 probe*, `QApplication([])` created first: with `PYMM_SIGNALS_BACKEND=psygnal` the core's `events` is `CMMCoreSignaler` (`pymmcore_plus.core.events._psygnal`), assertion holds. Without the variable under a QApplication, `auto` picks **`QCoreSignaler`** (`..._qsignals`); without a QApplication it picks `CMMCoreSignaler` | Confirms §4.2: the variable must be set before the core is constructed, and the hub's assertion is the right guard. `auto` inside ImSwitch2 would always give the Qt signaler |
| Signal delivery and thread (P0) | *P0 probe*, slot on `stagePositionChanged`, `setPosition("Z", …)` + `waitForDevice`: **psygnal** fires once, ≈ 0 ms after `waitForDevice` returns, **without any event loop**, on a thread that is **neither the caller (tried from the main thread and from a worker) nor the Qt main thread**, i.e. a thread inside MMCore / the device. **Qt signaler**: 0 deliveries without an event loop (waited 1 s); 1 delivery on the main thread after `processEvents()`, whichever thread moved the stage. Payload `('Z', 25.0)` | Handlers connected through the hub run on a foreign thread: they may only update plain state under the host lock (matches §4.5, nothing from a signal touches Qt) and must not assume the calling thread. A Qt-signaler core would deliver nothing until the main loop spins. Only DemoStage was observed; other adapters may call back on the calling thread |
| Full `MasterController` close + exit (P1) | *e2e test*, macOS arm64, Python 3.12: `view.close()` finalises camera, Z, XY, then the hub; `is_connected` False, no drain thread; a child interpreter that boots, streams, records 10 frames and closes exits 0. Windows not measured | The shutdown order the plan relies on holds in the real app |
| Exit with a live core (P0) | *P0 probe*, subprocess with a live `QApplication`, demo config (2 cameras) loaded, one sequence running, then falling off the end of the script (core and app die in interpreter finalization): **exit code 0 in all 24 runs** (8 variants × 3): {plain, `imswitch.imcommon.applaunch` imported, imported + `disownQtObjects(app)`} × {`stopSequenceAcquisition()` + `unloadAllDevices()`, no unload} with psygnal, plus Qt-signaler core without unload (plain and with `disownQtObjects`). Importing `applaunch` installs no hook by itself: it only defines `disownQtObjects`, which `launchApp` calls after `shutdownModules`; the PyQt5 atexit walker is PyQt5's own. The child held only the QApplication, no widget tree | No crash from a psygnal-backed core, with or without unload, with or without the disown step; the Qt-signaler core did not crash either in this small case. **Not measured:** a full `MasterController` close with the real widget tree (needs the mock app: moved to the P1 end-to-end), and Windows. `finalize()` still unloads: that is for the hardware, not for the exit code |
| Pixel size on the demo config (P0) | *P0 probe*: after `loadSystemConfiguration`, `getPixelSizeUm()` = **1.0** (current config `Res10x`; the Startup preset selects `Nikon 10X S Fluor`). Configs: `Res10x` 1.0, `Res20x` 0.5, `Res40x` 0.25 µm. `setConfig("Objective","20X")` → 0.5 (`Res20x`); `"40X"` → 0.25; back to `"10X"` → 1.0. **`Camera` `Binning` = 2 at 10X → `getPixelSizeUm()` = 2.0** (MMCore multiplies by binning). Objective at state 5 (no pixel config) → **0.0** and `getCurrentPixelSizeConfig()` = `''` | The MM value follows objective and binning, so the fallback for `cameraPixelSizeUm` is a snapshot: read after hub load and after a binning or objective change, never cached for good, and **0.0 means "unknown", not a size**. Whether ImSwitch2's `cameraPixelSizeUm` is per unbinned or per effective pixel must be checked against `DetectorManager` before the fallback is wired (divide by binning if unbinned). The setup value still wins, as in §4.4 |
| Windows adapter DLL path (P0) | *P0 probe:* **not measured**: no Windows machine in this spike and the demo adapters have no external SDK DLLs | Stays a P3 rig row (`adapterPaths` alone with a vendor adapter whose DLLs live elsewhere) |
| Licences | MMCore LGPL-2.1 via pymmcore wheel; MMDevice BSD; adapters mixed (some wrap proprietary SDKs, binary-only); pymmcore-plus BSD-3; plugin GPL-3.0-or-later like the other bundled plugins | No conflict; the plugin links nothing statically |

## 4. Design

### 4.1 Package

```
examples/plugins/imswitch-device-micromanager/
  pyproject.toml            requires-python >=3.11; deps imswitch2>=0.1, pymmcore-plus>=0.18,<0.19
                            extras: test = [mm-test-adapters, pytest, jsonschema]
  src/imswitch_device_micromanager/
    imswitch.json           manifest (ids below), setup_templates listed so the editor offers them
    _core.py                MMCoreHost: owns the one CMMCorePlus, the lock, the frame-size guard
    _frames.py              normalize_frame(), dtype/shape from PixelType / bytes-per-pixel
    _properties.py          MM property -> DetectorParameter mapping
    core.py                 MicroManagerCoreManager (rs232 kind, the hub)
    camera.py               MicroManagerCameraManager (detector)
    stage.py                MicroManagerStageManager (positioner)
    light.py                MicroManagerLightManager (laser)
    flipmirror.py           MicroManagerFlipMirrorManager (flip_mirror)  [P2]
    convert.py              `python -m imswitch_device_micromanager convert x.cfg`
    schemas/*.schema.json   managerProperties schemas, one per contribution
    setup_templates/        demo_scope.json (MMConfig_demo.cfg: Camera, Z, XY, shutters, wheels)
  tests/                    run against mm-test-adapters, no hardware
```

Manifest ids (all under one namespace, **no aliases of any in-tree manager
name** — the TIS lesson: an alias silently re-routes existing setups; a
manifest test pins this):

| id | kind | class |
|---|---|---|
| `micromanager.core` | rs232 | `MicroManagerCoreManager` |
| `micromanager.camera` | detector | `MicroManagerCameraManager` |
| `micromanager.stage` | positioner | `MicroManagerStageManager` |
| `micromanager.light` | laser | `MicroManagerLightManager` |
| `micromanager.flip-mirror` | flip_mirror | `MicroManagerFlipMirrorManager` |

The plugin depends on `imswitch2>=0.1`, never on `ImSwitch` (the unrelated
upstream v1 on PyPI), and is verified **without** `--no-deps`. It lives under
`examples/plugins/` first (the Thorlabs/TIS precedent, verified discoverable
with no core change), with an `external.py` install hint, and moves to its own
`Imswitch2/imswitch-device-micromanager` repository in P4.

### 4.2 The shared core: a hub in the `rs232devices` slot

Every bridged device needs the same `CMMCorePlus`. The prototype solved this
with a new `lowLevelManagers['pymmcManager']` key, which means editing
`MasterController` — a core change for one plugin (TriggerScope still works
that way). ImSwitch2 already has a "shared communication resource referenced
by name" slot: `rs232devices`. The ESP32 stage and the AA AOTF laser fetch
their controller via `lowLevelManagers['rs232sManager'][props['rs232device']]`,
the `rs232` kind is registry-backed, and three core services already key on
that property name:

- the config editor tracks renames/deletes through
  `managerProperties.rs232device` (`view/configeditor/editor.py`),
- the device graph builds the dependency edge from
  `rs232BackedPrimarySpec` / `sharedRs232ComponentSpec(rs232_name=…)`
  (`model/devices/graph.py`),
- the lifecycle service reconnects a shared transport through the rs232
  manager's `reconnectTransport()` (`model/devices/lifecycle_service.py`),
- the device supervisor classes rs232 entries as infrastructure.

Decision: **`micromanager.core` is an rs232-kind contribution, and every
bridged device names it in `managerProperties.rs232device`** — the one name
the editor, graph and reconnect machinery understand. A setup file declares
the hub once:

```json
"rs232devices": {
  "MMCore": {
    "managerName": "micromanager.core",
    "managerProperties": {
      "configFile": "~/MMConfig_scope.cfg",
      "adapterPaths": [],
      "circularBufferMB": null,
      "logDir": null,
      "adoptExistingCore": false
    }
  }
}
```

`MicroManagerCoreManager(info, name)` — `RS232sManager` is built first in
`MasterController` with no `lowLevelManagers`, so the hub takes none:

- subclasses `DeviceManagerStatusMixin`, reports through `_setConnected` /
  `_setConnectionError` / `_setDeviceNotFound`, and implements
  `reconnectTransport()` (reload the `.cfg`) so the lifecycle service's
  transport-reconnect path applies;
- **never lets a startup failure escape**: `RS232Info` has no `transient`
  and `MultiManager.__init__` has no try/except, so a raising hub would abort
  the whole `MasterController`. No MM install, no adapters, a bad `.cfg`: the
  hub logs, sets ERROR status with the MM message, and every dependent
  manager then fails per device with "hub MMCore is not connected";
- sets `PYMM_SIGNALS_BACKEND=psygnal` before import, constructs its own
  `CMMCorePlus()` (not `.instance()`), and asserts
  `type(core.events).__name__ == "CMMCoreSignaler"`; `adoptExistingCore:
  true` is the opt-in to reuse a core another package created;
- sets the adapter search path (`adapterPaths` > `MICROMANAGER_PATH` >
  `find_micromanager()`, which already falls back to `mm_test_adapters`),
  logs the one it chose and the device interface version on both sides;
- loads `configFile` if given (**system-config mode**); otherwise devices load
  themselves (**inline mode**, §4.3);
- sets `setAutoShutter(False)` so MMCore never toggles a shutter around a snap
  (the demo config turns it on);
- routes MMCore's log into ImSwitch2's log directory;
- `finalize()` → `unloadAllDevices()`. No reference counting: `closeEvent`
  finalises detectors, lasers, positioners and flip mirrors before
  `rs232sManager`, so the hub is always last (`RS232Manager.finalize` is the
  precedent).

Each bridged manager returns a graph descriptor via
`rs232BackedPrimarySpec(category=…, rs232_name=props["rs232device"])`, so the
device graph, the status dock and transport reconnect see the dependency.

The rs232 label is a misnomer for a software hub. The config editor shows the
section as "RS232 devices"; a label rename ("Hubs / communication") is a
small, separate config-editor task and is not a blocker.

Rejected alternative: a generic `pluginConfig` section in `SetupInfo`
forwarded to every manager. It would still need a per-device reference to the
hub, so it gains nothing here; it is a core change with no second customer.

### 4.3 Two ways to bring a device in

**System-config mode** (recommended, what a Micro-Manager user wants): the hub
loads the `.cfg`; a device entry names the label from that file:

```json
"Camera": {
  "managerName": "micromanager.camera",
  "managerProperties": {"rs232device": "MMCore", "label": "Camera",
                        "cameraPixelSizeUm": 6.5},
  "forAcquisition": true
}
```

**Inline mode** (one device, no `.cfg`): the entry carries `adapter`,
`device`, optional `parent` (hub label) and `preInitProperties`; the manager
calls `loadDevice` / `setParentLabel` / set pre-init properties /
`initializeDevice` itself and `unloadDevice` on finalize. Mixing modes in one
setup is allowed (a `.cfg` plus one extra inline device).

A manager that is handed a label the core does not have fails with the list of
loaded labels of the right MM device type, so a typo is a one-line fix.

### 4.4 Per-kind mapping

| ImSwitch2 contract | MM device type(s) | Mapping |
|---|---|---|
| `DetectorManager` | Camera, **mono only** | Exposure ⇄ `setExposure(label)` (ms, MM's unit). Every MM property becomes a parameter: allowed values → `DetectorListParameter`; numeric with limits → `DetectorNumberParameter`; read-only → non-editable; `Binning` allowed values → `supportedBinnings`. `fullShape` = `(width, height)` from `clearROI` + `getImageWidth/Height` at init. `startAcquisition` → host-guarded `startSequenceAcquisition(label, 2**31-1, 0, stopOnOverflow=True)`; `stopAcquisition` → `stopSequenceAcquisition(label)` and **lets MM errors raise** (the lease table turns that into the hardware fault); a detected frame loss (§4.5) is re-raised here too, so a recording that lost frames fails instead of finishing short. `getChunk` pops **this camera's queue in the host** (§4.5), never the MM buffer, and returns a list of equally-shaped `(h, w)` frames (a list is tolerated by `_distributeChunkLocked`; Hamamatsu returns one). `flushBuffers` clears **this camera's queue only**. `crop` → stop, `setROI(label, …)`, read `getROI(label)` back and set `_frameStart`/`_shape` from the applied values (adapters snap; Hamamatsu precedent), restart. **`dtype` overridden** from `PixelType`/bytes-per-pixel so an 8-bit camera is not declared uint16 to a storer created before the first frame; `bitDepth` stays itemsize×8. `isScanDriven` False. `cameraPixelSizeUm` from the setup file, else from MM's pixel-size config: `getPixelSizeUm()` **includes binning** and returns 0.0 when no pixel config matches (P0 probe), so the manager divides by the current binning to get the unbinned camera pixel size ImSwitch2 expects, treats 0.0 as "not configured", and logs which source won (the setup value wins). **Colour cameras (4-component frames) are refused at init** with a message: ImSwitch2's chunk contract is `(numFrames, height, width)`, the Zarr writer and napari layers assume it; a per-component split is a later feature. |
| `PositionerManager` | Stage, XYStage | Axes: a Stage has one axis (name from the setup file); an XYStage has exactly `["X","Y"]`. µm in both systems, no conversion. **`initialPosition` read from `getPosition`/`getXYPosition`** (not zeros). `setPosition`/`move` → `setPosition` / `setXYPosition` / relative variants, then `waitForDevice` when `waitForDevice: true`. `stagePositionChanged` / `XYStagePositionChanged` → `updateTrackedPosition`. `reference()` → `home(label)`. `requiresReference` is a **class attribute of `PositionerManager`** (default False), not a setup field, so the bridge manager sets it per instance from `managerProperties.homeable` (default false; `isReferenceActionable` = requiresReference × forPositioning × not hide); the manager **never calls `home` on its own** — homing moves hardware, and MMCore offers no support query — so an unsupported adapter surfaces as an error on the explicit reference action, not at startup. `forScanning` False (no analog line). |
| `LaserManager` (done, 0ab55bd39) | Shutter, State, property | `managerProperties.enable` = `{kind: shutter}` \| `{kind: state, on, off}` (label or index) \| `{kind: property, name, on, off}`; optional `power` = `{label?, property, units, decimals}` — `units` and `decimals` have **no default** (MM properties carry no unit; we do not guess `mW`); `isBinary` when `power` is absent. Device type must match `enable.kind`; state labels / property values are verified at init (read-only refused). `LaserInfo.valueRangeMin/Max` must lie inside MM's limits when MM states them. `setEnabled`/`setValue` read back and return False on failure (the laser convention); `applyEnabled`/`applyValue`/`applyRawDrive` raise `RawDriveError`; `supportsRawDrive` only with `power`. Safe state, reload and finalize switch the light off unguarded and **verify it reads back off**. Modulation: unsupported, `isModulated` False. |
| Flip mirror = N-position switch (done, 4fd0fda95; generalised 87e19a5bb) | State device with **two or more** positions (flip mounts, filter wheels, turrets, light paths), or a Shutter presented as a switch (closed = 0, open = 1) | No base class exists; `FlipMirrorsManager` instantiates `cls(info, name, **lowLevelManagers)` and `FlipMirrorController` duck-types `move_to(state)`, `get_state()`, `get_state_names()`, `is_connected()`, `get_cached_state()`, `get_last_error()`, `getDeviceLifecycle()`, `finalize()` (ThorlabsMFF is the model); `FlipMirrorWidget` draws exactly two buttons named from `get_state_names()`. `managerProperties`: `rs232device`, `label`, optional `states` = the two MM state labels to use as 0 and 1 when the adapter's own labels are unhelpful. A device with more than two positions is **refused** at init with its position count; `states` does not turn a wheel into a switch. Every position is the state of the same index, named by MM's state label unless `state_names` overrides it; `invert` and `states` exist for two-position devices only; `initial_state` (startup only) is range-checked; moves wait outside the hub lock and are read back; a reconnect that finds a different position count is an error (the widget is built once). Core change (87e19a5bb, Lenny's call: a wheel is in principle a flip mirror): `FlipMirrorWidget` draws one button per position up to four and a drop-down above (`MAX_BUTTON_POSITIONS`, the State column's width budget), links need equal position counts, restored states out of range are warned about, not applied; two-state setups and ThorlabsMFF unchanged. Formerly deferred (§9): ImSwitch2 has no selector device kind, and squeezing ten positions into two buttons would misrepresent the hardware. |
| `RotatorManager` [P3] | Stage used as a rotation axis | `move_rel` / `move_abs` in degrees → the Stage's µm API; conversion factor required in the setup. |
| not mapped | Serial, Hub, Core, Generic, ImageProcessor, Magnifier, SignalIO, Galvo, AutoFocus, SLM, pumps | Serial/Hub/Core are MM-internal; Galvo/SignalIO duplicate ImSwitch2's native waveform scanning; AutoFocus and SLM wait for a concrete user. |

### 4.5 Frame path and threading

MMCore has **one** circular buffer for all cameras, clears it when any
sequence starts, loses frames silently unless `stopOnOverflow` is set, and
relabels frames on the tagged-pop path (§3). So the buffer gets one owner.

- **The host drains, managers never touch the buffer.** `MMCoreHost` runs one
  drain thread per core that pops with `popNextImageAndMD()` and routes each
  frame by its metadata `Camera` tag into a per-camera bounded deque. Camera
  managers pop only their own deque. The deque bound is the per-detector
  consumer-queue limit from `imcommon/model/memory_limits.py` (a named limit
  with a rationale), so the plugin adds no number of its own; the MM buffer
  itself stays at MMCore's default unless `circularBufferMB` is set.
- **Nothing pending is lost on a state change.** Before any
  `startSequenceAcquisition`, `stopSequenceAcquisition`, `setROI` or
  `clearCircularBuffer`, the host drains the MM buffer to the deques under
  its lock. A camera's `flushBuffers` empties its own deque only;
  `clearCircularBuffer` is called only when no camera streams.
- **Loss is an error, not a shrug.** Every sequence starts with
  `stopOnOverflow=True`. With that flag MMCore never skips a frame: it **ends
  the sequence** instead, silently, and the delivered `ImageNumber`s stay
  contiguous (P0 probe), so a gap check cannot see overflow. The drain thread
  therefore detects loss as `isSequenceRunning(label)` turning False without
  a requested stop, confirmed by `isBufferOverflowed()`, which it reads
  **before** any `startSequenceAcquisition` or `clearCircularBuffer` (both
  reset the flag). `ImageNumber` continuity stays as defence in depth for
  adapters that drop frames upstream of the buffer. On detection it records
  `frames_lost`/`stopped_unrequested` on that camera and marks it
  faulted-pending. The camera manager raises from the next `stopAcquisition`
  (and refuses further `getChunk` with the reason), so a leased recording
  fails loudly and the lease table sets the hardware fault; the host restarts
  nothing by itself. A deliberate capacity-overrun test (1 MB buffer, 1 ms
  exposure, consumer sleeping 50 ms) pins this. A fault found while a
  settings change stops and restarts the stream is restored on the stream
  (`restore_fault`) and raised to the settings caller as well, so the
  recording still fails instead of hanging on an empty `getChunk`
  (688961391). A live view that hits a fault keeps its last frame and says so
  in the status summary while staying connected.
- **Signal handlers run on a foreign thread** (P0 probe: psygnal delivers on
  MMCore's callback thread, neither the caller's nor Qt's), so every handler
  only stores under the manager's lock; nothing else happens there.
- **Three invariants MMCore does not enforce** live in the host lock: a second
  camera may start streaming only if its frame shape and dtype equal the
  running one's; an ROI change always happens inside drain → stop → set →
  read back → restart; and **no start and no `setROI` of any camera while
  another running stream is being recorded** (688961391, review finding):
  both clear the one buffer and reset every `ImageNumber`, so a frame the
  recording camera inserts between the host's drain and MMCore's clear
  vanishes without a trace — the counter reset hides the gap, and loss is an
  error. "Being recorded" is what the camera manager reports through
  `register_camera(label, is_recording)`: leased **and** a RAW chunk consumer
  registered (live view, and the acquire window before a recording registers
  its consumers, do not count, so cameras that run together are started
  together before the recording begins). Violations raise with a message that
  names PR #936. "Nothing pending is lost" above therefore holds for queued
  frames; the sub-millisecond insert window is closed by refusal, not by
  detection.
- All other MMCore calls go through the same re-entrant lock. Signals are
  psygnal, emitted on the calling thread; the only consumers are position
  read-back (stage) and property echo (camera parameter refresh). Nothing
  from a signal touches Qt.

### 4.6 Converter (done, 26b2938f1)

`python -m imswitch_device_micromanager convert MMConfig.cfg -o setup.json
[--type LABEL=camera|stage|xystage|shutter|state]… [--no-classify]
[--adapter-path DIR]… [--hub-name NAME]` — pure Python (a test pins that it
imports neither `imswitch` nor Qt). It parses MMCore's `.cfg` grammar
(`Device`, `Parent`, `Property`, `Label`, `ConfigGroup`, `ConfigPixelSize`,
`PixelSize_um`, `Delay`, `FocusDirection`; unknown commands become report
warnings, a malformed line is `file:line` and exit 2). A `Device,` record
carries no type, so devices are classified through
`getAvailableDeviceTypes(library)` on a bare core (adapters installed, nothing
loaded or initialised — verified in §3); unresolved libraries stay unresolved,
`--type` supplies or overrides a type.

Output: the hub in `rs232devices`, one `detectors` entry per Camera, one
`positioners` entry per Stage (`["Z"]` when `Core,Focus` names it, else the
axis string `"TODO"`, which loads and is listed in the report) and XYStage
(`["X","Y"]`, `homeable` false), one `lasers` entry per Shutter (binary:
ranges `null`; `wavelength` **omitted** — MM does not know it; the loader
reads it as `None` and `validate-setup` warns once per light, which doubles as
the checklist), one `flipMirrors` entry per State device with a known count ≥ 2 (count
from the pre-init `Number of positions` or the `Label` lines; below 2, unknown
or contradictory → report), `availableWidgets` = what the written sections need.
`cameraPixelSizeUm` is omitted: MM's pixel-size configs supply it, else a
report TODO. Nothing is ever invented and nothing is written as a JSON
comment (`validate_setup_file` uses `json.load`): multi-position State
devices, AutoFocus, SignalIO, Galvo, Magnifier, ImageProcessor, Hub, Serial,
Core, pumps and unresolved devices go to `<out>.conversion-report.txt` and
stdout, one line each with label, library, device, type and reason. The demo
config converts to hub + Camera + Z + XY + two shutter lights + six switches
(Dichroic, Emission, Excitation, Objective, Path, LED) with 2 devices
reported (DHub, Autofocus), and the output boots through the real managers (test). **Config editor (ce8b353a1):** "Import Micro-Manager
configuration…" (toolbar + left panel; enabled when the plugin is importable)
runs this converter and merges the result into the open config with suffixed
names and a report dialog; bridge devices get a `label` picker filled from
the hub's `.cfg` (type-filtered when the adapters are installed) and an
`rs232device` picker listing the `micromanager.core` hubs; the plugin template
loader no longer flags sibling contributions' devices as errors. The palette
already listed the five managers with schema-driven forms before this.

### 4.7 Failure and reconnect behaviour

Device reconnect 2.0 applies as to every manager, in its own terms:

- Every bridged manager subclasses `DeviceManagerStatusMixin` (DetectorManager
  already does) and reports through `_setConnected` / `_setConnectionError` /
  `_setDeviceNotFound`; the supervisor reads `statusSnapshot()`.
- Managers never write `_hardwareFaulted`: it is owned by the DetectorsManager
  lease table and set when `stopAcquisition` raises. The camera manager
  therefore lets MMCore errors propagate from `stopAcquisition`.
- Reconnect runs through the **transport path** of the lifecycle service
  (`device-reconnect-2.0.md` §4.3): the hub's `reconnectTransport()` reloads
  the core (unload all, re-apply adapter paths, reload the `.cfg`); the
  service then calls each dependent manager's `_onTransportReconnected(real)`
  (the MHXY stage and AA AOTF managers are the precedents) and the errors it
  returns fail the reconnect. Because `_installBackend` alone does not re-open
  a transport-backed device, every bridged manager implements that hook:
  the camera re-loads its inline device if it has one, re-binds the label,
  re-applies cached exposure, binning, ROI and edited properties, and checks
  the resulting frame shape and dtype against what it reported; the stage
  re-reads its position from the device (the MHXY `syncPositionFromHardware`
  pattern); the light re-applies `enabled=False` and **verifies the shutter
  or state reads back closed** before returning no errors; the flip mirror
  re-reads its state. Until the hook returns, the manager reports
  reconnecting, not connected.
- Nothing falls back to a demo device by itself. The demo adapters are
  reachable only through an explicit setup (the bundled template), never by
  `useMockOnFailure`-style substitution — the mock is a different
  *configuration*, not a different *code path*. The registry's
  `prefer_mock` / `mock_python_name` hooks have no caller outside tests, so
  declaring none is compatible.

## 5. Phases

One branch, phases as commits; each phase ends with its tests green on the
real MMCore via `mm-test-adapters`.

**P0 — Spike (done 2026-10-24).** Verdict: **go** — the real MMCore with the demo adapters streams 512² 16-bit at ≈ 615–630 fps per camera at 1 ms exposure (two cameras at once, 0 lost frames in 11 748), frames are 0.2–0.6 ms old when popped, overflow with `stopOnOverflow=True` ends the sequence silently (no exception, no `ImageNumber` gap: only `isSequenceRunning`/`isBufferOverflowed` show it), psygnal holds under a `QApplication`, and 24 of 24 exit tests returned 0; still open are the Windows `adapterPaths`/DLL row (P3) and a full `MasterController` close (P1 end-to-end). Numbers in §3 (rows marked *P0 probe*), script `examples/plugins/imswitch-device-micromanager/tools/p0_probe.py`. What was asked: in a throwaway venv (Python 3.12): install
pymmcore-plus + mm-test-adapters and load `MMConfig_demo.cfg` through a
plain `CMMCorePlus`. Measure and record in §3: continuous-acquisition frame
rate and drain-thread latency at 512² for one and two cameras; frames counted
vs frames delivered under a deliberately slow consumer with
`stopOnOverflow=True` (loss detected, none fabricated); the psygnal
backend asserted under a running `QApplication`; whether a psygnal-backed
core survives the PyQt5 atexit walker (`imcommon/applaunch.py`) and a full
`MasterController` close without a segfault; on Windows (rig visit or VM),
whether a vendor adapter whose SDK DLLs live outside the adapter folder loads
with `adapterPaths` alone (pymmcore-plus prepends to `PATH`, no
`add_dll_directory`). Go/no-go on the numbers.

**P1 — Hub + camera + stage (DONE 2026-10-24).** Commits e61c177d2 (host +
hub, 91 tests), 13fdc2143 + 75c0d0d71 (camera), ad56f4964 + 3a3666368
(stage; waits release the hub lock), c6659eb74 (end-to-end + hint + CI lane):
286 plugin tests on the real MMCore with the demo adapters. The end-to-end
boots the real `ImConMainController` on the shipped template (napari stubbed:
the real canvas segfaults offscreen), runs live view, records 30 frames to
HDF5 and reads `(30, 512, 512)` uint16 back with none lost, moves Z/XY,
homes XY, closes with the hub last, and a child interpreter exits 0. Still
open from the list below: two cameras recorded at once *through the app*, ROI
change during live view *through the app*, transport reconnect *through the
app* (all covered at manager level). The `micromanager-plugin` CI job exists
but has not run yet. Original scope: `MMCoreHost` with the drain thread and
per-camera queues, `micromanager.core`,
`micromanager.camera`, `micromanager.stage`, both load modes, the demo setup
template, schemas, `external.py` hint, manifest tests (incl. "no in-tree
alias"), plugin discovery test, graph descriptor specs, and an end-to-end:
boot the mock app on the demo template, live view, record 50 frames through
`RecordingManager` with the count checked, two cameras recorded at once with
identity checked per frame, change ROI during live, move Z and XY, read back,
transport reconnect with the hooks. Headless, via the scripting API as `feat/sparse-scanning` did.
CI: a new lane for `examples/plugins/imswitch-device-micromanager` only, on
Python 3.11 — the first plugin lane in `ci.yml` (none run today) and the
first time imswitch2 itself installs on ≥ 3.11 in CI; it installs
`-e .[test]` first so `imswitch2>=0.1` resolves locally, then the plugin.

**P2 — Light, flip mirror, converter, docs.** (P2a done 2026-10-24: 7d8e772d3
extracts the shared hub plumbing into `_bridge.MicroManagerDeviceMixin`,
0ab55bd39 adds `micromanager.light`; 434 plugin tests; test devices `LED
Shutter`, `White Light Shutter`, `LED` (State), inline `D-DA` `Voltage`.)
P2b done the same day: 4fd0fda95 `micromanager.flip-mirror`, 26b2938f1
`convert` + `docs/devices/micromanager.rst` (linked from the docs index, the
device README, plugins.rst, DEVICE_PLUGINS.md); 590 plugin tests, `sphinx -W`
clean. **P2 is complete.** Original scope: `micromanager.flip-mirror`
(two-position devices only), `convert` with library-based classification and
the sidecar report, `docs/devices/micromanager.rst` (install matrix per platform,
both modes, the unmapped-type table, the mono-only limit), plans README row,
DEVICE_PLUGINS.md status line.

**P3 — Rig.** Lenny chose a Hamamatsu sCMOS, native `HamamatsuManager`
versus the bridge through `HamamatsuHam`, before anything is pushed. The kit
is in ee74b9e12: `docs/design/plans/micromanager-bridge-rig-test.md` (rows,
property mapping, results table), the template
`setup_templates/hamamatsu_native_vs_bridge.json` (one setup, one of the two
cameras disabled; DCAM allows one handle, so the comparison is sequential)
and `tools/compare_cameras.py measure|diff` (headless, never touches the
rig's `ImSwitchConfig`; validated on the mock and demo cameras). Original
scope: one Windows rig with a device that has both a native ImSwitch2
manager and a Micro-Manager adapter, bridged and native side by side:
same frames, same positions, same exposure semantics; plus the
`adapterPaths`/DLL row from P0. Rig rows written before the visit, results
recorded in the plan. Lenny or Guillaume on their rig, per the PR-review
rule.

**P4 — Publish.** Move to `Imswitch2/imswitch-device-micromanager`, PyPI
release, make the `external.py` hint live, drop the `examples/plugins` copy.
Prerequisite for installer users: the bundle moves to Python ≥ 3.11 (§6).

## 6. Risks and how the plan treats them

| Risk | Treatment |
|---|---|
| Python ≥ 3.11 floor from pymmcore while ImSwitch2 supports 3.10 | Plugin declares ≥ 3.11; CI has the `micromanager-plugin` job on 3.11 (added c6659eb74, **not yet run**: whether `.[test]` resolves on 3.11 is unconfirmed until the first push). **The Windows/macOS installers bundle Python 3.10** (`imswitch-bundle.yml`), so installer users cannot load this plugin until the bundle moves to 3.11+. Decision for Lenny: raise ImSwitch2's floor and the bundle together (P4 prerequisite for installer users) |
| No real adapters on Apple Silicon or Linux | Development and CI on test adapters; real hardware on Windows rigs only, stated in the docs install matrix |
| Silent frame loss: mixed sizes, mid-sequence ROI, buffer cleared on start, overrun with `stopOnOverflow=False`, relabelled tags | One buffer owner in the host (§4.5): drain thread, per-camera queues, drain-before-state-change, `stopOnOverflow=True`, `ImageNumber` gap check, identity from `popNextImageAndMD`; tests for each in P1; re-check PR #936 at P4 |
| Homing at startup moving a stage | Never probed; only the explicit reference action calls `home` |
| Reconnect leaving dependents stale or a light on | `_onTransportReconnected` hooks per manager, lights verified closed before success (§4.7) |
| Filter wheels / turrets have no ImSwitch2 kind | Resolved: the flip-mirror kind takes N positions (87e19a5bb); position count is fixed at startup |
| Hub startup failure aborting the whole app | Hub swallows and reports (§4.2); dependents fail per device |
| Qt signaler chosen by default; a Qt core created by another package | Env var + own core + backend assertion; adoption opt-in (§4.2) |
| MM properties are typeless strings without units | Types from `PropertyType`, limits from `getPropertyLowerLimit/UpperLimit`; units and decimals required in the setup where ImSwitch2 shows them |
| Colour cameras | Refused at init, documented; per-component split is a later feature |
| Shadowing an in-tree manager | No aliases; ids under `micromanager.*`; a manifest test pins it |
| `pip install` of the plugin pulling the unrelated PyPI `ImSwitch` v1 | Plugin depends on `imswitch2>=0.1`; verified **without** `--no-deps` |
| Vendor adapter DLL paths on Windows | `adapterPaths` list on the hub; P0/P3 row |
| ome-writers/tensorstore weight from pymmcore-plus | Accept for an optional plugin; note the install size in the docs |
| Config editor shows the hub under "RS232 devices" | Cosmetic; a later editor label change, not a blocker |
| ROI at binning > 1 doubles (rig row G3) | The Settings controller multiplies the ROI fields by the binning on Apply but not on restore, and the base `crop` contract names no unit; the bridge takes binned pixels (consistent with `shape`). This is the known core double-multiply defect, to be fixed in the core, not papered over in the bridge |

## 7. Validation

- Unit: property mapping (list / number / read-only / limits), frame
  normalisation (8/16-bit mono; 4-component refused), `.cfg` parser against
  `MMConfig_demo.cfg`, manifest + schema + no-alias pin, converter output
  passes `validate-setup`.
- Integration (real MMCore, demo adapters): two load modes, hub failure
  isolation, transport reconnect with every hook, ROI change during live with
  read-back and no pending frame lost, two cameras streaming with per-frame
  identity checked, pending frames preserved across a second camera's start,
  deliberate capacity overrun detected as an error, two cameras same size
  accepted and different size refused, graph descriptors present.
- End-to-end: mock app on the demo template through the scripting API.
- Rig: P3 rows.

## 8. Open questions for Lenny

1. Did the meetings with Tsuchida / Liu produce commitments (e.g. feeding
   requirements into PR #936, a shared test corpus, a talk)? They belong in §9.
2. Which Windows rig and which device for P3?
3. Raise ImSwitch2's Python floor and the installer bundle to 3.11, or keep
   3.10 and accept that this plugin is source-install only?
4. Is `examples/plugins/` the right first home, or straight to its own repo?

## 9. Stretch (not planned, recorded so it is not lost)

- **Reverse bridge** via UniMMCore: wrap ImSwitch2 managers as Python
  `CameraDevice` / `StageDevice` so pymmcore-plus MDA, useq-schema and
  napari-micromanager can drive ImSwitch2 hardware. Waits for UniMMCore to
  leave "experimental".
- `useq-schema` ↔ ImSwitch2 scan-acquisition-order interchange.
- Per-frame MM image tags into the OME recording metadata.
- Colour cameras as per-component detectors.
- ~~A generic selector kind~~ — done 2026-10-24 by generalising the flip-mirror
  kind to N positions (87e19a5bb); the Leica stand's cube turret could move
  onto it later.
- ~~Config-editor "Import Micro-Manager configuration…"~~ — done ce8b353a1.

## 10. Review log

**Whole-plugin review (2026-10-24, after P2).** 12 findings, all applied in
688961391: fault during a settings change stalled a recording silently (now
restored + raised); the drain→clear insert window is closed by refusing
starts/ROI changes while another camera records; the drain-order and
`startAcquisition` re-sync invariants were untested (two mutations survived;
now pinned, mutations confirmed failing); install hints covered 3 of 5 ids;
live-view faults were invisible; inline bookkeeping after a hub reload; e2e
module skips under xdist; converter wrote an empty `adapterPaths`. 620 tests
then; 616 after the flip-mirror generalisation replaced the refusal tests.

**Implementation log (2026-10-24, all on `feat/micromanager-bridge`, Sonnet
agents from structured briefs, reviewed and fixed in-session).** Deviations
from the revision-3 text that were accepted: the hub plumbing lives in one
mixin `_bridge.MicroManagerDeviceMixin` (camera, stage, light, flip mirror);
the camera keeps a manager-side staging list so `getLatestFrame` never steals
frames from a `readChunk` consumer (eviction there is a fault, like the
host's); every property write except Exposure restarts the stream (MMCore
cannot say which properties change the frame format); `Binning` is not a
parameter (only `setBinning`); the host selects the first streaming camera as
MMCore's current camera (pop shapes by the current camera, §3); blocking
stage/flip-mirror waits poll `deviceBusy` outside the hub lock at MMCore's 10 ms
cadence and fail at `getTimeoutMs`; a hub reload marks a homeable stage
unreferenced; an absent stage shows `nan`, not 0; `requiresReference` comes
from `managerProperties.homeable`; the light's `enable`/`power` objects replace
the flat `enableProperty`/`powerProperty` names; a refused light command sets
`lastError`, not connection ERROR (so "off" always stays possible); the
converter omits unknown values instead of writing markers. Known core issues
met, left alone: SettingsController ROI/binning double-multiply; the transport
reconnect path never calls `clearFaultAfterHardwareReplacement`; the real
napari canvas segfaults offscreen (the e2e stubs it).


**Revision 3 (2026-10-24)** — second independent review (8 findings), all
re-verified in our own probe venv and accepted. Buffer ownership moved to
the host: `popNextTaggedImage` relabels frames with the selected camera,
starting any sequence discards unread frames, `clearCircularBuffer` is
global, and `stopOnOverflow=False` loses frames with `isBufferOverflowed()`
False — so §4.5 now specifies a host drain thread, per-camera queues,
drain-before-state-change, `stopOnOverflow=True` and `ImageNumber` gap
detection with loss as an acquisition error. Homing is no longer probed at
startup. §4.7 specifies `_onTransportReconnected` hooks per manager. The
converter classifies devices through `getAvailableDeviceTypes(library)` on a
bare core (verified: only `Core` loaded), keeps unresolved entries, and
writes diagnostics to a sidecar report instead of JSON comments. Multi-position
State devices (filter wheels, turrets, light paths) are deferred: the
flip-mirror UI is binary; goal and template narrowed accordingly.

**Revision 2 (2026-10-24)** — independent review of revision 1 against the
code and a probe venv. Blockers fixed: (1) managers do not set
`hardwareFaulted`; §4.7 rewritten in `DeviceManagerStatusMixin` terms; (2)
colour frames `(h, w, 3)` would break the Zarr writer and napari layers —
mono-only, colour refused; (3) the hub reference must be
`managerProperties.rs232device` or the editor, device graph and transport
reconnect ignore it. Also: flip-mirror duck-typed surface replaces the
invented `setState` contract; hub signature `(info, name)`, no refcount,
`unloadAllDevices` in `finalize`; own `CMMCorePlus()` + psygnal assertion
instead of `.instance()`; `startSequenceAcquisition` takes a C long; mixed
frame sizes and mid-sequence `setROI` fail silently, so the host enforces
both; `LaserManager` needs `valueDecimals`, `LaserInfo` requires ranges and
wavelength; `requiresReference` is the setup's call; `initialPosition` from
the device; hub startup failures must not escape `MultiManager.__init__`; no
plugin tests run in CI today; the memory-budget claim was hollow; `dtype`
must be overridden; crop reads the applied ROI back; P0 questions already
answered moved into §3; TriggerScope was the wrong precedent (ESP32/AA are).
