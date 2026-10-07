# Lifetime 2.0 — Swabian Time Tagger, calibration tutorials, and a Lifetime widget

Status: Proposed — **revision 1**, for the first review round. Nothing here is
implemented. §12 lists the decisions this round should rule on; §13 lists the
rig facts the plan still needs from you.

Scope in one sentence: turn today's single-purpose FLIM detector into a
Time-Tagger subsystem — a shared device layer that exposes the card's
measurement and conditioning features, a set of ImScripting tutorials that
calibrate and debug every time-critical signal with the card itself, and a
dedicated **Lifetime** widget that makes FLIM, gated STED and tau-STED
first-class GUI modes instead of scripts.

---

## 1. Where we are

The code is sound for what it does, and it does one thing: a scan-driven FLIM
raster through a single `TimeTagger.Flim` measurement.

**What exists** (paths relative to the repo root):

| Layer | File | What it does today |
|---|---|---|
| Detector | `imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py` (1512 lines) | Follows `NidaqManager.sigScanBuilt/Started/Done`; two `EventGenerator`s turn each physical line-clock edge into Nx pixel-begin/end markers; one `Flim` with `n_pixels = Nx·Ny`; a worker polls `getCurrentFrame()` every 1 s, fits `moment` / `phasor` / `exp1` per pixel with IRF-peak compensation, and publishes the lifetime image as the detector frame. Accumulate mode averages lifetimes across scans in software. Side channel (`TimeResolvedDetectorMixin`) hands a `TimeResolvedScanProducts` (intensity, lifetime, decay, gate images, optional cube) to workflows. |
| Contract | `imswitch/imcontrol/model/timeresolved/` | `GateSpec`, `LifetimeFitConfig`, `TimeResolvedScanConfig`, `TimeResolvedScanProducts`, gate math. |
| Workflows | `imswitch/imcontrol/model/workflows/time_resolved.py` | `BinnedPhotonArrival`, `GatedSTED`, `TauSTED`: configure → `facade.scan.run_once()` → wait → save HDF5/NPZ/TIFF. |
| Scripts | `imswitch/_data/user_defaults/scripts/workflows/timeresolved/01..03_*.py` | Three ~30-line scripts that run those workflows. Hardware-only, untested, hard-coded `D:/Measurements`. |
| GUI | `FLIMHistWidget` / `FLIMHistController` (`FLIMHist` key) | One pyqtgraph bar plot: per-pixel lifetime histogram, or the aggregated decay with a global τ marker. Reads the manager's private `_last_*` attributes. |
| Diagnostics | `scripts/diagnostics/measure_laser_rep_rate.py` | Standalone CLI, `Countrate` on the sync channel. The only Time Tagger diagnostic. |
| Docs | `docs/devices/detectors.rst` §SwabianTimeTaggerManager, `docs/scripting-time-resolved-workflows.rst`, `docs/design/plans/time-resolved-detector-workflows.md` | Config reference, workflow cookbook, and the plan that produced the contract. |

**Swabian API actually used** — the complete list: `createTimeTagger`,
`setTriggerLevel`, `setInputDelay` (click channel, for `t0_ps`),
`EventGenerator`, `Flim(...)`, `Flim.getCurrentFrame`. Nothing else.

**What the card can do that we don't use** (this is the "unleash" list):

| Capability | Swabian API | Why it matters for us |
|---|---|---|
| Live count rates per channel | `Counter`, `Countrate` | "Is the SPAD alive? Is the sync there? Is the line clock firing?" — today answered by scanning and seeing zeros 10 s later. |
| Period / jitter of a periodic signal | `TimeDifferences`, `Histogram` (start = click = same channel) | Measure the laser period and its jitter; measure the line-clock period against the scan design. |
| Digital timing trace | `Scope` | An oscilloscope on the sync/line/frame/pixel channels, with ps resolution — the tool for debugging time-critical signals. |
| Cross-channel delay | `Correlation`, `Histogram` | IRF and `t0`; delay between two photon channels; delay from frame clock to first line edge. |
| Frame boundary | `Flim(frame_begin_channel=…)`, `getReadyFrame`, `getSummedFrames`, `n_frame_average`, `FlimFrameInfo` | The DAQ already outputs `frameStartClockLine`; Flim can use it, which gives us robust frame boundaries, hardware-side multi-frame accumulation, and the groundwork for z/t stacks. |
| Intensity-only imaging | `CountBetweenMarkers` | A Time-Tagger "APD mode": photon counts per pixel without TCSPC, cheap and sync-free. |
| USB bandwidth control | `setConditionalFilter`, `setEventDivider`, `setDeadtime`, `getOverflows` | An 80 MHz sync is 80 M tags/s. Depending on the card model, that saturates the link unless the sync is filtered to "only the sync after a click". Overflows silently corrupt a FLIM frame today; nothing reads `getOverflows()`. |
| Hardware delay on any channel | `setInputDelay` on the line channel | A ps-resolution, hardware-side fix for the APD-vs-TimeTagger line offset that ROADMAP M9 attributes to `phase_delay` never being applied to `line_clock`. |
| Self test | `setTestSignal` | A built-in ~800 kHz test source per channel: verify a channel and the pipeline without any cable. |
| Raw tags | `FileWriter`, `TimeTagStream`, `Dump`, `createTimeTaggerVirtual` | Record photons once, re-bin/re-gate forever; replay a recording as a virtual card for tests and tutorials. |
| Virtual channels | `DelayedChannel`, `GatedChannel`, `Combiner`, `Coincidence` | Software-defined gating and channel combination without re-cabling. |
| Identity | `getSerial`, `getModel`, `getConfiguration` | Put the card identity into the metadata of every measurement. |

**Known defects and gaps that this plan absorbs** (from the two code surveys):

1. **No mock.** `_isMock` is set but never read; with the library missing or
   the card absent, every scan with an enabled FLIM detector raises and rolls
   back. No shipped setup contains the detector. The worker and the `Flim`
   path have never run in CI.
2. **Products stay armed.** `TimeResolvedScanWorkflow.run()` never calls
   `clear()`, so after one workflow `_tr_enabled` stays true: every later
   scan copies products and computes gates, and any later z/t scan is
   rejected by `_validate_time_resolved_scan_shape` until something clears
   it.
3. **Line offset.** Pixel markers key off the uncompensated physical line
   clock (ROADMAP M9). APD and TimeTagger images differ by about a line.
4. **Outer axes.** `n_linesteps` is read and ignored; z/t stacks read only
   the current frame.
5. **`max_retained_products`** is accepted and not honoured.
6. **Config template drift.** The builtin template defaults `n_bins=64`
   (which triggers the truncation warning the code added to prevent exactly
   that) and `line_trigger=-0.5` (code: `+0.5`); it lacks
   `laser_rep_rate_mhz`.
7. **Dead code**: module-level `_fit_phasor` and `_fit_exp1`; only the
   worker's cached variants run.
8. **Cube dtype** is float32 (cast in the worker); counts are integers.
9. **FLIMHistController** docstring says seconds; the frame is in ns. It also
   reaches into private manager attributes.
10. **Docs drift**: the roadmap and the old plan point at
    `scripts/timeresolved/`; the files live in `scripts/workflows/timeresolved/`.

---

## 2. Goals and non-goals

**Goals**

- G1. A **shared Time Tagger device layer** so that diagnostics, the FLIM
  detector, an intensity detector, and scripts all use one connection and one
  channel configuration, and the card's conditioning features (trigger
  levels, delays, dead time, divider, conditional filter, test signals) are
  configured in one place and recorded in metadata.
- G2. **Calibration and debugging tutorials** in ImScripting that measure each
  time-critical signal with the card — laser sync, line clock, frame clock,
  pixel markers, photon channels — compare them against what the scan designer
  intended, and write the results back (`laser_rep_rate_mhz`, `t0_ps`,
  trigger levels, line delay). They must run on a simulated card, like every
  other shipped tutorial.
- G3. **Mode setup guides**: one tutorial + doc page per mode (FLIM, gated
  STED, tau-STED, two-colour FLIM, intensity-only) that says what to cable to
  which channel, which settings matter, how to verify, and gives a setup-JSON
  snippet.
- G4. A **Lifetime widget** that makes gated STED and tau-STED point-and-click:
  live decay with draggable gates, live gate images and lifetime overlays in
  napari, phasor plot, accumulation, one-click save, and a signal-health strip
  that shows the state of every Time Tagger channel at all times.
- G5. **Scan correctness**: frame-clock-anchored frames, linesteps and outer
  axes, and a measured, hardware-applied line delay.
- G6. **Testability**: a mock Time Tagger good enough that the worker, the
  widget, and every tutorial run in CI on a shipped mock setup.

**Non-goals (this plan)**

- Hardware-gated detection electronics or changing the STED laser timing.
  Gates stay software gates on photon arrival times; the Pulse Streamer is
  not wired to the card in code and stays that way here.
- A new fitting library. The three fitters stay; a phasor *plot* is added, an
  IRF-deconvolved tail fit is listed as a later option (§9).
- A second real time-tagger vendor. The contract stays vendor-neutral; the
  mock is the "second backend" that proves portability.
- Changing galvo trajectories or `GalvoScanDesigner` (M9 territory). The
  plan *measures* the line offset and applies a delay on the card side; the
  designer-side fix remains M9's.
- Raw-tag *streaming into recordings*. Recording raw tags to `.ttbin` from a
  tutorial is in; making `RecordingManager` understand tag streams is not.

---

## 3. Architecture

```
                 ┌──────────────────────────────────────────────────────────┐
 setup JSON      │  TimeTaggerManager   (new, low-level, one per card)      │
 "timeTagger": { │  - owns the TimeTagger connection (real or Mock)         │
   channels,     │  - named channel roles → physical channel + conditioning │
   filter, ... } │  - measurement factory + lock (Counter, Scope, Histogram,│
                 │    Correlation, TimeDifferences, Flim, CountBetweenMarkers│
                 │    FileWriter) ; health snapshot (rates, overflows)       │
                 └───────┬───────────────────┬───────────────────┬──────────┘
                         │                   │                   │
        ┌────────────────▼───┐   ┌───────────▼─────────┐  ┌──────▼──────────────┐
        │ SwabianTimeTagger  │   │ TimeTaggerCounter   │  │ TimeTaggerFacade    │
        │ Manager (FLIM det.)│   │ Manager (intensity  │  │ (scripts/tutorials, │
        │ slimmed: Flim +    │   │ det., CountBetween- │  │  widget "Signals")  │
        │ worker only        │   │ Markers)   [P6]     │  │                     │
        └────────┬───────────┘   └─────────────────────┘  └─────────────────────┘
                 │ TimeResolvedScanProducts (+ live, throttled)
        ┌────────▼───────────────────────────────────────────┐
        │ timeresolved/  types · processing · fitting (moved) │
        │                io (HDF5/NPZ/TIFF, shared)   (new)   │
        └────────┬──────────────────────────┬────────────────┘
                 │                          │
        ┌────────▼──────────┐      ┌────────▼───────────────┐
        │ workflows/        │      │ LifetimeWidget /        │
        │ time_resolved.py  │      │ LifetimeController      │
        │ (scripts)         │      │ (GUI: FLIM, Gated STED, │
        └───────────────────┘      │  Tau STED, Phasor,      │
                                   │  Signals)               │
                                   └─────────────────────────┘
```

### 3.1 `TimeTaggerManager` — the shared device layer

New file `imswitch/imcontrol/model/managers/TimeTaggerManager.py`, constructed
by `MasterController` from a new top-level `SetupInfo` block and passed to
detectors as `lowLevelManagers['timeTaggerManager']`, exactly like
`nidaqManager`.

```json
"timeTagger": {
  "serial": null,
  "mock": false,
  "channels": {
    "photons":    {"channel": 1, "trigger_v": 0.5,  "deadtime_ps": 0,    "delay_ps": 0},
    "laser_sync": {"channel": 2, "trigger_v": 0.5,  "event_divider": 1},
    "line_clock": {"channel": 3, "trigger_v": 1.5,  "delay_ps": 0},
    "frame_clock":{"channel": 4, "trigger_v": 1.5}
  },
  "conditional_filter": {"trigger": ["photons"], "filtered": ["laser_sync"]},
  "software_clock_hz": null,
  "test_signal": []
}
```

Responsibilities:

- **Connection**: `createTimeTagger(serial)` once, `freeTimeTagger` on
  `finalize()`. `mock: true` or a missing library → `MockTimeTagger` (§3.4),
  logged once at startup, visible in the widget and in metadata.
- **Channel roles**: `channel('laser_sync') -> int`. Roles are names, not
  numbers, everywhere above this layer. Detectors reference roles
  (`"click": "photons"`), so re-cabling is one edit.
- **Conditioning**, applied at connect and on change: trigger level, input
  delay, dead time, event divider, conditional filter, test signal, software
  clock. Every setting is a `Parameter` on the manager so the Settings tree
  and scripts can change them; every setting is snapshotted into
  `TimeResolvedScanProducts.metadata['time_tagger']`.
- **Measurement factory with a lock**: `with tt.measurement(Counter, ...) as m:`
  — measurements are created and freed through the manager so a tutorial's
  `Scope` and the FLIM worker's `Flim` never race on the hardware, and so
  nothing leaks when a script is stopped mid-way (the ImScripting
  `OperationCancelled` path must free the measurement).
- **Health**: `health() -> TimeTaggerHealth(rates_per_role, overflows_delta,
  sync_rate_vs_configured, model, serial, is_mock)` sampled at a slow cadence
  by a background `Counter`, and `sigHealth` for the widget. Overflows during
  a scan are passed to the FLIM detector, which marks the frame invalid in
  metadata (and, optionally, refuses to record it — decision D7).

**Backward compatibility**: a setup with a `SwabianTimeTaggerManager` detector
and *no* `timeTagger` block keeps working: the detector constructs a private
`TimeTaggerManager` from its own `click_channel`/`start_channel`/`line_channel`
and trigger properties, with a deprecation warning naming the new block. The
config editor gets a "migrate to timeTagger block" action (P1).

### 3.2 `SwabianTimeTaggerManager` slimmed

Keeps: scan lifecycle, pixel-marker generation, the `Flim` worker, the
`TimeResolvedDetectorMixin`. Loses: connection management, trigger-level
parameters (they move to the device manager; the detector's parameters become
role *selections*: `click = photons`, `start = laser_sync`, `line =
line_clock`, `frame = frame_clock | none`), and the fitters, which move to
`timeresolved/fitting.py` with the worker's cached trig tables preserved as a
`LifetimeFitter` object built once per scan.

New in the worker:

- `Flim(frame_begin_channel=…)` when a frame role is configured, with
  `n_frame_average` for hardware-side accumulation (replaces the software
  `accumulate_mode` average; the old parameter stays as an alias for one
  release).
- `getReadyFrame()`/`FlimFrameInfo` instead of `getCurrentFrame()` for the
  final frame: the card tells us the frame is complete; we stop guessing from
  `sigScanDone` timing. The 1 s live preview (`LIVE_PREVIEW_S`) becomes a
  setup parameter (closes the parked ROADMAP item).
- `n_pixels = Nx·Ny·S` with linesteps, and a frame per outer-axis position
  for z/t stacks (§7).
- `getOverflows()` delta per frame → `metadata['overflows']`.
- Live products throttled (`include_live_products` already exists) and
  published through a new `sigTimeResolvedProducts(products)` signal, so the
  widget never reads private attributes.
- Cube kept in the vendor's integer dtype.

### 3.3 `TimeTaggerFacade` — the scripting surface

Added to `MicroscopeFacade` as `facade.time_tagger`, built by
`buildWorkflowFacade()` when the setup has a `timeTagger` block. Thin,
synchronous, cancel-aware (every blocking call polls the script's cancel
token), returns plain NumPy/dataclasses:

```python
tt = facade.time_tagger
tt.channels()                                   # {role: ChannelInfo}
tt.count_rates(['photons','laser_sync'], duration_s=1.0)  # {role: Hz}
tt.period('laser_sync', duration_s=2.0)         # PeriodResult(mean_ps, std_ps, n, histogram)
tt.scope(['line_clock','frame_clock'], window_ns=2e6, trigger='frame_clock')  # ScopeTrace
tt.correlation('photons', 'laser_sync', binwidth_ps=16, n_bins=2000)         # CorrelationResult
tt.histogram('photons', 'laser_sync', binwidth_ps=32, n_bins=391)            # decay/IRF
tt.trigger_sweep('photons', levels_v=np.linspace(-1.0, 0.0, 21), duration_s=0.2)
tt.record_tags(path, roles, duration_s)         # .ttbin via FileWriter
tt.health()
tt.set_trigger_level(role, v); tt.set_input_delay(role, ps)  # write-back helpers
tt.raw                                          # the TimeTagger handle, under the lock (D5)
```

Every result dataclass has a `summary()` string the tutorials print and a
`to_dict()` the tutorials can save. Nothing is exported through
`api.imcontrol` directly; the facade is the one entry point, consistent with
`facade.time_resolved`.

### 3.4 `MockTimeTagger` — the second backend

New module `imswitch/imcontrol/model/interfaces/timetagger_mock.py`
implementing the subset of the `TimeTagger` module API the code and the
tutorials use: `createTimeTagger`, `freeTimeTagger`, `setTriggerLevel`,
`setInputDelay`, `setDeadtime`, `setEventDivider`, `setConditionalFilter`,
`setTestSignal`, `getOverflows`, `getSerial`, `getModel`, `Counter`,
`Countrate`, `Histogram`, `TimeDifferences`, `Correlation`, `Scope`,
`EventGenerator`, `Flim` (incl. `frame_begin_channel`, `getCurrentFrame`,
`getReadyFrame`, `getSummedFrames`), `CountBetweenMarkers`, `FileWriter`.

Behind it, a **signal model** generates tags on demand (no real-time thread
is needed; measurements compute their result analytically or by sampling when
read):

- laser sync: period `1/f_rep` with configurable jitter;
- line and frame clocks: taken from the **simulated scan plan** that
  `managers/mockscan/` already produces for APD/PMT, so the mock card sees
  exactly the edges the mock DAQ "emits" — same count, same period, same
  flyback — which is what makes the line-clock tutorials meaningful;
- photons: Poisson, from a synthetic sample (a lifetime map and an intensity
  map, e.g. two lifetime populations of beads) convolved with a Gaussian IRF
  of configurable width and `t0`; dark counts; optional afterpulsing;
- a configurable per-channel pulse amplitude so trigger-level sweeps show a
  plateau;
- an "overflow when total rate > N" rule so the bandwidth tutorial can
  demonstrate the conditional filter;
- faults you can switch on from a tutorial (`mock.faults.missing_line_clock =
  True`) so the debugging tutorials have something to find.

Shipped mock setup: **`galvo_flim_mock_scan_setup.json`** = the existing galvo
APD mock setup + a `timeTagger` block (`mock: true`) + a `FLIM` detector +
`Lifetime` widget. All tutorials and the widget's UI tests run on it. The
real library's `createTimeTaggerVirtual` (replay of a `.ttbin`) is used
additionally where the library is installed, so one recorded rig session can
drive the test suite with real tags (P2, optional).

---

## 4. Calibration and debugging tutorials

Location: `imswitch/_data/user_defaults/scripts/tutorial/timetagger/` (a
fourth tutorial block next to `basic/` and `scanning/`), so they fall under the
existing `test_shipped_tutorials.py` contract: header format, named mock setup,
headless run in CI. The three existing `workflows/timeresolved/` scripts get
the same header and are folded into the block as its last three steps.
Helper module: `timetagger_helpers.py` (pretty-printing, plotting into the
Output panel as text sparklines, writing a results JSON next to the script).

Each tutorial follows the same arc — *what the signal should look like →
measure it → compare with the design value → what to change if it differs →
optionally write it back*.

| Step | Script | Measures / teaches | Swabian API | Writes back |
|---|---|---|---|---|
| 01 | `01_meet_the_card.py` | Identify the card (model, serial, channel count, resolution), list role → channel mapping, enable the built-in test signal on one channel and count it. Explains mock vs. real. | `getSerial`, `getModel`, `setTestSignal`, `Counter` | — |
| 02 | `02_count_rates_and_trigger_levels.py` | Count rates of every role for 1 s; trigger-level sweep on the photon channel; find the plateau; explain SPAD/PMT polarity. | `Countrate`, `setTriggerLevel` | `trigger_v` for the photon role (after confirmation prompt pattern: print the suggestion, apply only if `APPLY = True`) |
| 03 | `03_laser_sync.py` | Rep rate, period, period jitter (histogram of successive differences), missing-pulse detection; compare to `laser_rep_rate_mhz`; explain why the phasor fit needs it. | `Countrate`, `TimeDifferences` | `laser_rep_rate_mhz` on the FLIM detector |
| 04 | `04_bandwidth_and_overflows.py` | Total tag rate vs. the card's link budget; show `getOverflows()` climbing with an unfiltered 80 MHz sync; enable the conditional filter / event divider and show it stop. | `getOverflows`, `setConditionalFilter`, `setEventDivider`, `setDeadtime` | `conditional_filter` block suggestion |
| 05 | `05_irf_and_t0.py` | Photon-vs-sync histogram with a scattering/reflective sample: IRF FWHM, peak position, suggested `t0_ps`; log-scale text plot; shows what a truncated window looks like. | `Histogram`, `setInputDelay` | `t0_ps`, `binwidth_ps`/`n_bins` suggestion |
| 06 | `06_line_clock_during_a_scan.py` | Runs the configured scan (`runScanAndWait`) while counting line-clock edges and measuring their period: edges = Ny·S?, period = Nx·dwell + flyback?, jitter; detects the M9 linestep edge-count bug if present. Prints a design-vs-measured table. | `Counter`, `TimeDifferences` + scan info from `loadScanParamsFromFile` | — (diagnostic) |
| 07 | `07_frame_clock_and_pixel_markers.py` | Frame-clock → first-line delay; builds the same `EventGenerator` pixel markers the detector uses and checks the last pixel-end lands before the next line edge; `Scope` trace of frame/line/pixel-begin for one line, printed as a timing diagram. | `Scope`, `EventGenerator`, `Correlation` | — |
| 08 | `08_line_delay_alignment.py` | Runs a scan with the APD and the FLIM detector both participating on a structured mock sample; cross-correlates the TimeTagger intensity image against the APD image → shift in pixels → µs → suggested `line_clock.delay_ps`; applies it via `setInputDelay` and re-runs to show the shift go to zero. The card-side counterpart of the M9 detector-sync fix. | `setInputDelay`, Flim intensity product | `channels.line_clock.delay_ps` |
| 09 | `09_record_raw_tags.py` | Records all roles for N seconds to `.ttbin`; shows how to open it with `createTimeTaggerVirtual` and re-run 05 on it offline. | `FileWriter`, `createTimeTaggerVirtual` | — |
| 10 | `10_flim_setup_check.py` | The pre-flight: runs 02/03/04/05 checks silently, prints a green/red checklist ("sync rate matches config", "window spans a period", "no overflows in 2 s", "photons/sync ratio plausible"), the same checklist the widget's Signals tab shows. | all of the above | — |
| 11 | `11_binned_photon_arrivals.py` | (existing, re-headed) | `Flim` cube | — |
| 12 | `12_gated_sted.py` | (existing, re-headed; gates taken from a JSON preset file like `scan_params/`) | — | — |
| 13 | `13_tau_sted.py` | (existing, re-headed) | — | — |

Mode setup guides (G3) are docs pages, one per mode, each ending with a
"verify with tutorials N, M" line and a setup-JSON snippet:

- `docs/timetagger/index.rst` — the card, roles, cabling, bandwidth budget,
  conditioning, metadata; the mock.
- `docs/timetagger/mode-flim.rst`, `mode-gated-sted.rst`, `mode-tau-sted.rst`,
  `mode-two-colour-flim.rst` (two photon roles → two FLIM detectors sharing the
  sync and line roles; P6), `mode-intensity-only.rst` (the counter detector;
  P6).
- `docs/timetagger/debugging.rst` — symptom → tutorial table ("image is all
  zeros", "image shifted by a line", "lifetimes all ~0.9 ns", "frame never
  finishes", "overflows").

The `scripts/diagnostics/measure_laser_rep_rate.py` CLI stays for
no-GUI use but is reduced to a wrapper around the same measurement function
the tutorial and the facade use.

---

## 5. The Lifetime widget

Widget key **`Lifetime`**; files `view/widgets/LifetimeWidget.py`,
`controller/controllers/LifetimeController.py`
(`ImConWidgetController + StatefulComponentMixin`), dock name "Lifetime
(FLIM / STED)", default position: right dock next to Scan. `FLIMHist` stays as
a key for one release and resolves to the Lifetime widget opened on its
histogram view (D4).

### 5.1 Layout

```
┌ Lifetime (FLIM / STED) ──────────────────────────────────────────────────────┐
│ Detector [FLIM ▾]   Mode: ( FLIM ) ( Gated STED ) ( Tau STED ) ( Phasor ) ( Signals ) │
├──────────────────────────────────────────┬───────────────────────────────────┤
│  Decay (log ▢)                           │  mode panel                        │
│  ▲ counts                                │  ┌─ Gated STED ────────────────┐   │
│  │   ╭╮                                  │  │ gate  start  stop   colour  │   │
│  │   │ ╰╮      ┃early┃  ┃   late   ┃     │  │ early 0.50   2.50   ■       │   │
│  │   │  ╰─╮    ┃     ┃  ┃          ┃     │  │ late  2.50   8.00   ■       │   │
│  │  ╭╯    ╰──╮ ┃     ┃  ┃          ┃     │  │ [+] [−] presets ▾ ratio ▢  │   │
│  └──┴────────┴─┴─────┴──┴──────────┴──▶ ns│  │ Live gate layers in viewer ▣│   │
│  IRF peak 1.02 ns · t0 0 ps · 391×32 ps  │  └─────────────────────────────┘   │
├──────────────────────────────────────────┴───────────────────────────────────┤
│ ● photons 1.2 Mcps  ● sync 80.00 MHz (cfg 80.0)  ● line 0 Hz  ● frame 0 Hz  overflows 0  [mock] │
│ [ Run once ]  [ Live ▶ ]  [ Stop ]   accumulate [ 4 ] frames   name [tau_sted_cells]  [ Save ] │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Decay plot** (always visible): aggregated decay of the last frame, log-y
  toggle, IRF-peak and `t0` markers, draggable `LinearRegionItem`s for gates
  (in Gated STED mode), a cursor readout. Clicking a napari ROI (the existing
  `ViewerToolManager` ROI tool) switches the decay to that ROI's pixels
  (requires the cube; the panel says so and offers to enable it).
- **FLIM panel**: fit method, min counts, rep rate (with a *measure* button →
  runs the tutorial-03 measurement in-process and fills the field),
  window/bins/t0 (with *find t0 from decay*), lifetime colour range, and the
  two histogram views that FLIMHist has today. Display options: lifetime
  image (today's), intensity image, and an **intensity-weighted lifetime
  overlay** (HSV: hue = τ, value = intensity) pushed as an RGB napari layer —
  the standard FLIM picture, missing today.
- **Gated STED panel**: gate table with colours, presets (JSON files, the
  same ones tutorial 12 loads), ratio image (late/early) toggle, *live gate
  layers* toggle: each gate image is a napari layer `FLIM › gate:late`,
  updated on every live product, scaled from the scan pixel size.
- **Tau STED panel**: the FLIM panel plus a τ-vs-intensity scatter (the
  tau-STED quality view) and accumulation count (hardware
  `n_frame_average`).
- **Phasor panel**: (g, s) cloud on the universal semicircle at ω = 2π f_rep,
  per-pixel from the cube or per-frame from the aggregated decay; a cursor
  circle selects phasor-space pixels and highlights them as a napari labels
  layer. Needs the cube; the panel enables it.
- **Signals panel**: live per-role rates (`Counter`), configured vs measured
  sync rate, overflow counter, per-role trigger-level spin boxes, *trigger
  sweep* button (plots rate vs level), *scope snapshot* button (one `Scope`
  trace of line/frame/sync drawn as digital traces), the tutorial-10
  checklist, and "Open tutorial…" links that open the relevant script in the
  Scripting tab (`mainWindow.setCurrentModule('imscripting')` + the Files
  controller's open).
- **Status strip**: the health snapshot, always on, mock badge, the active
  detector's role mapping on hover.
- **Footer**: Run once (= `facade.scan.run_once` through the workflow path,
  so the widget and the scripts produce identical products and files), Live
  (repeat), Stop, accumulate N, measurement name, Save (shared `timeresolved/io`
  savers; HDF5 + TIFFs; path from the Recording widget's folder).

### 5.2 Controller wiring

- Listens to `detector.sigTimeResolvedProducts` (new, throttled live
  products) — not to `sigUpdateImage`, not to private attributes.
- Pushes layers through `ImageWidget.addStaticLayer`/`setImage` via a small
  `LayerSink` helper owned by the controller so layer names are stable and
  removed when the mode changes (fits the `dynamic-layer-lifecycle` plan).
- Mode and gate changes call `configureTimeResolvedProducts(...)`; they take
  effect on the next scan, and the panel says "applies to next scan" until
  then. **Every** widget-initiated configure is paired with a `clear()` when
  the mode is switched off, fixing gap §1.2 for the GUI path; the workflow
  `run()` gets the same `finally: clear()`.
- Holds a `LeasePurpose.WORKFLOW` lease on the detector only while Run/Live is
  active, like the workflow does.
- Refuses Run while a recording with the FLIM detector is in progress unless
  the recorded product selection (§6) is set, and says why.
- State persisted (gates, presets, mode, display options, accumulate N) via
  `StatefulComponentMixin`; trigger levels and delays are *not* persisted
  here — they live on the device manager and follow the "setup file wins"
  rule used for `cameraPixelSizeUm`.

### 5.3 Tests

- `_test/unit/test_lifetime_controller.py`: headless, fake products →
  expected layer calls, gate edits → configure calls, mode switch → clear.
- `_test/ui/test_lifetime_widget.py`: Qt, gate region drag updates the table
  and vice versa; preset load; log toggle.
- Shipped-setup smoke: the widget constructs on `galvo_flim_mock_scan_setup.json`
  and one mock scan produces non-empty gate layers.

---

## 6. Data, saving, recording

- **`TimeResolvedScanProducts` v2** adds: `phasor_g`, `phasor_s` (per pixel,
  optional), `frames_accumulated`, `overflows`, `irf` (optional, from the
  last IRF measurement stored on the device manager), `time_tagger` metadata
  (model, serial, channel roles and conditioning), `format_version = 2`.
  Cube in integer dtype. `max_retained_products` honoured (ring buffer) or
  removed (D8).
- **`timeresolved/io.py`**: the HDF5/NPZ/TIFF writers move out of
  `workflows/time_resolved.py` so the widget and the workflows share them; the
  HDF5 layout gains `time_tagger/` and `phasor/` groups and a
  `format_version` attribute. A reader (`load_products(path)`) comes with it,
  used by tests and by ImProcess later (the roadmap's "lifetime overlay when
  FLIM data is present").
- **Recording**: today a recording of a FLIM detector saves the lifetime
  image only. P5 adds a detector parameter *recorded product* ∈ {lifetime,
  intensity, gate:<name>} and, when the cube is enabled, attaches the full
  products HDF5 next to the recording file (`<rec>_timeresolved.h5`) via a
  `RecordingManager` post-scan hook rather than teaching the recorder about
  `(Y, X, bin)` frames. Decision D7.

---

## 7. Scan integration (red zone — hardware verification before enabling)

| Change | Why | Gate |
|---|---|---|
| `frame_begin_channel` from the `frame_clock` role | Frames anchored by the card, not by `sigScanDone` timing; enables `n_frame_average` and stacks | Config-gated: only when a frame role is configured |
| `n_pixels = Nx·Ny·S` | Linesteps are ignored today (gap §1.4) | Unit test on marker count; rig check with tutorial 06 |
| Outer axes → one Flim frame per outer position; cube axes `(z|t, y, x, bin)` | Lifts the "2D only" restriction the workflows raise on | Memory budget check (`docs/design/plans/memory-budgets.md`); products stay opt-in |
| `line_clock.delay_ps` applied with `setInputDelay` | Card-side, ps-resolution fix for the APD/TimeTagger line offset; measured by tutorial 08 | Default 0 → no behaviour change; documented in the M9 section of the roadmap as the detector-side half |
| Overflow → frame flagged invalid | Silent corruption today | Metadata only by default; refusing to record is D7 |
| Conditional filter / divider at connect | Bandwidth; depends on card model (§13) | Default: no filter (today's behaviour) until the rig says otherwise |
| `pixel_end = begin + period − 1 ps` | Keep; document the reason in the device doc | — |

---

## 8. Phases

Each phase is one or two PRs, lands green on CI without hardware, and names
what must be checked on the rig before the next phase relies on it.

**P0 — Review and rig facts.** This document, revised until agreed. You answer
§13. Outcome: decisions D1–D10 recorded in §12.

**P1 — Device layer + mock, no behaviour change.**
`TimeTaggerManager`, `SetupInfo.timeTagger` + schema + config-editor section
and template fix (gap §1.6), `MockTimeTagger` with the signal model wired to
`mockscan`, `galvo_flim_mock_scan_setup.json`, detector takes the device
manager (with the compatibility path), fitters moved to
`timeresolved/fitting.py` (dead code removed, gap §1.7), `clear()` in the
workflow's `finally` (gap §1.2), docstring/ns fix (gap §1.9).
*Tests*: the worker runs end-to-end against the mock in CI for the first time;
fit methods recover the mock sample's lifetimes within tolerance; template
drift test updated.
*Rig check*: existing FLIM scan unchanged on the old config; new block connects.

**P2 — Facade + tutorials 01–05, 09, 10.** `TimeTaggerFacade`,
`timetagger_helpers.py`, device-level tutorials, `docs/timetagger/index.rst`,
`debugging.rst`, `tutorial/README.md` rows, `update_user_defaults_history`.
The CLI script becomes a wrapper.
*Rig check*: 02–05 on the real card; record one `.ttbin` session with 09 for
the optional replay tests.

**P3 — Scan-aware diagnostics + scan correctness.** Tutorials 06–08; frame
role, linesteps, `line_clock.delay_ps`, overflow flagging, configurable
preview cadence. Roadmap M9 section updated with the card-side half.
*Rig check*: 06 line count/period against design; 08 shift → 0 after delay.

**P4 — Lifetime widget v1.** FLIM and Tau STED panels, Signals panel, status
strip, footer, `sigTimeResolvedProducts`, `LayerSink`, intensity-weighted
overlay, FLIMHist alias. `docs/gui.rst` section replaces the FLIMHist one.
*Rig check*: live τ overlay on a reference dye; Signals checklist green.

**P5 — Gated STED panel + shared io + recording hook.** Gate table and
regions, presets, live gate layers, ratio image, `timeresolved/io.py` with
reader, products v2, recorded-product parameter, attached HDF5.
Tutorials 11–13 re-headed onto the mock setup and under test.
*Rig check*: gated STED image from the widget equals the one from tutorial 12
on the same scan (same file contents).

**P6 — Phasor panel, two-colour, intensity detector, outer axes.** Phasor
cloud with selection → labels layer; second photon role → two FLIM detectors
on one card (`mode-two-colour-flim.rst`); `TimeTaggerCounterManager`
(`CountBetweenMarkers`, `mode-intensity-only.rst`); z/t stacks in products.
*Rig check*: two-colour on a dual-labelled sample; z-stack cube memory.

**P7 — Validation campaign and release.** Roadmap 13.A FLIM row closed with
the per-fit-method convergence test on a reference dye (now scripted as
tutorial 10's extended mode), changelog, FLIMHist removal scheduled, old plan
marked superseded by this one.

Rough size: P1 and P4 are the big ones (device layer + mock; widget). P2/P3
are many small files. P5/P6 are medium. Everything before P4 is invisible to a
user who never opens the Scripting tab, except the fixed template and the
cleared products.

---

## 9. Later options (recorded, not planned)

- IRF-deconvolved tail fit / two-exponential fit using the stored IRF.
- Hardware gating via `GatedChannel`/`DelayedChannel` on the card (software-
  defined but zero-cost at acquisition time); needs the Pulse Streamer wired
  to a card channel for a STED-pulse reference.
- Live phasor during STED alignment (sub-second Flim frames with
  `frame_begin_channel` and a fast scan).
- ImProcess: open `*_timeresolved.h5`, re-gate and re-fit offline with the
  same `timeresolved/` code; lifetime overlay processor.
- `TimeTagStream` for photon-by-photon export (FCS, antibunching) — a separate
  capability as the old plan said.

---

## 10. Files touched (first cut)

| Area | Files |
|---|---|
| New model | `model/managers/TimeTaggerManager.py`, `model/interfaces/timetagger_mock.py`, `model/timeresolved/fitting.py`, `model/timeresolved/io.py`, `model/managers/detectors/TimeTaggerCounterManager.py` (P6) |
| Changed model | `SwabianTimeTaggerManager.py` (slimmed), `model/SetupInfo.py` (`timeTagger` block), `MasterController.py` (construct + lowLevelManagers), `workflows/facade.py` (`time_tagger`), `workflows/time_resolved.py` (clear, io import), `model/timeresolved/types.py` (v2) |
| Config | `configeditor/schemas/…/TimeTaggerManager.json`, `sections/timeTagger.json`, template fix, `setupinfo-reference.rst` |
| GUI | `view/widgets/LifetimeWidget.py`, `controller/controllers/LifetimeController.py`, both `__init__.py` maps, `ImConMainView.py` dock tables, `ViewSetupInfo.py` docstring |
| Scripts | `scripts/tutorial/timetagger/01..13_*.py`, `timetagger_helpers.py`, `gate_presets/*.json`, `tutorial/README.md`, `scripts/diagnostics/measure_laser_rep_rate.py` (wrapper) |
| Setups | `imcontrol_setups/galvo_flim_mock_scan_setup.json` |
| Docs | `docs/timetagger/*.rst`, `docs/gui.rst`, `docs/devices/detectors.rst`, `docs/scripting.rst` (tutorial block), `docs/changelog.rst`, `ROADMAP.md` (new milestone "Lifetime 2.0" + M9 note), this plan |
| Tests | `test_timetagger_manager.py`, `test_timetagger_mock.py`, `test_swabian_flim_worker_mock.py`, `test_timetagger_facade.py`, `test_lifetime_controller.py`, `ui/test_lifetime_widget.py`, shipped-tutorial test picks up the new block automatically |

---

## 11. Risks

- **Bandwidth model is card-dependent.** Whether an unfiltered 80 MHz sync
  overflows depends on the model (Time Tagger 20 vs Ultra vs X). The plan
  defaults to today's behaviour and lets tutorial 04 and the Signals panel
  prove the need. See §13.
- **`Flim` with `frame_begin_channel` changes frame semantics.** Config-gated
  so a rig without the frame clock cabled keeps the current path.
- **Shared-hardware lock vs. scan worker.** A tutorial's `Scope` while a scan
  runs must not stall the FLIM worker; the factory lock is per-measurement
  creation, not per-read, and the facade refuses heavy measurements while a
  scan lease is active (clear error).
- **Mock fidelity.** A mock that is too kind hides real-rig problems; the
  fault switches and the `.ttbin` replay path exist to keep it honest.
- **Widget scope creep.** The phasor selection and ROI decay are the
  features most likely to balloon; they are last (P6) and independent.

---

## 12. Decisions for this review round

- **D1 — Device block.** Introduce `SetupInfo.timeTagger` with named roles as
  proposed, with the compatibility path for the old detector-only config? Or
  keep channel numbers on the detector and only add the device layer
  internally?
- **D2 — Tutorial home.** `scripts/tutorial/timetagger/` under the shipped-
  tutorial test contract (proposed), or keep calibration under
  `scripts/workflows/`?
- **D3 — Write-back policy.** Tutorials print suggestions and apply only when
  an `APPLY = True` constant is set (proposed), or always apply, or never
  apply and leave it to the widget?
- **D4 — FLIMHist fate.** Alias for one release then remove (proposed), or
  remove at once, or keep as a separate small widget?
- **D5 — Raw handle.** Expose `facade.time_tagger.raw` (the vendor object,
  under the lock) for advanced scripts? Proposed yes, documented as
  unsupported territory.
- **D6 — Widget name.** `Lifetime` (proposed) vs `FLIM` vs `TCSPC`.
- **D7 — Overflow and recording.** On overflow during a scan: flag in metadata
  only (proposed default), or also refuse to hand the frame to a recording?
  And: attach the products HDF5 next to recordings (proposed) vs teach
  `RecordingManager` cube frames?
- **D8 — `max_retained_products`.** Honour as a ring buffer, or drop the field.
- **D9 — Accumulation.** Replace the software average with Flim's
  `n_frame_average` (proposed; needs the frame role) and keep the old
  parameter as an alias for one release?
- **D10 — Phase order.** Widget before scan-aware diagnostics (P4 before P3)
  if you want something visible sooner? The plan puts correctness first.

## 13. Rig facts the plan needs

1. Which Time Tagger model(s) and firmware (20 / Ultra / X)? Channel count,
   and whether the Ultra/X conditional filter and hardware Flim options are
   available to you.
2. Laser sync into the card: direct at the full rep rate, or divided? Do you
   see overflows today (`getOverflows()` is never read, so you may not know)?
3. Is `frameStartClockLine` (DAQ) cabled to a card channel today? Is there a
   pixel clock, or only the line clock?
4. Photon detector(s): SPAD/APD or PMT, pulse polarity and amplitude (sets the
   trigger-level defaults and the sweep range in tutorial 02).
5. One or two photon channels? Any plan for two-colour FLIM or a second
   card?
6. Typical scan: Nx·Ny, dwell, laser rep rate, expected photons per pixel —
   sets the mock's defaults and the memory budget for the cube.
7. Where measurements should land (today's scripts hard-code `D:/Measurements`;
   the widget would use the Recording widget's folder).
8. Is the Pulse Streamer in use on the STED rig at all? (In code it is never
   constructed.) If yes, the hardware-gating option in §9 becomes plannable.

---

## 14. Relationship to existing plans

- Supersedes `docs/design/plans/time-resolved-detector-workflows.md` once P1
  lands; its contract and workflows are kept, its pending items (hardware
  validation, multidimensional output, second backend) are P7, P6 and P1 here.
- Complements ROADMAP **M9** (detector-sync): M9 fixes the designer side
  (`phase_delay` into `line_clock`); this plan measures the offset with the
  card and applies a card-side delay, so both detector families align on rigs
  where M9 is not yet enabled.
- Uses the lease model from `detector-acquisition-selection.md` as it stands
  (`LeasePurpose.WORKFLOW`), adds no new purpose.
- Follows `dynamic-layer-lifecycle.md` for the gate/overlay layers.
