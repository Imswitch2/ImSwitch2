# SimplePointScan: plan

*Work-in-progress plan. The user documentation will be a Sphinx `.rst` page
written in P4.*

- **Status:** planned 2026-09-25; P0 done 2026-09-25 (speed rule, estimator,
  tests); P1 not started.
- **Branch:** `feat/simple-point-scan`, worktree `../Imswitch2-simple-point-scan`.
- **Base:** stacked on PR #49 (`claude/quizzical-hofstadter-88bff6`, "a refused
  scan design ends the request with its reason"). P0 reports its new refusal
  through #49's `ScanDesignRefusedError`, so this branch must be rebased onto
  `main` once #49 merges.

## 1. Goal

A second NI-DAQ point-scan panel for APD/PMT confocal and STED rigs. It
replaces the Advanced panel's line-program matrix with a few controls a first-time
user can read. It is built on the Advanced backend. The panel has two modes:

- **Overview** is for looking around and finding cells. It scans live over the
  largest field the scanners can reach, using big pixels and the fastest safe
  dwell. Each frame should take one second or less.
- **Acquisition** is for taking the image. It uses a region drawn as a
  rectangle in the napari viewer. Two sliders set the pixel size (from the
  overview pixel size down to Nyquist) and the dwell time (from the fastest
  safe value up to 10 ms). A live readout shows how long the scan will take.

Both modes share these controls:

- **Laser channels.** Each channel is one pass over the line. Lasers in the same
  channel fire together; lasers in different channels are recorded separately.
- **Dimensions.** A spin box sets how many there are. Each one gets a combo box
  listing the scanning axes and T, so the user can build X-Y, X-Z, X-Y-Z or
  X-Y-T.

Everything the panel offers is derived from the setup file: sample rate, scanner
velocity/acceleration and voltage limits, conversion factors, laser wavelengths
and which lasers have an analog channel, and detector types. Nothing is
hard-coded for a particular rig.

## 2. Decisions (Lenny, 2026-09-25)

| Question | Decision | Consequence |
|---|---|---|
| Coexist with Advanced? | **Replace.** Selected per rig with `scanWidgetType: "SimplePointScan"` | Still one scan panel per setup. Moving a rig between Simple and Advanced is a config change, and scan files load in both directions (see §5.2). |
| What does T mean in an acquisition? | **N frames, back to back, one run, no interval** | Intervals stay a Recording feature (ScanLapse). |
| Which lasers does the overview use? | **Channel 1 only.** A dropdown picks another channel | The overview always needs just one line pass. |
| Does Acquire save? | **Optional "Save" toggle** | Arms an ordinary Scan-once recording bound to this panel. There is no second writer. |

## 3. Findings that shape the design

### F1. One NI-DAQ scan panel per setup

The `'Scan'` dock loads `ScanWidget{scanWidgetType}` and
`ScanController{scanWidgetType}` (`ImConMainView.py:582`,
`ImConMainController.py:111`). Loading a second `SuperScanController` next to it
fails in four ways:

- **Startup crash.** Every subclass inherits the exports `saveScanParamsToFile` and
  `loadScanParamsFromFile`, so `generateAPI` raises a `NameError` on the duplicate
  names (`imcommon/model/api.py:74`).
- **Settings cross-talk.** Both controllers react to the same `ScanStage`/`ScanTTL`
  shared attributes, so each overwrites the other's settings.
- **Doubled signals.** The `sigScanBuilt` and `sigScanStarted` relays have no owner
  guard, so every scan emits them twice.
- **Lost state.** Both register for state persistence as `'Scan'`, and the last one
  to register wins.

This confirms the "Replace" decision.

### F2. The Advanced controller already has the seam we need

`ScanControllerAdvanced.getParameters()` builds its two parameter dicts through
`_buildAnalogParameterDict()` and `_buildDigitalParameterDict()`. These delegate
to `AdvancedScanParameterSerializer`, which reads about 30 duck-typed widget
methods.

A subclass that fills the same dicts from a plain plan object inherits
everything else unchanged:

- the run lifecycle and coordinator arming;
- repeat re-arming;
- recording layouts (`getAcquisitionLayouts`, `getNumCamTTL`, `getDimsScan`);
- per-line-step analog laser power;
- #49's refusal handling.

A saved scan is simply `{analogParameterDict, digitalParameterDict, mode}`
(`SuperScanController.getComponentState`). So any scan that Simple can express
loads in Advanced as it is.

### F3. Flyback dominates overview frame time

These durations come from real `GalvoScanDesigner` builds on
`galvo_apd_mock_scan_setup.json`, which uses `vel_max` 0.1 µm/µs and
`acc_max` 1e-4 µm/µs², the same values as `example_sted`:

| Field, pixels, dwell | Real frame time | Time spent on pixels |
|---|---|---|
| 200 µm, 200 × 200, 20 µs | **1.65 s** | 0.80 s |
| 200 µm, 100 × 100, 20 µs | **0.80 s** | 0.20 s |
| 100 µm, 200 × 200, 20 µs | 1.31 s | 0.80 s |
| 50 µm, 1000 × 1000, 20 µs | 21.45 s | 20.0 s |

Each line spends about 4 ms turning around. A readout of pixels × dwell would
be wrong by up to a factor of four in overview. The prediction is only as good
as the configured `vel_max`/`acc_max`; P5 compares it with measured rig times.

### F4. The duration can be predicted exactly and cheaply

Build the same scan with 2 and 3 steps on every slow axis, then extrapolate
multilinearly to the real counts. The fast axis keeps its real length, so its
turnaround is exact.

| Case | Full build | Estimate | Error | Estimate cost |
|---|---|---|---|---|
| 200 × 200 µm, 1 µm, 20 µs | 1.6511 s | 1.6482 s | −0.17 % | 3 ms |
| 100 × 60 µm, 0.5 µm, 20 µs | 0.7880 s | 0.7864 s | −0.20 % | 2 ms |
| 50 × 50 µm, 0.05 µm, 20 µs | 21.4518 s | 21.4510 s | −0.00 % | 4 ms |
| 20 × 20 µm, 0.04 µm, 50 µs, 2 line steps | 25.9006 s | 25.9007 s | +0.00 % | 5 ms |
| XYZ 50 × 40 × 4 µm, 2 line steps, 0.5 ms slice delay | 5.1234 s | 5.1184 s | −0.10 % | 8 ms |
| XYZ 100 × 100 × 10 µm, 1 ms slice delay | 5.2716 s | 5.2545 s | −0.32 % | 6 ms |
| XZ 30 × 6 µm (Z stepped) | 0.1738 s | 0.1738 s | +0.00 % | 2 ms |

The error comes from the slow galvo axis travelling to its first position and
back. The proxy's slow axis spans only 2–3 steps, so that travel differs from
the real scan's. It amounts to a few milliseconds per slow sweep, so it shows
in relative terms only on very short scans: P0's XZ test case with a stepped
piezo fast axis estimates 9.8 ms for a 9.1 ms scan. The tested bound is
**1 % or 2 ms, whichever is larger**.

A full build takes up to 1.2 s (1000 × 1000 at 100 µs), far too slow for a
slider. The estimate reuses the designer's own code, so it cannot drift when the
designer changes. Its cost grows with the fast-axis length: a 300-pixel line at
10 ms dwell takes 230 ms to estimate. The panel therefore debounces the readout.

### F5. `GalvoScanDesigner` does not enforce `vel_max` (fixed in P0)

The fast-axis scan speed is `step / dwell`. `__d2scan_poly` builds the turnaround
with `vel_max` but never compares the scan speed against it. The 100 µm line on
the mock setup:

| Step, dwell | Scan speed | Reach of the X signal | Peak speed |
|---|---|---|---|
| 1 µm, 20 µs | 0.05 µm/µs | ±62.5 µm | 0.10 µm/µs |
| 2 µm, 20 µs | 0.10 µm/µs | ±100 µm | 0.10 µm/µs |
| 2 µm, 10 µs | **0.20 µm/µs** | **±250 µm** | 0.20 µm/µs |
| 4 µm, 10 µs | **0.40 µm/µs** | **±850 µm** | 0.40 µm/µs |

Today only the voltage check stops such a scan, with the generic message "Signal
voltages outside scanner ranges". With enough voltage headroom it runs the
scanner at several times its configured limit.

The panel needs this rule to set its minimum dwell, and Advanced users need it
for their own scans. Per the recurring lesson "a rule enforced in one place is
not enforced", the rule lives in the designer and the panel only asks it.

**Done in P0.** `make_signal` raises `ScanDesignRefusedError`, so the scan
managers, the Advanced run path, its plots and the estimate all refuse the same
designs. At a sweep of exactly `vel_max`, the turnaround spline's rounded
corners peak 0.25 % above the limit. That is the spline's own shape, not the
sweep, and the test allows 0.5 %.

### F6. The viewer has no scanner coordinates

Point-detector live layers get `scale` (the pixel size) but never `translate`
(`ImageWidget.setImage`). World coordinates are therefore micrometres from the
first pixel of whatever frame is on screen. No existing code maps a napari
rectangle to scanner coordinates.

The shared "Viewer Tools" Shapes layer is not usable. `ViewerToolsController`
clears it on every tool click, LineProfile reacts to every rectangle drawn on it,
and `enforce_single` keeps only one shape. The lifecycle to copy is
`NapariROISetOverlay` in `naparitools.py:1874`: it has its own layer, aligns
with the image layer, and removes itself cleanly.

Two more pitfalls:

- **Display transform.** Detector settings can rotate or flip the image before it
  is shown (`display_transform.py`), and only the forward mapping exists.
- **Stale geometry.** The mapping must use the geometry of the frame on screen,
  not the panel's current values, which may already have been edited.

### F7. Detectors determine what "separate channels" produce

- **APD.** Keeps every line step as its own channel, so 2 APDs × 2 channels give
  4 images.
- **PMT.** Sums the line steps into one image, so separate channels only exist
  on APDs. The panel says so when a PMT is present.
- **TTL-triggered cameras.** Out of scope; those rigs keep Advanced.

### F8. The setup has no objective NA

A Nyquist end for the pixel slider needs new config (§5.6).

### F9. Lasers are armed from the scan's TTL list

`LaserController.scanDevicesResolved` arms exactly the lasers that have a TTL
program, and switches on their linked `powerDevice` (e.g. 561 → 561AOTF).
Putting a laser in a channel is therefore enough. The panel lists only lasers
that have a digital line. Power entries without one, such as `561AOTF`, appear
only as the power of their gate.

## 4. Architecture

```
ScanWidgetSimplePointScan  (view: modes, region, dims, sliders, lanes)
        │  getPlan() / setPlan(plan) / signals
        ▼
ScanControllerSimplePointScan(ScanControllerAdvanced)
        │  overrides _buildAnalogParameterDict / _buildDigitalParameterDict,
        │  setParameters, updatePixels, plotSignalGraph, get/applyComponentState
        ▼
model/simple_scan.py  (pure, Qt-free, unit-tested)
   SimpleScanPlan, ScanLimits.from_setup(), plan_to_dicts(), dicts_to_plan(),
   estimate(), pixel/dwell slider mappings
        │
        ▼
GalvoScanDesigner.estimateScanTime() / scanSpeedRefusal()    ← P0
```

Everything below the controller is the existing Advanced path. The panel never
builds waveforms itself.

## 5. Design

### 5.1 Model (`imswitch/imcontrol/model/simple_scan.py`)

`SimpleScanPlan` is a dataclass that round-trips through JSON:

| Field | Meaning |
|---|---|
| `mode` | `'overview'` or `'acquisition'` |
| `dims` | For example `['X', 'Y']`, `['X', 'Z']`, `['X', 'Y', 'Z']`, `['X', 'Y', 'T']`. Entries are positioner names or `'T'`. |
| `overview` | Centre and size per overview axis, plus the channel index to use. |
| `acquisition` | Centre and size per spatial dimension, the XY pixel size, the Z step, the dwell, and the number of T frames. |
| `channels` | A list of lists of laser names, e.g. `[['561'], ['640']]`. |
| `channel_power` | Per-channel power percentage for lasers with an analog channel. |

`ScanLimits.from_setup(setupInfo)` derives the limits:

- **Scan axes.** The scanning positioners, whether each is swept smoothly
  (`is_smooth_scan_axis`), its micrometre range from `minVolt`/`maxVolt` ×
  `conversionFactor`, and `vel_max`.
- **Overview axes.** The first two scanning positioners in config order, unless
  `overviewAxes` is set.
- **Minimum dwell** is the larger of:
  - `minSamplesPerPixel / sampleRate`;
  - fast-axis step ÷ `vel_max` (from `GalvoScanDesigner.scanSpeedRefusal`'s rule;
    the model does not keep its own copy).
- **Maximum dwell** is `maxDwellMs`.
- **Nyquist pixel size** is described in §5.6.

The model's functions:

- **`plan_to_dicts(plan, limits, positioners, ttlDevices)`** returns exactly the
  two dicts `AdvancedScanParameterSerializer` produces. The channels become
  `n_linesteps = len(channels)` and
  `linestep_enable[laser][s] = laser in channels[s]`, with
  `advanced_mode = False`. Overview mode sends only the overview channel.
- **`dicts_to_plan(analog, digital)`** is the inverse. It refuses, with the
  reason, a program that uses timing windows, sequence rows, intra-pixel
  positioner movement or device locks: "This scan uses timing windows; open it
  in the Advanced panel."
- **`estimate(plan)`** returns the frame time and the total time (× Z planes × T
  frames) from `GalvoScanDesigner.estimateScanTime`.
- **Slider mappings** are logarithmic in both directions:
  - pixel size runs from the overview pixel size to the Nyquist size;
  - dwell runs from the minimum dwell at the current pixel size to the maximum.

### 5.2 Controller (`ScanControllerSimplePointScan`)

- **Parameters.** It overrides the two parameter builders to call
  `plan_to_dicts(self._widget.getPlan(), …)`. `setParameters()` goes through
  `dicts_to_plan` and then `setPlan`.
- **Two plans.** It keeps an overview plan and an acquisition plan and swaps
  them when the mode changes. Overview turns repeat on; acquisition turns it off,
  unless the user ticks Live.
- **Overview pixel count.** It picks the largest pixel count for which the
  estimated frame time with channel 1 at minimum dwell is at most
  `overviewFrameTimeS`. The search is a bisection on the estimate, a few
  milliseconds per step. Minimum dwell depends on the step, so the search
  re-evaluates it each time.
- **Geometry stamp.** On every `sigScanBuilt` it records the centre, step and
  pixel count per axis, and the detector's display transform. This is what the
  rectangle is converted with (F6).
- **Saved state.** It adds a `simplePlan` key to its component state. On
  restore it uses `simplePlan` when present, then tries to convert the
  Advanced-style dicts. If neither works it keeps the defaults and warns.
- **Save toggle.** It arms a Scan-once recording bound to this controller. The
  hook to use is still to be confirmed in P3; the candidate is
  `scanWorkflow.prepare_recording_for_scan` in `WorkflowServices.py:207`, which
  is how TriggerScope binds.
- **T frames.** One run repeats N times. The mechanism is still to be confirmed
  in P3: either a bounded variant of `_shouldContinueRepeat`, or N frames as one
  build. Also still to check: how a Scan-once recording and the acquisition
  layout see a run that contains several frames.

### 5.3 Panel

The interactive mockup shown in the planning chat, from top to bottom:

1. **Mode and start.**
   - Overview / Acquisition segmented control.
   - A Live checkbox: on by default in Overview, off in Acquisition.
   - The main button reads Start overview, Start live or Acquire, followed by
     Stop.
   - A Save toggle next to Acquire.
2. **Region.**
   - In Overview: "Full field · 200 × 200 µm".
   - In Acquisition: "Drawn region · 40 × 30 µm at (12.0, −8.5) µm". The numbers
     are editable.
   - A Draw in viewer button switches to Acquisition and gives napari a
     rectangle tool on the panel's own Shapes layer.
   - Moving or resizing the rectangle updates the numbers, and editing the
     numbers moves the rectangle.
3. **Dimensions.**
   - A spin box from 1 to 4, with one combo per dimension listing the scanning
     axes (labelled by their physical axis, e.g. "Z (ND-PiezoZ)") and T.
   - Rules: T is always last; no duplicates; the first dimension must be a
     positioner. A stepped fast axis is allowed but marked slow.
   - Z adds a range (±, around the current centre) and a step with a Nyquist-Z
     hint. T adds a frame count.
4. **Sampling.**
   - The pixel-size slider (overview → Nyquist) shows the pixel size and the
     image size in pixels.
   - The dwell slider (fastest safe → 10 ms) shows the dwell.
   - Two readouts: Per frame and Total (frames per second in Overview).
   - A note explains what multiplies the time: channels, Z planes, T frames.
     Both readouts are debounced by about 100 ms.
5. **Lasers and channels.**
   - A row of laser chips coloured by wavelength
     (`colorutils.wavelengthToHex`).
   - Channel lanes: clicking a chip puts that laser in the selected lane.
     "Add channel", "All together" and "One per laser" buttons.
   - Each lane is labelled "fired together" or "one line pass".
   - Lasers with an analog channel get a per-lane power spin box.
   - The output line says, for example, "APDred, APDgreen × 2 channels →
     4 images". A PMT adds "PMT sums channels".
   - The overview channel is chosen in a small dropdown.
6. **Expert** (collapsed): phase delay, slice delay, Save / Load scan file.

When a setting is refused, the panel shows the designer's reason next to the
readout, e.g. "Too fast for X: dwell ≥ 20 µs at 2 µm pixels". Controls are not
disabled without saying why.

### 5.4 How dimensions map to the scan

| Panel dims | `scan_dim_target_device` | Where the extents come from | Overview-plane axis not in dims |
|---|---|---|---|
| X, Y | `[X, Y, 'None']` | rectangle | — |
| X, Z | `[X, Z, 'None']` | X from the rectangle; Z range field | Y fixed at the rectangle centre (non-scanned axes are parked at `axis_centerpos` by `runScanAdvanced`) |
| X, Y, Z | `[X, Y, Z]` | rectangle; Z range field | — |
| X, Y, T | `[X, Y, 'None']` + N frames | rectangle | — |

Overview always scans the two overview axes in 2D at the current Z.

### 5.5 Mapping the rectangle to scanner coordinates

For a layer at scale `(s_y, s_x)` with the identity display transform, and the
geometry stamp `(c_x, c_y, N_x, N_y)` of the frame on screen:

- `x = c_x − (N_x − 1)/2 · s_x + col_world`
- `y = c_y − (N_y − 1)/2 · s_y + row_world`

When the detector has a display rotation or flip, its inverse is applied first.
This needs a new inverse function in `display_transform.py`, tested as a round
trip.

When the frame on screen changes geometry, for example from overview to
acquisition, the rectangle is re-projected. It stays fixed in scanner
coordinates.

The later option of giving scanning detectors a real `translate` in napari, so
the acquisition shows up inside a frozen overview like a map, is out of scope.
It touches the shared image path, and LineProfile and the viewer tools assume
the origin is the first pixel.

### 5.6 Config: an optional `scan.simplePointScan` block

```json
"simplePointScan": {
  "objectiveNA": 1.4,
  "nyquistPixelSizeUm": null,
  "overviewAxes": null,
  "overviewFieldUm": null,
  "overviewFrameTimeS": 1.0,
  "minSamplesPerPixel": 2,
  "maxDwellMs": 10.0
}
```

- **Nyquist pixel size** is `nyquistPixelSizeUm` when set. STED rigs need that
  override, because their resolution is below the diffraction limit. Otherwise
  it is `λ_min / (8 · NA)`, the confocal lateral Nyquist rule (Scientific
  Volume Imaging's formula), where `λ_min` is the shortest wavelength among the
  selected lasers. With neither set, the slider stops at 4× finer than the
  overview pixel and says so. **To review: is the confocal formula the rule you
  want?**
- **`minSamplesPerPixel: 2`** gives 20 µs at 100 kHz, the "about 0.02 ms" from
  the request.
- **Overview field** is `overviewFieldUm`, or else the largest centred field
  that passes the voltage check including the turnaround overshoot.

The whole block is optional. Without it the panel works with the fallbacks
above.

### 5.7 Scripting

Proposed exports:

- `setSimpleScanMode('overview' | 'acquisition')`
- `setAcquisitionRegion(centerUm: dict, sizeUm: dict)`
- `setPixelSizeUm`
- `setDwellTimeUs`
- `setChannels([[...], [...]])`
- `setScanDims([...])`
- `estimateScanTimeS()`

Runs go through the existing `runScanAndWait(source)`. A scan controller must
not export its own `runScan` (`test_scan_source_contract.py`).

## 6. Registration checklist

A new `scanWidgetType` must be registered in all of these places:

- **Controller and widget lookup.** `controller/controllers/__init__.py` and
  `view/widgets/__init__.py`.
- **Scan manager.** `MasterController.py:89-107` must map `SimplePointScan` to
  `ScanManagerAdvanced`. An unknown value makes this constructor return early,
  which silently skips all the recording signal wiring below it.
- **Config editor.** `view/configeditor/builtin_templates/sections/scan.json`
  lists the `scanWidgetType` options.
- **Tests.**
  - `test_scan_lifecycle.py:156-180` holds the expected set of controller classes.
  - `test_scan_source_contract.py` discovers the controller and checks it.
  - A new shipped mock setup changes the pinned count of 16 in
    `test_configeditor_type_authority.py`, and the UI smoke test
    `test_example_setups.py` starts every shipped setup.
- **Docs.** `gui.rst` (Scanning section), `setupinfo-reference.rst` (the `scan`
  block's `scanWidgetType` values), the `scan-lifecycle.rst` controller
  inventory, and a new user page `simple-point-scan.rst` in the toctree.

## 7. Phases

| Phase | Content | Done when |
|---|---|---|
| **P0** | Backend. `GalvoScanDesigner.scanSpeedRefusal` enforced inside `make_signal` (raises `ScanDesignRefusedError` with a fix hint). `ScanDesigner.estimateScanTime` (base returns `None`; Galvo implements §F4). An Advanced-controller helper that builds designer parameters once, for building, plotting and estimating. | Tests: refused above `vel_max`, accepted at or below it, stepped axes exempt, and the reason reaches the request through the Advanced controller. Estimate within 1 % of full builds for 1D, 2D, line steps, XZ, XYZ and a stepped fast axis. Existing scan tests pass. `advanced-scanning.rst` and the changelog updated. |
| **P1** | `simple_scan.py`, controller and panel without napari. New mock setup `galvo_apd_simple_mock_scan_setup.json`. Registration checklist (§6). | Headless controller tests: plan → dicts equals the Advanced serializer's output for the same scan. A Simple scan file loads in Advanced and back. An overview frame is at most `overviewFrameTimeS` by the estimate. The mock setup starts. |
| **P2** | The panel's own napari rectangle layer, the geometry stamp, and the inverse display transform. | Round-trip test: draw, convert to region, re-draw after a geometry change. Tested for every display rotation and flip. |
| **P3** | Channel lanes, per-channel power, PMT note, Z and T, Save toggle. | Widget tests (offscreen). A Scan-once recording from the Save toggle has the expected axes on the mock setup. |
| **P4** | `simple-point-scan.rst`, config-editor section, scripting API plus a tutorial script. Optional: a mock APD that images a synthetic cell sample from the galvo waveform, so overview → rectangle → acquisition can be demonstrated without hardware. | Sphinx `-W` build, tutorial runner, CI lanes. |
| **P5** | Rig check on STED/confocal. | Estimated time vs measured time per frame; overview frame rate; rectangle lands on the right cells; channel separation on APDs. |

## 8. Risks and open questions

1. **Enforcing `vel_max` changes behaviour on rigs (P0).** A rig whose
   `vel_max` is a copied placeholder (0.1 µm/µs, from `example_sted`) but whose
   galvo is really faster will now refuse fast, large-pixel scans that used to
   run. The reason says which limit was hit and what dwell would pass. The
   shipped Advanced defaults (0.1 µm step, 0.02 ms dwell, so 0.005 µm/µs) are
   well inside the limit. **To confirm before merging:** the real `vel_max` and
   `acc_max` on each rig.
2. **Overview frame rate depends on the turnaround.** With the placeholder
   limits, a 200 µm overview reaches 1 s per frame only at about 100–140 pixels
   across. Measured galvo limits will probably allow much more.
   Bidirectional scanning would halve the turnarounds, but `GalvoScanDesigner`
   doesn't support it; that would be a separate project.
3. **Re-arm overhead per repeat frame** (NI-DAQ task rebuild plus detector
   thread) isn't in the estimate. Measure it in P5 and add it as a constant if
   it matters.
4. **T frames in one run**, and how recording and the acquisition layout see
   them, still has to be confirmed (P3).
5. **Nyquist formula** (§5.6): still to review.
6. **Name.** `SimplePointScan` sits next to the legacy `PointScan` panel. Its
   dock title will be "Point scan" with the mode segmented control. Rename
   before P1 if you'd prefer another name.
