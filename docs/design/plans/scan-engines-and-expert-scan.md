# Unleashing the scanner: one scan model, engines that say what they can do

**Status:** Exploration, draft 2, for review. Nothing is implemented.
**Date:** 2026-10-08
**Branch:** `docs/scan-engines-expert-scan`, off `feat/simple-point-scan`
(56ef43f0, which carries the scan cloak and the Advanced scan-freeze work).
File references are to this branch.

**Asked (Lenny, 2026-10-08):**

- **(a)** Run the Advanced scan, and SimplePointScan with it, on other scan
  hardware than the NI-DAQ: the TriggerScope, a Teensy scan generator. For a
  scanner that only takes analog signals, grey out the TTL parameters.
- **(b)** Add a further layer, ExpertScan, for more complicated patterns: the
  Snouty multi-scan RESOLFT schemes, and arbitrary curves such as a circle or
  a spiral.

## Changes since draft 1 (Lenny's review, 2026-10-08)

1. **The firmware follows the model, not the other way round.** The
   TriggerScope is specific to the lab. Its six fixed routines, and the
   raster's shared TTL window, show how far the firmware got, not what
   scanning should be.

   Draft 1 compiled scans onto those routines when the shapes matched. That
   is dropped as a design goal. The model is defined first. Then an *engine
   protocol* is defined, which the TriggerScope firmware is reprogrammed to
   (§5). The six routines stay only as long as their panels do.
2. **The model is the centre, and it is unleashed** (§1, §2):
   - interleaved orders (1, 3, 5, 2, 4, 6: the cycles and planes of OPM
     RESOLFT and of the SNOUTY de-interlacer);
   - intra-pixel steps beside line steps (Guillaume's work, half ported);
   - custom axes that combine devices;
   - the rest of what a scan can be.
3. **The capability model stays and gets sharper.** For each feature of a
   scan, the engine says whether it runs it natively, emulated, or not at
   all, and what would change that. The panels show exactly that (§3).

---

## 0. In brief

- **One scan model** (`ScanProgram`), Qt-free, in the vocabulary of the
  acquisition-order spec (draft 4):
  - nested **loops** (outermost first), separate from the **devices** they
    drive;
  - each device's position is an affine function of the loop counters, or a
    table;
  - a **step timeline** inside each innermost step: pulses, analog levels,
    positioner micro-moves, detector windows, waits;
  - **phases** generalise line steps to any loop level;
  - loops can be **stepped or swept**, and **trajectories** cover free
    curves.

  Interleaving, multi-sheet RESOLFT, serpentine, composite axes and
  intra-pixel moves are then not special features. They are ordinary values
  of the model.
- **One meaning, many executors.** A Python reference interpreter turns a
  program into the exact timeline of what every device does. It is used to:
  - densify step programs for the NI-DAQ;
  - simulate the engine for every mock setup;
  - act as the conformance oracle for firmware: a TriggerScope or Teensy
    running the engine protocol must produce the same timeline.
- **Engines declare capabilities.** Compiling a program for an engine gives
  a **feasibility report**: each feature the program uses is native,
  emulated (with its cost) or impossible (with the reason and the remedy).
  The panels grey out options from that report, and the run refuses from
  it. There is one authority.
- **Three panels on one model:**
  - **Simple:** the cloak over Advanced, unchanged.
  - **Advanced:** the raster editor it is today, plus interleaved axis order
    and intra-pixel moves.
  - **Expert:** the whole model: loops, composite axes, phases, step
    timeline, trajectories.
- **Recommended first steps:**
  1. M0, the model, the interpreter and the Advanced ↔ program adapter.
  2. M1, the engine seam and capabilities.
  3. M2, interleave and intra-pixel moves in Advanced on the NI-DAQ.
  4. M3, the Expert panel.
  5. M4, the engine protocol and new TriggerScope firmware.

---

## 1. What the scanner should be able to say

### 1.1 Interleaved orders: loops are not axes

**The scheme.** MS-RESOLFT on an OPM visits one axis in two nested loops:

```
for cycle in range(cycles):          # outer
    for plane in range(planes):      # inner
        position index = cycles * plane + cycle
```

With 2 cycles and 3 planes this visits 0, 2, 4, then 1, 3, 5. That is
Lenny's 1, 3, 5, 2, 4, 6, counted from 1.

**Where it lives today:**

- **The TriggerScope pLS-RESOLFT routine** drives one device from two loops:
  the *cycle* scan and the *RO* scan "apply to the same device"
  (`docs/gui.rst:1003-1006`). With an RO step of `cycles × cycle step`,
  the result is the interleave above.
- **The SNOUTY reconstructor** undoes it. `restack_interleaved`
  (`improcess/reconstructors/snouty/restack.py`) reads the `cycle` and
  `plane` loops from the acquisition layout (`snouty/metadata.py:31-80`).
  It refuses a layout whose plane loop is not immediately inside the cycle
  loop.

**The general rule.** The acquisition-order spec (§3.1 and its MS-RESOLFT
row, `z = cycles·plane + cycle`) already states it: loops are chronological
counters, and a device position is an affine map of them. A raster is the
identity map. An interleave is a two-loop map onto one device. Serpentine is
a traversal rule. An explicit permutation (bit-reversed, random) is a table.

### 1.2 Composite axes: one loop, several devices

One loop counter can drive several devices, each with its own coefficient
and offset. Examples:

- an oblique scan (a galvo and a piezo together);
- a stage scan with a de-scanning galvo;
- a tilt that follows a translation.

Each device keeps its own limits (range, `vel_max`, `acc_max`, voltage),
scaled by its coefficient. Two loops may drive the same device, as in §1.1;
their contributions add.

### 1.3 Phases: line steps at any level

**Today.** A *line step* repeats each fast line `n_linesteps` times, each
time with its own TTL enables, pulse windows and analog power.

**Generalised,** this is a *phase loop* that can sit at any level:

| Level | Meaning |
|---|---|
| Pixel | Pixel-interleaved multicolor |
| Line | Today's line steps |
| Frame | Frame-interleaved |
| Volume | Volume-interleaved |

Each phase sets device states and offsets. The layout already has a
non-positional loop kind for it, `condition`.

### 1.4 The step timeline, with intra-pixel moves

Inside every innermost step (a pixel, a camera exposure) there is a timeline
of the step's dwell, which may differ per phase. It holds:

- TTL windows, as Advanced has them now;
- analog levels (laser power, a modulator) and ramps;
- **positioner micro-moves:** a device offset by Δ µm during a window;
- detector windows (gate, exposure, trigger);
- waits for an external trigger;
- marker outputs (line and frame clocks).

**The intra-pixel moves were designed, and half ported.** Guillaume's
commit `1dcbbe2af` (2026-06-12) added positioners as devices in the
"Advanced Line Program". The panel holds per-line-step start, end and step
µm. `BetaScanDesigner` added the offsets to the scan waveform as square
windows inside each pixel, tiled along the line.

Only the widget half came to ImSwitch2 (`feebad5ec`): the fields are
serialized and forwarded (`ScanControllerAdvanced.py:364-375`), but no
designer reads them. The designer half is on upstream `testalab_scanDev`
and uses only the first enabled line step.

As Lenny says, this is easy where steps last milliseconds (stepped axes,
`BetaScanDesigner`) and hard on a galvo sweep. The capability report says so
per axis (§3).

### 1.5 Stepped or swept, and free trajectories

**A loop is stepped or swept:**

- **Stepped:** hold a position during each step.
- **Swept:** move continuously, with flyback or turnaround, as the
  smooth-galvo fast axis does today.

Interleaving, intra-pixel moves and per-step waits need stepped loops. A
swept loop needs a streaming engine.

**Free trajectories** (circle, spiral, Lissajous, a curve from a file or
from a few lines of Python) are the swept case with no loop structure: a
path, and measurement windows along it.

### 1.6 Time, triggers and detectors

- **Time:** time-lapse loops with intervals; waits between loops (the
  RESOLFT delay after a DAC step); steps released by an external trigger.
- **Detectors:** each detector binds to the events that produce its data.
  The **acquisition layout follows from the program's loops**, so a
  recording describes its own interleave and the SNOUTY reconstructor needs
  no widget values.
- **Later:** event-driven branches (EtSnouty starting a program on a
  detected event), adaptive orders, and multi-engine scans.

---

## 2. The scan model

### 2.1 Shape (sketch)

```python
@dataclass(frozen=True)
class Loop:
    id: str                       # 'cycle', 'plane', 'y', 'x', 'phase', 'time'
    kind: str                     # acquisition-layout loop kind
    count: int
    mode: str = 'stepped'         # or 'swept'
    traversal: str = 'forward'    # 'reverse', 'serpentine' (parity loops below)
    parity_loops: tuple = ()

@dataclass(frozen=True)
class DeviceDrive:
    device: str                   # positioner name
    offset_um: float
    weights_um: Mapping[str, float] = field(default_factory=dict)
    # loop id -> µm per count; or a table:
    table_um: Optional[Sequence[float]] = None   # explicit positions per visit

@dataclass(frozen=True)
class TimelineEvent:
    device: str
    kind: str                     # 'ttl', 'level', 'offset', 'detector', 'wait', 'marker'
    start_s: float
    end_s: float
    value: float | None = None    # level (%, V), offset (µm)
    phases: Optional[frozenset] = None           # None: every phase

@dataclass(frozen=True)
class ScanProgram:
    loops: tuple[Loop, ...]       # outermost first
    drives: tuple[DeviceDrive, ...]
    step_s: float                 # dwell of the innermost step
    timeline: tuple[TimelineEvent, ...]
    phases: tuple[Phase, ...] = ()               # the phase loop's per-phase states
    waits: tuple[LoopWait, ...] = ()             # time between iterations of a loop
    park: Mapping[str, float] = field(default_factory=dict)
    detectors: tuple[DetectorBinding, ...] = ()
    trajectory: Optional[Trajectory] = None      # §1.5: free path instead of loops
```

**Positions are in µm and times in seconds.** Volts, sample rates and table
formats belong to the engines and their compilers, never to the program.

### 2.2 Examples in the model

- **Advanced raster with 2 line steps:**
  - loops `y` (count Ny), `phase` (count 2, at line level), `x` (count Nx,
    swept);
  - drives X ← x and Y ← y;
  - timeline: a TTL window per laser per phase.

  This is exactly today's dicts.
- **MS-RESOLFT (2 cycles × 3 planes):**
  - loops `time` (n, with an interval), `cycle` (2), `plane` (3), all
    stepped;
  - drive RO ← `{cycle: dy, plane: 2·dy}`;
  - timeline: on pulse, wait, off pulse, wait, readout pulse with the
    camera exposure.

  The layout's `cycle`/`plane` loops are what the reconstructor reads.
- **An oblique composite:**
  - loop `s` (N, stepped);
  - drives GalvoY ← `{s: a}` and PiezoZ ← `{s: b}`.
- **A Beta scan with an intra-pixel move:**
  - loops `y` and `x` (stepped, dwell 5 ms);
  - timeline event `offset` on Z, +0.2 µm during 2-4 ms, phase 1 only.
- **A spiral:** `trajectory` (a sampled path in µm) with measurement windows
  every 20 µs.

### 2.3 The reference interpreter

`interpret(program) -> Timeline` expands a program into absolute,
time-ordered device actions:

- `t, device, value` for every position change, edge and level;
- the measurement windows, each with its loop indices and coordinates.

It is the meaning of the model, and it is used in three places:

1. **Densifying for streaming engines** (the NI-DAQ): sample the timeline at
   the engine's rate.
2. **Simulating an engine** for mock setups. The mock APD's `mockSample`
   then images any program, interleaved and composite included.
3. **Conformance:** a firmware engine replays a program in a logging mode,
   and its log must equal the interpreter's timeline (to its timing
   resolution).

**Advanced keeps its own designers for what it does today.**

- `GalvoScanDesigner`'s swept fast axis (turnaround, flyback, `vel_max`) is
  not re-derived. A swept loop compiles through the existing designer, so
  today's waveforms stay byte-identical (the goldens).
- The interpreter covers stepped loops, timelines, phases, composites and
  trajectories.
- Merging the two is a later question, not a prerequisite.

---

## 3. Capabilities and the feasibility report

### 3.1 What an engine declares

```python
@dataclass(frozen=True)
class EngineCapabilities:
    name: str
    analog: Mapping[str, AnalogOut]        # channel -> range, resolution, settle
    digital: tuple                         # drivable lines
    streaming: Optional[Streaming]         # rate, max samples, chunked or whole
    stepping: Optional[Stepping]           # min step, timing resolution, max loops
                                           # nested, max table entries, max events
                                           # per step, waits, trigger in
    detector_inputs: frozenset             # 'counter', 'ai'
    triggers_out: tuple                    # camera / marker lines
    abort: str                             # 'none', 'between-steps', 'immediate'
```

**Which engine drives a device** is named by the device's channel:

- `Dev…/` is the NI-DAQ;
- `Triggerscope/…` is the TriggerScope;
- the `teensyPulse.pinMap` names the Teensy's lines.

A program whose devices sit on two engines needs multi-engine sync (§5.3).
Until then it is refused, with the reason.

### 3.2 Feasibility, per feature

Compiling a program for an engine returns, for every feature it uses, one of
three answers:

| Answer | Example |
|---|---|
| **Native** | The NI-DAQ plays a swept X; a step engine evaluates the affine positions on board |
| **Emulated, at a cost** | The NI-DAQ densifies a 5 ms-dwell stepped scan: "62 M samples per channel, 0.5 GB, uploaded in 1.2 s" |
| **Impossible, with the reason and the remedy** | "X is swept by a galvo: an interleaved order needs stepped positions. Make X stepped (`smoothScan: false`)." / "The TriggerScope firmware does not run step programs: needs the engine-protocol firmware (§5.2)." / "405 is on the TriggerScope, the scan runs on the NI-DAQ: needs multi-engine sync." |

The features: swept loop, stepped loop, interleave/affine map, explicit
table, composite drive, phase loop at each level, timeline events by kind
(and their number per step), waits, external trigger, trajectory, each
detector kind, abort.

### 3.3 What the panels do with it

- **Each control maps to the feature it would use.** An impossible feature
  greys out its control, with the reason as the tooltip. An emulated one
  shows its cost.
- **The TTL question from draft 1** becomes one row: an analog-only scanner
  declares no digital lines. The TTL parts (line-step TTL rows, the Advanced
  Line Program, pulse editors, the plot's TTL option) are then greyed, with
  "This scanner has no digital lines: the scan does not switch lasers. Set
  them in the Laser panel." SimplePointScan hides its laser channels.
- **One authority.** The run compiles again and refuses from the same report,
  because a rule enforced in one place is not enforced. The greying is the
  courtesy.
- **Hardware → What this setup can scan…** shows the report for the setup's
  engines: every feature, native, emulated or impossible, and why. It
  answers "what can this rig do" before anyone builds a scan.

---

## 4. The panels

| Panel | Edits | Engine path |
|---|---|---|
| **SimplePointScan** | Overview / acquisition, as today | Cloak over Advanced, unchanged |
| **Advanced** | The raster, as today, plus: per-axis order (forward, serpentine, interleave *k*), intra-pixel moves (the half-ported line program, finished) | Today's designers for today's features (goldens unchanged); the new features through the program compiler; refused on a swept axis with the reason |
| **Expert** (`scanWidgetType: "Expert"`) | The whole model: a loops table (kind, count, stepped/swept, traversal, wait); a drives matrix (device × loop weights, offsets, tables); a phase table; a step-timeline editor (Advanced's sequence builder grown to positions, levels, detectors, waits); trajectory patterns | The program compiler; the feasibility report beside the editor |

**Expert also has:**

- **Presets:** MS-RESOLFT (cycles × planes), pLS-RESOLFT multicolor,
  LS-XY-RESOLFT, a Beta raster with an intra-pixel move, a circle and a
  spiral.
- **Pattern plugins** (built-in and drop-in, `param_spec` forms) that
  generate programs or trajectories.
- **A preview:** the path, per-device traces and the timeline, with the
  limits readout: peak speed and acceleration against `vel_max` and
  `acc_max`, voltage span, duration and samples.

**Relationships:**

- Expressiveness: Simple ⊂ Advanced ⊂ Expert.
- Advanced scan files open in Expert. The reverse holds when the program is
  Advanced-shaped; otherwise the scan opens on the Expert page with the
  reason (the cloak's rule).
- A simple cloak over Expert, for example "Simple RESOLFT", fits the
  existing `ScanCloak` bases.
- All three panels run on `SuperScanController`: one lifecycle.

---

## 5. Engines

### 5.1 NI-DAQ (streaming)

- **Native:** swept loops (today's designers), clocked counters and AI,
  camera and marker lines.
- **Emulated:** everything stepped, by densifying the interpreter's
  timeline at 100 kHz. That is cheap for ms dwells on small scans and costly
  for large ones. The report states the cost. Chunked streaming
  (regeneration) for long programs is a later engine capability, not a
  model change.
- **Not possible:** external-trigger waits. The counters need a clock.
- **Seam:**
  - `NidaqScanEngine` wraps `NidaqManager.runScan`.
  - The controllers stop calling the NI-DAQ manager directly: the relays at
    `basecontrollers.py:443-465`, `:1673` and `:1698`, and the progress bar
    at `ScanControllerAdvanced.py:160`.
  - The coordinator is held by the master and armed through
    `armWithStarter(engine.run, …)`.

### 5.2 The engine protocol: step engines, and the TriggerScope rewritten to it

**A small, versioned serial protocol for boards that step,** derived from
the model and not from today's firmware:

- **Upload:**
  - loops (count, wait), with the drives as affine weights per DAC channel
    (the board evaluates `offset + Σ weight × counter`, which is cheap and
    has no tables);
  - optional explicit tables per drive;
  - the step timeline as an event list per phase (start µs, end µs, line or
    DAC, value);
  - the trigger-in policy.
- **Run:** autonomous. Progress messages (`STEP i/n`) go up the link, and
  STOP is checked between steps.
- **Introspection:** `*CAPS?` returns the capabilities (§3.1): DAC count and
  range, TTL count, timing resolution, limits. The engine reports what the
  connected board says, as the Teensy v4 `*IDN?` already does.
- **Conformance:** a log mode replays the run's timeline for comparison with
  the interpreter (§2.3). The Python interpreter running behind the protocol
  is also the simulated board for mock setups and tests.

**The TriggerScope** gets a new firmware implementing this protocol (Lenny:
"worst case reprogram the TriggerScope firmware"). The six old routines
remain only while the old panels exist. EtSnouty then starts an Expert
program instead of `sigRunScanTriggerScopePLSRMulticolor`.

**The Teensy:**

- Its v4 pulse firmware is the digital subset of the protocol: `SEQ` steps
  are one timeline with no drives, at most 256 steps.
- A Teensy with DACs (a "scan generator") implements the whole protocol,
  roughly what a TriggerScope 4 board already is (to check on the Snouty
  board).
- As shipped, a Teensy can run pulse programs with no moving axes.

### 5.3 Several engines in one scan (later)

One engine starts the others: the NI-DAQ start trigger or sample clock goes
to a board's trigger input. Then one program can span engines, for example
NI-DAQ galvos with TriggerScope piezo steps, or Teensy-gated lasers on an
NI-DAQ scan.

The program needs no change; the compiler splits it. This needs `trigger
in` on the step engines, which is part of the protocol.

---

## 6. Data

- **The layout comes from the loops.** The implemented acquisition-layout
  contract (`imcommon/model/acquisition_layout.py`) already has the loop
  kinds the model needs (`scan_x/y/z`, `cycle`, `plane`, `time`,
  `condition`, `repeat`) and serpentine traversal. A program emits its
  layout directly: no per-panel geometry code, and no reconstructor that has
  to trust widget values.
- **Two gaps in the layout:**
  - A layout loop names one `device`; a composite drive needs a list.
  - General affine maps and tables need the spec's weights and explicit
    order. Until then, an interleave is described as `cycle` and `plane`
    loops, as now.
- **Trajectories** produce measurements plus coordinates. The display grids
  them, as a weighted 2-D histogram published as a `ScanFrame`. Recording
  stores the measurements and coordinates (the spec's *tabulated geometry*)
  plus the gridded image. Draft 1 §3.5 still holds; the question is Q4.

---

## 7. Phases

| Phase | Content | Behaviour change | Checked by |
|---|---|---|---|
| M0 | `ScanProgram`, the reference interpreter, the Advanced dicts ↔ program adapter (round trip, as the cloak contract) | none | Qt-free tests; Advanced-shaped programs densify to today's waveforms for stepped scans |
| M1 | Engine seam (`NidaqScanEngine`), capabilities, feasibility report; Advanced and Simple grey out from it; refusals for devices on another engine | Greyed parts; refusals instead of silent drops (§9) | Goldens; "Advanced as today"; an analog-only mock setup |
| M2 | Advanced: per-axis order (interleave) and intra-pixel moves (port the designer half through the interpreter; stepped axes only) | New options, off by default | Mock: an interleaved Z stack restacks to the true order; an intra-pixel offset appears in the AO trace |
| M3 | Expert panel: loops, drives, phases, timeline, presets (MS-RESOLFT first), report | New panel | Mock APD and mock camera; layouts read by the SNOUTY reconstructor |
| M4 | Engine protocol spec, simulated step engine (the interpreter behind the protocol), TriggerScope firmware rewrite (lab side), TriggerScope engine | New engine | Conformance logs; then a rig |
| M5 | Trajectories (circle, spiral, Lissajous, script), APD/PMT measurement mode, gridded display | New pattern kind | Mock APD images the sample along a spiral |
| M6 | Trajectory recording, multi-engine sync, event-driven programs | — | — |

---

## 8. Risks

- **Two definitions of a swept scan.** `GalvoScanDesigner` keeps the swept
  case and the interpreter does the rest. They meet where a program mixes a
  swept loop with phases or timeline events. The adapter must prove that
  today's Advanced scans come out byte-identical, and a mixed program that
  neither path can build exactly is refused, not approximated.
- **The raster scan description has about twenty readers** (APD, PMT,
  TimeTagger, recording, layouts, `scan_frame`). Programs that are not
  raster-shaped need those readers to take the program's layout instead.
  That is the larger part of M3/M5.
- **Densifying long stepped scans on the NI-DAQ** can be large. The report
  states the cost; chunked streaming is the fix.
- **Firmware is a second code base.** The protocol keeps it small: no scan
  types, only loops, drives and events. The interpreter is its test oracle.
- **No engine aborts mid-iteration today.** The protocol adds STOP between
  steps; the NI-DAQ abort is the open step on `perf/advanced-scan-freeze`.
- **Galvo dynamics on fast curves.** The commanded path is not the actual
  path. The coordinates are commanded at first; measured positions come
  later.

---

## 9. Questions for Lenny

1. **Teensy.** Is the "Teensy scan generator" a board with DACs that would
   run the whole engine protocol, or the existing pulse Teensy (the digital
   subset)?
2. **Point detectors on step engines.** Does a TriggerScope rig need
   APD/PMT data, which means the NI-DAQ counting started by the board (multi
   engine, M6), or cameras only for now?
3. **Order after M1.** Is M2 (interleave and intra-pixel in Advanced) first,
   or M3 (Expert with MS-RESOLFT), or M5 (trajectories)?
4. **Trajectory data.** Store the measurements, their coordinates and a
   gridded image in HDF5/Zarr, or the gridded image only?
5. **Guillaume's intra-pixel model** (offset windows per line step) for M2:
   keep it as he designed it, or go straight to the timeline's per-phase
   `offset` events, which also allow ramps and more than one line step?
6. **The firmware.** Who writes the TriggerScope firmware against the
   protocol? It decides how small the protocol must stay.

---

## 10. Found on the way (not fixed)

- **A laser on a non-NI-DAQ line silently never fires in an NI-DAQ scan.**
  `getTTLDevices` lists it (`SetupInfo.py:753`), so the Advanced TTL table
  offers it, but `runScan` skips any line without `Dev` in it, with no
  message (`NidaqManager.py:1254`).
- **A TTL-only NI-DAQ scan with point detectors would hang on hardware.**
  With no AO task, the DO task and the detectors wait on `ao/StartTrigger`
  (`NidaqManager.py:1394-1402`, `APDManager.py:1216`). Advanced always has
  AO for its scanned axes, so it does not reach this today.
- **The intra-pixel positioner fields are dead.** They are serialized and
  forwarded (`ScanControllerAdvanced.py:364-375`), but the designer half
  (`BetaScanDesigner`, upstream `testalab_scanDev`, `1dcbbe2af`) was never
  ported, so the option does nothing.
- **`ScanManagerBase.makeFullScan` would store the Advanced TTL designer's
  `(dict, scanInfo)` tuple as the TTL signal dict** (`ScanManagerBase.py:153`,
  `AdvancedScanTTLCycleDesigner.py:331`). It is unreachable today: the
  Advanced controller builds its designers itself.
- **`ScanManagerAdvanced.getTTLCyclePreview*`** has no callers.
- **`ARCHITECTURE.md:186`** names scan controllers as pulse-generator
  consumers; none is.

---

## Appendix: today's hardware, in short (from draft 1)

- **NI-DAQ:**
  - one 100 kHz clock (`NidaqManager.py:523-535`);
  - waveforms in volts through `conversionFactor`;
  - point detectors own NI counter and AI tasks started by `ao/StartTrigger`
    (`APDManager.py:1211-1219`);
  - no mid-iteration abort.
- **TriggerScope:**
  - Snouty firmware outside the repository, ASCII over RS-232, about 50 ms
    per parameter;
  - six fixed routines with scalar parameters and no tables; `RASTER_SCAN`
    fires TTL 0-3 in one window;
  - cameras only, no abort;
  - its controllers re-implement the scan lifecycle
    (`_triggerscope_scan_lifecycle.py`, 612 lines).
- **Teensy v4 pulse firmware:** 16 digital lines, 1 µs, 256-step sequences
  with one outer repeat, no DAC, no trigger in, STOP between steps. No scan
  uses it.
