# Unleashing the scanner: one scan model, engines that say what they can do

**Status:** Plan, draft 3, for review. The direction is agreed (Lenny,
2026-10-08: "that sounds like what we're aiming for"). Nothing is
implemented.
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

## Changes since draft 2: the decisions (Lenny, 2026-10-08)

| # | Question | Decision | Where |
|---|---|---|---|
| 1 | Which Teensy | The lab's Teensy is digital only, and it stands for a class of boards. Anyone should be able to connect what they have (other Arduinos, whatever exists) through the device-plugin interface. | §5.0 engines are plugins; §5.3 the Teensy as the representative digital-only engine |
| 2 | Point detectors on the TriggerScope | Cameras only, for now. | §5.2 |
| 3 | Order after the seam | Mine to choose. | §7, with the reasons |
| 4 | Trajectory data | The gridded image, under today's scan schema. A reconstructor recovers the positions later from the scan info. | §6, with one caveat (re-gridding) |
| 5 | Guillaume's intra-pixel model | Generalise it. | §1.4, §2.2 |
| 6 | Who writes the TriggerScope firmware | The lab, later. | §7: the protocol and firmware move to the end; the simulated step engine comes earlier |

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
- **Engines are plugins.** `scan_engine` is a device-plugin kind with a
  public base class, so anyone can connect a board. The NI-DAQ, the
  TriggerScope and the Teensy are the built-in examples. Any pulse generator
  becomes a digital-only engine through one adapter.
- **Three panels on one model:**
  - **Simple:** the cloak over Advanced, unchanged.
  - **Advanced:** the raster editor it is today, plus interleaved axis order
    and intra-pixel moves.
  - **Expert:** the whole model: loops, composite axes, phases, step
    timeline, trajectories.
- **Order** (§7):
  1. M0, the model, the interpreter and the Advanced ↔ program adapter.
  2. M1, the engine seam as the plugin interface, the capabilities and
     greying out.
  3. M2, interleave and intra-pixel moves in Advanced.
  4. M3, the Expert panel with MS-RESOLFT.
  5. M4, trajectories.
  6. M5, the step-engine protocol and the TriggerScope firmware (the lab,
     later).

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

**Generalised (decision 5).** Guillaume's model becomes the timeline's
`offset` events:

| His model | Generalised |
|---|---|
| A positioner joins the line program as a device | Any drivable device, positioners included |
| Start, end and step µm per line step | One or more `offset` events per phase, each with a start, an end, Δ µm and a shape: hold (his square window) or ramp |
| Only the first enabled line step counts | Every phase has its own events |
| Offsets tiled along the fast line | Offsets in every step of the stepped loops they apply to; a swept loop refuses them with the reason |

His panel fields map one to one onto `offset` events in the Advanced ↔
program adapter. A scan saved with them opens as it was meant. Today it is
silently ignored.

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
    shape: str = 'hold'           # 'hold' or 'ramp' (levels and offsets)
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
- **A Beta scan with intra-pixel moves:**
  - loops `y`, `phase` (2, line level) and `x` (stepped, dwell 5 ms);
  - timeline events `offset` on Z: +0.2 µm held during 2-4 ms in phase 0,
    and a ramp from 0 to +0.4 µm over 1-5 ms in phase 1.

  Guillaume's model is the first event alone.
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

**Which engine drives a device** is decided by the device's channel. Each
engine says which channels it owns (`owns(channel)`):

- `Dev…/` is the NI-DAQ;
- `Triggerscope/…` is the TriggerScope;
- the `teensyPulse.pinMap` names the Teensy's lines;
- a plugin engine names its own prefix, for example `"MyBoard/…"`.

Devices keep their `analogChannel` and `digitalLine` fields, so the setup
format for devices does not change. A channel no engine owns is reported as
such; today it is dropped silently (§10). A program whose devices sit on two
engines needs multi-engine sync (§5.4). Until then it is refused, with the
reason.

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
| **Advanced** | The raster, as today, plus: per-axis order (forward, serpentine, interleave *k*), and intra-pixel moves as generalised `offset` events (Guillaume's line program, finished: every line step, hold or ramp). One device per axis row; composite axes are Expert's (§9.2) | Today's designers for today's features (goldens unchanged); the new features through the program compiler; refused on a swept axis with the reason |
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

### 5.0 Engines are plugins (decision 1)

**`scan_engine` becomes a device-plugin kind,** beside `detector`, `laser`,
`positioner` and the others (`docs/design/DEVICE_PLUGINS.md`, manifest
`kind`).

- **The public base class and its data types** are exported by
  `imswitch.pluginapi`:
  - `ScanEngine`, which has `capabilities()`, `owns(channel)`,
    `compile(program) -> (plan, report)`, `run(plan)`, `abort()`, and the
    built, started, done and failed signals;
  - `EngineCapabilities`;
  - `ScanProgram` and the interpreter.

  A plugin is then a manifest entry plus one class:

  ```json
  {"id": "acme.arduino-scan", "kind": "scan_engine",
   "display_name": "Acme Arduino scan board",
   "python_name": "imswitch_acme.engines:ArduinoScanEngine",
   "mock_python_name": "imswitch_acme.engines:MockArduinoScanEngine"}
  ```
- **The setup file names its engines** in a new `scanEngines` map (name →
  `managerName`, `managerProperties`), resolved through the registry like
  any other device. A setup without the map gets the NI-DAQ engine from its
  `nidaq` section, so existing setups keep working. `scan.engine` chooses
  the one a scan panel runs on, until §5.4.
- **A board can join in three ways:**

  | Route | For | What it costs |
  |---|---|---|
  | Implement the step-engine protocol in firmware (§5.2) | A microcontroller board someone programs (Arduino, Teensy, ESP32, a TriggerScope) | No Python: the built-in protocol engine drives it, and `*CAPS?` says what it can do |
  | Write a Python engine plugin | A board with its own API (a vendor DAQ, a Red Pitaya, a LabJack, a PulseStreamer, an FPGA) | `compile` (program to the board's form, with the feasibility report) and `run` |
  | Be a pulse generator | Any `PulseGeneratorManager` (§5.3) | Nothing: one adapter makes it a digital-only step engine |
- **Every engine passes the same conformance suite**, like the pulse
  generator's 18 contract tests (`test_pulsegenerator_base.py`):
  - Its report never calls a feature native that its run cannot do.
  - Running the conformance programs on its mock produces the interpreter's
    timeline.
  - Stop, done and failure behave as the coordinator expects.
- **The config editor** gets the kind like the others:
  `setup_metadata.py` is the single source for kind, category and section.

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
"worst case reprogram the TriggerScope firmware"). The lab writes it, later
(decision 6).

- **Detectors:** cameras only, for now (decision 2). Its capabilities
  declare camera trigger lines and no detector inputs, so APD and PMT are
  not offered on it.
- **Until the firmware exists,** the six routines and their panels stay as
  they are. The engine layer does not wrap them.
- **When it exists,** EtSnouty starts an Expert program instead of emitting
  `sigRunScanTriggerScopePLSRMulticolor`, and the old panels can retire.
- **Before the firmware:** the simulated step engine (the interpreter behind
  the protocol) lets everything above it be built and tested: the protocol
  engine, Expert programs on a "step board", the reports.

### 5.3 The Teensy, the representative digital-only engine

The lab's Teensy is digital only (decision 1), and that is the point: it
stands for the boards people have.

- **One adapter, `PulseGeneratorScanEngine`, turns any
  `PulseGeneratorManager` into a scan engine.**
  - The capabilities come from the manager's own properties:
    `n_digital_channels`, `min_pulse_width_ns`, `jitter_ns`,
    `supports_hw_trigger_in` and `supports_analog`
    (`PulseGeneratorManager.py:73-110`).
  - `compile` turns a program's timeline into `PulseStep`s with a repeat
    count. The feasibility report states the board's limits: "a pixel cycle
    of 6 steps × 4096 pixels needs one repeat, fits the Teensy's 256
    steps."
- **A program with no drives** runs on it natively: pulse programs, for
  example camera and laser switching on a widefield RESOLFT setup.
- **A program with drives** is refused with the reason: "Z needs an analog
  output; the Teensy has none."
- **A program that also needs the scanner's clock** waits for §5.4.
- **The same adapter serves `PulseStreamerManager`** (8 digital lines,
  analog outputs, 8 ns pulses, a trigger input) and any future
  pulse-generator plugin. Pulse generators are not yet
  registry-resolved, and `MasterController` builds only the Teensy
  (`MasterController.py:40-47`). Registry resolution for `pulse_generator`
  comes with M1.

### 5.4 Several engines in one scan (later)

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
- **Trajectories (decision 4): the gridded image, under today's schema.**
  - A trajectory produces one measurement per window. The engine side grids
    the measurements as a weighted 2-D histogram (sum over count) onto a
    grid: the pattern's bounding box and pixel size.
  - The grid is published as a `ScanFrame` with its `FrameGeometry`, and
    recorded like any assembled scan image. The schema does not change.
  - The program goes into the scan metadata beside today's scan parameters,
    as one additive attribute. The interpreter recomputes every
    measurement's position from it, which is what a later reconstructor
    needs.
- **One caveat, so the choice is made knowingly.** The gridded image is a
  reduction:
  - measurements that land in one bin are averaged;
  - a bin no measurement reached is empty.

  From the image and the program, a reconstructor knows every position but
  not every value, so it cannot re-grid finer or correct the path, for
  example for galvo lag. If that is ever wanted, the measurements in
  acquisition order can be recorded as a second payload, a detector-frame
  stream, which the layout contract already has. That is an option, off by
  default, and not part of the first trajectory phase.

---

## 7. Phases

**The order (decision 3: mine to choose), and why:**

1. **M0 first.** Everything stands on the model. It is Qt-free and
   reviewable on its own. It proves the adapter round trip on today's scans
   before anything visible changes.
2. **M1 next, as the plugin interface from the start.** A seam built
   NI-DAQ-shaped first would be rebuilt for plugins. Greying out is the
   capability model's first visible use. On the NI-DAQ, nothing else
   changes.
3. **M2 before Expert.** It is the smallest visible step on rigs that exist:
   NI-DAQ setups with stepped axes. It puts the interpreter's densify path
   into real use behind a panel people know. It also finishes Guillaume's
   option, which does nothing today.
4. **M3 then.** Expert reuses M2's compiler. Its MS-RESOLFT preset runs on
   the NI-DAQ and the mock before any TriggerScope firmware exists. Layouts
   from loops replace per-panel geometry code.
5. **M4, trajectories.** They need a new detector path (measurement mode and
   gridding) but nothing from the step engines. They come after M3 because
   the Expert panel hosts the patterns.
6. **M5, the protocol and firmware, last.** The lab writes the firmware,
   later (decision 6). The simulated step engine lets the protocol engine be
   built and tested before a board runs it.

| Phase | Content | Behaviour change | Checked by |
|---|---|---|---|
| M0 | `ScanProgram`, the reference interpreter, the Advanced dicts ↔ program adapter (round trip, as the cloak contract) | none | Qt-free tests; Advanced-shaped stepped programs densify to today's waveforms |
| M1 | `scan_engine` plugin kind and `imswitch.pluginapi.ScanEngine`; `NidaqScanEngine` as its first contribution; capabilities and the feasibility report; Advanced and Simple grey out from it; refusals for channels no engine owns; registry resolution for `pulse_generator` | Greyed parts; refusals instead of silent drops (§10) | Goldens; "Advanced as today"; an analog-only mock setup; the engine conformance suite on the NI-DAQ mock |
| M2 | Advanced: per-axis order (forward, serpentine, interleave *k*) and intra-pixel moves as generalised `offset` events (Guillaume's fields, all line steps, hold or ramp; stepped axes only) | New options, off by default; saved intra-pixel scans start to do what they say | Mock: an interleaved stack restacks to the true order through the SNOUTY restack; the offsets appear in the AO trace per phase |
| M3 | Expert panel: loops, drives (composite axes), phases, timeline, presets (MS-RESOLFT first), the report beside the editor; `PulseGeneratorScanEngine` (the Teensy runs drive-less programs) | New panel | Mock APD and mock camera; layouts read by the SNOUTY reconstructor; the Teensy mock against the interpreter |
| M4 | Trajectories (circle, spiral, Lissajous, script pattern); APD/PMT measurement mode; gridded display and recording (§6) | New pattern kind | Mock APD images the sample along a spiral (correlation, as the overview test) |
| M5 | Step-engine protocol spec; the simulated step engine (the interpreter behind the protocol); the built-in protocol engine; then the TriggerScope firmware (the lab) and a rig | New engine | Conformance logs against the interpreter; then a rig |
| M6 | Multi-engine sync; event-driven programs (EtSnouty); the optional raw measurement stream for trajectories | — | — |

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
  That is the larger part of M3 and M4.
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

## 9. Open points for this review

Draft 2's six questions are answered (the table at the top). What is left
are proposals that want a yes or a correction:

1. **The setup format for engines** (§5.0): a `scanEngines` map resolved
   through the registry, the NI-DAQ engine implied by the `nidaq` section
   when the map is absent, and `scan.engine` naming the engine a panel runs
   on.
2. **Composite axes are Expert only.** Advanced gains order and intra-pixel
   moves (M2) but keeps one device per axis row, so it stays the raster
   editor people know.
3. **What M2 refuses.** An interleaved order or an intra-pixel move on a
   swept axis (a smooth galvo) is refused with the remedy "make the axis
   stepped", not approximated.
4. **The gridded-image caveat** (§6): re-gridding later needs the raw
   measurements, which stay an option for M6.

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
