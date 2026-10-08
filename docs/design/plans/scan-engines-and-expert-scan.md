# Unleashing the scanner: one scan model, engines that say what they can do

**Status:** Plan, draft 5, for review. The direction is agreed (Lenny,
2026-10-08: "that sounds like what we're aiming for"). Draft 5 answers the
second review round, on draft 4. Nothing is implemented.
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

## Changes since draft 4: the second review round (2026-10-08)

The review answered open points 5-8 (§9) and found four rules that needed
correcting. The code claims were checked and hold:

- Beta warns, not refuses, when a whole-dwell exposure integrates the move
  (`BetaScanDesigner.py:257-262`);
- the PMT averages voltage samples (`PMTManager.py:1128-1135`).

| # | Finding | Change | Where |
|---|---|---|---|
| 1 (P1) | The stationary-only rule would change today's acquisitions: Beta exposes through the move, swept scans acquire while moving | Stationarity per device and per instant, offset transitions counted as motion. Each event's `during` is `stationary` or `any`. The Advanced adapter maps every existing window to `any`; new programs default to `stationary` | §2.1, §2.2, M0 |
| 2 (P1) | Accepted timing changes can still change the experiment (6-24 µs becoming 10-20 µs) | Durations realized as durations; critical separations realized relative to their reference; separate tolerances; repeated steps exact. Timing correction kept apart from detector normalization. A compatible realization for today's designers, reported | §2.6 |
| 3 (P2) | Replaying the engine's own releases does not test trigger handling | Trigger handling tested with independent arrival traces, releases checked as outputs; replay kept for execution after release. `skip` renamed `continue`; real skipping deferred | §2.4, §2.7, M1 |
| 4 (P2) | Count-rate gridding does not fit the PMT | Gridding by measurement type: count rate for counters, time-weighted mean voltage for analog, calibrated units when converted | §6, M4 |

## Changes since draft 3: the review (2026-10-08)

The review accepted the direction and asked for the contracts behind it to
be defined before implementation, above all before a plugin API is
published. Every finding was checked against the code and holds:

- `BetaScanDesigner` puts its move and settle inside each dwell;
- `PulseStep` is digital only;
- `run()` has no trigger policy;
- the Teensy floors ns to µs;
- `build_advanced_scan_layouts` carries per-detector masks, pulse counts
  and spans.

| # | Finding | Change | Where |
|---|---|---|---|
| 1 (P1) | "Stepped" does not say how devices move, so the Beta waveforms cannot be reproduced | `Motion`: transition shape, move and settle, placement (inside or extending the dwell), line return, origin and restore. Stationary interval; limits on the combined output; offsets are motions. The Beta goldens are captured first; if they cannot be matched exactly, Beta stays a compile path | §2.1, §2.2, §2.7, M0 |
| 2 (P1) | External waits contradict an absolute timeline | Segments with barriers; `interpret(program, inputs)` with an input trace; conformance re-interprets with the logged releases; outputs during a wait, timeout, stop during a wait | §2.4, §5.2 |
| 3 (P1) | Layouts must come from measurement events, not loops | `DetectorBinding` in M0; `layout_for(program, binding)` must equal `build_advanced_scan_layouts`; the arm-time frame-count check stays, fed from the same bindings | §2.5, §6, M0 |
| 4 (P1) | M2 needs detector assembly | Index-mapped assembly in APD and PMT moves into M2. Acceptance goes through the real mock detector and recording path: interleaved equals forward on the deterministic mock sample, and the recorded coordinates are right | §2.5, M2 |
| 5 (P2) | The pulse-generator interface cannot supply the capabilities | The adapter is limited to a stated digital subset; the contract gains `max_sequence_steps`, `timing_resolution_ns`, `max_repeats` and `run(trigger=…)`; refusals are tested | §5.3 |
| 6 (P2) | Timing quantization needs a policy | `RealizedSchedule` from every compile, with a declared acceptance policy. Preview, acquisition and metadata use it | §2.6 |
| 7 (P2) | The second engine comes too late | An independent in-process step engine in M1, not the interpreter: its own plan, its own executor, its log checked against the interpreter. The API stays experimental until M3 | §2.7, §5.0, M1 |
| — | Trajectory gridding needs units | Count rate (sum counts over sum realized integration time); NaN for unvisited; a coverage channel; the assumptions stated | §6, M4 |
| — | Multi-engine sync is understated | A start trigger is not a clock; wait propagation; coordinated stop. Model compatibility is a hypothesis until designed | §5.4, M6 |

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
class Motion:                     # how a device gets from one position to the next (§2.2)
    transition: str = 'smooth'    # 'jump', 'linear', 'smooth' (Beta's ramp)
    move_s: float = 0.0           # duration of a transition
    settle_s: float = 0.0         # rest after it before the device counts as stationary
    placement: str = 'in-step'    # 'in-step': at the end of the step, inside the dwell
                                  # (Beta); 'between-steps': extends the step instead
    return_s: float = 0.0         # end of a loop pass back to its first position (Beta's
                                  # return_time); serpentine traversal skips it
    origin: str = 'captured'      # positions relative to the one captured before the
                                  # scan (Beta's axis_position_before_scan), or 'absolute'
    restore: str = 'captured'     # where the device ends: 'captured', 'park', 'hold'

@dataclass(frozen=True)
class DeviceDrive:
    device: str                   # positioner name
    offset_um: float
    weights_um: Mapping[str, float] = field(default_factory=dict)
    # loop id -> µm per count; or a table:
    table_um: Optional[Sequence[float]] = None   # explicit positions per visit
    motion: Motion = Motion()     # filled from the device's setup when not given

@dataclass(frozen=True)
class TimelineEvent:
    device: str
    kind: str                     # 'ttl', 'level', 'offset', 'detector', 'wait', 'marker'
    start_s: float
    end_s: float
    value: float | None = None    # level (%, V), offset (µm)
    shape: str = 'hold'           # 'hold' or 'ramp' (levels and offsets)
    phases: Optional[frozenset] = None           # None: every phase
    during: str = 'stationary'    # 'stationary' or 'any' (motion-inclusive), §2.2
    still: Optional[tuple] = None # devices that must be stationary; None: every
                                  # device that moves in this step, offsets included
    id: Optional[str] = None      # for bindings and timing constraints

@dataclass(frozen=True)
class Separation:                 # a critical delay between two edges, §2.6
    after: tuple                  # (event id, 'start' | 'end')
    before: tuple                 # (event id, 'start' | 'end')
    tolerance_s: Optional[float] = None          # None: the policy default

@dataclass(frozen=True)
class Wait:                       # §2.4
    at: str                       # 'step' / a loop id: before each iteration of it
    trigger: str                  # input line, or 'time' for a fixed interval
    timeout_s: Optional[float]    # None: wait until stopped
    on_timeout: str = 'fail'      # 'fail', or 'continue' (run the gated step
                                  # without the trigger, recorded as a timeout)
    hold: str = 'idle'            # outputs while waiting: 'idle' (logical: illumination
                                  # off, positions held, no window open) or 'hold'
    resume_s: float = 0.0         # guard after release before the segment starts

@dataclass(frozen=True)
class DetectorBinding:            # §2.5
    detector: str
    events: str                   # which timeline 'detector' events it takes (by id)
    per_step: int = 1             # events per step it takes part in
    phases: Optional[frozenset] = None           # phases it takes part in
    window: str = 'event'         # 'event' (the event's window) or 'step' (the
                                  # stationary interval of the step)
    assembly: str = 'stream'      # 'stream' (a frame per event, cameras) or
                                  # 'image' (windows placed by index, point detectors)

@dataclass(frozen=True)
class ScanProgram:
    loops: tuple[Loop, ...]       # outermost first
    drives: tuple[DeviceDrive, ...]
    step_s: float                 # dwell of the innermost step
    timeline: tuple[TimelineEvent, ...]
    phases: tuple[Phase, ...] = ()               # the phase loop's per-phase states
    waits: tuple[Wait, ...] = ()
    separations: tuple[Separation, ...] = ()
    park: Mapping[str, float] = field(default_factory=dict)
    detectors: tuple[DetectorBinding, ...] = ()
    trajectory: Optional[Trajectory] = None      # §1.5: free path instead of loops
```

**Positions are in µm and times in seconds.** Volts, sample rates and table
formats belong to the engines and their compilers, never to the program.

### 2.2 How devices move: stepped is not enough

A position per step does not say how the device gets there. Today's
`BetaScanDesigner` already answers that, and its answer becomes the model's
`Motion`:

- **Inside the dwell.** The fast axis ramps to the next pixel with a smooth
  ramp of `move_time` and then rests for `settle_time`, both at the end of
  every dwell window. The rest of the dwell is stationary. A dwell no longer
  than the two together is refused (`BetaScanDesigner.py:246-263`).
- **Between lines,** each axis returns to its first position in
  `return_time` (`:265`, `:303`, `:336`, `:359`).
- **Relative to where the scan started.** Positions are offsets from
  `axis_position_before_scan`, the position captured just before the scan
  (`:158-239`). Parking and restoring follow from that.

What the model fixes, so an engine cannot read it two ways:

- **Stationarity is per device, per instant.** A device is stationary when
  its realized position (§2.2, last point) is constant.
  - With `placement: in-step`, a stepped drive is stationary during
    `[0, step_s − move_s − settle_s)` of each step.
  - With `between-steps`, it is stationary for the whole step, and the
    motion is added between steps.
  - A swept drive is never stationary.
  - **An offset's transitions are motion of its device.** Holding Z at
    +0.2 µm during 2-4 ms is a transition up, a settle, the hold, a
    transition back and a settle, each with the device's own `move_s` and
    `settle_s`. A stationary base drive does not make Z still during them.
- **Each event says whether motion is allowed (`during`):**
  - `stationary`: every device in `still` must be stationary throughout the
    event. The default for `still` is every device that moves in that step,
    offsets included.
  - `any`: motion-inclusive. The event happens wherever it is placed.
  - For an `offset`, `stationary` means the rest of the step's motion (the
    base drives, other devices' offsets) is still while it acts. Its own
    transitions are what it is.
- **Today's scans are motion-inclusive, and stay so.** `BetaScanDesigner`
  does not refuse a detector exposing through the move. It warns when less
  than half the dwell is stationary, "so a detector exposing for the whole
  dwell integrates the move" (`BetaScanDesigner.py:257-262`). Swept scans
  acquire during the sweep by design.
  - **The Advanced adapter maps every existing window and pulse to
    `during: 'any'`,** so "Advanced as today" holds for what is exposed,
    not only for the AO waveforms.
  - **Beta's warning becomes a report warning** with the same meaning.
  - **New programs** (Expert, and Advanced's new options) default detector
    windows and offsets to `stationary`. Choosing `any` is explicit and
    shown.
- **Limits apply to the combined output.** The device's realized position
  is the loop drive, plus its offsets, plus every transition. Range,
  `vel_max`, `acc_max` and volts are checked on that, not on each part.

So "make the axis stepped" is necessary but not sufficient for an
intra-pixel move. The feasibility report checks two things:

- the offset, with its transitions, fits where it is placed within the
  device's limits;
- every `stationary` event that names the device lies outside the offset's
  transitions.

For example: "The Z offset needs 2.4 ms with its moves and settles; the
stationary part of this 5 ms dwell is 1.5 ms." Or: "The camera window
(stationary) overlaps Z's transition at 2.0-2.5 ms."

### 2.3 Examples in the model

- **Advanced raster with 2 line steps:**
  - loops `y` (count Ny), `phase` (count 2, at line level), `x` (count Nx,
    swept);
  - drives X ← x and Y ← y;
  - timeline: a TTL window per laser per phase, all `during: 'any'`;
  - bindings: each detector with its line-step mask and pulse count.

  This is exactly today's dicts.
- **MS-RESOLFT (2 cycles × 3 planes):**
  - loops `time` (n, with an interval), `cycle` (2), `plane` (3), all
    stepped;
  - drive RO ← `{cycle: dy, plane: 2·dy}`, with the stage's motion;
  - timeline: on pulse, wait, off pulse, wait, readout pulse with the
    camera exposure;
  - binding: the camera, one frame per step.

  The camera's layout carries the `cycle` and `plane` loops the
  reconstructor reads.
- **An oblique composite:**
  - loop `s` (N, stepped);
  - drives GalvoY ← `{s: a}` and PiezoZ ← `{s: b}`.
- **A Beta scan with intra-pixel moves:**
  - loops `y`, `phase` (2, line level) and `x` (stepped, dwell 5 ms, move
    0.5 ms, settle 0.5 ms);
  - timeline events `offset` on Z: +0.2 µm held during 1.0-2.5 ms in phase
    0, and a ramp from 0 to +0.4 µm over 0.5-3.5 ms in phase 1. Both lie
    inside the 4 ms stationary interval.

  Guillaume's model is the first event alone.
- **A spiral:** `trajectory` (a sampled path in µm) with measurement windows
  every 20 µs.

### 2.4 Waits and triggers

A program that waits for an external trigger has no absolute timeline until
the trigger arrives. The model therefore splits the timeline at each wait:

- **The timeline is a sequence of segments.** Each segment starts at a
  **barrier**: the run start, or a wait's release. Every time inside a
  segment is relative to its barrier.
- **`interpret(program, inputs=None) -> Timeline`:**
  - With no `inputs`, it returns the segments with their barriers
    unresolved.
  - With an input trace (the release time of each barrier), it returns
    absolute times.
- **Conformance tests trigger handling and execution separately:**
  1. **Trigger handling.** The test drives the engine's trigger input with
     an independently specified arrival trace (on a mock, the simulated
     input line) and checks the engine's barrier releases as **outputs**:
     - each release follows its arrival within the engine's declared
       latency;
     - no release comes before its arrival (an early release fails);
     - an ignored trigger is a release that never happens;
     - a timeout is handled exactly as `on_timeout` says.
  2. **Execution after release.** The interpreter re-interprets with the
     engine's logged releases, and the timelines must match. This tests what
     the engine does after a barrier, not whether the barrier was right.

  Replaying the logged releases alone would let an engine that releases
  early, or ignores a trigger, pass.
- **While waiting,** outputs follow `Wait.hold`. The default is `idle`,
  defined on **logical** device states:
  - illumination off;
  - positions held;
  - no detector window open;
  - markers inactive.

  "Off" is the device's logical off, not an electrically low line. The
  compiler maps it per device (§2.6a). A laser is never left on through an
  unbounded wait by accident. `hold` keeps the outputs exactly as they were
  at the barrier, and must be asked for.
- **Resuming.** On release, the engine sets the **segment-initial state**:
  the logical state the program defines at the start of the next segment,
  computed by the interpreter. It then waits the wait's `resume_s` (default
  0), a guard for devices that need time to switch on, such as a shutter.
  Then the segment's time 0 begins.
  - Nothing is restored implicitly. Illumination comes back because the next
    segment turns it on, not because it was on before the wait.
- **Timeout.** `timeout_s` with `on_timeout`:
  - `fail` ends the run as failed, with the reason;
  - `continue` releases the barrier without the trigger, runs the gated
    step, and records the release as a timeout in the run log and the
    metadata.

  Skipping the gated step (no data for it) is not offered. It needs recorded
  spans for missing events in the layout, which can come with event-driven
  programs (M6).
- **Stop during a wait.** Stop is honoured at once: the step has not started.
  Outputs go to the engine's safe state (logical: illumination off, no
  window open, positions held, then restored per `Motion.restore`). The run
  ends as stopped.
- **Stop elsewhere.** Stop is honoured at the next barrier or step boundary.
  "Between steps" is a step boundary or a wait, never a blocked step.
- **On the NI-DAQ** an external wait is refused: its counters need a clock.
  Fixed-interval waits (`trigger: 'time'`) are ordinary segments.

### 2.5 Detectors: bindings, events and layouts

Detectors that share the motion loops can still record different things:

- one camera takes two frames per step;
- another takes part only in phase 1;
- an APD assembles many windows into one image.

Each `DetectorBinding` says which events it takes, how many per step, in
which phases, over which window, and how they are assembled.

- **Layouts come from bindings and the program, not from the loops alone.**
  `layout_for(program, binding)` builds the detector's
  `AcquisitionLayout`.
- **Today's semantics are the acceptance bar.** `build_advanced_scan_layouts`
  already handles per-detector line-step masks, pulse counts per condition,
  repeat loops and recorded spans (`_acquisition_layout_source.py:488-601`).
  For every Advanced-shaped program, `layout_for` must produce the same
  layouts as `build_advanced_scan_layouts` does for the same dicts.
- **The frame-count checks stay.** The recording's arm-time
  `SCAN_POSITION_COUNT_MISMATCH` check (`RecordingManager.py:2282`) and the
  layout validation keep running. Their expected counts come from the same
  bindings, so the program and the check cannot disagree.
- **Assembly.**
  - `stream`: one frame per taken event, in acquisition order. The layout
    says where each frame belongs: interleaved cycles and planes, phases,
    repeats.
  - `image`: each taken window is placed at its index tuple in an image.
    For a raster in forward order this is today's reshape. For any other
    order it is index-mapped (M2, §7).

### 2.6 Timing as realized, not as requested

Every engine has a timing grid: 10 µs samples on the NI-DAQ at 100 kHz,
1 µs steps on the Teensy, whatever a plugin declares. Requested times have
to land on it.

Compiling returns a **`RealizedSchedule`**: every event, wait and step
period as requested and as it will happen. Preview, acquisition and recorded
metadata all use it. The plot shows what will run, the detectors integrate
what ran, and the file says what ran.

**How times are realized.** Rounding each edge on its own is not enough. A
pulse requested from 6 to 24 µs on a 10 µs grid becomes 10 to 20 µs: each
edge moves only 4 µs, but the illumination falls from 18 to 10 µs. So:

- **Durations are realized as durations.** Illumination pulses, levels and
  detector windows get their start rounded to the nearest grid point and
  their **duration** rounded to the nearest whole number of grid steps.
  Markers and clocks round their edges.
- **Critical separations are realized relative to their reference.**
  `Separation(after, before)` places `before` from `after`'s realized edge,
  not from the step start. Examples: the end of the off-switching pulse to
  the start of the readout, or a pulse to its camera window.
- **Repeated steps do not accumulate.** A step period is a whole number of
  grid steps, so every repetition starts exactly one period after the last.

**Acceptance: each quantity has its own tolerance.** There is no overall
percentage.

| Quantity | Default tolerance | Beyond it |
|---|---|---|
| Edge position (rounding) | ½ grid step. This is a property of the rounding, reported, not an acceptance criterion | — |
| Pulse, level and detector-window duration | 1 % of the requested duration. A duration that is a whole number of grid steps realizes exactly | Refused, with the nearest representable durations ("18 µs is not on the NI-DAQ's 10 µs grid: 10 µs or 20 µs") |
| Critical separation | ½ grid step | Refused, naming both events |
| Order and overlap | Exact: an order may not change, a gap may not close, pulses may not start to overlap, no event may leave its step, and a `stationary` event may not leave its stationary interval | Refused |
| Pulse or window below one grid step or the engine's minimum pulse | Not acceptable | Refused |
| Step period | Exact: must be a whole number of grid steps | Refused, offering the two nearest periods. Editors snap the dwell to the grid, as SimplePointScan's `snap_dwell_s` already does |
| Waits and intervals (fixed time) | ½ grid step each | Reported |
| Accumulated drift at repeated-step boundaries | Zero by construction. The schedule reports the total against the request, which is at most ½ grid step per segment from waits and intervals | — |

The tolerances are per program, with these defaults, and shown in the
report.

**Timing correction is not intensity normalization.** The realized schedule
is a fact about timing, the same for every detector. A detector normalizes
its own measurement by its own realized window (§6), and only that.
Illumination changes are never "corrected" by normalization: they are held
to the duration tolerance above and recorded as realized.

**Advanced as today (the compatible realization).** Today's designers
already realize: `BetaScanDesigner` rounds a dwell that is not a whole
number of samples **up**, with a warning (`BetaScanDesigner.py:266-271`).
A 25 µs dwell at 100 kHz runs as 30 µs, 20 % longer per pixel.

- An Advanced-shaped program compiled through today's designers is realized
  the way they realize, so the waveforms stay byte-identical. That covers
  the dwell and the TTL designer's sample rounding of pulse windows. The schedule
  reports it ("dwell 25 µs realized as 30 µs; the scan takes 20 % longer")
  and the metadata records the realized dwell. It is not refused.
- The Advanced dwell field shows the realized dwell. That is a display fix;
  nothing that runs changes.
- Everything else (Expert, Advanced's new options, plugin engines) uses the
  strict policy above.

#### 2.6a Logical and electrical states

The program speaks in logical states: illumination on or off, a level in %,
a window open or closed. Each engine's compiler maps them per device:

- **A TTL line** maps on to its active level. A new optional device key,
  `ttlActiveLevel` (`"high"` by default, `"low"` for an inverted line), says
  which. None exists today: every scan line is assumed active-high.
- **An analog level** maps through the device's own mapping (laser power %
  onto its `valueRange`). Logical off is the device's off value, which is
  not necessarily 0 V.
- **Positions** are held at their realized value.

Idle and safe states (§2.4) are defined logically. The engine's
capabilities declare how it reaches them (`safe_state`).

### 2.7 The reference interpreter

`interpret(program, inputs=None) -> Timeline` (§2.4) expands a program into
time-ordered device actions per segment. It covers every position, with its
transitions (§2.2), every edge and level, and the measurement windows, each
with its loop indices and coordinates.

**It is the meaning of the model, and it is used to:**

1. **densify for streaming engines** (the NI-DAQ): sample the realized
   schedule (§2.6) at the engine's rate;
2. **be the conformance oracle for execution:** an engine's log,
   re-interpreted with its recorded barrier releases, must equal the
   interpreter's timeline. Trigger handling itself is tested against
   independently specified arrivals (§2.4);
3. **simulate detectors' samples on mock setups.** The mock APD's
   `mockSample` images any program, interleaved and composite included.

It is **not** the simulated engine. An engine that executed by calling the
interpreter would be checked against itself and prove only the plumbing. The
step engine in M1 (§7) is an independent executor of its own compiled plan.

**Today's designers stay where they are proven.**

- **`GalvoScanDesigner`'s swept fast axis** (turnaround, flyback,
  `vel_max`) is not re-derived. A swept loop compiles through the existing
  designer, so today's waveforms stay byte-identical; the Galvo goldens
  exist (`test_galvo_signal_goldens.py`).
- **`BetaScanDesigner` has no goldens.** M0 captures them first, over its
  parameter space: centred and absolute, 1 to 3 axes, return times, move
  and settle. The interpreter's stepped path with Beta's `Motion` must then
  reproduce them. If an exact match cannot be reached, Beta stays a compile
  path, like Galvo. The plan says so instead of approximating.
- **The interpreter covers** stepped loops with motion, timelines, phases,
  composites, waits and trajectories.

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
    timing_grid_s: float                   # the grid edges land on (§2.6)
    min_pulse_s: float
    waits: frozenset                       # 'time', 'start-trigger', 'step-trigger'
    sync_in: frozenset                     # 'start' (a start trigger) and/or
                                           # 'clock' (an external sample clock), §5.4
    safe_state: str                        # what outputs do on stop and while idle
    abort: str                             # 'none', 'step-boundary', 'immediate'
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

- **The public base class and its data types** are exported, first as
  `imswitch.pluginapi.experimental`. Nothing published there is a
  compatibility promise.
  - **M3 is the earliest point** at which the API can become the stable
    `imswitch.pluginapi`.
  - **What decides readiness** is evidence, not the date: both engines (the
    NI-DAQ and the M1 step engine) must pass the conformance suite and the
    acquisition tests through the real mock detector and recording paths.

  The exports are:
  - `ScanEngine`, which has:
    - `capabilities()` and `owns(channel)`;
    - `compile(program) -> (plan, realized, report)`: the plan in the
      engine's own form, the realized schedule (§2.6) and the feasibility
      report;
    - `prepare(plan)`, then a readiness answer once the detectors are armed;
    - `run(plan)` and `abort()`;
    - the prepared, started, released, done, stopped and failed signals.
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
  - Its report never calls a feature native that its run cannot do, and
    every refusal names the feature and the limit.
  - Running the conformance programs on its mock logs what it executed. The
    interpreter, given the log's barrier releases, must produce the same
    timeline (§2.4, §2.7).
  - Preparation, detector readiness, start, stop (also during a wait),
    failure and completion behave as the coordinator expects.
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
- **Run:** autonomous. Progress messages (`STEP i/n`) and barrier releases
  (`RELEASE k t`) go up the link.
- **STOP** is honoured at the next step boundary, and at once while waiting
  (§2.4). The board then goes to the declared safe state, in logical terms
  (§2.6a): illumination off, no window open, positions held. The host sends
  the electrical values each line takes for that, so polarity lives on the
  host. The board reports `STOPPED` and where it stopped.
- **Waits:** a step or loop may wait on the trigger input with a timeout,
  and outputs follow the wait's `hold` (§2.4). On release, the board sets
  the segment-initial state it was sent, waits `resume_s`, then runs the
  segment. It reports each release, so the trigger conformance tests (§2.4)
  can check them.
- **Introspection:** `*CAPS?` returns the capabilities (§3.1): DAC count and
  range, TTL count, timing resolution, limits. The engine reports what the
  connected board says, as the Teensy v4 `*IDN?` already does.
- **Conformance:** a log mode reports what the board executed, for
  comparison with the interpreter (§2.7).
- **The simulated board** is the M1 step engine's executor behind the
  protocol, not the interpreter. It executes the uploaded tables the way the
  firmware will, so it can disagree with the interpreter and the
  conformance test means something.

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
- **Before the firmware:** the simulated board lets everything above it be
  built and tested: the protocol engine, Expert programs on a "step board",
  and the reports.

### 5.3 The Teensy, the representative digital-only engine

The lab's Teensy is digital only (decision 1), and that is the point: it
stands for the boards people have.

- **One adapter, `PulseGeneratorScanEngine`, turns any
  `PulseGeneratorManager` into a scan engine for a digital subset of the
  model, and refuses everything else with the reason.**
- **What today's contract can and cannot say.** `PulseGeneratorManager`
  declares `n_digital_channels`, `min_pulse_width_ns`, `jitter_ns`,
  `supports_hw_trigger_in` and `supports_analog`
  (`PulseGeneratorManager.py:73-110`). That is not enough:
  - `supports_analog` means a static `setAnalog` works. A `PulseStep`
    carries only digital states (`PulseGeneratorManager.py:23-38`), so no
    sequence can hold a level.
  - `run(n_reps, blocking)` has no trigger policy, although
    `supports_hw_trigger_in` describes one.
  - Sequence capacity (the Teensy's 256 steps), timing granularity (1 µs)
    and the repeat limit live only in the Teensy driver.
- **The contract is extended with optional properties**, conservative when
  absent:
  - `max_sequence_steps` (absent: refuse any program that needs the number);
  - `timing_resolution_ns` (absent: `min_pulse_width_ns`);
  - `max_repeats`;
  - `run(..., trigger='none' | 'start')`, offered only when
    `supports_hw_trigger_in`.

  The Teensy and PulseStreamer managers implement them.
- **The representable subset.** The adapter accepts a program only when all
  of these hold:
  - it has no drives;
  - its timeline holds only TTL and marker events;
  - its waits are at most one start trigger, and only on a board that takes
    one;
  - after compressing runs of identical states, it fits `max_sequence_steps`
    with one outer repeat count within `max_repeats`;
  - its realized schedule passes §2.6 on the board's grid.

  Natively, that covers pulse programs, such as camera and laser switching
  on a widefield RESOLFT setup.
- **Refusals name the feature and the limit:**
  - "Z needs an analog output; the Teensy has none."
  - "The 488 power level is an analog event; pulse generators play digital
    states only."
  - "This timeline needs 412 distinct steps; the Teensy holds 256."
  - "Waiting before every step needs a per-step trigger; the Teensy takes
    none."
- **The tests cover refusals as well as successes:** each refusal above,
  plus repeated sequences at the capacity edge (256 steps, and 257).
- **A program that also needs the scanner's clock** waits for §5.4.
- **The same adapter serves `PulseStreamerManager`** (8 digital lines,
  8 ns pulses, a trigger input) and any future pulse-generator plugin.
  Pulse generators are not yet registry-resolved, and `MasterController`
  builds only the Teensy (`MasterController.py:40-47`). Registry resolution
  for `pulse_generator` comes with M1.

### 5.4 Several engines in one scan (later, a hypothesis)

**The goal:** one program spanning engines, for example NI-DAQ galvos with
TriggerScope piezo steps, or Teensy-gated lasers on an NI-DAQ scan. That
the model needs no change for it is a hypothesis until these contracts are
designed:

- **A shared start trigger is not a shared clock.**
  - A start trigger aligns the starts. The clocks then drift apart by their
    ppm difference over the run: a few µs per second, a pixel within a long
    scan.
  - A shared sample clock (or a reference clock both lock to) keeps them in
    step. It is wired differently and not every board can take it.

  The capability model declares which an engine accepts, and the report
  states the drift bound a program would have.
- **Waits must propagate.** A wait on one engine has to stall the others, so
  one engine gates the rest, or the program is split at its barriers.
- **Stop and failure must be coordinated:** one engine stopping or failing
  stops all, into each engine's safe state, with one run outcome.
- **The compiler splitting the program** is the last piece, not the first.

---

## 6. Data

- **Each detector's layout comes from its binding and the program** (§2.5),
  with `build_advanced_scan_layouts`' semantics as the acceptance bar. The
  implemented acquisition-layout contract
  (`imcommon/model/acquisition_layout.py`) already has the loop kinds the
  model needs (`scan_x/y/z`, `cycle`, `plane`, `time`, `condition`,
  `repeat`), serpentine traversal and recorded event spans. There is then no
  per-panel geometry code, and no reconstructor that has to trust widget
  values.
- **Two gaps in the layout:**
  - A layout loop names one `device`; a composite drive needs a list.
  - General affine maps and tables need the spec's weights and explicit
    order. Until then, an interleave is described as `cycle` and `plane`
    loops, as now.
- **Trajectories (decision 4): the gridded image, under today's schema.**
  - A trajectory produces one measurement per window, over the window's
    **realized** integration time (§2.6).
  - **Gridding** is onto the pattern's bounding box at a chosen pixel size,
    and **depends on what the detector measures:**

    | Detector | Measurement per window | Bin value | Unit |
    |---|---|---|---|
    | Photon counter (APD, TimeTagger counts) | counts | Σ counts / Σ integration time | counts/s |
    | Analog (PMT, today averaging voltage samples per pixel, `PMTManager.py:1128-1135`) | mean voltage | integration-time-weighted mean voltage, Σ(V·t) / Σ t | V |
    | Analog with a calibration that converts it | calibrated quantity | the calibration applied to the weighted mean | the calibration's unit |

    Weighting by integration time keeps the value correct when windows
    differ in length, which a plain mean does not. The unit is recorded with
    the detector's image.
  - **The assumptions are stated with the result:** a linear detector, no
    dead-time correction, and position = the commanded position at the
    window's centre.
  - **Unvisited and zero are different.** A bin no measurement reached is
    NaN (the image is float). A measured zero is 0.
  - **Coverage is an auxiliary measurement** (decision 7):
    - each bin's total integration time, **in seconds**;
    - stored with its detector and its grid (the same geometry as the
      image), marked as auxiliary, with its unit;
    - so a reader can tell sparse from dark, and weight bins when combining.
  - The grid is published as a `ScanFrame` with its `FrameGeometry`, and
    recorded like any assembled scan image, with the coverage beside it.
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

1. **M0 first.** Everything stands on the model, and the model now includes
   the contracts the review found missing: motion, waits, detector bindings
   and realized timing. It is Qt-free and reviewable on its own. It proves
   the adapter on today's scans (Beta goldens, the layouts) before anything
   visible changes.
2. **M1 next, as the plugin interface, with two engines.** A seam built
   NI-DAQ-shaped first would be rebuilt for plugins. An interface used by
   one execution model is not yet hardware-neutral, so M1 also brings an
   independent step engine. Greying out is the capability model's first
   visible use. On the NI-DAQ, nothing else changes.
3. **M2 before Expert.** It is the smallest visible step on rigs that exist:
   NI-DAQ setups with stepped axes. Interleaving changes which pixel a
   measurement belongs to at once, so M2 brings the index-mapped detector
   assembly with it. It also finishes Guillaume's option, which does nothing
   today.
4. **M3 then.** Expert reuses M2's compiler and assembly. Its MS-RESOLFT
   preset runs on the NI-DAQ, the mock and the M1 step engine before any
   TriggerScope firmware exists.
5. **M4, trajectories.** They need the measurement mode and gridding
   (§6) but nothing from the step engines. They come after M3 because the
   Expert panel hosts the patterns.
6. **M5, the serial protocol and firmware, last.** The lab writes the
   firmware, later (decision 6). The M1 step engine's executor, put behind
   the protocol, is the simulated board until then.

| Phase | Content | Behaviour change | Checked by |
|---|---|---|---|
| M0 | `ScanProgram` with `Motion`, per-device stationarity and `during`, `Wait`, `Separation` and `DetectorBinding`; logical states and their mapping (§2.6a); the interpreter with segments and input traces (§2.4); `RealizedSchedule` with the strict and compatible policies (§2.6); `layout_for` (§2.5); the Advanced dicts ↔ program adapter (every existing window `during: 'any'`); **Beta goldens captured first** | none | Qt-free tests. Advanced-shaped programs: the interpreter reproduces the Beta goldens (or Beta stays a compile path, §2.7); `layout_for` equals `build_advanced_scan_layouts`; the realized schedule equals what today's designers play (compatible). Round trip as the cloak contract. Strict-policy cases: 6-24 µs on 10 µs refused (duration 18 → 20 µs), a critical separation held to ½ step, a non-grid period refused with its neighbours, a closing gap refused, an offset transition under a stationary window refused |
| M1 | `scan_engine` plugin kind under `imswitch.pluginapi.experimental`; `NidaqScanEngine`; **an independent in-process step engine** (its own compiled plan of tables and an event list, executed on a worker thread against a clock it advances, logging what it did); capabilities and the feasibility report; Advanced and Simple grey out from it; refusals for channels no engine owns; registry resolution for `pulse_generator` | Greyed parts; refusals instead of silent drops (§10) | Goldens; "Advanced as today"; an analog-only mock setup. The conformance suite on both engines: prepare, detector readiness (a mock camera triggered by the step engine's TTL events), start, stop (also during a wait), failure, completion. Trigger handling against independently specified arrival traces: on time, early-release and ignored-trigger mutants caught, `fail` and `continue` timeouts. The step engine's log against the interpreter |
| M2 | Advanced: per-axis order (forward, serpentine, interleave *k*), intra-pixel moves as `offset` events (Guillaume's fields, every line step, hold or ramp, refused on swept axes and when they do not fit the stationary interval, §2.2). **Index-mapped assembly** in APD and PMT (windows placed by their index tuple, not reshaped in acquisition order). Camera frame streams with the interleaved layout | New options, off by default; saved intra-pixel scans start to do what they say | **Through the real mock detector and recording path.** An interleaved acquisition of the deterministic mock sample must give the same assembled image as the forward scan. The recorded file's layout must place every frame and pixel at its true coordinates, read back through the layout reader and the SNOUTY restack. The offsets appear in the AO trace per phase |
| M3 | Expert panel: loops, drives (composite axes), phases, timeline, waits, presets (MS-RESOLFT first), the report and the realized schedule beside the editor; `PulseGeneratorScanEngine` on the extended pulse-generator contract (§5.3) | New panel | Mock APD and mock camera; layouts read by the SNOUTY reconstructor; the Teensy mock against the interpreter, refusals included (§5.3) |
| M4 | Trajectories (circle, spiral, Lissajous, script pattern); APD/PMT measurement mode; gridding by measurement type (count rate, time-weighted voltage), NaN for unvisited, coverage in seconds as an auxiliary measurement (§6); recording | New pattern kind | Mock APD images the sample along a spiral (correlation, as the overview test). Unequal windows give the same rates (APD) and the same voltages (PMT). Unvisited bins are NaN, measured dark bins 0. Units and coverage are in the file |
| M5 | Step-engine serial protocol; the M1 executor behind it as the simulated board; the built-in protocol engine; then the TriggerScope firmware (the lab) and a rig | New engine | Conformance logs against the interpreter; then a rig |
| M6 | Multi-engine design (§5.4: clocks, wait propagation, coordinated stop) and then sync; event-driven programs (EtSnouty); the optional raw measurement stream for trajectories | — | — |

---

## 8. Risks

- **Two definitions of a swept scan.** `GalvoScanDesigner` keeps the swept
  case and the interpreter does the rest. They meet where a program mixes a
  swept loop with phases or timeline events. The adapter must prove that
  today's Advanced scans come out byte-identical, and a mixed program that
  neither path can build exactly is refused, not approximated.
- **Beta may not be reproducible exactly** (§2.7). Then the stepped path has
  two definitions too, Beta's and the interpreter's. Intra-pixel moves on a
  Beta axis must then compose with Beta's waveform, and the M0 goldens
  decide which way.
- **The raster scan description has about twenty readers** (APD, PMT,
  TimeTagger, recording, layouts, `scan_frame`). Programs that are not
  raster-shaped need those readers to take the binding's layout and the
  realized schedule instead. Part of that lands in M2 (index-mapped
  assembly), the rest in M3 and M4.
- **Densifying long stepped scans on the NI-DAQ** can be large. The report
  states the cost; chunked streaming is the fix.
- **Firmware is a second code base.** The protocol keeps it small: no scan
  types, only loops, drives, events and waits. The interpreter is its test
  oracle, and the M1 executor its reference.
- **No engine aborts mid-iteration today.** The protocol adds STOP at step
  boundaries and during waits; the NI-DAQ abort is the open step on
  `perf/advanced-scan-freeze`.
- **Galvo dynamics on fast curves.** The commanded path is not the actual
  path. The coordinates are commanded at first; measured positions come
  later.

---

## 9. Open points for this review

**Answered in the second review round:**

| # | Point | Answer | Where |
|---|---|---|---|
| 5 | Outputs during a wait | Idle by default: positions held, illumination off, as **logical** states; resuming is defined | §2.4, §2.6a |
| 6 | Timing defaults | Revised: separate tolerances for edges, durations, critical separations, order and overlap, and step periods; no overall percentage | §2.6 |
| 7 | Coverage | Yes: integration time in seconds, with its detector and grid, as an auxiliary measurement with its unit | §6 |
| 8 | Experimental API | Yes: M3 is the earliest point; readiness is both engines passing conformance and the acquisition tests | §5.0 |

**Still open (from drafts 3 and 4):**

1. **The setup format for engines** (§5.0): a `scanEngines` map resolved
   through the registry, the NI-DAQ engine implied by the `nidaq` section
   when the map is absent, and `scan.engine` naming the engine a panel runs
   on.
2. **Composite axes are Expert only.** Advanced gains order and intra-pixel
   moves (M2) but keeps one device per axis row.
3. **What M2 refuses.** An interleaved order or an intra-pixel move on a
   swept axis is refused with the remedy "make the axis stepped". An offset
   is also refused when it does not fit with its transitions, or when its
   transitions overlap a `stationary` event (§2.2).
4. **The gridded-image caveat** (§6): re-gridding later needs the raw
   measurements, which stay an option for M6.

**New in draft 5:**

5. **`ttlActiveLevel`**, a new optional device key (§2.6a), so a logical
   "off" is right on an inverted line.
6. **The compatible realization** for Advanced-shaped programs on today's
   designers (§2.6). Today's rounding is reported and recorded, never
   refused, and the Advanced dwell field shows the realized dwell.
   Everything new is strict, and a scan that uses one of Advanced's new
   options moves onto the strict policy as a whole, because it is compiled
   by the program compiler. The panel then snaps its dwell to the grid
   instead of rounding it up.
7. **The duration tolerance default is 1 %.** With the duration-first
   rounding, only durations that are not whole grid steps can miss it, and
   the refusal offers the representable neighbours.
8. **`on_timeout` is `fail` or `continue`.** Skipping a gated step waits for
   recorded spans of missing events (M6).

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
- **Beta stretches dwells with only a warning.** A dwell that is not a whole
  number of samples is rounded up per pixel (`BetaScanDesigner.py:266-271`).
  A 25 µs dwell at 100 kHz runs as 30 µs, and the scan takes 20 % longer
  than the panel says.
- **Scan TTL lines have no polarity setting.** Every line is driven
  active-high, so an inverted laser or shutter input is on when ImSwitch
  thinks it is off.
- **The Teensy shortens pulses silently.** `TeensyPulseManager` converts
  `duration_ns // 1_000`, at least 1 µs (`TeensyPulseManager.py:197`), so a
  1.9 µs step plays as 1 µs.
- **`PulseGeneratorManager.run(n_reps, blocking)` has no trigger argument,**
  although `supports_hw_trigger_in` says "True if `run` can wait for an
  external trigger" (`PulseGeneratorManager.py:94-96`).

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
