# Unleashing the scanner: one scan model, engines that say what they can do

**Status:** Plan, draft 7. The direction is agreed (Lenny, 2026-10-08:
"that sounds like what we're aiming for"), and the review is closed with
draft 7's amendments. **M0 to M4 are implemented** on
`feat/scan-engines` (§7.1-§7.5).
**Date:** 2026-10-08
**Branch:** `feat/scan-engines`, off `feat/simple-point-scan` (56ef43f0,
which carries the scan cloak and the Advanced scan-freeze work): the plan and
every milestone, one commit each. File references are to this branch.

**Asked (Lenny, 2026-10-08):**

- **(a)** Run the Advanced scan, and SimplePointScan with it, on other scan
  hardware than the NI-DAQ: the TriggerScope, a Teensy scan generator. For a
  scanner that only takes analog signals, grey out the TTL parameters.
- **(b)** Add a further layer, ExpertScan, for more complicated patterns: the
  Snouty multi-scan RESOLFT schemes, and arbitrary curves such as a circle or
  a spiral.

## Changes since draft 6: the fourth review round, closing (2026-10-08)

The review accepted draft 6's fixes, answered the four open questions and
asked for three corrections, after which it closes. All are in §9. In
short:

- `on_timeout` defaults to `fail`;
- policy documents are versioned, immutable once published, recorded, and
  refused when unknown;
- preparation keys have units, semantics and three states, with an explicit
  legacy rule;
- hardware without deadlines is judged per compiled program;
- `strict-v1` requires exact edges;
- deadlines have an overrun rule;
- the legacy fallback applies only to recognized legacy Advanced files.

## Changes since draft 5: the third review round (2026-10-08)

The review accepted open points 1-6, rejected the 1 % default (7) under the
project's rule against magic numbers, and found three issues. The code claim
holds: Beta's default 2 ms move and 2 ms settle (`BetaScanDesigner.py:12-13`)
refuse a 25 µs dwell before any waveform is built, so draft 5's example was
wrong and is replaced.

| # | Finding | Change | Where |
|---|---|---|---|
| 1 | The resume guard lengthened pulses and could trigger cameras early | A scheduled preparation stage after each barrier. Only devices that need preparation change state in it, for reasons their configuration states. Timed illumination and detector triggers keep their scheduled times. The stage is in the realized schedule and in conformance | §2.4 |
| 2 | Exact dwells do not stop drift from repeated rounded waits | The complete repeat period of each loop (steps, motion, waits, preparation) is validated against the request; iterations are scheduled against absolute deadlines where the engine can; the run-wide error is computed and reported, not assumed | §2.6, §3.1 |
| 3 | Showing the realized dwell must not rewrite the requested scan | Requested values, realized schedule and `timingPolicy` are kept apart and persisted. The panel shows realized beside requested. Enabling a new option migrates visibly and nothing is snapped silently. Load → preview → save → reload test | §2.6, M0 |
| — | No magic numbers | Hardware numbers from capabilities and device configuration; tolerances only from named, versioned policies with units and rationale; derived bounds calculated; unknowns reported, not guessed; numbers and sources recorded. `strict-v1` is exact | §2.8, §2.6 |
| — | The 25 µs Beta example | Replaced: 4.025 ms runs as 4.03 ms, +1.3 s over 512 × 512 | §2.6, §10 |

Guessed numbers elsewhere in the plan went too:

- the pulse-generator timing resolution no longer defaults to the minimum
  pulse (§5.3);
- the multi-engine drift is derived from declared clock accuracies (§5.4);
- an invented upload time was dropped from an example (§3.2).

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
    # After release: a scheduled preparation stage, from device configuration (§2.4)

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
    timing_policy: str = 'strict-v1'             # a named, versioned policy (§2.6);
                                                 # persisted with the program, never inferred
    park: Mapping[str, float] = field(default_factory=dict)
    detectors: tuple[DetectorBinding, ...] = ()
    trajectory: Optional[Trajectory] = None      # §1.5: free path instead of loops
```

**Positions are in µm and times in seconds.** Volts, sample rates and table
formats belong to the engines and their compilers, never to the program.

**The program holds what was requested.** What will run is the realized
schedule (§2.6), derived from the program, the engine and the timing
policy. Neither overwrites the other.

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
- **Resuming: a scheduled preparation stage.** The segment after a barrier
  begins with a preparation stage, part of the schedule like any other.
  - **Only devices that need preparation change state in it,** and each
    needs it for a reason its configuration states:
    - a positioner whose segment-initial position differs from where it was
      held needs its `Motion` (move and settle);
    - a device with a declared preparation time needs that time: a shutter's
      opening delay, a laser's enable-to-emission delay.
  - **The stage lasts as long as its longest preparation.** Each device
    starts its preparation so that it is ready at the stage's end.
  - **Timed events are untouched.** Illumination pulses and detector
    triggers happen at their scheduled times in the segment. None of them
    is switched on early, and no pulse is lengthened by the preparation.
  - **Nothing is restored implicitly.** Illumination comes back because the
    segment schedules it, not because it was on before the wait.
  - **The realized schedule and conformance include the stage:** every
    preparation transition, with its start, duration and device, is checked
    like any other event.
  - **The device key `preparation`** has precise semantics and units.
    - It is the time from the engine's output transition that requests the
      device's state (the line's edge, or the command) until the device is
      **ready**, meaning it delivers that state as specified: a shutter
      fully open, a laser emitting at its set level.
    - It is in seconds.
  - **It has three states, never inferred from absence:**

    | Value | Meaning | Effect |
    |---|---|---|
    | `"notRequired"` | The device is ready at the transition | No preparation |
    | `{"durationS": 0.004}` | Known duration | Prepared for that long |
    | `"unknown"` | Required, but not known | Every program that needs this device after a barrier is refused as "preparation unknown" (§2.8) |

  - **An absent key is not zero.** In a setup with a `scanEngines` section
    (the new format), an absent key on a device kind that can need
    preparation (shutters, gated lasers) reads as `"unknown"`.
  - **The legacy rule is explicit:** `legacy-preparation-v1`. In a setup
    without a `scanEngines` section, every device without the key is
    `"notRequired"`. That is exactly what ImSwitch does today. The rule
    names itself as the value's source in the recorded metadata, so it is
    never mistaken for a measured zero.
  - Positioners need no key: their preparation is their `Motion`.
- **Timeout.** `timeout_s` with `on_timeout`:
  - **`fail`, the default,** ends the run as failed, with the reason;
  - **`continue`** releases the barrier without the trigger and runs the
    gated step. It is used only when selected explicitly. The run log and
    the metadata record that this step ran without its trigger.

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

**How times are realized.** Under `strict-v1` every time must already be on
the grid; the rules below apply where a policy allows a deviation, and
decide which deviation it is. Rounding each edge on its own is not enough. A
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

**Acceptance: exact unless a named policy allows otherwise.** Every
program names its timing policy, a named and versioned document (§2.8):

- **`strict-v1`, the default, is exact representability.**
  - Every duration, critical separation and repeat period must be a whole
    number of grid steps.
  - Every order and overlap relation must hold: an order may not change, a
    gap may not close, pulses may not start to overlap, no event may leave
    its step, and a `stationary` event may not leave its stationary
    interval.
  - **Every edge must be exact too,** markers and clocks included.
  - What is not representable is refused, with the representable neighbours
    offered ("18 µs is not on the NI-DAQ's 10 µs grid: 10 µs or 20 µs").
  - Rounding is never part of `strict-v1`. A policy that permits it is
    another, explicitly named policy. The ½-grid-step bound describes the
    error of nearest-grid rounding; it does not authorize rounding.
- **A policy may allow deviations,** for example a duration tolerance for a
  preset. It states each tolerance with its unit and its rationale, has a
  version, and is recorded with the acquisition. There is no built-in
  percentage.
- **`advanced-compatible-v1` is the compatibility policy** (below).

**Repeats and drift: the whole period, against absolute deadlines.** Exact
step periods are not enough: a wait or a motion that rounds, repeated every
step, still accumulates.

- **The complete repeat period** of every loop is the realized sum of its
  steps, motion, waits and preparation. It is computed and validated
  against the request: exact under `strict-v1`, within a policy's stated
  tolerance otherwise.
- **Where the engine can, iterations are scheduled against absolute
  deadlines.** Iteration *k* of a loop starts at `t₀ + k × requested
  period`, rounded once from the segment start rather than accumulated.
  - **The overrun rule:** the start-time error stays bounded only if every
    iteration, preparation and motion included, fits before the next
    deadline. The compiler validates that for every iteration. An iteration
    that would overrun is refused, with the iteration and the overrun.
  - An iteration is never truncated, and the next one never starts late, to
    make a deadline.
  - **External waits begin a new timing segment.** Deadlines restart from
    the release. No wall-clock schedule is kept across an external trigger,
    because none can be.
- **Hardware without deadlines is judged on the compiled program, not
  refused wholesale.** Such hardware repeats a fixed sequence: a pulse
  generator's repeat count, a waveform played N times.
  - A tolerant policy may still change pulses while the repeat period stays
    exact.
  - A changed period is acceptable only if the policy explicitly permits
    both its error per repeat and its accumulated error over the requested
    repeat count.
  - Otherwise that program is refused, with the calculated errors ("period
    +4 µs per repeat, +4.1 ms over 1024 repeats; policy `strict-v1` permits
    none").
- **The schedule reports the timing error over the whole run:** the largest
  deviation of any scheduled time and the deviation at the end of the run,
  accounting for every independently rounded interval. The bound of ½ grid
  step per segment only holds for deadline-scheduled segments; the report
  computes the bound, it does not assume it.

**Timing correction is not intensity normalization.** The realized schedule
is a fact about timing, the same for every detector. A detector normalizes
its own measurement by its own realized window (§6), and only that.
Illumination changes are never "corrected" by normalization: the policy
refuses them or records them.

**Advanced as today: the compatibility policy `advanced-compatible-v1`.**
Today's designers already realize, and the policy documents exactly how:

- `BetaScanDesigner` rounds a dwell that is not a whole number of samples
  **up**, per pixel, with a warning (`BetaScanDesigner.py:266-271`).
  - Its default move and settle (2 ms each, `BetaScanDesigner.py:12-13`)
    refuse any dwell of 4 ms or less before a waveform is built, so the
    case that occurs is a dwell such as 4.025 ms.
  - At 100 kHz that is 402.5 samples, which runs as 403 samples: 4.03 ms,
    5 µs longer per pixel, and 1.3 s longer over a 512 × 512 scan.
- The TTL designer rounds pulse windows to samples.
- Under this policy those roundings are what runs, so the waveforms stay
  byte-identical. They are reported ("dwell 4.025 ms realized as 4.03 ms;
  the scan takes 1.3 s longer") and recorded. They are not refused.

**Requested, realized and policy are kept apart, and all three persist:**

- **The program keeps the requested values,** for example the 4.025 ms
  dwell. Saving writes the requested values, never the realized ones, so
  loading, compiling again and saving again cannot drift.
- **The realized schedule is derived.** It is shown and recorded with the
  acquisition, but never written back into the program.
- **The policy is persisted with the scan** (`timingPolicy`), not inferred
  from its shape.
  - **A recognized legacy Advanced file without a policy uses
    `advanced-compatible-v1`.** Recognized means it parses as today's
    Advanced scan-state format, with its analog and digital dicts.
  - **A new-format program without a policy, or with a malformed one, is
    refused.** It does not silently gain compatibility exemptions.
  - Advanced saves write the key, as an additive field.
- **The Advanced panel shows the realized dwell beside the requested one**
  ("4.025 ms, runs as 4.03 ms"). It does not replace the field's value.
- **Enabling a new option migrates the scan to `strict-v1` visibly.** The
  panel says so and keeps every requested value. It reports every value the
  strict policy refuses, with its representable neighbours, and the user
  chooses. Nothing is snapped silently.
- **The test:** load → preview → save → reload on legacy Advanced files
  must give identical requested values, identical waveforms and an
  identical realized schedule. The saved file differs only by the added
  `timingPolicy`.

#### 2.6a Logical and electrical states

The program speaks in logical states: illumination on or off, a level in %,
a window open or closed. Each engine's compiler maps them per device:

- **A TTL line** maps on to its active level. A new optional device key,
  `ttlActiveLevel` (`"high"` by default, `"low"` for an inverted line), says
  which. None exists today: every scan line is assumed active-high.
  - **One mapping, used everywhere** the line is driven: scan output, manual
    control (the Laser panel's on and off), waiting, stopping and failure
    cleanup. The device owns the mapping, so no path can forget it.
  - A test drives an inverted line through each of these paths.
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

### 2.8 Where numbers come from

The project's rule against magic numbers (`magic-number-audit.md`: "a fact
that is right for the case it was written for, silently wrong for its
neighbour") applies to every number this plan uses:

- **Hardware quantities come from the engine's capabilities or the device's
  configuration:** timing grid, minimum pulse, sequence capacity, repeat
  limit, move and settle, preparation times, trigger latency and clock
  accuracy.
- **Experimental tolerances come from a named, versioned timing policy**
  (`strict-v1`, `advanced-compatible-v1`, or a preset's own), with units
  and a documented rationale. A constant with a name is not a policy.
- **Derived bounds are calculated, not chosen.** Half a grid step follows
  from nearest-grid rounding; a drift bound follows from the declared clock
  accuracies and the run time.
- **A missing capability is reported as unknown or unsupported, never
  guessed.** The features that need it are refused with "unknown: the
  engine does not declare its trigger latency" until it does.
- **The resolved values and their sources are recorded with the
  acquisition:** each number, its unit, and where it came from (engine
  capability, device key, policy and version, or derivation).

**Policy documents.**

- **Versioned, validated data beside the scan model:** one file per
  version, with a schema.
- **Tests pin each version's behaviour:** what it accepts, what it refuses,
  with which message.
- **A published version is immutable.** A change is a new version.
- **Presets reference a policy by identifier and version, or bundle one,**
  in the same schema.
- **An acquisition records the policy's identifier and its resolved
  contents.**
- **An unknown identifier or version is refused.** Another policy is never
  substituted.

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
    trigger_latency_s: Optional[float]     # None: unknown (§2.8)
    clock_accuracy_ppm: Optional[float]    # None: unknown; bounds multi-engine drift
    deadlines: bool                        # schedules iterations against absolute
                                           # deadlines (§2.6), or repeats a sequence
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
format for devices does not change. **Ownership must be unambiguous:** a
channel claimed by two engines is a setup error when the setup loads, and
the error names both engines and the channel. A channel no engine owns is reported as
such; today it is dropped silently (§10). A program whose devices sit on two
engines needs multi-engine sync (§5.4). Until then it is refused, with the
reason.

### 3.2 Feasibility, per feature

Compiling a program for an engine returns, for every feature it uses, one of
three answers:

| Answer | Example |
|---|---|
| **Native** | The NI-DAQ plays a swept X; a step engine evaluates the affine positions on board |
| **Emulated, at a cost** | The NI-DAQ densifies a 5 ms-dwell stepped scan: "62 M samples per channel, 0.5 GB", computed from the program and the engine's grid |
| **Impossible, with the reason and the remedy** | "X is swept by a galvo: an interleaved order needs stepped positions. Make X stepped (`smoothScan: false`)." The remedy is offered only when the device and the engine can step it. / "The TriggerScope firmware does not run step programs: needs the engine-protocol firmware (§5.2)." / "405 is on the TriggerScope, the scan runs on the NI-DAQ: needs multi-engine sync." |

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
  and outputs follow the wait's `hold` (§2.4). On release, the board runs
  the segment's preparation stage it was sent (§2.4), then the segment. It
  reports each release and each preparation transition, so the trigger and
  preparation conformance tests can check them.
- **Introspection:** `*CAPS?` returns the capabilities (§3.1): DAC count and
  range, TTL count, timing resolution, limits. The engine reports what the
  connected board says, as the Teensy v4 `*IDN?` already does.
- **Conformance:** a log mode reports what the board executed, for
  comparison with the interpreter (§2.7).
- **The simulated board** is the M1 step engine's executor behind the
  protocol, not the interpreter. It executes the uploaded tables the way the
  firmware will, so it can disagree with the interpreter and the
  conformance test means something.

**Specified** in `docs/devices/step-engine-protocol.rst` (M5, §7.6).

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
  - `timing_resolution_ns` (absent: unknown, so every timing check that
    needs it is refused as unknown, §2.8);
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

### 5.4 Several engines in one scan (M6)

**The goal:** one program spanning engines, for example NI-DAQ galvos with
TriggerScope piezo steps, or two step boards. The model needs no change for
it; the contracts below are what running it takes. (Draft 7 left them as a
hypothesis; M6 designed them, §7.7.)

**One engine leads; the others follow step by step.**

- **The leader** is the engine the panel runs on (`scan.engine`). It runs
  the whole program, so its timing is the program's -- including the moves,
  returns and preparation of devices it does not drive, which it waits for
  and does not write ("timing-only" drives).
- **A follower** is an engine that drives a device the program uses. Its
  trigger input is wired to a line the leader drives, and the setup says
  which: `scanEngines.<name>.triggerInput`. Without the wire it takes no
  part, and a program using its devices is refused, saying so.
- **The leader pulses that line at the start of every step.** A follower
  waits for the pulse before every step (a step-trigger wait) and runs its
  part of the step shifted by its declared trigger latency: an event at `t`
  in the step happens `t - latency` after its release, which is `t` after the
  leader's pulse; its step is `latency` shorter, so it ends with the
  leader's. An event earlier in the step than the latency cannot happen on
  the follower: refused, naming both times. Moves need no shift: they are
  placed from the step's end, which both share.

**Why every step, not once at the start.** A shared start trigger is not a
shared clock: two crystals drift apart by their combined accuracy times the
time since they last agreed. Synchronised once, a strict program would have
to fit its whole run into half a tick of drift -- 50 ms for two ±50 ppm
boards on a 10 µs grid. Synchronised every step, the drift is bounded by the
longest time between two pulses (a step, plus a return or a between-step
move):

    bound = (ppm_leader + ppm_follower) × 1e-6 × longest pulse interval

and the report checks it against half the finer of the two grid ticks: an
edge off by less than that still lands on its tick, which is the strict
policy's edge rule (derived, not a new tolerance). An engine that declares no
clock accuracy makes the bound unknown, and the program is refused as such.

**Waits propagate by themselves.** A wait is the leader's: while it waits it
sends no pulse, and every follower is held at its next step. A follower has
no waits of its own besides the pulses, and no preparation stage: its
positions for a step are written at the end of the step before, as always,
and a device's preparation after a barrier is the leader's to wait for.

**The pulse** lasts the longest minimum trigger pulse the followers
declare, rounded up onto the leader's grid (at least that long, so up is the
derivation); an undeclared minimum makes it unknown. The leader's timeline
gets it as a `marker` on the wired line at step time 0.

**Stop and failure: one run.** The leader and its followers run as one
composite engine with one outcome. **Stop** stops every engine (a follower
waiting for its pulse at once). The first engine to fail stops the others,
and the run fails with its reason. Done is every engine done. Each engine
goes to its own safe state.

**What runs where.**

- *Leaders:* step engines (the simulated step board and protocol boards).
  The NI-DAQ cannot lead yet: its scan drives only devices' lines, and the
  sync line belongs to no device. That needs `NidaqManager` to drive an
  engine line; until then it is refused with that reason.
- *Followers:* step engines with a trigger input (`step-trigger` waits).
  The NI-DAQ and the pulse generators cannot follow: they cannot wait for a
  trigger before every step.
- *Simulated wires:* between simulated engines the pulse is simulated -- the
  follower's trigger arrivals are the leader's pulse edges, on its own
  ticks. A real follower needs a real leader: a simulated one cannot pulse a
  wire, and is refused.
- *Later:* a shared sample clock (`sync_in: clock`, no drift at all), NI-DAQ
  leading, followers of followers, a program split at its barriers instead
  of per step.

**The compiler split** is per engine and per device: the leader compiles
the whole program with the followers' devices as timing only and the pulse
added; each follower compiles the program with only its devices, its steps
shortened and its events shifted by its latency, and a step-trigger wait.
Every engine realizes its share on its own grid under the program's policy,
so a time one of them cannot represent is refused by it.

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
  - **The reduction is recorded explicitly** as provenance with the image:
    - that it was gridded from a trajectory;
    - the grid;
    - the weighting (count rate or time-weighted mean);
    - that the raw measurements were not stored.
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

  **M6 recorded them as auxiliary streams instead** (§7.7): arrays in the
  detector's group with one row per recorded frame, beside the coverage.
  A detector-frame stream would need a loop kind for path windows, which the
  layout contract does not have (a window is not a `time` step: windows
  differ in length), and a second payload per detector through every
  storer. The rows carry their meaning as attributes instead: acquisition
  order, unit, and the per-window time and centre arrays beside them.

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
| M0 | `ScanProgram` with `Motion`, per-device stationarity and `during`, `Wait`, `Separation` and `DetectorBinding`; logical states and their mapping (§2.6a); the interpreter with segments and input traces (§2.4); `RealizedSchedule` with `strict-v1` and `advanced-compatible-v1` as versioned, schema-validated policy documents (§2.8), repeat periods and absolute deadlines, and the run-wide error report (§2.6); preparation stages (§2.4); number provenance (§2.8); `layout_for` (§2.5); the Advanced dicts ↔ program adapter (every existing window `during: 'any'`; requested values, realized schedule and `timingPolicy` kept apart); **Beta goldens captured first** | none | Qt-free tests. **Advanced-shaped programs:** the interpreter reproduces the Beta goldens (or Beta stays a compile path, §2.7); `layout_for` equals `build_advanced_scan_layouts`; the realized schedule equals what today's designers play; load → preview → save → reload of legacy files gives identical requested values, waveforms and schedule. **Strict cases:** 6-24 µs on 10 µs refused with 10/20 µs offered; a non-representable separation or period refused with its neighbours; a closing gap refused; an offset transition under a stationary window refused; a rounded wait repeated N times reported as run-wide error, or held by deadlines. **Preparation:** pulses and triggers keep their scheduled times after a wait; the preparation transitions are in the schedule. **Unknowns:** a missing capability is reported as unknown, never defaulted; preparation `"unknown"` refuses; legacy setups resolve preparation through `legacy-preparation-v1`, recorded as the source. **Policies:** an unknown policy identifier or version is refused; a new-format program without a valid policy is refused; each published policy version is pinned by its own tests. **Deadlines:** an iteration that would overrun its deadline is refused. The resolved numbers and their sources are recorded |
| M1 | `scan_engine` plugin kind under `imswitch.pluginapi.experimental`; `NidaqScanEngine`; **an independent in-process step engine** (its own compiled plan of tables and an event list, executed on a worker thread against a clock it advances, logging what it did); capabilities and the feasibility report; Advanced and Simple grey out from it; refusals for channels no engine owns, and a setup error for channels two engines claim; `ttlActiveLevel` through every path that drives a line (§2.6a); registry resolution for `pulse_generator` | Greyed parts; refusals instead of silent drops (§10) | Goldens; "Advanced as today"; an analog-only mock setup. The conformance suite on both engines: prepare, detector readiness (a mock camera triggered by the step engine's TTL events), start, stop (also during a wait), failure, completion. Trigger handling against independently specified arrival traces: on time, early-release and ignored-trigger mutants caught, `fail` and `continue` timeouts. The step engine's log against the interpreter |
| M2 | Advanced: per-axis order (forward, serpentine, interleave *k*), intra-pixel moves as `offset` events (Guillaume's fields, every line step, hold or ramp, refused on swept axes and when they do not fit the stationary interval, §2.2). **Index-mapped assembly** in APD and PMT (windows placed by their index tuple, not reshaped in acquisition order). Camera frame streams with the interleaved layout | New options, off by default; saved intra-pixel scans start to do what they say | **Through the real mock detector and recording path.** An interleaved acquisition of the deterministic mock sample must give the same assembled image as the forward scan. The recorded file's layout must place every frame and pixel at its true coordinates, read back through the layout reader and the SNOUTY restack. The offsets appear in the AO trace per phase |
| M3 | Expert panel: loops, drives (composite axes), phases, timeline, waits, presets (MS-RESOLFT first), the report and the realized schedule beside the editor; `PulseGeneratorScanEngine` on the extended pulse-generator contract (§5.3) | New panel | Mock APD and mock camera; layouts read by the SNOUTY reconstructor; the Teensy mock against the interpreter, refusals included (§5.3) |
| M4 | Trajectories (circle, spiral, Lissajous, script pattern); APD/PMT measurement mode; gridding by measurement type (count rate, time-weighted voltage), NaN for unvisited, coverage in seconds as an auxiliary measurement (§6); recording | New pattern kind | Mock APD images the sample along a spiral (correlation, as the overview test). Unequal windows give the same rates (APD) and the same voltages (PMT). Unvisited bins are NaN, measured dark bins 0. Units and coverage are in the file |
| M5 | Step-engine serial protocol; the M1 executor behind it as the simulated board; the built-in protocol engine; then the TriggerScope firmware (the lab) and a rig | New engine | Conformance logs against the interpreter; then a rig |
| M6 | Multi-engine design (§5.4: clocks, wait propagation, coordinated stop) and then sync; event-driven programs (EtSnouty); the optional raw measurement stream for trajectories | — | — |

---

### 7.1 M0 status (2026-10-08)

**Delivered,** Qt-free, in `imswitch/imcontrol/model/scan_program/`:

| Module | What it does |
|---|---|
| `program.py` | The model and its validation, which lists every problem |
| `motion.py` | The transition shapes. Beta's ramp now lives here and the designer calls it |
| `interpreter.py` | Expansion into steps, returns, moves, waits and preparation stages |
| `schedule.py` | Realization under a policy: changes, refusals with neighbours, run-wide error, deadlines with the overrun rule, order and overlap, per-device stationarity, separations, sources |
| `densify.py` | Samples for streaming engines |
| `policy.py`, `policies/` | `strict-v1` and `advanced-compatible-v1` as schema-validated documents, pinned by content hash |
| `preparation.py` | The three-state preparation key and `legacy-preparation-v1` |
| `advanced.py` | The Advanced adapter, the requested-as round trip, legacy-file recognition and the layout inputs |

**The acceptance results:**

- **Beta goldens were captured first** (`test_beta_signal_goldens.py`, ten
  cases), on unchanged code.
- **The interpreter reproduces every one of them** within the golden
  tolerance, under `advanced-compatible-v1`.
- **It also reproduces the Advanced TTL designer on the same scans,
  sample-exactly,** clocks included (four cases).
- So for stepped Advanced scans neither designer has to stay a compile path.
  The swept (Galvo) fast axis still does, by design.
- **Layouts.** `build_advanced_scan_layouts` fed from the program
  (`layout_inputs`) gives the same layouts as fed from the designers. The
  frame-count check gives the same verdict on the interpreter's TTL as on
  the designer's.
- Mutation checks were run on the return placement, the stationarity
  check, the phase masks, the compatible rounding, the overrun rule and
  unknown preparation; all are caught.

**Found by M0:**

- **The Advanced TTL designer's clocks are 10 samples, hard-coded**
  (`clock_len=10` in `__generate_all_clocks`): 100 µs at 100 kHz,
  whatever the line. Its window edges round half to even (Python's
  `round`). The compatibility policy documents both.
- **The layout repeat loop counts phases a detector is off in.** The
  controller counts pulses with every line step forced on
  (`_layoutPulseCounts`), so a phase the camera is off in can size its
  repeat loop. A program knows only the phases it takes part in. They differ
  only when a disabled phase has more pulses than every enabled one; this is
  pinned by `test_a_disabled_phase_does_not_size_the_repeat_loop` and is
  **open for M2**. *Decided and done (§7.8): the steps it takes part in.*

**Deliberate differences from today:**

- A step whose rounded move and settle leave no stationary sample is
  refused. Beta plays a ramp that folds into the previous pixel there; its
  own check runs only on the unrounded values. Recorded in the policy's
  rationale.

**Not yet done:**

- **Critical separations are checked, not yet placed** relative to their
  reference. Under `strict-v1` the two are the same, because every edge is
  exact. A tolerant policy would refuse where it should place.
- **Logical-to-electrical mapping** (§2.6a, `ttlActiveLevel`) belongs to
  the engines, in M1. Densify emits logical states. *Done in M1.*
- **The panel-level load → preview → save → reload test** waits for M2.
  The model-level round trip (the request kept verbatim) is in M0. *Done in
  M2.*

### 7.2 M1 status (2026-10-08)

**Delivered:**

| Where | What |
|---|---|
| `scan_program/engine.py` | `EngineCapabilities`, `FeasibilityReport` (native, emulated, impossible, unknown), `ScanEngine`, channel ownership (`setup_channels`, `assign_owners`) |
| `scan_program/conformance.py` | The interpreter's schedule as an engine log; trigger handling checked against arrival traces the test chooses |
| `managers/scan_engines/` | `NidaqScanEngine` (the NI-DAQ manager seen through the interface; its signals are the manager's own); `SimulatedStepEngine` (an independent executor of its own compiled plan, which never calls the interpreter); `loader.py` (engines from the setup) |
| Setup | `scanEngines` (registry kind `scan_engine`, built-ins registered), `scan.engine`, `ttlActiveLevel` and `preparation` on devices |
| Controllers | `SuperScanController` and the Advanced progress bar talk to the engine. A non-streaming engine is refused for the waveform panels, with the reason |
| Panels | Advanced greys out TTL devices the engine does not drive, and every TTL part on an analog-only scanner, and refuses a scan that fires one. Simple does not offer such lasers and hides its palette when none remain |
| Plugin API | `imswitch.pluginapi.experimental` |
| Docs | `docs/devices/scan_engines.rst`, `setupinfo-reference`, the plugin pages, the changelog |

**Checked:**

- The unit lane (Advanced as today; the Beta and Galvo goldens unchanged).
- Conformance on the simulated board:
  - its log equals the interpreter's for a raster with phases, an
    interleave, waits and preparation;
  - trigger handling is checked against independent arrivals, with an
    early-release mutant and an ignored-trigger mutant both caught;
  - `fail` and `continue` timeouts, stop at a step boundary and during an
    unbounded wait, failure, prepare and readiness (a mock camera counts its
    frames).
- On the NI-DAQ mock: lifecycle order and the feasibility report.
- `ttlActiveLevel` on every NI-DAQ path (scan DO, `setDigital`) and on the
  TriggerScope's manual path.

**The independent executor paid for itself at once.** The trigger check
caught a clock bug in the simulated board itself: in-step DAC writes moved
its segment clock without its wall clock, so waits began early. Replaying the
board's own releases would not have seen it.

**Moved:** registry resolution for `pulse_generator` moves to M3, beside the
`PulseGeneratorScanEngine` adapter that is its only consumer. Doing it in M1
would change the setup format and four registry tests for nothing that uses
it yet.

**Found by M1:**

- **NI-DAQ digital lines on a device not named `Dev…`** (`PXI1Slot2/port0/line1`)
  are dropped by today's `runScan` filter, like the TriggerScope lines.
  `NidaqScanEngine.owns` keeps that rule exactly, so such a line now greys and
  refuses instead of dropping silently. Whether the NI-DAQ should drive it is
  open. *Decided and done (§7.8): it drives every NI-DAQmx line.*
- **The TriggerScope's built-in scan routines drive their lines active-high
  whatever `ttlActiveLevel` says;** only manual on/off honours it. That is
  documented, and fixed by the protocol firmware (M5).
- **A Simple plan with no channel had zero line passes,** which crashed the
  TTL designer. Any user who took every laser out could reach it. It is now
  one line pass that fires nothing.

### 7.3 M2 status (2026-10-08)

**Delivered:**

| Where | What |
|---|---|
| `scan_program/advanced.py` | Per-axis order (`axis_order`: forward, serpentine, `interleave:k`); an interleave as a table drive, a serpentine as a traversal; Guillaume's intra-pixel fields as `offset` events per line step; `program_scan_signals` (the analog waveforms on Beta's sample structure, under `strict-v1`); `axis_visit_orders`; `scan_timing_policy` and `timing_note` |
| `scan_program/interpreter.py`, `program.py` | A serpentine alternates on the flat index of its parity loops, which must be the loops right outside it -- the acquisition layout's rule |
| `scan_program/schedule.py` | `realized_duration`, `format_seconds` |
| Detectors | `_visit_order.VisitOrder`: APD and PMT place each visit at its image index. The Swabian TimeTagger refuses an order other than forward (`require_forward`) |
| Layouts | A frame stream with an interleaved axis has `cycle` x `plane` loops (the SNOUTY order); a serpentine axis a serpentine traversal. Forward layouts are unchanged |
| Advanced controller | The program path for scans that use the new options (Beta only; a swept designer is refused with the reason); `timingPolicy` saved with the scan, an unknown one refused on load; the timing note |
| Advanced panel | The **Order** column (only the interleaves the pixel count allows; greyed on a swept rig); the timing note (strict timing, or the realized dwell beside the requested one) |
| Docs | `advanced-scanning.rst` (Order, intra-pixel moves, strict timing), the changelog |

**Checked:**

- Model level, without noise: a point detector reading a test object at
  the middle of every stationary interval of the program's waveforms, and
  placing lines through the visit order, images the object at every pixel's
  position -- for interleaves on x and y, serpentines on x (with line steps)
  and y, and combinations. The same lines placed in time order do not.
- Refusals with their reasons: a swept designer; an interleave the count
  does not allow; a serpentine on the outermost axis; a dwell off the clock
  (with its neighbours); an intra-pixel window without room for its moves.
- Intra-pixel moves appear in the AO trace in their line step only.
- A camera frame stream's layout gives every frame the position the
  waveform held at its trigger; SNOUTY reads cycles and planes from it and
  restacks into position order.
- On the panel: the order column, the strict-timing note, the realized
  dwell, an ordered scan saved and loaded back with identical waveforms, an
  unknown `timingPolicy` not applied, a swept rig's greyed column.
- **The legacy round trip** (deferred from M0): a legacy file loads,
  previews, saves and reloads with identical requested values and
  waveforms; the saved file differs only by the added `timingPolicy`. A
  previous order does not survive loading it.
- **End to end on the mock rig:** the simulated APD images the synthetic
  sample from the AO waveforms; an interleaved and serpentine 24 x 24 scan
  correlates with the forward one above 0.9, the same lines as read below
  0.5.
- **Through the recording path:** a camera records an interleaved scan
  through the gate, the worker, the HDF5 storer and the resolver; every
  frame resolves to the position held at its trigger, and SNOUTY restacks it.

**Found by M2:**

- **The interpreter's serpentine summed its parity ordinals.** With two
  parity loops (lines and line steps) that plays forward, back, back,
  forward; the acquisition layout alternates on the flat index. The
  interpreter now uses the layout's rule, and a program must name the loops
  right outside the serpentine, so every serpentine it plays can be
  recorded. No existing scan was serpentine.
- **Saved intra-pixel moves did nothing.** Every designer ignored them (the
  docs said so). They now move the stage on Beta: a saved scan with them
  does what it says, as the milestone table promised.
- **"Interleaved equals forward" is exact only without the mock's noise.**
  The mock APD draws Poisson counts and integrates the whole dwell, moves
  included, and a different order moves to different neighbours. Exactness is
  shown at the model level (above); on the mock, by correlation.
- **SNOUTY's reader refuses loops it does not place,** a held axis included.
  An interleaved frame stream leaves its held (one-position) scan axes out of
  the layout; they add no coordinate.

**Still open:**

- **The layout repeat loop** (§7.1): unchanged, still for Lenny. *Done, §7.8.*
- **Two interleaved axes in one frame stream** are refused: a layout has one
  `cycle` and one `plane`. Point detectors take them.
- **Orders on swept scans** wait for the Galvo compile path (§2.7).
- **The timing note** shows the dwell; the realized moves, settles and
  windows are in the schedule but not yet on the panel. *Done, §7.8.*
- **Rig validation:** an interleaved Beta scan on Snouty's stage, recorded
  and reconstructed with SNOUTY.

### 7.4 M3 status (2026-10-09)

**Delivered so far: the pulse-generator engine (§5.3).**

| Where | What |
|---|---|
| `managers/pulsegen/PulseGeneratorManager.py` | Optional capabilities, `None` when undeclared: `max_sequence_steps`, `timing_resolution_ns`, `max_repeats`, `run_triggers`, `stop_drives_low`, `stop_granularity` |
| `TeensyPulseManager` | Declares them from the v4 firmware: `*IDN?`'s step count (0 on v3, which plays no uploaded sequences), whole microseconds on the wire, `n_reps` in a `uint32_t`, no trigger input, `allChannelsLow()` on STOP, STOP checked between steps. `run(trigger=)` refuses `'start'` |
| `PulseStreamerManager` | Declares its 1 ns resolution; nothing else is in the repository, so the rest stays unknown |
| `managers/scan_engines/PulseGeneratorScanEngine.py` | The adapter: the digital subset's refusals, realization on the generator's grid, the shortest exactly repeating period as the step table, off levels after a run and a stop, the run in its own thread |
| Registry, editor, docs | Built-in `scan_engine`; schema, fixture, docs card (`devices/scan_engines.rst`) |

**Checked:** the mock Teensy's pin timeline equals the interpreter's
schedule, line by line (one frame repeated; phases that alternate); every
refusal of §5.3 by its message; the capacity edge (256 fit, 257 and 258 do
not, 256 repeated 50 times fit); a time off the 1 µs grid with its
neighbours; undeclared capabilities as unknown; the setup loader building it
from `teensyPulse` and `scanEngines`; a stop leaving every line off.
Mutation-checked: a repeat too many, a step a tick long and an ignored
active-low mapping all fail the tests.

**Found:**

- **The Teensy's STOP drives every channel low** (`allChannelsLow()` in the
  firmware), whatever the line's polarity. An active-low line on it would be
  switched on by every stop, so the adapter refuses one with that reason.
  Firmware that stops to each line's off level would lift it (M5).
- **On DONE the Teensy keeps its last step's state on the wire.** The
  adapter sets every line to its off level after the run.
- **The firmware parses numbers without an overflow check** (`parseUInt`
  multiplies into a `uint32_t`): a repeat count above 2^32 - 1 wraps
  silently. The adapter never sends one; M5's protocol should refuse it.

**Delivered in part 2 (d230caae5): the model under the Expert panel.**
Programs as JSON (`serialize.py`, `scan-program/1`, read strictly; no
timing policy, no program); presets (`presets.py`: MS-RESOLFT, a pulse
sequence, a stepped raster; every value a parameter, kept in
`requested_as`); the NI-DAQ engine compiles programs (realized on its own
clock, volts through `conversionFactor`, voltage limits); `scan_info.py`
(the `ScanInfoContract` of a regular raster, with its visit orders, or the
reason point detectors cannot assemble the program); `build_program_layouts`
(a frame stream has the program's loops, spans where a camera takes frames
in some steps only; a point detector an assembled image of a raster).

**Delivered in part 3: the panel.**

| Where | What |
|---|---|
| `ScanWidgetExpert` | Tabs for loops, drives (a weight column per loop, the motion), timeline, waits and detector bindings; step and timing policy; **Make…** with a form generated from the preset's fields (checked before it closes); the report; **Plot** |
| `ScanControllerExpert` | The program is authoritative (the tables are read back only after an edit); one compile, cached on the program and the captured positions, for the report, the run and the recording; NI-DAQ programs through the base lifecycle; step engines armed through the coordinator and ended by their done, stopped and failed signals; recording queries (positions, frames per position, dims, layouts); saved states with `requiredFeatures`; Advanced files opened as programs and saved back in the Advanced format until edited |
| `NidaqScanEngine.assess` | A scan clock the setup does not wire is reported as not played, as the NI-DAQ does today, instead of blocking |
| `ScanManagerExpert`, `MasterController` | The panel type, without signal designers |
| `expert_mock_scan_setup.json` | The panel without hardware |
| Docs | `expert-scanning.rst`, the panel lists, the changelog |

**Checked (part 3):** the panel's choices are the model's; an empty panel
says what to do; a preset fills the tables and the report (88 ms, 12 steps);
the tables read back as the same program and are stable; an edited cell
changes the program, a bad one is named; refusals reach the report; the
preset form reopens with its values and names what is missing. **An
MS-RESOLFT recording** through the recording manager reads back at its
positions and SNOUTY restacks it (2 x 3 x 2). **The mock APD images a
serpentine raster program** in place (correlation above 0.9). A program
point detectors cannot assemble is refused while they take part, naming
them. Saved programs load back; the Advanced panel refuses them with the
reason. **An Advanced file opens as a program that plays the same analog
waveforms and TTL lines**, is saved back in the Advanced format while
unchanged, and needs the Expert panel once edited. A file naming a missing
device is not applied. **A Teensy runs a pulse program from the panel** and
a laser on the NI-DAQ is reported as not driven by it.

**Found:**

- **The M2 interleave split carried a direction onto `cycle` and `plane`,**
  which a non-spatial loop may not have; with the directions the controller
  passes, the camera layout was invalid. Fixed in part 2, and the M2 test
  now passes directions.
- **Advanced scans always carry the TTL designer's clocks;** the NI-DAQ
  plays them only on configured lines. An opened Advanced file would have
  been refused on a rig without clock lines.
- **A preset's step is a float sum** (6 + 0.5 + 0.5 ms is
  0.007000000000000001 s); the tables keep what they show (7 ms). The
  panel's ms are divided back into seconds, the inverse of the display, so
  a value survives the round trip.

**Still open in M3:** registry resolution for `pulse_generator` (a second
pulse generator in one setup, a plugin's generator); **Hardware → What this
setup can scan…** (§3.3); the panel shows the selected engine only (choosing
among a setup's engines per scan is not offered); the timeline table has no
column for an event's `still` list, so editing the table drops it (a loaded
program keeps it until then). *The `still` column, registry resolution for
`pulse_generator` and **What this setup can scan…**: done, §7.8.*

### 7.5 M4 status (2026-10-09)

**Delivered: trajectories, measured and gridded.**

| Where | What |
|---|---|
| `program.Trajectory` | A free path instead of loops and drives: devices, pattern and its parameters, time on the path, approach, grid pixel; `step_s` is the measurement window; timeline events switch lines on the path (anchor `path`) |
| `scan_program/trajectory.py` | Patterns as functions of time (circle, spiral at constant angular or linear speed, Lissajous, points, a Python `path(t, duration)`); realization on an engine's clock under the program's policy (windows tile the path, the last may be shorter); approach and return as cubic Hermite curves that leave rest and join the path with its position and velocity; the limits check (`vel_max`, `acc_max`, undeclared = refused); `Grid`; `GridAccumulator` (count rate or time-weighted mean, NaN unvisited, coverage in s); the provenance recorded with the image |
| `NidaqScanEngine` | Compiles trajectories: volts, limits, the scan info with every window's start, length and grid bin, and the grid as the frame geometry |
| APD, PMT (`_trajectory_measure.py`) | A trajectory worker per detector reads the stream window by window (cumulative counts per window; mean voltage after the offset) and bins it; the image is float32; TTL multiplying is refused with a trajectory |
| `RecordingManager` | `recordingAuxiliary()` on a detector: attrs on its dataset and auxiliary arrays -- HDF5 (before SWMR), Zarr (in the detector's group), TIFF (MapAnnotation and a `_coverage.tif` sidecar) |
| Expert panel | Trajectory presets (one per pattern) with point, pair, code and optional-laser fields; the report's trajectory lines and limits readout; the plot; recording queries from the grid; the trajectory carried through table edits |
| `minmaxLevels` | Auto-levels over finite values: an empty bin no longer makes the contrast limits NaN |

**Checked:**

- **The mock APD images the sample along a spiral** (40 turns, 1 s,
  20 µs windows, 0.25 µm grid): correlation above 0.9 with the sample at
  the bin centres, below 0.5 three micrometres off. The corners are NaN; the
  coverage sums to the time on the path exactly.
- **Unequal windows give the same rates and voltages:** exactly in the
  model (1, 2 and 7 ms windows, one rate; a time-weighted mean); on the mock
  PMT, 20 µs and 70 µs windows (the last of a 200 ms path shorter) both give
  the mock's 1 V within five standard errors.
- **Unvisited bins are NaN, measured dark bins 0** (the accumulator).
- **Units and coverage are in the file**, for HDF5, Zarr and TIFF: the
  float image with its NaN, `measurement:unit` counts/s, the reduction, and
  the coverage in s summing to the time on the path.
- The approach joins the path without a jump in speed (a mutant whose
  approach ends at rest fails); off-grid windows and a missing approach are
  refused with the reason; limits refuse with the numbers; JSON round trip.

**Found:**

- **Beta's smooth ramp starts at full speed.** Fine between stepped pixels,
  but onto a moving path it is a jump in velocity, which the acceleration
  check rightly refused. The approach is a Hermite curve instead.
- **A constant-linear-speed spiral has an unbounded acceleration at its
  centre** and is refused on the mock galvos; the constant-angular one is
  the default in the panel's examples.
- **How well a trajectory images a sample depends on how densely it covers
  the grid** (correlation 0.82 with rings one bin apart, 0.96 at a quarter
  bin): that is the reduction, not an error, and the coverage shows it.
- **Auto-levels took `arr.max()`**, NaN for any image with an empty bin.

**Still open:** pattern plugins (drop-in patterns with `param_spec` forms;
the built-ins are a registry today; *drop-in trajectory patterns done, §7.8*); the optional raw measurement stream
(M6); trajectories on step engines (refused: they need a streaming engine);
the coverage of a recording with several frames is the first frame's.

### 7.6 M5 status (2026-10-09)

**Delivered: the step-engine protocol, its simulated board, and the
built-in engine that drives any board speaking it.** The TriggerScope
firmware is the lab's, later (decision 6); everything above it is built and
tested against the simulated board.

| Where | What |
|---|---|
| `docs/devices/step-engine-protocol.rst` | The specification, IMSWITCH_STEP/1, for firmware authors: framing, integer widths, `*IDN?`/`*CAPS?`, the program lines, what the board does step by step, the messages while it runs, stop and the safe state, `LOG?`, error codes, a worked example checked against the simulated board |
| `scan_engines/step_executor.py` | The M1 executor, out of `SimulatedStepEngine`, shared by both simulated boards; it reports to a listener (started, progress, release, preparation, done, stopped, failed) |
| `scan_engines/step_protocol.py` | The codec both ends share: plan to lines and back, capabilities, messages, the log; numbers checked against the protocol's widths |
| `scan_engines/step_board.py` | `SimulatedStepBoard`: the board end in process -- answers, checks a program as firmware does (channels, capacity, trigger input, widths), runs it on the executor in its own thread, reports, keeps its log |
| `StepProtocolScanEngine` | Capabilities from `*CAPS?`; channels `<engine>/dac<i>`, `<engine>/ttl<j>`; compile = the shared step compile, then the board's terms (µm to volts from `conversionFactor`, checked against the DAC and positioner ranges; polarity from `ttlActiveLevel`; safe levels); upload, run, follow, stop, `LOG?` back in the program's terms; serial or `"simulated"`; closed at shutdown |
| `StepEngineBase` | What the two step engines share: capabilities as fields, the host compile, which now uses the engine's own `assess` |
| Expert controller | A step engine's failure reason is kept for the run's failure; the lifecycle signal ends the run |

**Checked:**

- **Conformance through the wire.** Every conformance test of the
  simulated step board runs on the protocol engine too -- raster,
  interleave, the two-parity serpentine, releases against chosen arrivals
  (and the early and late mutants, as boards), timeouts, preparation, stop
  during an unbounded wait, the lifecycle -- with the positioners at 2 and
  0.5 µm per volt, so volts are not µm. Mutation-checked: weights sent
  unconverted fail eleven tests; parity loops sent in reverse fail the
  serpentine and the round trip.
- **The panel on a step board:** an MS-RESOLFT program on Z, two lasers
  and the camera wired to the board runs from the Expert panel, and the
  board's log matches the interpreter's schedule (twelve camera frames); a
  failure ends the run with the board's reason; a device on the NI-DAQ is
  refused by name, and so is a smooth transition.
- **The codec:** a decoded program encodes to the same lines; caps and logs
  read back; what a board must refuse it refuses with the code and reason
  (an unknown command, a run without a program, a channel it lacks, an
  event or a safe level on a DAC, a bad choice, a missing STEP, more steps
  than it holds, a number wider than the protocol's).
- **The host refuses before uploading:** a positioner without
  `conversionFactor` (unknown), volts beyond the DAC or the positioner
  (both ranges named), a power level, two devices on one line, a wait that
  does not idle, a loop count beyond 32 bits. A board on another protocol
  version, or silent, is unknown -- and asked again at the next compile.
- **Polarity and the safe state:** the active-low camera's line is sent as
  on 0, off 1; after a stop and after a run every line is at its safe
  level and the DACs hold.

**Found:**

- **The Expert panel's failure slot never got a reason** (M3): it listened
  to `sigScanFailed`, which no step engine emitted, and had it been, the
  slot would have ended the run a second time beside the lifecycle signal.
  The engines emit the reason now, and the slot only keeps it.
- **The shared step compile skipped an engine's own assessment** (it called
  `assess_program` directly), so a refusal only the engine knows -- a board
  that does not answer, a power level -- would not have stopped a compile.
- **A stop before the run starts cannot be RUN then STOP:** a short program
  finishes before the STOP arrives. The engine does not start it.
- **The Teensy's two firmware findings are protocol rules now:** numbers
  have declared widths and one that does not fit is refused, not wrapped
  (M3, `parseUInt`); a stop leaves each line at the safe level the host
  sent, not low (M3, `allChannelsLow`).

**Still open:**

- the TriggerScope firmware (the lab), and its first rig run;
- a runner that puts a real board through the conformance programs over its
  port (trigger tests need a signal on its trigger input) *done, §7.8;*
- a power level inside a step on a board DAC, which needs the laser's
  power-to-volt conversion on the host; *done, §7.8;*
- point detectors on a step board (cameras only, decision 2);
- a DAC's resolution in `*CAPS?`, so conformance can compare what a board
  can set rather than what it was told; *done, §7.8;*
- the progress messages are read but not shown; a board that falls silent
  in the middle of a run is noticed only when the link is closed; *both
  done, §7.8;*
- `expert_mock_scan_setup.json` has no step board; the tests build one.
  *Done: `expert_step_boards_mock_setup.json`, §7.8.*

### 7.7 M6 status (2026-10-09)

**Delivered, in three parts:**

| Part | Where | What |
|---|---|---|
| Raw measurements (§6) | `Trajectory.record_measurements`, the preset form's 'flag' field | Off by default. Each window's measurement in acquisition order, with its integration time and commanded centre, beside the gridded image |
| | `_trajectory_measure.py`, APD, PMT | The worker fills the values in place; the manager keeps a scan's values when the scan completes and attaches them to that scan's raw frame |
| | `ChunkPayload.raw_auxiliary`, `frame_auxiliary_of`, the recording writer | What travels with a raw frame reaches the storer with that frame, through truncation, queueing and batching |
| | HDF5, Zarr, TIFF storers | Streams a detector declares with its auxiliary arrays: resizable datasets created before SWMR, Zarr arrays, TIFF sidecars; a row per frame, NaN for a frame that carried nothing |
| Event-driven programs (§1.6) | EtSnouty widget and controller | **Slow scan**: the TriggerScope routine (default) or a scan panel. An event runs that panel's program once through the channel's scan-source resolver; the run carries the event as `ScanEvent:triggeredBy` (JSON), blank when it ends |
| Several engines (§5.4) | `scan_program/split.py` | Who drives what; the leader's share (the whole program and the pulses) and each follower's (its devices, steps and step-anchored events shortened by its latency, a wait for every pulse); the pulse length; the drift bound |
| | `CompositeScanEngine` | Assess, compile, prepare and run across engines; one report; one outcome (Stop stops all, the first failure stops the rest); a simulated wire between simulated engines; the leader alone for programs on its devices |
| | `StepExecutor`, the protocol | Timing-only drives (`output: false`; `time<k>` channels on the wire); `trigger_min_ticks` and `clock_ppm` in `*CAPS?` |
| | `SimulatedStepEngine`, `StepProtocolScanEngine` | `triggerMinPulseS`/`clockAccuracyPpm`; quiet member runs with an outcome and an end callback |
| | `ScanEngineInfo.triggerInput`, the loader | A wired follower makes the selected engine a composite; a wire to any other engine is a setup error |

**Checked:**

- **Raw measurements:** in each format, the recorded measurements bin back
  to the recorded image; the two scans of a lapse in one file keep their own
  (they differ); nothing is recorded unless asked. Mutation-checked: values
  dropped by the writer, values out of order.
- **Events:** the real EtSnouty controller in the Expert rig runs the program
  on an event; the run carries the event while it lasts and not after.
  Mutation-checked: the event not published.
- **Several engines:** a simulated step board leading and a protocol board
  (or a second simulated board) following on a finer grid play an
  MS-RESOLFT program: every device's writes and edges, in absolute time,
  where the interpreter puts them; also with a leader trigger wait holding
  the follower. The follower is released by each pulse, its events its
  latency early. Refused, with the reason: an event before the follower can
  start, an undeclared latency, pulse minimum or clock accuracy, a drift that
  would move an edge off its tick (with the numbers), a follower that cannot
  wait before every step, a simulated leader pulsing a real board, the NI-DAQ
  leading, a separation across engines. Stop stops both with one outcome; a
  leader's failure and a follower that misses its pulse fail the run. The
  Expert panel runs one program on a leader and its follower. Mutation-checked:
  events not shifted, steps not shortened, the leader ignoring the follower's
  timing.

**Found:**

- **A shared start trigger would not have been enough.** Drift over a whole
  run must stay under half a tick for strict timing: 50 ms of run for two
  ±50 ppm clocks on a 10 µs grid. Pulsing every step bounds it by the time
  between two pulses instead -- and propagates the leader's waits for free.
- **Per-frame data must travel with its frame.** Auxiliary arrays were
  fetched once per recording, which suits the coverage (the same every
  frame) but would have kept the first frame's measurements for every frame.
- **A blocking simulated run fails a wait nothing releases**, by design
  (M1). The composite therefore runs its leader as on its own, and hands a
  follower every pulse there will be, so a missed pulse fails it instead of
  hanging the simulation.
- **A scan can complete before its last live image arrives** (M4's, found
  by the raw-measurement test failing once in three full lanes). The live
  images reach the detector queued, and a teardown on another thread can
  complete the scan first: the raw frame then took the image the live
  images had reached, and -- with measurements -- none of them. The raw
  frame is now finished from the measurement itself, which the detector
  holds from the worker's start, and a late live image is ignored; a test
  completes a scan before any live image.

**Still open:** the NI-DAQ leading (`NidaqManager` driving an engine line),
the NI-DAQ and pulse generators following, a shared sample clock, followers
of followers; the connected (real-board) composite run is untested on
hardware -- its aggregation is tested with fakes; raw measurements for
trajectories only, and a layout-contract loop kind for path windows; the
event's position is recorded, not used to place the program; skipping a
gated step on a timeout (`on_timeout: skip`); the EtSnouty widget screenshot
predates **Slow scan** (*regenerated, §7.8*).

### 7.8 Leftovers (2026-10-09)

The open items of §7.1-7.7, closed or placed. **Decided by Lenny
(2026-10-09):**

- **The layout repeat axis is sized by the line steps a detector takes part
  in** (§7.1, §7.3). A camera off in a step that has more camera pulses than
  any step it is on in had a repeat axis deeper than any frame it took, half
  empty; the Advanced controller's layouts now equal the program's for it
  (`_enabled_conditions` in `_acquisition_layout_source.py`; the pinned test
  is now a case of the controller-equals-program comparison).
- **The NI-DAQ drives every NI-DAQmx line** (§7.2): `<device>/port<n>/line<m>`
  and `<device>/PFI<n>` on any device name, a PXI chassis slot's included,
  besides `Dev...` as before. One rule, `nidaq_drives_line`, for the scan's
  digital outputs and the engine's ownership.

**Done:**

- **The timing note lists every time that runs differently** (§7.3): the
  dwell, and each move, settle, return and window edge, once each, from a
  copy of the scan two positions deep realized under its policy.
- **The timeline table has a Still column** (§7.4): blank is every drive, a
  list is those devices; editing the table keeps it.
- **Stale items** (§7.1): the logical-to-electrical mapping was done in M1,
  the panel-level load-preview-save-reload test in M2.
- **A power level inside a step plays** (§7.6), on a step board's DAC and on
  the NI-DAQ, through one conversion with the laser's manual one
  (`lasers/_power_volts.py`: its `calibCsvPath`, or the value as the
  voltage). **Found:** on the NI-DAQ a `level` event was reported native and
  then not played at all -- `densify` had no place for it.
- **Step boards are set by hand** (found while making a mock setup with
  them: the protocol had no manual control, which the TriggerScope's own
  firmware has): `SET,<channel>,<value>` between runs;
  `StepBoardPositionerManager` and `StepBoardLaserManager`, which reach their
  engine through a lookup `MasterController` hands every device manager
  (`scanEngineNamed`: the engines are built after the devices).
- **A DAC's resolution** (§7.6): `CAPS,dac_bits`; a board sets the nearest
  code (the executor's `settable`), the host reports each drive's step in µm
  and refuses a finer step, and `log_differences(..., resolution_um)`
  compares positions within half a code.
- **Progress and liveness** (§7.6): step engines emit `sigScanProgress`
  (a composite's leader too, in quiet runs), and the Expert panel shows it
  by step, or by time on the NI-DAQ. A board may declare `alive_ticks`: it
  sends at least one message that often while it runs (`WAITING` while it
  waits), and the host fails a run that falls silent for that long plus the
  link's reply time, stopping the board.
- **A conformance runner for real boards** (§7.6):
  `tools/step_board_conformance.py` (`conformance_runner.py`) runs the
  conformance programs on a board's own channels over its port and compares
  each `LOG?` with the interpreter; `--trigger` with a signal on its input,
  `--simulated` on the reference board. A board whose DAC writes land off
  fails it, naming the write.
- **`expert_step_boards_mock_setup.json`** (§7.6): a simulated
  TriggerScope-like board leading and a second following, Z, three lasers
  and a camera on them; a test scans on the leader alone and on both, and
  sets Z and a laser by hand.
- **Hardware → What this setup can scan…** (§3.3): every engine of the
  setup, what the panel does with it (runs on it, follows that engine, or
  neither, with the remedy), each feature's verdict and why, what it
  declares and where that came from, the devices on it, and the devices on
  no engine. A follower is told how far apart the leader's pulses may be:
  `split.max_pulse_interval_s`, the drift rule itself as a longest interval
  (`drift` decides by it). Built afresh on each open
  (`capability_report.setup_capabilities`); a board that has not answered is
  unknown with the reason.
- **Pulse generators resolve through the registry** (§5.3, §7.4):
  `pulseGenerators` by name, kind `pulse_generator`, built as
  `cls(info, name, **lowLevelManagers)` (`managers/pulsegen/loader.py`); a
  consumer names its generator (`pulseGenerator`), `pulse_generator_named`
  is the one lookup, and naming none is the `teensyPulse` section's
  `pulseGeneratorManager`. The kind moved from a bespoke loader to a
  standalone registry-backed one, so a plugin may contribute a generator;
  the editor's category is now `pulseGenerators`, its section. **Found:**
  `PulseStreamerManager` was unreachable (its construction was commented
  out), and the editor wrote pulse generators to a `pulsegen` section no
  code read.
- **Drop-in trajectory patterns** (§4, §7.5): a `.py` in
  `ImSwitchConfig/scan_patterns` defines `PATTERNS`, each a `Pattern` whose
  `parameters` are its `param_spec` (the Make… form's fields); loaded when
  the Expert panel starts (`pattern_plugins.load_user_patterns`), tolerant
  as ImProcess's drop-ins. Refused with the reason: a built-in's or another
  file's id, other than two devices, a parameter kind the form has no field
  for. `Pattern.source` (file and SHA-256) goes into the gridded image's
  provenance. Program patterns (drop-ins that make whole programs, not
  paths) stay open: the Make… presets are the built-in registry.
- **The EtSnouty screenshot** (§7.7) shows **Slow scan**
  (`tools/screenshot_widgets.py EtSnoutyWidget`).

**Placed, not done:**

- **The NI-DAQ leading** (§5.4 lists it under *Later*): the composite runs
  a leader through a step engine's run, log and outcome, and the NI-DAQ
  runs through the scan coordinator; a leading NI-DAQ also needs its clock
  accuracy declared (`nidaq`, from the card's specification) and its
  pulses known to a simulated follower. Refused with that reason until then.
- **Choosing the engine per scan** (§7.4): not designed. Two shapes, for
  Lenny: an engine chooser on the Expert panel (the run rebinds the
  panel's engine signals), or the program's devices choosing it (every
  device on one engine's channels runs there; one panel, no chooser).

### 7.9 Review round 1 (2026-10-10)

Two independent reviewers went through `56ef43f07..0afdf8d7b`: about thirty
distinct findings, seven high, most with a reproduction. Fixed in four
batches on this branch, each with its tests mutation-checked:

1. **Run lifecycle and hardware safety** -- done:
   - every engine ends a run one of three ways (`ScanEngine`): done; stopped
     then done; failed then build-failed. The pulse generator emitted only
     `sigScanFailed`, so a failed run held the scan lease forever;
   - a step board the host gives up on (unreadable message, `ERR`, link
     error) is sent `STOP` and the end confirmed within `_stop_limit` -- the
     longest stretch without a step boundary plus the reply time; the run
     counts as running until then. It used to run on, lasers switching;
   - a composite part that raises on start fails the run by name
     (`_start`, `_guarded`); armed followers are stopped, later parts not
     started. The step executor turns any error into a failure;
   - an engine wired to follow the NI-DAQ wrapped it in a composite without
     `run_signals`, so every NI-DAQ scan was refused -- and the setup
     dialog advised that wiring. The composite forwards the leader's calls;
     `can_lead` / `lead_refusal` are the one rule, in the composite and the
     dialog;
   - NI-DAQ: a wired scan clock the program does not mark is held low (it
     was `KeyError: 'line_clock'`); a marker on an NI-DAQmx line of its own
     is played (`nidaq_raw_line`), one on a device's line refused; a
     program with no analog output is refused when something waits for the
     AO start trigger (`AO_TRIGGERED_DETECTORS`, pinned against the
     detectors' source; `nidaq.startTrigger`);
   - `log` passes over a late reply; `finalize` gives up after
     `_stop_limit`; the conformance runner gives each program its duration
     plus the reply time (`--trigger-timeout` per trigger wait).
2. **One schedule decides** -- done:
   - a wait span holds every drive where it is (the NI-DAQ drove every
     positioner to 0 V for each pause);
   - **the approach** (Lenny, 2026-10-10: an approach span, Advanced opted
     out): the run's first span moves every drive not yet at its first
     position (or whose position is unknown) there and settles it, for the
     longest move + settle (`interpreter._approach`, a `preparation` span;
     `schedule.approach_span`). `ScanProgram.approach` is false only for the
     Advanced adapter, whose Beta designer has none -- its goldens are
     unchanged. A step board plays it as the first start time wait; point
     detectors throw its samples (`scan_throw_startzero`); followers have
     none of their own (the leader's covers their drives);
   - a move at the end of a pass that outlasts the return is still a move in
     the next step: stationary events there are refused (`carry` in
     `realize`; checks only, so `during='any'` and Advanced are untouched);
   - step engines refuse what the schedule refuses: the host compile
     realizes the program on the board's grid (followers' events left to
     them) and keeps the schedule (`Compiled.schedule`), which the panel
     shows -- no second realization, no uncaught `ScheduleRefused`; size
     refusals (capacity, the protocol's integers) come first, before a
     program is expanded;
   - one grid rule, `schedule.grid_ticks` (`GRID_RELATIVE_ERROR`, two
     epsilons, derived); presets add times exactly (`exact_sum`), the panel
     parses them as typed (`ScanWidgetExpert.stored`);
   - the Advanced program path checks the line program's windows under
     strict timing (the TTL designer rounded them);
   - preparation follows trigger waits only (the interpreter's rule); a
     leader keys PREPARE by its own lines and its followers' on a `time`
     channel; an undeclared leader or follower grid is unknown, not a
     `TypeError`; `inner_drives`/`return_seconds` are one rule (a zero
     weight is no return); a trajectory without an approach ends where it
     started.
3. **Semantics and the spec** -- done:
   - after a run each positioner it moved is told where it is
     (`updateTrackedPosition`): a finished run's restore position or a
     trajectory's last sample, a stopped or failed step board's last write
     from its log; unknown is said, never guessed (`_trackRunEndPositions`,
     from the compile that started);
   - a pulse-generator laser by hand follows `ttlActiveLevel`;
   - no-magic defaults: the NI-DAQ refuses an undeclared `conversionFactor`
     (`conversion <device>`, unknown); the raster preset's move shape is a
     field, and `Field.absent` says what a parameter set saved before a
     field existed means (`smooth`, what it did);
   - the log carries each edge's written level (`LOG,ON/OFF,...,<level>`),
     and the conformance runner checks it against the host's EVENT levels
     (`level_differences`), with an active-low line and, on a second DAC
     that takes 0 V, a power level;
   - the spec defines `ALIVE,<segment>` through anything longer than
     `alive_ticks`, and a safe state that puts power DACs at their off
     volts.
4. **Recording, docs and tests** -- done:
   - the file says what ran: `ScanStage:scan_schedule` beside
     `ScanStage:scan_program` -- the policy with its contents, the grid and
     its source, the changes, the approach and duration
     (`RealizedSchedule.summary`, `RealizedTrajectory.summary`); an Advanced
     scan's distinct changes from the sample its timing note reads
     (`advanced.schedule_summary`), blank for a swept scan;
   - a policy declaring tolerances is refused: nothing applies them;
   - **What this setup can scan…** takes each feature's verdict from the
     engine's own `assess` on probe programs on its own channels (the
     report restated verdicts: a pulse generator's start trigger read
     unknown where its engine answered for it), and opening it asks a board
     that had not answered again;
   - the plugin docs say pulse-generator plugins resolve;
   - `_ensure_qcore_app` (tests) creates a full `QApplication`: a bare
     `QCoreApplication` aborted a widget test later in the same xdist worker
     (reproduced, exit 134) -- possibly some of the "xdist segfault" flakes;
   - the pulse-generator safe-state tests end on a line high, on a generator
     that holds its outputs on a stop, so they can fail.
   - **Open, unconfirmed:** a trajectory's coverage and stream declaration
     are read from live detector state at the first flush (reviewer B; their
     Repeat reproduction recorded one frame and was inconclusive).

**For Lenny:** loading a file with a trajectory script runs its code (ask,
or keep only a hash?); the Expert **Detectors** tab has no effect (build the
bindings, or remove the tab?); choosing the engine per scan; the NI-DAQ
leading.

### 7.10 Lenny's decisions on review round 1 (2026-10-10)

The branch was rebuilt on main as one commit per milestone (SimplePointScan,
this plan, M0-M6, the leftovers, review round 1). Lenny's answers to the four
questions §7.9 left:

1. **A trajectory script runs only once approved on this computer** (ask
   once, remember). *Done.* `scan_program/script_approval.py` keeps the
   approved code's SHA-256 (line endings ignored) in
   `ImSwitchConfig/config/approved_scan_scripts.json`; the one place code
   runs (`trajectory._script`) refuses unapproved code with
   `ScriptNotApproved`, a `ProgramError`, so every compile -- the report,
   estimates, recording, a restore -- holds it with the reason and runs
   nothing. Only someone at the panel is asked: **Load** shows the code a
   file brought, **Plot** and **Start** ask until it is approved; code
   written or read in **Make…** counts as approved. The startup restore and
   scripts never ask. Drop-in pattern files stay ungated: they are installed
   by hand.
2. **The Detectors tab: build §2.5** -- each detector's layout from its
   binding, checked against what its line plays. *Done.*
   `scan_program/bindings.py`: a camera takes a frame on every rising edge
   of its line, so `binding_problems` compares the binding's frames per
   step (`per_step` in the steps of its `phases`, none elsewhere) with the
   line's, on the leader's or a follower's schedule, and refuses the first
   step that differs, with its phase; a camera bound to another device's
   events, two bindings, an unknown detector or a binding on a trajectory
   are refused. The Expert compile runs it, so the report, the run and the
   recording refuse alike; `build_program_layouts` and `getNumCamTTL` take
   `frames_per_step` -- the binding's, or without one the line's -- so the
   layout and the recording's counts come from the binding. Built: cameras
   `event` / `stream`; point detectors `step` / `image`, one value per
   step, every phase (they integrate the whole step); other combinations
   are refused as not built. The report says how each detector is
   recorded.
3. **The program's devices choose the engine** -- no chooser. *Done.*
   `ScanEngines.for_program` takes each device's channels to their engine:
   one engine runs it; `scan.engine` with its followers run it together (a
   follower's devices alone run on the follower); no device on an engine
   runs on `scan.engine`; any other spread is refused, naming which device
   is where. The Expert panel binds to the chosen engine as a new run
   begins (`SuperScanController._bindScanEngine` moves its run slots), a
   run in progress keeps its engine, and the report names it. Advanced and
   SimplePointScan stay on `scan.engine`, and **What this setup can
   scan…** words each engine's role for the panel the setup uses.
4. **The NI-DAQ leading stays under *Later*** (§5.4), refused with its
   reason until a rig needs it.

**What to test on the rig:** `scan-engines-rig-test-plan.md`, beside this
plan. **Not built, by decision or deferred** (each refused with its reason,
never approximated):

- the Advanced and SimplePointScan panels on an engine that does not stream
  (they build sampled waveforms); a stepped Advanced scan reaches a board by
  opening its file in the Expert panel;
- one program on the NI-DAQ and a pulse generator together: the NI-DAQ
  cannot lead, a pulse generator cannot follow step by step, and a shared
  clock or start-trigger sync is *Later* (§5.4);
- a real step board: the TriggerScope firmware is the lab's, later; the
  conformance runner is ready for it;
- streaming a long densified program in chunks (stopping an NI-DAQ
  iteration midway is built since 2026-10-18: `scan-stop-now.md`);
- point detectors on step boards, over an event's window, or per phase;
  orders on swept axes; two interleaved axes in one frame stream;
  trajectories on anything but a streaming engine, and from measured
  rather than commanded positions;
- followers of followers, a program split at its barriers, `on_timeout:
  skip`, an event's position used to place the program, drop-ins that make
  whole programs;
- still to confirm: whether a trajectory's coverage and stream declaration
  are read from live detector state under Repeat (§7.9).

**Out of scope, by decision (Lenny, 2026-10-11): adaptive scans.** A scan
that changes what it does from what it measures -- MINFLUX-type closed
loops (count, estimate, shrink and re-centre the pattern, iterate) -- is not
the Expert program's job. Its programs stay static, which is what makes them
checkable, exactly timed and recorded as they ran; a general "if" would break
that. Such loops belong on dedicated FPGA hardware with its own logic.
Host-level adaptivity stays as it is: an event starts a program (EtSnouty's
Slow scan; etSTED works the same way); placing that program at the event's
position is the one open item in that direction.

### 7.11 The Expert panel made over (2026-10-11)

Lenny: the tables were "very user unfriendly and not intuitive". The panel
now describes the scan top to bottom (concept agreed on a mockup first):
a header (what, whether it runs and where, steps, duration, frames, clock,
Run); **Scan order** (loops as blocks with count and order, waits as
badges, and the first drive's visit order as bars); **Positions** (each
drive as a formula, its motion as a sentence); **Inside every step** (a
custom-painted timeline: drag to move, edges to resize, snapped to the
engine's grid from the check, in-step moves hatched, phases, an inspector
for every field); **Waits** and **Recording** as sentences; **Check** (the
report as data: `ScanControllerExpert.updateReport` sends `setCheck` beside
the text); **Preview** (a lane per device, zoom to one step, one pass or
the run, from `_preview_windows`). `AdaptiveColumns` puts Check and Preview
beside the editor when both fit at their preferred widths.

**One state.** The tables (`_RowTable`) stay the program's only state in the
widget, behind **Tables**; every part reads their raw rows and writes cells
(`setTableValue`), and is drawn again after each edit except the field being
typed in. Parse errors, the controller contract and the tests of the tables
stay as they were (`_expert_scan_parts.py`, `test_scan_widget_expert_composer.py`).

**An independent review** (one agent, its own worktree, screenshots and
probes) found 5 high and 11 medium problems; all fixed, each with a test
that fails without it: the mouse wheel edited fields it scrolled past (a
`WheelGuard`); drags left the step, inverted bars, could not move narrow ones,
and lost a value typed just before (clamped, a tick minimum, narrow bars move
from inside, the click draws the commit first); the order button made
serpentines the model refuses (the parity is the loop right outside; never
the outermost); errors from Draw and a declined script went to the hidden
report (shown in the preview, under the header); a refused program lost the
clock (the engine's declared grid); refusals were said twice and per step
(once, collapsed); the point-detector note showed for cameras; narrow docks
scrolled sideways (flow layouts, the policy out of the step header);
trajectories and empty programs were described as loops (their own cards);
the visit strip vanished for unrelated reasons (loops and drives only, the
loops the drive moves with); an O(N²) paint; removing or renaming a loop
broke what named it (cascades, renames carried, also from **Tables**); new
events were zero-length (from the last event to the step's end -- derived,
no default length); the preview was slow on long scans (clip to view,
downsampled); light-theme contrast (colours by theme); 640 and 775 nm drawn
alike. Added from its suggestions: undo/redo, arrow-key nudges, a row per
device, the drive's range, More in the inspector, plainer words (*Runs
exactly as written*, *exact timing, 10 µs clock*, messages naming the part,
row and field), the header pinned above the scroll, Keep drawn. Not taken:
a default step or event length (a number nobody declared); the check still
compiles on the GUI thread (as before the makeover).

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

## 9. Review status

**Closed** (the reviewer, on draft 6, with the amendments below made in
draft 7): proceed to M0.

**Answered in the fourth review round:**

| # | Point | Answer | Where |
|---|---|---|---|
| 1 | `on_timeout` | `fail` or `continue`, default `fail`. `continue` must be selected explicitly and records that the gated step ran without its trigger. Skipping stays deferred (M6) | §2.4 |
| 2 | Policy files | Versioned, validated data beside the scan model; tests pin each version; published versions immutable; presets reference or bundle a policy in the same schema; the identifier and resolved contents are recorded; unknown versions are refused, never substituted | §2.8 |
| 3 | Preparation keys | Units and precise semantics (what starts the delay, what "ready" means); three states (not required, known, required but unknown); an absent key is not zero; existing behaviour kept by an explicit legacy rule, `legacy-preparation-v1` | §2.4 |
| 4 | Hardware without absolute deadlines | Not refused wholesale: the compiled program is judged. An exact repeat period with changed pulses, or a changed period whose per-repeat and accumulated errors the policy explicitly permits; otherwise refused with the calculated errors | §2.6 |

**Corrections in draft 7:**

- **`strict-v1` requires exact edges,** markers and clocks too. Rounding
  belongs to a separately named policy. The ½-step bound describes
  rounding; it does not authorize it.
- **Absolute deadlines have an overrun rule.** Every iteration, preparation
  and motion included, must fit before the next deadline. Nothing is
  truncated or started late. External waits begin a new segment.
- **The legacy fallback is format-specific.** A recognized legacy Advanced
  file without a policy uses `advanced-compatible-v1`. A new-format program
  without a valid policy is refused.

---

## 10. Found on the way

Found while writing draft 1; where each stands at the end of the branch
(2026-10-10):

- **A laser on a non-NI-DAQ line silently never fired in an NI-DAQ scan.**
  *Fixed (M1):* the Advanced panel greys it with the reason and refuses a
  scan that fires it.
- **A TTL-only NI-DAQ scan with point detectors would hang on hardware**
  (no AO task, so the DO task and the detectors wait on `ao/StartTrigger`).
  *Fixed for programs:* the NI-DAQ engine refuses a program that drives no
  analog output while point detectors would wait for its start; Advanced
  always has AO for its axes.
- **The intra-pixel positioner fields were dead.** *Fixed (M2):* the moves
  play.
- **`ScanManagerBase.makeFullScan` would store the Advanced TTL designer's
  `(dict, scanInfo)` tuple as the TTL signal dict.** *Unchanged:* still
  unreachable (the Advanced controller builds its designers itself).
- **`ScanManagerAdvanced.getTTLCyclePreview*`** has no callers. *Unchanged.*
- **`docs/design/ARCHITECTURE.md`** names scan controllers as
  pulse-generator consumers. *Now true, through the engine:* a scan
  controller runs `PulseGeneratorScanEngine`, which drives the generator.
- **Beta stretched dwells with only a warning.** *Reported (§7.8):* the
  timing note lists every time that plays differently, the dwell included.
- **Scan TTL lines had no polarity setting.** *Fixed (M1):*
  `ttlActiveLevel`, on every NI-DAQ path, the protocol boards and the pulse
  generators (by hand too, §7.9).
- **The Teensy shortens pulses silently** (`duration_ns // 1_000`).
  *Partly:* a scan program is realized on the generator's 1 µs grid, and a
  time off it is refused with its neighbours; the manager's own API still
  floors.
- **`PulseGeneratorManager.run(n_reps, blocking)` has no trigger argument.**
  *Partly:* the Teensy's `run` takes `trigger`, and generators declare
  `run_triggers`; the base interface does not.

---

### 7.12 Panel vocabulary and flow (2026-10-12, built)

Lenny, after the makeover: the panel is "still not very intuitive. It's
mostly the naming and a bit the flow of setting up a scan." A microscopy
expert "still only understands half of it". Lenny agreed to the proposal
below as written ("build it all with the renames as proposed") with one
change: the order visited is a line through points, not bars. Built the
same day; the table's *Proposed* column is what the panel now says.

**Diagnosis.** The panel speaks the model's words (program, loop, drive,
event, binding, anchor, emulated, densified) where a microscopist has words
already (scan, dimension, positioner, pulse/exposure, record, dwell, step
size). Three words are overloaded: *step* means a time slot in the panel
and a distance in the template forms (*Cycle step (µm)*); *order* means
loop traversal on one card and loop nesting on another; *positions* names
a card, a combo (*positions absolute*) and a fact. The template forms ask
for every number blank, with no defaults from the setup and X and Y
devices both defaulting to the first positioner.

**Vocabulary (model term → panel word).** The model, files and API keep
their names; only what the panel shows changes.

| Model | Today on the panel | Proposed | Why |
|---|---|---|---|
| program | *Scan program*, *program* | *scan*; the title is the template name or *New scan* | "program" reads as code |
| loop | *Scan order, outermost first*; *+ Loop* | *Scan order (slowest to fastest)*; *+ Dimension*; chips read *2 timepoints › 2 cycles › 3 planes* | OME/ImageJ word for T, Z, C, tiles, positions; outer = slowest is the microscopy idiom |
| traversal | *→* button, *Order* | a combo in words: *forward / backward / serpentine* | an arrow glyph without a label explains nothing |
| drive | *Positions*, *+ Drive*, *Z = 1 µm + 0 µm × time + 0.25 µm × cycle + 0.5 µm × plane* | *+ Positioner*; *Z starts at 1 µm, moves 0.25 µm per cycle and 0.5 µm per plane* with zero terms hidden behind *+ also per…*; range line gains the spacing (*6 positions from 1 to 2.25 µm, 0.25 µm apart*) | "per cycle" is how one thinks; the formula stays the model, the sentence is the view |
| motion | *jumps · at the end of each step · move 0.5 ms · settle 0.5 ms · return 1 ms · positions absolute · after the scan to its first position* | two sentences: *Moves: jumps, at the end of each step, taking 0.5 ms, then settles 0.5 ms. Return to the line start: 1 ms.* and *Positions are absolute. After the scan: back to its first position.* | "positions absolute" (origin) and a bare "return" were the two most cryptic fields |
| step_s | *Inside every step*, *step 7 ms* | *Within one step*, *dwell 7 ms*; the facts chip stays *12 steps* | *dwell* is the Advanced panel's word for the same thing; *step size* keeps meaning µm |
| preset distances | *Cycle step (µm)*, *X step (µm)* | *Position spacing (µm)*, *X spacing (µm)* | frees *step* for the iteration |
| event | *+ Event ▾ (Pulse / Camera exposure / Power level / Move by)*; inspector kind words *on / exposes / power level / moves by / marker* | four buttons *+ Pulse, + Exposure, + Power level, + Move*; kind words *pulse / exposure / power / move / marker* | the inspector read "405 (ON) on from 0 to 1 ms" |
| during / still | *only while the drives stand still*, *still: every drive* | *while the stage stands still / any time, even while moving*; *waits for: every positioner* | |
| anchor | *every step / first step of each pass of cycle* | unchanged | already in words |
| wait | *Waits*, *+ Wait ▾ (A pause / Wait for a trigger)* | *Pauses and triggers*; *+ Pause*, *+ Wait for trigger* | |
| binding | *Camera takes 1 per step from exposure in phases every · one per pulse · as frames* | *Camera records 1 frame per step, triggered by exposure · in every phase*; window and assembly are NOT shown as choices: a camera is event+stream, a point detector step+image, every other combination is refused (docs, "The program") | two combos that only ever have one legal value |
| recorded line | *Camera: 1 frame per step, as bound; its line agrees.* | *Camera: 1 frame per step; its trigger line agrees. 12 frames in all.* | |
| check | *Check*; chip *1 played another way*; *~ stepped loop: densified into samples* | *Can it run?*; chip *Yes, on nidaq* / *No: <first reason>*; verdict wording at the source: NI-DAQ *stepped positions are played as sampled waveforms*; an emulated verdict with no cost is a muted note, not an amber chip | the amber chip on every stepped scan on the NI-DAQ cries wolf |
| preview | *Draw*, *Keep drawn* | *Show preview*, *Auto-update* | |
| status chip | *Ready on nidaq*, *Not runnable on nidaq* | *Ready to run on nidaq*, *Cannot run on nidaq* | |
| toolbar | *Templates ▾* (docs say *Make…*) | *New from template ▾*; the docs follow | one name |
| engine line | *Runs on nidaq.* under the toolbar | shown only when devices span several engines; otherwise the chip says it | redundant |
| ProgramError | *The scan program is not well formed:* …*outside its step (0 to 0.007 s)* | *The scan cannot run yet:* …*outside its step (0 to 7 ms)* (format_seconds) | seconds on a panel that shows ms |
| timing chip | *rounded timing, 10 µs clock* on an empty or refused program | *10 µs clock* when the policy is not known; the panel falls back to its own policy combo | today it says "rounded" for strict-v1 whenever the program does not parse |

**Flow.**

1. *Empty panel*: only the start card (*Start from a template*: the three
   templates, a *Trajectory ▾* menu, and *or build it yourself: + Dimension*).
   The empty Pauses and Recording cards are hidden until there is a scan.
2. *Template forms* prefill from real sources, never from a guess (no magic
   numbers): positioner fields default to distinct positioners (X, Y; the
   first for a single one); *First position* from the positioner's tracked
   position; move and settle times from the positioner's declared
   properties when it declares them, else blank with the placeholder
   *not declared in the setup*; the last values used for that template are
   remembered in the widget state and offered again (today only when the
   current scan came from the same template). The form is grouped
   (Positions / Pulses / Camera / Time lapse / Motion) and says which fields
   are required.
3. *Template-made scans* carry *Change template values…* in the header
   (as a trajectory has *Edit the path…*); once edited by hand the button
   says the form holds the template's values, not the edits.
4. *Editing*: unchanged (drag the timeline, type in the sentences).
5. *Check and run*: the check stays automatic; the preview gets
   *Auto-update*; Run unchanged.

**Cost.** Panel strings in `ScanWidgetExpert.py` / `_expert_scan_parts.py`;
the two verdict strings in `NidaqScanEngine`; `ProgramError` wording in
`program.py`; `presets.py` labels; the form defaults in `PresetDialog` and
the controller; `docs/expert-scanning.rst`, `docs/gui.rst`, the changelog;
about 30 label assertions in `test_scan_widget_expert_composer.py` and
`test_scan_controller_expert.py`.

### 7.13 Review round 2 (2026-10-14)

An independent review of 22f866a7c (eight findings, four P1, with a probe
file of 62 tests, ten failing). All verified against the code and fixed;
the probe file is kept as `test_scan_review_round2.py`, adapted where the
fix is a refusal, with four probes added.

| # | Finding | Fix |
|---|---|---|
| 1 | The tracked position after a run was the requested restore, not the last sample played: a return of 0 s and a one-sample `linear` return both left the drive elsewhere | `transition('linear', …, 1)` ends at its endpoint; `RealizedSchedule.final_um()` is the last position played and `_end_positions` reports it; a final return of no duration to the restore positions is played in the drives' declared move + settle (one tick when none is declared) and recorded as a `Change`; `realize` checks final == restore. An Advanced program says `restore='hold'`: its waveforms end at the last position and the Advanced controller parks by a command |
| 2 | `wire_frames` merged pulses per step; a camera window over whole consecutive steps is one edge on the wire but counted once per step | Edges are counted over the run: windows that touch across steps are one pulse; a line held through a `hold` wait stays up (and densify now holds lines and levels through such a wait on the NI-DAQ; the step-protocol engine refuses `hold` waits as before) |
| 3 | A table on a loop in reverse or serpentine order was placed as if forward | Refused for point detectors with the reason (one mapping per visit cannot express a per-pass order); cameras unaffected |
| 4 | NaN positions validated, compiled and passed the voltage range check | `validate` checks every number finite and every count whole (`_finite`, `_whole`); the NI-DAQ refuses a non-finite waveform and a `conversionFactor` that is not a finite non-zero number |
| 5 | Events anchored on the step's end were compared with start-anchored ones in different frames and refused as overlapping | `_check_relations` shifts `from_end` requests by `step_s` |
| 6 | A phases-only program (camera pulse sequence) crashed `scan_info_for` (`dims[0]`) | One index-unit pixel stands in for the image axes; the raster is refused with the reason |
| 7 | A fractional loop count (3.9) loaded and played as 3 | Refused by `_whole`; phases and `per_step` too |
| 8 | Engine choice took every channel of a device, so a laser with analog power on the NI-DAQ and a TTL line on a Teensy was refused for a TTL-only program | `program_channels` says which channel each device's operations use; `for_program` considers only those |

**Recheck (same day, of 1a7e7bc7e):** four follow-ups, all fixed, the probe
file kept as `test_scan_review_round2_followups.py`.

| # | Finding | Fix |
|---|---|---|
| R1 | The derived final return was taken as every line's return: `scan_samples_d2_period` 301 for lines 300 ticks apart | The final return is a terminal interval (in the total, in no line); the line period uses the returns between passes; passes whose returns differ refuse the raster |
| R2 | Both step-board backends compile one `return_ticks` from the program and report the run done before the derived final return has elapsed | Refused on step engines with the way out ("a step board plays one return time: give the return that duration, or restore 'hold'") -- encoding a distinct final return in the plan and the wire protocol is a protocol change, left for a later protocol version |
| R3 | A trajectory compiled with a NaN captured origin | `compile` refuses a captured position that is not finite (both paths); `_volts` refuses a non-finite path |
| R4 | `_check_separations` compared requested edges of start- and end-anchored events in different frames | Edges are requested in the step's frame (`+ step_s` for `pass-end` anchors), as `_check_relations` does |

**Second recheck (same day, of 92f844726): no actionable findings.** The
reviewer's 44 independent boundary checks (line and slice starts against the
metadata over lines x slices x returns x restores; both board backends on
explicit, hold and unrepresentable returns; NaN, ±inf and malformed captured
origins; four anchor combinations of a separation) are kept as
`test_scan_review_round3_boundaries.py`. The board limitation stands as
documented: one return time per pass; a distinct final return needs a later
protocol version.

GUI recommendations (six): done now -- 2 (the header wraps; the panel's
minimum width fell from 622 to 249 px on the test rig; the columns' threshold
derives from the stretch ratio, so drawing the preview no longer flips them)
and 5 ("implemented another way" in grey for emulation; an amber header chip
"N times played differently" for the schedule's changes, clicking it opens
the check). Open for Lenny: 1 (independent scroll of editor and check), 3
(phase and traversal context in the timeline: first/middle/last step views),
4 (refusals beside the field: off-grid neighbours at the input, stationarity
collisions highlighted), 6 (why a choice is unavailable on this rig, which
outputs the chosen engine plays).

### 7.14 Rig-readiness review (2026-10-16)

Before the rig test, with the scan-timing work and main (#70, #68) on this
branch, four read-only reviewers went over the NI-DAQ path, the designers,
the Expert panel and engines, and the live view, setups and docs. Nothing
blocked the daily scans. The should-fix items for this plan, each fixed with
a test that fails without the fix:

1. **No Stop on the Expert panel.** Run is disabled while a run is in
   progress and nothing else reached `abortScan`. A **Stop** beside Run,
   enabled for the run: a step engine stops at its next step, the NI-DAQ
   finishes the pass in progress and starts nothing more. (Superseded
   2026-10-18: the NI-DAQ stops at once, `scan-stop-now.md`.)
2. **An integer channel meant two things.** `setup_channels` kept the raw
   integer, the NI-DAQ and the Advanced panel read the `Dev1/port0/line<N>`
   shorthand: an NI-DAQ laser written `"digitalLine": 1` was owned by the
   Teensy, a Teensy laser written `"digitalLine": 3` was played on
   Dev1/port0/line3. Now one reading per device (`device_digital_line`,
   `device_analog_channel`): a pulse generator's channel on a
   pulse-generator laser, the shorthand on everything else; the NI-DAQ
   never targets a pulse-generator laser; the Advanced panel greys a line
   by the engine that plays it (the leader, not the composite).
3. **EtSnouty events at numpy coordinates** failed in `json.dumps` and
   never ran their program; serialized as plain numbers.
4. **NI-DAQ programs densified on the GUI thread with no size check** (an
   hour of a time-lapse: gigabytes, a MemoryError in the report's timer).
   The compile's peak (positions and volts per drive and power level, a
   byte per line) is set against the free memory before a sample is built,
   and refused with both numbers; `scan.maxScanTimeMin` is checked before
   it too (one rule, `duration_cap_refusal`, for the panel and the engine).
   A program that fits still builds on the GUI thread: long pauses make the
   panel slow until the NI-DAQ can play a pause as anything but samples.
5. **The Expert scan info paired rounded ticks with the requested step**
   under `advanced-compatible-v1`; the dwell is the ticks' time now.

Minor, left open and on the rig plan: a step board that never answers STOP
holds the run when it declares no heartbeat; a Teensy with a wrong port runs
on its mock (`useMockOnFailure` defaults to true) while the report says it is
runnable; the hardware status names a step board's `aux/...` lines as an
NI-DAQ board.

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
