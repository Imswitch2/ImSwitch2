# Scan engines and ExpertScan

**Status:** Exploration, draft 1, for review. Nothing is implemented.
**Date:** 2026-10-08
**Branch:** `docs/scan-engines-expert-scan`, off `feat/simple-point-scan`
(56ef43f0, which carries the scan cloak and the Advanced scan-freeze work).

**The two questions (Lenny, 2026-10-08):**

- **(a)** Can the Advanced scan, and SimplePointScan with it, run on scan
  hardware other than the NI-DAQ, such as the TriggerScope or a Teensy scan
  generator? For a scanner that takes only analog signals and no digital
  TTL, can the TTL parameters be greyed out?
- **(b)** Can there be a further layer, ExpertScan, for more complicated
  patterns, such as the Snouty multi-scan RESOLFT sequences or arbitrary
  curves like a circle or a spiral?

File references are to this branch.

---

## 0. The answer in brief

**(a) Yes, in three levels.** The first two are software only.

1. **One engine seam.** The scan controllers call the NI-DAQ manager
   directly in about ten places. Behind one interface, the Advanced scan
   runs on whatever engine the setup names. On the NI-DAQ, nothing changes.
2. **Engines say what they can do** (capabilities). Advanced and Simple
   build their controls from that.
   - No digital lines means the TTL parts are greyed out, with the reason.
   - A scan an engine cannot run is refused, with the reason. This is the
     same representable-subset pattern the scan cloak already uses.
3. **The real limit is the firmware.**
   - The TriggerScope's Snouty firmware runs six fixed routines and plays no
     waveform tables. Advanced on a TriggerScope therefore means a stepped
     raster with cameras.
   - The Teensy's v4 firmware is digital only, with 256 steps. It is a pulse
     engine and cannot drive a galvo.
   - A generic step-table firmware would lift both limits. That is firmware
     work, optional, and last (§2.7).

**(b) Yes, but "ExpertScan" covers two different things.**

- **Trajectories** (circle, spiral, Lissajous, point lists, any curve) are
  continuous. They need a streaming engine, which today means the NI-DAQ.
  They also need a data path that is not a raster: measurements plus their
  coordinates, gridded into an image for display.
- **Sequence programs** (pLS-RESOLFT and its relatives) are stepped: nested
  loops of DAC steps, pulse sequences and camera exposures. They run on any
  engine that can step. The six TriggerScope panels are presets of one such
  program.

**Recommended order:**

1. G1, the engine seam.
2. G2, capabilities and greying out.
3. E1, trajectories on the NI-DAQ. The mock APD can already image a spiral,
   because it samples its synthetic sample at the waveform's positions.
4. E2, sequence programs.
5. G3, the TriggerScope engine.
6. F, firmware.

---

## 1. What the code does today

### 1.1 The Advanced and SimplePointScan path

```
widget ──► analog + digital dicts
             │
             ├─ ScanDesigner (scan.scanDesigner: Galvo / Beta)
             │     └► scanSignalsDict {positioner: volts[n]}  + ScanInfoContract
             ├─ TTLCycleDesigner (scan.TTLCycleDesigner)
             │     └► TTLCycleSignalsDict {device: bool[n]} + line/frame clocks
             └─ coordinator.arm ──► NidaqManager.runScan
                    AO + DO tasks, one clock (100 kHz)
                    APD / PMT open their own NI counter / AI tasks in the
                    sigScanBuilt slot, started by ao/StartTrigger
```

- **One clock, everything pre-sampled.** The arrays have equal length at
  `scan.sampleRate`. The NI-DAQ refuses any rate other than 100 kHz
  (`NidaqManager.py:523-535`).
- **Volts.** Designers build waveforms in volts through `conversionFactor`
  (`GalvoScanDesigner.py:264-295`), checked against `minVolt`/`maxVolt`
  (`basesignaldesigners.py:129-191`).
- **The scan description is a raster.** `ScanInfoContract` holds pixel
  counts per axis, samples per pixel, line and frame, and the fast-axis pixel
  count (`basesignaldesigners.py:307-385`). About twenty readers rely on it:
  APD, PMT, TimeTagger, RecordingManager, the acquisition layouts and
  `scan_frame`.
- **Point detectors are NI-DAQ detectors.** They open their own tasks, read
  the timer clock and arm on `ao/StartTrigger` (`APDManager.py:1211-1219`,
  `PMTManager.py:1086-1094`). They cannot follow another engine.
- **Where the controllers touch the NI-DAQ:**
  - `basecontrollers.py:443-465`: creates the coordinator and relays the
    built, started, done and build-failed signals.
  - `basecontrollers.py:1673`: `resolveScanTTLDevices`.
  - `basecontrollers.py:1698`: `coordinator.arm`, which calls `runScan`
    (`_scan_execution.py:384-396`).
  - `ScanControllerAdvanced.py:160`: the progress bar.
  - The shared coordinator is stored on the NI-DAQ manager object
    (`_scan_execution.py:674-704`).
- **Already engine-neutral:**
  - `ScanExecutionCoordinator.armWithStarter` (`_scan_execution.py:398-481`),
    which the TriggerScope panels use.
  - The run and iteration tokens, and the `finishScan` barrier.
- **Analog-only already runs on the NI-DAQ:** the DO task is skipped
  (`NidaqManager.py:1395`).

### 1.2 TriggerScope

- **Link.** ASCII over RS-232, at about 50 ms per parameter write
  (`TriggerScopeManager.py:73, 115`). The firmware is not in the repository;
  it is the Snouty `TriggerSwitch_0.1.ino`
  (`docs/triggerscope_firmware_quirks.md`).
- **No waveform tables.** `ScanManagerTriggerScope.runScan(params,
  scan_type)` uploads named scalar parameters for one of six routines and
  then sends the routine's command (`ScanManagerTriggerScope.py:60-112`).
  The routines are `RASTER_SCAN`, `pLS-RESOLFT_SCAN`,
  `pLS-RESOLFT-Multicolor_SCAN`, `galvo_Detection_SCAN`, `multicolor_SCAN`
  and `LS-XY-RESOLFT_SCAN`.
- **`RASTER_SCAN`:**
  - Stepped, point by point, up to 4 dimensions, each given as start, length
    and step in volts.
  - One shared pulse window: the firmware fires TTL lines 0-3 together and
    ignores the per-line rows (quirks §1).
  - The axis loop includes the endpoint, which the controller compensates
    for (`TriggerScopeRasterController.py:21-43`).
- **The RESOLFT routines:**
  - On, off and readout pulses, DAC ramps and time-lapse delays.
  - Loop order is time, then cycle, then readout plane.
  - The exact order inside a step is known only from the firmware source.
- **Detectors:** cameras only, by TTL. APD, PMT and TimeTagger cannot take
  part.
- **No abort:** a started iteration always finishes.
- **A second lifecycle.** None of its controllers is a `SuperScanController`.
  `_triggerscope_scan_lifecycle.py` (612 lines) re-implements run
  reservation, repeat, stop and external requests on the same coordinator.
  Four controllers repeat the same 17-key parameter block.
- **One scan manager per setup** (`MasterController.py:90-112`). An Advanced
  NI-DAQ scan and a TriggerScope scan cannot coexist in one setup.

### 1.3 Teensy

- **Base class.** `PulseGeneratorManager` declares honest capabilities:
  channels, minimum pulse, jitter, trigger input, analog
  (`PulseGeneratorManager.py:73-110`). A sequence step holds a duration plus
  the state of every channel.
- **What it can do.** `TeensyPulseManager` with the v4 firmware:
  - 16 digital lines and 1 µs resolution.
  - Sequences of up to 256 steps (`SEQ,<n>,<reps>`, then `<dur_us>,<bitmask>`
    lines), with one outer repeat.
  - No analog output and no trigger input.
  - STOP is checked between steps, with up to 16 ms latency.
- **Who uses it.** Only `PulseGeneratorLaserManager` and the snap trigger.
  No scan uses it, although `ARCHITECTURE.md:186` lists scan controllers as
  consumers.

---

## 2. (a) The Advanced scan beyond the NI-DAQ

### 2.1 Three levels

| Level | What | Firmware? | Result |
|---|---|---|---|
| G1 seam | Controllers talk to a `ScanEngine`, not to the NI-DAQ manager | no | Nothing visible; prepares everything else |
| G2 capabilities | Engines declare what they can do; the panels grey out what is missing and refuse what cannot run | no | The analog-only case answered. Silent failures (§7) become messages |
| G3 engines | TriggerScope (stepped raster, cameras); Teensy (pulse programs) | no | Advanced runs on the TriggerScope where the firmware allows it |
| F firmware | One generic step-table firmware for Teensy-class boards | yes | Any stepped scan on the TriggerScope or a Teensy with DACs; abort |

### 2.2 The engine seam (G1)

```python
class ScanEngine(SignalInterface):
    sigScanBuilt = Signal(object, object, object)   # scanInfo, program, devices
    sigScanStarted = Signal()
    sigScanDone = Signal()
    sigScanBuildFailed = Signal(str)

    name: str                                      # 'nidaq', 'triggerscope', ...

    def capabilities(self) -> 'EngineCapabilities': ...
    def refusal(self, program) -> str: ...         # '' when it can run it
    def run(self, program, scanInfoDict): ...      # the coordinator's starter
    def abort(self) -> bool: ...                   # False: the iteration finishes
```

- **`NidaqScanEngine`** wraps `NidaqManager`: `run` is `runScan(signalDict,
  scanInfoDict)`, and the signals are relayed.
  - `SuperScanController` talks only to `self._scanEngine`.
  - `coordinator.arm` becomes `armWithStarter(engine.run, ...)`.
  - The coordinator is held by the master, not by the NI-DAQ manager.
- **Which engine.** A new optional key, `scan.engine` (default `"nidaq"`),
  names it. Every device the scan drives must belong to that engine. The
  channel already says which engine a device belongs to:
  - `Dev…/` is the NI-DAQ;
  - `Triggerscope/DAC<n>` and `Triggerscope/TTL<n>` are the TriggerScope;
  - the `teensyPulse.pinMap` names the Teensy's lines.
- **A device on another engine** is greyed out in the tables, with the
  reason. If it is selected anyway, the scan is refused: "405 is wired to
  the TriggerScope; this setup scans with the NI-DAQ." This replaces the
  silent drop in §7.
- **Detectors say which engine they need:**
  - APD and PMT need the NI-DAQ.
  - A camera needs any engine with a trigger line for it.
  - The TimeTagger needs an engine that gives it a line clock and the scan
    signals.

  A participant the engine cannot serve is refused.
- **The rule from SimplePointScan holds** (Lenny, 2026-09-25): on the NI-DAQ
  the Advanced scan behaves as it does today. The waveform goldens and the
  "Advanced as today" tests guard it.

### 2.3 Capabilities (G2)

```python
@dataclass(frozen=True)
class EngineCapabilities:
    analog_channels: Mapping[str, tuple]  # channel -> (min V, max V)
    digital_lines: tuple                  # lines the engine can drive
    timing: frozenset                     # {'stream', 'steps', 'routines'}
    sample_rate_hz: float | None          # for 'stream'
    max_samples: int | None
    max_steps: int | None                 # for 'steps'
    min_pulse_s: float
    pulse_windows_per_step: int | None    # None: any
    detector_inputs: frozenset            # {'counter', 'ai'}
    camera_trigger_lines: tuple
    abort_mid_iteration: bool
    trigger_in: bool
    trigger_out: bool
```

The `timing` values:

- **`stream`:** arbitrary sampled waveforms on one clock, as on the NI-DAQ.
- **`steps`:** a value per step plus a pulse timeline inside each step,
  played by the board on its own.
- **`routines`:** fixed firmware programs that take parameters.

| | NI-DAQ | TriggerScope (Snouty firmware) | Teensy (v4 firmware) |
|---|---|---|---|
| timing | stream (steps by densifying) | routines | steps (digital only) |
| analog | AO channels of the board | 16 DAC (setup-declared) | none |
| digital | DO lines (`Dev…`) | 16 TTL | 16 |
| limits | 100 kHz, RAM | 4 raster dims, 1 merged window, µs fields `uint16` | 256 steps, 1 µs |
| detectors | counters, AI, camera triggers | camera triggers | camera triggers |
| abort mid-iteration | no (today) | no | between steps |
| trigger in | yes | no | no |

The NI-DAQ row is not yet complete: these are the properties the code uses
today.

### 2.4 Greying out (the analog-only question)

**The rule.** The widget asks the engine and the setup what is available and
greys out the rest, with the reason as a tooltip and a one-line note. The
controller checks again before every run, because a rule enforced in one
place is not enforced. The refusal is the authority; the greying is a
courtesy.

| Missing | Advanced panel | SimplePointScan | Note shown |
|---|---|---|---|
| Any digital line the engine can drive (analog-only scanner, or no TTL device in the setup) | Line-step TTL rows, *#Line repeats*, the Advanced Line Program (timing windows, Sequence builder, locks), pulse editors, *Include TTL* in the plot | Laser palette and channel lanes hidden; one implicit channel | "This scanner has no digital lines: the scan does not switch lasers. Set them in the Laser panel." |
| More than one pulse window per step (TriggerScope raster) | Timing windows limited to one; Sequence builder off | unchanged (Simple uses whole-pixel pulses) | "The TriggerScope fires all armed lasers in one window." |
| Streaming (a steps-only engine) | Positioner rows shown as stepped; vel_max rule hidden; a smooth sweep is refused | Overview planner uses the stepped time model | "This scanner steps; it cannot sweep." |
| Analog power for a laser | Power column hidden (already today, `ScanControllerAdvanced.py:119-125`) | Channel power hidden (P3) | — |
| Point-detector inputs | APD/PMT not offered as participants | Not available: SimplePointScan needs a point detector the engine reads; refused at startup with that reason | — |

**On the NI-DAQ, "analog only" means a setup with no TTL device.** Today the
Advanced panel then shows an empty TTL table. It would show the greyed parts
and the note instead.

### 2.5 The TriggerScope engine for the Advanced scan (G3)

- **No designer change is needed.** When every scanned axis is stepped, the
  Advanced dicts already are a compact step program: the loops are the axes
  (count, start and step, µm to V), each device has its pulse windows per
  pixel, plus the line steps.
  `advanced_dicts_to_step_program(analog, digital, setup)` would be a pure
  function.
- **It compiles to `RASTER_SCAN` when the scan is representable:**
  - at most 4 axes, all stepped;
  - one pulse window, with the firmware's merged lines 0-3 said in the note;
  - one line pass;
  - cameras only;
  - a dwell the firmware accepts.

  The endpoint compensation moves from `TriggerScopeRasterController` into
  the compiler.
- **Anything else is refused with the reason,** for example: "The
  TriggerScope runs a stepped raster with one pulse window; this scan has 2
  line passes."
- **It runs through `SuperScanController`,** so the code does not get a
  third copy of the lifecycle. The six existing TriggerScope panels keep
  their path until G4 or E2 (§3.4).
- **Testing needs a simulated TriggerScope.** None exists:
  `TriggerScopeManager` has no simulation mode, and the tests use fakes. The
  simulation would accept parameters and answer "Scan done" after the
  routine's computed duration.

### 2.6 Teensy

- **As shipped, it is a digital-only step engine.** It can run the Advanced
  TTL timeline only when that timeline compresses into at most 256 steps per
  repetition. A per-pixel cycle repeated for every pixel compresses; most
  line-step and window combinations do too.
- **It cannot drive galvos** (there is no DAC).
- **It cannot follow an NI-DAQ scan's clock** (there is no trigger input).
  A Teensy-gated laser on an NI-DAQ Advanced scan therefore needs firmware
  work (F2).
- **What it is useful for without firmware work:** pulse programs with no
  moving axes, such as camera and laser switching cycles on a widefield
  RESOLFT setup. That is an Expert sequence program with zero axes (E2).
- **A "Teensy scan generator" with analog outputs** would be a Teensy 4.1
  plus external DACs. That is roughly what a TriggerScope 4 is (to be
  checked on the Snouty board). It is F1.

### 2.7 Firmware (F, optional and last)

- **F1, a generic step-table program for Teensy-class boards.**
  - Upload per-step DAC values or ramps, a per-step pulse timeline
    (run-length coded) and nested loop counts.
  - The board runs on its own, reports progress and checks STOP between
    steps.
  - It extends the Teensy v4 `SEQ` format with DAC columns.
  - Then the TriggerScope and a Teensy with DACs run any step program, the
    six routines become presets, and there is an abort.
  - It needs someone to own the firmware (the Snouty firmware lives outside
    this repository) and rig time.
- **F2, multi-engine sync.** The NI-DAQ sample clock or start trigger goes
  to a Teensy or TriggerScope trigger input. Then one scan can span two
  engines, for example NI-DAQ galvos with TriggerScope piezo steps, or
  Teensy-gated lasers on an NI-DAQ scan.

---

## 3. (b) ExpertScan

### 3.1 Two things under one name

| | Trajectories | Sequence programs |
|---|---|---|
| What moves | Positions as continuous functions of time; detectors sample along the path | Nested loops; each loop steps a device; the innermost step plays a pulse sequence and triggers the camera |
| Examples | Circle, spiral (Archimedean, constant linear speed), Lissajous, rosette, point list or curve from a file, multi-ROI | pLS-RESOLFT (time, then cycle, then readout plane: on, off, readout pulses), multicolor, GalvoDetection, LS-XY-RESOLFT |
| Engine | Streaming (the NI-DAQ) | Any engine that can step (NI-DAQ densified, TriggerScope routines, later F1) |
| Detectors | Point detectors (also cameras, one exposure per event) | Cameras (point detectors on the NI-DAQ) |
| Data | Measurements plus coordinates, gridded for display (§3.5) | Frames per step, a layout like today's TriggerScope geometry (time × cycle × readout) |

### 3.2 The panel, and how it relates to Advanced and Simple

- **A new panel type,** `scanWidgetType: "Expert"`, with its own controller
  on `SuperScanController` (one lifecycle) and the same engine seam.
- **The Advanced scan is not rebuilt on Expert;** it stays as it is.
  - In what they can express, Simple is a subset of Advanced, and Advanced a
    subset of Expert.
  - Expert could later open Advanced scan files as its raster pattern; that
    is not needed at first.
- **The Expert panel has:**
  - a pattern picker;
  - a parameter form generated from the pattern's `param_spec`, the same
    principle as the ImProcess workflow editor;
  - a preview: the XY path drawn inside the scanners' limits, time traces
    for each axis, and pulse rows. It reuses the Advanced panel's graph;
  - a limits readout: peak speed and acceleration against `vel_max` and
    `acc_max`, the voltage span, the duration and the sample count;
  - Live, Start and Stop, as on `ScanCloakView`.
- **A simple cloak over Expert** later (for example a "Simple RESOLFT"
  panel) fits the existing `ScanCloak` bases.

### 3.3 Patterns as plugins

```python
class ScanPattern(ABC):
    id = 'spiral'
    title = 'Spiral'
    kind = 'trajectory'                      # or 'sequence'

    def param_spec(self) -> list: ...
    def build(self, params, limits) -> 'Trajectory | StepProgram': ...
```

- **Built-in and drop-in patterns.** Drop-ins use the same discovery as the
  ImProcess drop-in plugins: a `.py` file in a folder.
- **An arbitrary curve** is a `script` pattern: a few lines of Python
  returning position arrays, in the spirit of the ImProcess Python step.
- **A `Trajectory`** (µm and seconds) holds:
  - the sample rate and the positions per axis;
  - measurement windows (sample ranges, each to a measurement index);
  - pulse tracks per device.
- **The compiler adds** the ramp in from rest and the ramp out to the park
  position, within `vel_max` and `acc_max`.
- **The compiler converts and checks.** It converts µm to V through
  `conversionFactor`, then checks the voltage range, `vel_max` and `acc_max`
  (by finite differences on the sampled path), the sample count and the
  engine. A failed check is refused with the reason, in the same wording as
  the Galvo designer's `vel_max` refusal (P0).

### 3.4 Sequence programs and the TriggerScope panels

**A `StepProgram` holds:**

- loops, outermost first, as (name, count, device ramps `{device: (start,
  step)}`);
- the time-lapse loop and its delay;
- the pulse sequence of the innermost step, as (device, start, duration)
  entries;
- the camera exposure window;
- delays: after a DAC step, between cycles.

**The six TriggerScope routines are fixed step programs.** For example,
pLS-RESOLFT is:

- loops: time (`timeLapsePoints`, `timeLapseDelayUs`), then cycle
  (`cycleSteps` on the readout device), then readout plane (`roSteps`);
- each step: on, `delayAfterOn`, off, `delayAfterOff`, readout plus camera,
  `delayAfterRo`.

**Where a step program runs:**

- **On the stock TriggerScope firmware** only if it has the shape of one of
  the routines. It is then compiled to that routine's parameters; otherwise
  it is refused with the difference. This is the cloak's
  representable-subset pattern again.
- **On the NI-DAQ** any step program runs, densified into samples.

**What follows from that:**

- The Expert presets are the programs today's panels run.
- The six panels, their 612-line lifecycle and the repeated parameter blocks
  could later retire (this is the alternative to G4).
- EtSnouty would ask for an Expert preset run instead of emitting
  `sigRunScanTriggerScopePLSRMulticolor`.
- A RESOLFT program could also run on an NI-DAQ rig.

**Prerequisite:** read the firmware source. The pulse order inside the
RESOLFT routines is only there.

### 3.5 Data from trajectories

- **Point detectors need a stream mode.** They assemble rasters today
  (reshaping by `scan_samples`). A trajectory needs one value per
  measurement window (counts summed) plus each window's coordinates (the
  mean commanded position).
- **Live display.** The measurements are gridded onto a display grid: the
  pattern's bounding box and a pixel size, as a weighted 2-D histogram (sum
  divided by count). Empty bins show as empty. The grid is published as a
  `ScanFrame` with its `FrameGeometry`, so the viewer overlay and region
  drawing from SimplePointScan work on it.
- **Recording.** The gridded image is a reconstruction; the measurements and
  their coordinates are the data. The acquisition-order spec (draft 4)
  already calls spiral and Lissajous scans *tabulated geometry*: a
  coordinate table per sample. The proposal:
  - HDF5 and Zarr store the measurement stream, its coordinate table and the
    gridded image;
  - OME-TIFF stores the gridded image only, with provenance saying it was
    gridded from a trajectory.

  This is a decision for Lenny (Q4).
- **Testable on the mock.** The mock APD's `mockSample` samples the
  synthetic sample at the waveform's positions. A spiral images the sample
  on the mock with no new simulation, so E1 can be tested headless, like the
  overview correlation test.
- **Cameras on a trajectory.** An exposure that integrates a circle (ring
  illumination, circular scanning) is one event per exposure (premise d of
  the spec): one frame per exposure, nothing to grid.
- **Commanded is not actual.** A galvo driven around a fast circle lags and
  loses amplitude. The `vel_max`/`acc_max` checks catch only the gross case.
  At first the coordinates are the commanded path, shifted by the phase
  delay. Measured positions (a galvo position-feedback AI channel) are a
  later option.

---

## 4. Phases

| Phase | Content | Behaviour change | How it is checked |
|---|---|---|---|
| G1 | Engine seam; `NidaqScanEngine`; the coordinator held by the master | none | Existing suites, the waveform goldens, the cloak contract tests |
| G2 | Capabilities; greying out in Advanced and Simple; refusals for devices on another engine; analog-only | Greyed parts; refusals instead of silent drops | An analog-only mock setup; "Advanced as today" |
| E1 | Expert trajectories on the NI-DAQ: pattern API, circle, spiral, Lissajous, point list; compiler and checks; APD/PMT stream mode; gridded live display | New panel only | Mock APD spiral images the sample (correlation), refusal tests, limit tests |
| E2 | Expert sequence programs on the NI-DAQ (densified); the RESOLFT presets | New panel only | Golden pulse timelines per preset |
| G3 | TriggerScope engine: Advanced stepped raster, Expert presets compiled to the routines | New engine | Simulated TriggerScope; then a rig |
| E3 | Recording trajectories (after Q4) | Recording gains a stream layout | Round trip HDF5/Zarr; OME gridded |
| G4 | TriggerScope panels become Expert presets, or move onto `SuperScanController` | Panels change | Rig (Snouty) |
| F1/F2 | Step-table firmware; multi-engine sync | Firmware | Rig |

---

## 5. Risks

- **NI-DAQ assumptions are spread** (§1.1): the 100 kHz clock, volts and the
  `Dev` filter. G1 keeps them inside the NI-DAQ engine. Stream engines can
  still take the designers' sampled volts. Step engines get the step
  program, not waveforms.
- **The scan description stays a raster.** Trajectories carry their own
  geometry. Every reader of `img_dims` and `scan_samples` must not assume a
  raster when the scan is a trajectory. That is about twenty places, listed
  by the G1 audit.
- **The TriggerScope firmware is outside the repository.** What happens
  inside a routine is not visible here.
- **No engine aborts mid-iteration today.** Long trajectories (a Lissajous
  with many periods) inherit "Stop ends after the iteration". SimplePointScan
  keeps that rule anyway. A hardware abort is the next step on
  `perf/advanced-scan-freeze`; it would become an engine capability.
- **Galvo dynamics on fast curves** (§3.5).

---

## 6. Questions for Lenny

1. **Teensy.** Which hardware is the "Teensy scan generator": the existing v4
   pulse firmware (digital only), or a board with DACs, like a TriggerScope?
   Is firmware work in scope, and who owns the Snouty TriggerScope firmware?
2. **TriggerScope with Advanced.** Is a stepped raster with cameras enough?
   Point detectors on a TriggerScope rig need NI-DAQ counters started by the
   TriggerScope, which is F2.
3. **ExpertScan priority.** Trajectories first (circle and spiral, NI-DAQ,
   point detectors), or sequence programs first (RESOLFT, cameras,
   TriggerScope)?
4. **Trajectory data.** Store the measurements, their coordinates and a
   gridded image in HDF5/Zarr, or the gridded image only?
5. **The six TriggerScope panels.** Should they eventually become Expert
   presets (retiring the panels and their lifecycle), or stay, with Expert
   beside them?
6. **Engine choice.** Is one engine per setup (`scan.engine`) enough for now?
   Two engines in one scan waits for F2.
7. **The Snouty firmware.** May it change: per-line pulse windows in
   `RASTER_SCAN`, STOP, a table command?

---

## 7. Found on the way (not fixed)

- **A laser on a non-NI-DAQ line silently never fires in an NI-DAQ scan.**
  `getTTLDevices` lists every laser with a `digitalLine`
  (`SetupInfo.py:753-769`), so the Advanced TTL table offers it.
  `runScan` skips any line without `Dev` in it, with no message
  (`NidaqManager.py:1254`). G2 turns this into a refusal.
- **A TTL-only NI-DAQ scan with point detectors would hang on hardware.**
  With no AO task, the DO task and the detectors' input tasks wait on
  `ao/StartTrigger`, which never fires (`NidaqManager.py:1394-1402`,
  `APDManager.py:1216`). Advanced always has AO for its scanned axes, so it
  does not reach this today.
- **`ScanManagerBase.makeFullScan` would store a tuple as the TTL signal
  dict** with the Advanced TTL designer, which returns `(dict, scanInfo)`
  for a full scan (`ScanManagerBase.py:153`, `ScanManagerAdvanced.py:73`,
  `AdvancedScanTTLCycleDesigner.py:331`). It is unreachable today: the
  Advanced controller builds its designers itself.
- **"Intra-pixel positioners movement"** is serialized and forwarded
  (`ScanControllerAdvanced.py:364-375`), but no designer reads it.
- **`ScanManagerAdvanced.getTTLCyclePreview*`** has no callers.
- **`ARCHITECTURE.md:186`** names scan controllers as pulse-generator
  consumers; none is.
