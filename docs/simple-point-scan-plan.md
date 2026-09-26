# SimplePointScan: plan

*Work-in-progress plan. The user documentation will be a Sphinx `.rst` page
written in P4.*

- **Status:** planned 2026-09-25; P0 done 2026-09-25 (speed rule, estimator,
  tests). Review 1 (2026-09-25) found that the plan overstated what Advanced
  gives for free; its six findings are verified and answered in §6, a design
  phase **D** that now comes before P1. Review 2 (2026-09-25) found four
  contract gaps (power semantics vs compatibility, the recorded-T iteration
  owner, pixel-storage ownership, imported lengths); all four hold and are
  answered in D5, D3, D1 and D4. Review 3 added two D5 fixes. Decisions
  2026-09-25: T as proposed in D3; no mid-iteration Stop; Advanced's
  behaviour stays unchanged, except P0's `vel_max` refusal, which is kept.
  The design phase is settled. Phase D's shared-code part is done
  (2026-09-26): D4's load hook, D1's frame geometry, D2's `recordScanSeries`.
  The Simple-side contracts (D3 policy, D5 checks, D4 equality, D6), and D2/D3's
  end-to-end series test (N iterations, N partitions, one run end), are
  tested with P1–P3, where their code lives. **P1 done 2026-09-26** (see the
  P1 note in §8); P2 next.
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
  dwell. It aims for about one frame per second and shows the measured frame
  rate. When a setup cannot reach that rate, it says so and shows the fastest
  overview it can (§6, D6).
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
| Coexist with Advanced? | **Replace.** Selected per rig with `scanWidgetType: "SimplePointScan"` | Still one scan panel per setup. Moving a rig between Simple and Advanced is a config change. Scan files load across only within an explicit representable subset; anything outside it is refused with the reason (D4). |
| What does T mean in an acquisition? | **N frames, back to back, one run, no interval** | Intervals stay a Recording feature (ScanLapse). Executed as N bounded NI-DAQ iterations in one run; with Save, as a ScanLapse with interval 0 (D3; confirmed 2026-09-25). |
| Which lasers does the overview use? | **Channel 1 only.** A dropdown picks another channel | The overview always needs just one line pass. |
| Does Acquire save? | **Optional "Save" toggle** | Runs the Recording controller's own Scan-once transaction, bound to this panel. There is no second writer (D2). |
| May Simple change how Advanced behaves? | **No** (2026-09-25). The one exception is P0's `vel_max` refusal, already committed and kept on purpose | Phase D is scoped so that Advanced's behaviour is unchanged. Shared code is touched only additively, or behind conditions Advanced never meets (§6, "Advanced stays as it is"). |

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

A subclass that fills the same dicts from a plain plan object inherits these
unchanged:

- the run lifecycle and coordinator arming;
- recording layouts for a single frame (`getAcquisitionLayouts`,
  `getNumCamTTL`, `getDimsScan`);
- #49's refusal handling.

**Corrected after review 1.** The first version of this plan also listed
"repeat re-arming" and "per-line-step analog laser power" as inherited, and
said any Simple scan "loads in Advanced as it is". None of the three holds:

- Advanced's `scanDone()` decides on the widget's Repeat box, so bounded T
  frames need a new continuation policy (D3).
- Per-step power is applied only in `advanced_mode`, only to the gate's own
  AO channel, and is filtered to TTL devices (D5).
- The Simple model and Advanced's saved state each hold things the other
  cannot represent (D4).

A saved scan is `{analogParameterDict, digitalParameterDict, mode}`
(`SuperScanController.getComponentState`).

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
| `acquisition` | Centre, size and **step per spatial dimension** (the pixel slider sets X and Y together; a loaded anisotropic scan keeps its steps, D4), the dwell, and the number of T frames. |
| `delays` | `phase_delay` and `d3step_delay` in µs, the Expert section's two fields. |
| `channels` | A list of lists of laser names, e.g. `[['561'], ['640']]`. |
| `channel_power` | Per gate and channel, the power percentage; per gate, the enabled flag (Advanced's `linestep_power_enabled`); and whether channel power is on at all, which serializes as Advanced's `advanced_mode`. Only for gates whose power has an AO path (D5). |

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

- **`plan_to_dicts(plan, limits, positioners, ttlDevices)`** returns the two
  dicts the Advanced path executes. The channels become
  `n_linesteps = len(channels)` and
  `linestep_enable[laser][s] = laser in channels[s]`. `advanced_mode` is the
  channel-power state: true exactly when channel power is on, and then always
  with empty pulse windows, false otherwise (D5). Overview mode sends only the
  overview channel.
  Per-channel power follows D5, not the Advanced serializer's AO-only rule.
- **`dicts_to_plan(analog, digital)`** is the inverse, over the representable
  subset defined in D4 only. Anything outside it is refused with the reason
  ("This scan uses timing windows; open it in the Advanced panel."), never
  normalized.
- **`estimate(plan)`** returns the frame time and the total time: × Z planes,
  and for T, N frames plus N − 1 measured re-arm gaps (D3). It uses
  `GalvoScanDesigner.estimateScanTime`.
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
- **Overview planning.** Field, pixel count and dwell are chosen together,
  in the order and with the fallback defined in D6.
- **Displayed geometry.** The rectangle is converted with the geometry of the
  image the layer is showing, delivered with that image. See D1; this replaces
  the earlier "stamp on `sigScanBuilt`", which the review showed can describe
  a different image from the one on screen.
- **Saved state.** It adds a `simplePlan` key to its component state. On
  restore it uses `simplePlan` when present, else converts the Advanced dicts
  within D4's subset, else keeps the defaults and says why.
- **Save toggle.** It asks the Recording controller to run its own Scan-once
  transaction bound to this controller (D2). The panel does not start the scan
  itself when Save is on.
- **T frames.** N bounded NI-DAQ iterations in one run, through an explicit
  continuation policy instead of the widget's Repeat box (D3).

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
first-pixel centres `(x₀, y₀)` delivered with the image the layer is showing
(D1):

- `x = x₀ + col_world`
- `y = y₀ + row_world`

(`x₀ = c_x − (N_x − 1)/2 · s_x` only when the swept length is exactly
`N_x · s_x`, which the Simple model guarantees; the geometry carries `x₀`
itself so that nothing depends on it.)

When the detector has a display rotation or flip, its inverse is applied first.
This needs a new inverse function in `display_transform.py`, tested as a round
trip.

When the image on screen changes geometry, for example from overview to
acquisition, the rectangle is re-projected at the moment that image reaches the
layer, not when the next scan is built. It stays fixed in scanner coordinates.

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
  "overviewMinFieldUm": null,
  "overviewMinPixels": 64,
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
- **Overview field, pixel count and dwell** are chosen together, as D6
  defines. The field depends on the sweep speed through the turnaround
  overshoot, so it cannot be chosen first on its own.

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

## 6. Design phase D: contracts before P1

**Advanced stays as it is (Lenny, 2026-09-25).** Every contract below is
scoped so that Advanced behaves exactly as today. The one exception is P0's
`vel_max` refusal, kept deliberately. Shared code is touched only in three
ways:

- **Additively:** a new entry point (`recordScanSeries`), and a load-refusal
  hook that reacts only to the `simplePlan` key, which only Simple writes.
- **Behind a condition Advanced never meets:** the detector managers attach
  geometry and use fresh storage only when a scan carries `frame_geometry`,
  which only Simple sets.
- **Not at all:** continuation, power validation and power verification live
  in Simple's controller.

Two latent Advanced gaps found on the way are documented, not fixed:

- per-step power for a gate whose linked `powerDevice` has an AO channel is
  silently dropped;
- per-step power for a calibrated laser skips its calibration table.

The plan's test lists include a check that Advanced's existing tests pass
unchanged.

Review 1 (2026-09-25) showed that the plan overstated what can be inherited
from Advanced unchanged. Each finding was checked against the code before
being accepted; all six hold, and one more turned up while checking finding 3.
These contracts come before P1 because they fix the model's and the
controller's interfaces. Each one names the tests that hold it.

| Review finding | Verified | Answer |
|---|---|---|
| 1. Geometry stamped at `sigScanBuilt` can describe a different image from the one displayed | yes | D1: geometry travels with the image and is applied with it |
| 2. `prepare_recording_for_scan` does not arm a recording | yes; it also returns success with no Recording controller | D2: use the Recording controller's own Scan-once transaction through a new synchronous entry point |
| 3. T execution deferred too late; `scanDone()` decides on the widget's Repeat box | yes; also, recording expects one assembled frame per scan-driven session | D3: bounded iterations under a continuation policy; with Save, a ScanLapse with interval 0 (confirmed) |
| 4. File compatibility exceeds what the model represents | yes | D4: explicit representable subset, refusal instead of normalization, two claims tested separately |
| 5. Linked laser power is not resolved | yes, and worse: per-step power is applied only in `advanced_mode`, which Simple does not set | D5: power routes |
| 6. Overview field and sampling are coupled; an estimate cannot promise a real frame rate | yes | D6: joint planning, fallback, measured rate |
| New: Stop cannot interrupt a running NI-DAQ iteration | yes; true for every NI-DAQ scan today | D3: Stop means "after this frame" until a separate mid-iteration Stop exists |

Review 2 (2026-09-25) found four gaps in the answers above. All four were
checked and hold:

| Review 2 point | Verified | Answer |
|---|---|---|
| 1. D5's Simple-only power rule broke D4's equal-waveform promise, and could activate inactive Advanced power | yes | D5: no Simple-only rule; power serializes as Advanced's own `advanced_mode` (checked: with no windows it leaves every TTL waveform identical), keys stay per gate, `linestep_power_enabled` is kept, inactive imports stay inactive. Also found: the injector skips calibrated lasers' voltage table. |
| 2. Recorded T had two possible iteration owners | yes | D3: one execution mode per run, fixed by the entry point; external starts are always `SINGLE` |
| 3. Pixel storage, not just geometry, needs an owner | yes | D1: fresh display storage per generation, never cleared in place; a snapshot on the one queued path |
| 4. Imported lengths conflict with `length = N · step` | yes | D4: imported lengths kept verbatim; D1's geometry works for any length |

Review 3 (2026-09-25), both checked and holding:

| Review 3 point | Answer |
|---|---|
| A power refusal must stop the scan; today the injection's blanket `except` swallows it | D5: in Simple, a pre-build `powerRefusal` and a post-build check that every requested power waveform exists, both raising `ScanDesignRefusedError`; tests prove no scan starts. Advanced's own handler is left as it is (decision 2026-09-25: Advanced's behaviour unchanged). |
| §5.1 still said `advanced_mode = False` | §5.1: `advanced_mode` is the channel-power state |

### D1. Displayed geometry travels with the image (review finding 1)

Verified:

- The first plan updated the geometry on `sigScanBuilt`, but napari can still
  be showing the previous scan at that moment. A build that then fails or is
  aborted publishes no frame, so the mismatch would last until the next
  successful scan.
- The image path carries no scan identity: `sigImageUpdated(image, init,
  scale)` → `DetectorsManager` → `sigUpdateImage` → `ImageController.update` →
  `apply_display_transform` → `ImageWidget.setImage`.
- `sigUpdateImage` has eight subscribers: Image, BeadRec, FLIMHist,
  AlignAverage, AlignXY, FFT, EventTriggered and EtSnouty (plus one-shot
  lambdas in SLMs and the binary-mask lease). Its argument list is pinned by
  `communication_channel_signal_inventory.json`. Two relays take a defaulted
  trailing parameter (`detectorName=` in `DetectorsManager.py:78`,
  `captureGeneration=` in `EventTriggeredBaseController.py:759`), so adding an
  argument would silently fill those instead of failing. Changing the
  signature is therefore out.
- `sigScanBuilt` fires even when the build then fails: the failure is raised
  only after the emit returns (`NidaqManager.py:1405`).
- For APD, PMT and Time Tagger, the emit and `layer.data = …` run in one
  synchronous call on the GUI thread. The one queued path is `LVWorker`, when
  something holds an EVENT_STREAM lease on a point detector. A frame that
  carries its own geometry is correct on both paths; a manager attribute read
  at apply time would race on the second.
- **Same-shape reuse mixes two images.** If scan B has the same shape and
  dtype as scan A, `initiateImage` neither reallocates nor clears `_image`
  (`APDManager.py:775`, `PMTManager.py:699`). B's partial frames show B's
  lines over A's remaining rows, drawn at B's scale. This is already a
  display defect today, and with geometry attached it would be a false one.
- **Fast-axis centring.** The fast axis sweeps `centre ± length / 2`, while
  `N = round(length / step)`. The `(N − 1)/2` centring used in §5.5 is exact
  only when `length == N · step`.
- `_recreateLiveLayer` (called when the number of dimensions changes)
  copies name, colormap, contrast, scale and visibility, but not `metadata`.
- Every detector has its own always-visible `Live: <name>` layer.
  `setCurrentDetector` changes neither napari's selection nor visibility.
- The display transform is re-read per frame from the setup file. Nothing
  changes it at run time, and there is no inverse.

Contract:

1. **Identity.** Before arming, the controller puts a `frame_geometry` into
   the `scanInfoDict` the detectors receive. It holds:
   - the run and iteration identity;
   - per scanned axis: device name, step, pixel count and the **position of
     the first pixel's centre**, in image axis order.

   The first-pixel position is computed with the designer's own conventions,
   for **any** length (D4 keeps imported lengths verbatim): stepped axes by
   `axis_pixel_positions` (`centre − (N − 1)/2 · step`); the swept fast axis
   from the start of its constant-velocity sweep, `centre − length/2 + step/2`.
   The two agree only when `length = N · step`, which is why the geometry
   carries the position rather than a centre. A test compares the recorded
   positions with the positions the generated waveform actually visits, for
   an integral and a non-integral length.

   Managers copy it per generation at build time. The same dict object is
   reused across repeat frames, and the coordinator strips its own keys at
   completion.

   **Scope.** Only the Simple controller sets `frame_geometry`. Everything in
   points 2–4 happens only for a generation that carries it. A scan without
   it (every Advanced scan) takes today's path unchanged: same buffer reuse,
   same published arrays, same display. That includes the existing quirk that
   a same-shape rescan briefly shows the previous scan's leftover rows, which
   stays as it is for Advanced.
2. **Published with the pixels.** Scan-driven managers (APD, PMT, Time
   Tagger) publish each frame of that iteration, partial or final, as a
   `ScanFrame`: an `ndarray` subclass with a read-only `geometry` attribute.
   - Every subscriber still receives an array; no signature changes.
   - Derived arrays do not inherit the attribute (`__array_finalize__` drops
     it), so a stale geometry cannot ride along on a copy.
   - Checked 2026-09-25: such a subclass passes through ImSwitch's
     `Signal(np.ndarray, …)`, directly and queued across threads, as the same
     object with its attribute intact; `frame[1:]` and `frame * 2` carry none.
   - The helper lives once, on `DetectorManager`.
3. **Applied atomically.** `ImageController.update` reads the geometry
   **before** `apply_display_transform`. `ImageWidget.setImage` stores
   `(geometry, display transform)` in the layer's `metadata` in the same call
   that sets `layer.data`, on both of its branches, including after
   `_recreateLiveLayer`. The geometry on a layer is therefore always that of
   the pixels it shows.
4. **Pixel storage is owned per generation** (review 2 point 3). A
   `ScanFrame` wraps pixels; it does not copy them. APD publishes its mutable
   `_image` (or a view of it), and with an identity display transform napari's
   `layer.data` **is** that array. So clearing or reusing the buffer, which
   the first D1 proposed for same-shape scans, would rewrite pixels already
   on screen or queued while they keep the old geometry. That includes after a
   build that then fails. The rule:
   - every generation **that carries `frame_geometry`** allocates **fresh**
     display storage in `initiateImage`,
     even when the shape and dtype are unchanged. It is never cleared or
     reused in place;
   - an array published for generation k is never written after generation
     k ends. Within generation k, published partials alias the buffer being
     filled; they share its geometry, so that is consistent;
   - the queued `LVWorker` path publishes a snapshot copy, because its frame
     may be applied after more of the same generation has been written.

   The cost is one allocation per iteration (4–8 MB at 1000 × 1000), which is
   negligible at overview frame rates. This also removes today's mixed-frame
   defect (a same-shape scan showing the previous scan's remaining rows)
   without clearing anything.
5. **Nothing published, nothing changed.** A build that fails, is refused or
   is aborted before its first frame leaves the old image and its geometry in
   place, consistent by construction.
6. **Which layer.** Every detector's layer is visible at once, and "current
   detector" changes neither napari's selection nor visibility. So the panel
   names its **reference detector** explicitly: a small choice in the Region
   card, defaulting to the first scan-driven detector of the scan. The
   rectangle is converted with that layer's geometry.
   - Changing the reference re-projects the rectangle with the new layer's
     geometry.
   - A layer without geometry (a camera, or a detector that has not scanned
     yet) disables drawing, and says why.

**Implemented 2026-09-26** (`scan_frame.py`, `_frame_publication.py`, APD /
PMT / Time Tagger managers, `ImageController`, `ImageWidget`,
`display_transform.py`). What the implementation settled:

- **The geometry is the nominal grid**, the one the scan parameters declare
  and a recording writes as its OME pixel size. Testing it against the
  generated waveform found two `GalvoScanDesigner` defects:
  - the swept fast axis realizes a pitch 0.6–1 % larger than the step;
  - for non-integral lengths the first pixel is read about 0.7 pixel before
    the sweep starts.

  Both are pinned as strict expected failures and listed in
  `current-state-and-known-issues.rst`. Fixing them changes Advanced's
  waveforms and is separate work. On a 10 µm line the pitch error puts the
  line ends ±0.03–0.05 µm from the nominal grid, which is irrelevant for
  choosing a region.
- **Geometry and storage are paired**, so a reader on another thread can tell
  whether both came from the same swap. The recording path's chunk and the
  display latch carry the geometry (`ChunkPayload.display_geometry`); the
  latch used to reduce every frame to a plain array.
- **Time Tagger accumulate mode** mixes earlier scans' lifetimes into the
  displayed frame; the geometry is the current scan's. Simple should switch
  accumulation off, or warn, when the region changes (P3).

Tests:

- a delayed frame of scan k arriving after scan k+1 was built keeps k's
  geometry until k+1's first frame is applied;
- a failed build and an aborted build leave the displayed geometry unchanged;
- two detectors with different geometries (one never scanned) and switching
  between them;
- partial frames carry the new geometry;
- a same-shape scan after another shows no pixels of the first under the
  second's geometry;
- **a retained frame of generation k keeps its pixels unchanged** when
  generation k+1 starts: after a successful build, after a failed build, and
  after an aborted one;
- the geometry survives a 2D ↔ 3D layer recreation;
- recorded first-pixel positions match the waveform, for the swept fast
  axis and for stepped axes;
- an EVENT_STREAM-leased (queued) frame carries its own geometry;
- round trips under every display rotation and flip;
- a derived array has no geometry;
- an Advanced scan (no `frame_geometry`) publishes plain arrays and reuses
  its buffer exactly as today.

### D2. Acquire with Save: the transaction (review finding 2)

Verified:

- `prepare_recording_for_scan` (`WorkflowServices.py:207`) binds only a recording
  that the standalone flow has already armed (`_awaitingScanSourceArm`), and
  returns success when there is no Recording controller. It cannot arm
  anything.
- On a setup with a `'Scan'` panel, the Recording controller's own Scan-once
  path already is the complete transaction (`RecordingController.py:342` and
  `:452`):
  1. preflight (`_preflightNewScanRequest`);
  2. bind the source (`getRecordingScanSource()`, i.e. the `'Scan'` controller);
  3. geometry and layouts (`_applyScanGeometryToRecordingArgs`:
     `getNumScanPositions`, `getNumCamTTL`, `getAcquisitionLayouts`);
  4. arm the writer (`_startManagerRecording`);
  5. wait for the arm (`_waitForManagerArm`);
  6. request the scan (`_requestScanStart`). A failed or refused start calls
     `_handleRecordingFailure(…, abortManager=True)`, which rolls the writer
     back.
- That path is reachable only through the REC button. The API's
  `startRecording()` presses it and returns nothing, and `setRecModeScanOnce`
  / `setDetectorToRecord` rewrite the user's Recording panel as a side effect.
  A second `startRecording()` while REC is already checked is silently
  ignored.
- A failure before the recording manager assigns a generation (preflight,
  geometry, layout validation) publishes no `sigRecordingFailed`. The reason
  reaches only the log (`RecordingController.py:2135`). This is why the entry
  point must return the reason itself.
- The scan start goes through `runScanExternal`, which forces `setScanMode()`
  and `setRepeatEnabled(False)` (`basecontrollers.py:508`). A Scan-once
  recording therefore always covers one iteration.
- `_waitForManagerArm` blocks the GUI thread for up to 35 s while the writer
  opens. The panel shows "Arming the recording…" and does not pretend to be
  responsive.
- The inherited `getNumScanPositions` builds from
  `scanManager.getScanSignalsDict(self._analogParameterDict)`: no
  `getParameters()`, no line-step count, no positioner program. The layouts
  build from `_stage_parameters`. Only the recording manager's
  `SCAN_POSITION_COUNT_MISMATCH` check holds the two together. The Simple
  controller computes both from `_stage_parameters`.

Contract:

- With Save on, the panel never starts the scan itself. It calls a new
  Recording entry point, `recordScanSeries(source, detectorNames, frames=1)`.
  With one frame it runs steps 1–6 above; with more it runs a ScanLapse with
  interval 0 (D3). It uses an **explicit** source and the given detectors, and
  returns synchronously: accepted (with the request, to follow its end), or
  refused with the reason. It leaves the Recording panel's own mode and
  detector selection as the user set them.
- Failure at each step:

| Step | Failure | Outcome |
|---|---|---|
| Recording controller present | missing | Save is disabled with "Load the Recording panel to save"; Acquire without Save still works |
| 1 preflight | a recording or scan already in progress | refused; nothing changes |
| 3 geometry, layouts | an accessor fails, or the design is refused (#49) | refused with the reason; no file |
| 4–5 arm writer | fails or times out | refused with the reason; the writer is rolled back; no scan |
| 6 scan start | refused (`ScanRequestRejectedError`) | the writer is rolled back; the reason is shown in the panel |
| run | Stop | the current iteration completes; see D3 for what is kept |

- **Double start.** Acquire is disabled from the click until the request
  resolves, and `recordScanSeries` refuses while a request is pending. Both
  layers are tested.

**Implemented 2026-09-26** as `RecordingController.recordScanSeries(source,
detectorNames, frames)`. A request object is alive only while its recording
runs. The REC flow's read sites consult it before the widget: detectors, the
Scan-once source, the lapse source, the point count, single file, and a
zero interval. The recording starts by pressing REC, so the button, Stop and
every existing path behave as for a user. The failure reason is captured in
`_handleRecordingFailure`. The terminal reset clears the request and
restores the controller's mode. Unit tests cover the refusals, what the REC
press sees, the returned reason and the cleanup (`test_record_scan_series.py`).
"Missing Recording" is the panel's check (P3).

Tests: missing Recording; writer arm failure (no scan starts); refused design
(nothing left armed); refused scan start (writer rolled back); double click;
Stop mid-series; success writes exactly the planned frames.

### D3. T frames and Stop (review finding 3, plus a new finding)

**New finding: Stop cannot interrupt a running NI-DAQ iteration.** `abortScan`
only sets flags (`_scanStopRequested`, `_repeatPending = False`). Nothing
stops the NI-DAQ tasks of the iteration that is already running; the
`'abort'` finish mode is applied only after the iteration ends. This is true
for every NI-DAQ scan today: a long XYZ stack in Advanced also runs to its end
after Stop. For a beginner panel, a Stop button that does not stop is a defect.

Ways to execute T:

| | **A. Bounded iterations** | B. One waveform with a virtual T axis |
|---|---|---|
| Stop | after the current frame | only after all N frames (see above) |
| Waveform size | one frame | N frames; 512 × 512 × 40 already exceeds the 10 M-position cap |
| Frame timing | a re-arm gap between frames (to be measured) | exact, no gaps |
| Estimate | frame × N + (N − 1) × measured gap | exact from the designer |

B is ruled out by Stop and by the size cap. A is the execution model; what
differs is who drives the iterations when the series is recorded.

**Recording N iterations.** The recording agent's trace shows the recording
manager expects exactly **one** assembled frame per session from a
scan-driven detector (`_expectedFramesFor`, `RecordingManager.py:3939`), and
scan-driven layouts describe one frame per session
(`_acquisition_layout_source.py:502-518`). "N iterations in one Scan-once
session" would need new code in the recording manager. The Recording
controller's **ScanLapse** already does the rest:

- it runs N timepoints in one run (non-final parts keep the reservation, so
  no other scan can start in between; `sigScanEnded` fires once);
- it writes one file with one group and one time partition per timepoint
  (`scan0`, `scan1`, …, `with_time_partition`);
- an interval of 0 starts the next point on the next event-loop turn;
- Stop mid-lapse discards the current point and keeps the earlier ones.

**Decided (Lenny, 2026-09-25):**

- **T without Save:** the Simple controller runs N bounded iterations itself,
  under its own continuation policy. No recording is involved.
- **T with Save:** the Save transaction (D2) runs a **ScanLapse with interval
  0 and N points**, single file, instead of a Scan-once. It is the same
  entry point with a point count, `recordScanSeries(source, detectorNames, N)`;
  N = 1 is the Scan-once case.

This keeps your decision's meaning (N frames, back to back, one run, no
interval; intervals remain the Recording panel's feature) while reusing the
recording path that already handles time partitions and mid-series Stop. The
file layout is one group per frame with a time partition, not one array with
a T axis. ImProcess already resolves that as time.

Contract:

- **Every run has one iteration owner, fixed when the run starts** (review 2
  point 2). The earlier wording let the controller's `framesDone < N` rule and
  the Recording lapse both advance a recorded series: forcing Repeat off does
  not switch off a bounded rule. The run's execution mode is chosen by the
  entry point and stored on the run, never read back from a widget:

  | Run started by | Execution mode | Iterations per start |
  |---|---|---|
  | `runScanExternal` (Recording Scan-once or lapse point, workflow API, tiling) | `SINGLE` | exactly one; the external driver owns the series |
  | the panel, T without Save | `BOUNDED(N)` | N, by the controller |
  | the panel, Live | `UNBOUNDED` | until Stop |

- The continuation policy is
  `_wantsAnotherIteration() = not stop and (mode is UNBOUNDED or (mode is BOUNDED(N) and framesDone < N))`.
  `SINGLE` never continues.
- Advanced's `scanDone()` branches on `self._widget.repeatEnabled()`
  directly, and `_shouldContinueRepeat` reads the widget too. The Simple
  controller **overrides both** with the policy above. Advanced's code is not
  touched, so its behaviour is unchanged, including the edge case where
  re-ticking Repeat during a recording-started scan keeps it going.
- With Save, each lapse point arrives through `runScanExternal` and so runs
  in `SINGLE` mode: the lapse drives the points, and the controller never adds
  one.
- Live and T are exclusive: Live is disabled while T is a dimension, and
  choosing T turns Live off, visibly.
- Stop during frame k of N:
  - without Save, frame k completes, no frame k+1 is armed, and the run ends
    once with `sigScanEnded`;
  - with Save, the lapse's own cancel applies: frames 1 … k−1 are kept, the
    current point is discarded, and the run ends once.
- Found in passing, before relying on ScanLapse: `lapseTotal` is read with no
  N ≥ 1 check, and N = 0 sends the first point as non-final, so the recording
  waits for a run end that never comes until REC is pressed off (read, not
  run). `recordScanSeries` validates N itself; the ScanLapse bug is worth its
  own fix.

Tests:

- without Save: exactly N NI-DAQ iterations and one `sigScanEnded`;
- with Save: N NI-DAQ iterations, N saved time partitions and one
  `sigScanEnded`, **asserted together in one test**, so neither the lapse nor
  the controller can add an iteration unnoticed;
- an external start of a Simple controller whose panel has T set still runs
  exactly one iteration;
- Stop mid-series with and without Save;
- Live disabled with T;
- Advanced's Repeat behaviour unchanged.

**No mid-iteration Stop (Lenny, 2026-09-25).** A real mid-iteration Stop for
NI-DAQ (stopping the AO/DO/counter tasks, finishing detectors with `'abort'`,
parking the scanners) is not scheduled. Stop therefore always means "after
the current frame", and the panel says so instead of pretending. While
stopping, the button reads "Stopping after this frame (≈ 12 s)", using the
estimate of what remains of the running iteration.

This is also why D3's per-frame iterations matter beyond T: a long single
waveform, such as an XYZ stack, cannot be stopped before it ends. The panel
shows the estimated duration before such a scan starts, so the operator knows
up front.

### D4. Compatibility contract (review finding 4)

Two claims, tested separately:

1. **Same executable scan.** For a plan inside the representable subset
   below, `plan_to_dicts(plan)` loaded into the Advanced controller builds
   the same waveforms (every AO and DO array equal) as the Simple controller
   builds from the same plan.
2. **Settings survive a round trip.** This is *not* promised through
   Advanced. A Simple file keeps everything only when it is reloaded into
   Simple. Advanced re-saving a Simple file drops `simplePlan`, and that is
   stated, not hidden.

Representable subset, Advanced → Simple (anything else is **refused** with
the reason; nothing is normalized):

| Advanced state | Simple |
|---|---|
| no timing windows, no sequence rows, no intra-pixel positioner program, no device locks | required |
| `advanced_mode` | true is accepted only when it carries nothing but per-step power (no windows, rows, program or locks); it becomes "channel power on". False becomes "channel power off", with any stored percentages kept inactive (D5) |
| `linestep_power_enabled` | kept per gate; a missing key means enabled, as in Advanced |
| every enabled TTL device is a laser with a digital line | required (a TTL-triggered camera or other TTL device is refused) |
| every line step enables at least one laser | required (an empty line pass is refused) |
| per-axis step sizes | kept per axis; anisotropic X/Y shows as such, and only moving the pixel slider makes it isotropic |
| per-axis lengths | **kept verbatim**, including a length that is not a whole number of steps (10 µm at 0.3 µm: 33 pixels, not rewritten to 9.9 µm, which would change the swept waveform and the first pixel's position). Plans Simple creates itself snap `length = N · step`. An imported length is re-derived only when the user moves that axis's region or the pixel slider. |
| `phase_delay`, `d3step_delay` | model fields |
| `linestep_power_percent` | per channel; applied only for gates with their own AO channel, exactly as in Advanced. Stored values for other gates are kept and never applied (D5) |
| `scan_dim_target_device` | `dims` |
| Repeat | Live |

Simple → Advanced:

- The file's executable dicts are the **acquisition** plan. The overview
  plan and the T count live only in `simplePlan`.
- A plan with T > 1 has no Advanced equivalent (Advanced has no bounded
  repeat count). The base `applyComponentState` gains a refusal hook: a
  controller that cannot represent a state's declared features refuses to
  load it, with the reason, instead of loading one frame.
- Model gains the fields the review found missing: per-axis steps, phase
  delay, slice delay.

The load-refusal hook is **implemented** (2026-09-26, `b3172881`): a state's
`requiredFeatures` against the controller's `supportedStateFeatures`.

Tests: executable equality for each representable feature, including an
imported non-integral length (10 µm at 0.3 µm) and imported inactive power;
one refusal test per unrepresentable feature, in each direction.

### D5. Laser power mapping (review 1 finding 5, review 2 point 1, review 3)

**Advanced's power semantics, used as they are.** Simple does not add power
behaviour that Advanced lacks, and it does not change Advanced's. Review 2's
point holds: the same saved dicts must build the same waveforms in both
panels, and Simple must never switch on power that was off in an Advanced
file.

- **The switch is Advanced's own `advanced_mode`.** Checked 2026-09-25 on the
  mock setup: `advanced_mode = True` with no timing windows produces
  **identical** TTL waveforms to `advanced_mode = False` (every DO array
  equal, two line steps). It additionally applies `linestep_power_percent`,
  which `False` ignores. So a Simple plan whose channels set power serializes
  as `advanced_mode = True` with empty pulse windows. Advanced loads that as
  its own "advanced mode, no windows, per-step power", and both panels build
  the same waveforms.
- **Keys stay Advanced's.** `linestep_power_percent[gate][step]` and
  `linestep_power_enabled[gate]` are keyed by the gate (the TTL device). The
  model carries both: per gate and channel the percentage, per gate the
  enabled flag. On import `linestep_power_enabled` is preserved, and a
  missing key means enabled, as in Advanced.
- **Inactive stays inactive.** An Advanced file with `advanced_mode = False`
  and stored percentages imports with channel power **off**, values kept.
  Simple applies them only after the user turns channel power on, which flips
  `advanced_mode` visibly.

Which lasers get per-channel power: **exactly those Advanced applies it to**,
gates with their own `analogChannel`.

| Configuration | Per-channel power in Simple | Advanced today |
|---|---|---|
| gate has its own `analogChannel` (an AOM) | yes, same waveform as Advanced | applied |
| gate's `powerDevice` has an `analogChannel` | no; the lane shows the Laser panel's setpoint, read-only | silently dropped (latent gap, documented, not fixed) |
| power set over serial (an AOTF over RS232, e.g. `example_sted`'s 561AOTF) or no power device | no; setpoint shown read-only | not applied |

- Stored percentages for a gate without its own AO channel are kept and never
  applied, in both panels, so executable equality holds.
- Percent to volts: both panels use today's mapping in
  `_inject_linestep_power_ao`, linear onto `valueRangeMin … valueRangeMax`.
  For a laser calibrated with `calibCsvPath` this skips the calibration table
  that `NidaqLaserManager` uses for its static value (found by reading,
  2026-09-25). This is a documented limitation of both panels. Simple labels
  the value "% of the analog range" for such a laser.

**Refusals stop the scan, in Simple** (review 3). Advanced wraps the
injection in `except Exception` and logs at debug level
(`ScanControllerAdvanced.py:165-173`). That stays as it is. Simple's
controller makes its own path strict without touching that code:

- **Before building:** `powerRefusal(plan)` validates channel power (a gate
  without its own AO channel, a value outside 0–100 %, a channel naming a
  laser that is not a gate). It raises `ScanDesignRefusedError` with the
  reason, so #49's path turns it into a refused start.
- **After building, before arming:** Simple checks that every gate with
  channel power on has its AO waveform in `scanSignalsDict`. If one is missing
  (the injection failed and Advanced's handler swallowed it), it raises
  `ScanDesignRefusedError` ("Channel power for 488 could not be built; the
  scan was not started"). The run is still unpublished at that point, so this
  too is a refused start.

Tests:

- same dicts give equal AO and DO waveforms in Simple and Advanced, with
  channel power on and off;
- an Advanced file with inactive power imports inactive and builds no power
  waveform;
- `linestep_power_enabled` round-trips, including the missing-key default;
- a gate with a linked AO `powerDevice` gets no per-channel power control,
  and its stored values build no waveform in either panel;
- a refused power setting and a forced injection failure: through the API
  the request is rejected with the reason, the Scan button publishes only the
  refusal, and **no scan starts** (no run reserved, no NI-DAQ `runScan`, no
  laser armed);
- Advanced with the same forced injection failure behaves exactly as today:
  it scans without the power waveform and logs at debug level. This test
  exists to show the difference is deliberate.

### D6. Overview planning (review finding 6)

Unknowns: field `F` per overview axis, pixel count `N` across the longer
axis (step `s = F / N`), dwell `d`. Rules, in order:

1. Dwell is not free: `d = d_min(s) = max(minSamplesPerPixel / sampleRate, s / vel_max)`.
   Overview always runs at the fastest safe dwell.
2. For a given `F`, the feasible `N` form an interval:
   - voltage feasibility **improves** with `N`: smaller steps mean a slower
     sweep and less turnaround overshoot;
   - frame time **worsens** with `N`.
   Two bisections give `N_voltage` (smallest feasible) and `N_time` (largest
   within budget). Take `N = N_time` if `N_voltage ≤ N_time` and
   `N_time ≥ N_min` (default 64).
3. Voltage feasibility per candidate:
   - the fast axis from the proxy build's `minmaxes` (measured: identical to
     the full build's, overshoot included);
   - the slow axis analytically, `centre ± (N − 1)/2 · s`.
4. If no `N` works, shrink `F` by 20 % and repeat, down to
   `overviewMinFieldUm`.
5. If nothing meets the budget, run the fastest feasible combination at
   `F_min` and show its frame time in amber with the reason ("1 s per frame
   is not reachable on this setup; this overview takes 1.8 s"). Never refuse
   to show an overview for timing alone.
6. The chosen combination is validated by the real build when the overview
   starts. A refusal there (#49's path) shrinks `F` once more and retries,
   at most three times, then reports.

Budget and the promise: the budget is `overviewFrameTimeS − overhead`,
where `overhead` is the measured re-arm gap between frames. It is learned,
not assumed: during Live the controller measures the frame period, keeps an
average of (period − estimate), and re-plans when the target is missed by
more than 10 %. The panel shows the **measured** frame rate. The opening
promise becomes "aims for about one frame per second and shows the
measured rate".

Tests: monotonicity assumptions on the mock setup; the chosen `N` meets the
budget and the next `N` does not; the fallback path; re-planning from a
simulated overhead.

## 7. Registration checklist

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

## 8. Phases

| Phase | Content | Done when |
|---|---|---|
| **P0** | Backend. `GalvoScanDesigner.scanSpeedRefusal` enforced inside `make_signal` (raises `ScanDesignRefusedError` with a fix hint). `ScanDesigner.estimateScanTime` (base returns `None`; Galvo implements §F4). An Advanced-controller helper that builds designer parameters once, for building, plotting and estimating. | Tests: refused above `vel_max`, accepted at or below it, stepped axes exempt, and the reason reaches the request through the Advanced controller. Estimate within 1 % or 2 ms of full builds for 1D, 2D, line steps, XZ, XYZ and a stepped fast axis. **Done** (`4c0c671b`). Existing scan tests pass. `advanced-scanning.rst` and the changelog updated. |
| **D** | The contracts in §6, with the tests each one names written first, failing, against stubs where the code does not exist yet. The code they need, all scoped so that Advanced's behaviour is unchanged: Simple's own continuation policy (D3); Simple's `powerRefusal` and post-build power verification (D5); the additive state-refusal hook in `applyComponentState` (D4); `ScanFrame` and fresh storage per generation, only for scans carrying `frame_geometry`, plus the geometry handling in `ImageController` / `ImageWidget` (D1); and the additive `RecordingController.recordScanSeries` (D2, D3). | The D1–D5 contract tests pass. Advanced's existing tests pass **unchanged**, and the "Advanced as today" tests in D1 and D5 pass. |
| **P1** | `simple_scan.py` (with D4's fields), controller and panel without napari. D6 overview planning. New mock setup `galvo_apd_simple_mock_scan_setup.json`. Registration checklist (§7). | D4 executable-equality and refusal tests pass. D6 planning tests pass. The mock setup starts. The measured overview frame rate is shown on the mock. |
| | **P1 note (2026-09-26).** Implemented: `simple_scan.py`, `ScanControllerSimplePointScan` (Advanced subclass), `ScanWidgetSimplePointScan`, `galvo_apd_simple_mock_scan_setup.json`, registration, and `Scan.simplePointScan` in SetupInfo and the config editor. What building it settled: (1) **D4 needs a grid.** Advanced stores positions, lengths and steps to 1 nm and delays in whole µs, so a slider step like 0.0720577 µm built a different scan once loaded there; plans are normalised to that grid, and imported values already on it stay verbatim. (2) **D5's refusal reads the plan, not the dicts.** The dicts cannot express power for a laser without an analog channel, so it is checked in `_buildScanSignals` (#49's build hook); the post-build waveform check stays on the dicts. (3) In Live, the channel's `sigScanDone` fires once per run, not per frame; iterations are NI-DAQ completions. (4) On the mock, the overview plans a 28 µm field (35 µm overshoots ±10 V) at 195 px and 0.995 s estimated, measured 0.9 frames/s: about 0.1 s re-arm per frame. Tests: `test_simple_scan_model.py` (29), `test_simple_point_scan_controller.py` (15, real MasterController on the simulated NI-DAQ); mutation-checked. | |
| **P2** | The panel's own napari rectangle layer, drawn over the current detector's layer, converted with D1's geometry; the inverse display transform. | D1's tests pass, plus drawing is disabled on a layer without geometry. |
| **P3** | Channel lanes, per-channel power (D5), PMT note, Z and T (D3), Save toggle (D2). | D2, D3 and D5 end-to-end tests on the mock setup: N time partitions recorded, one run end, Stop mid-series with and without Save, missing Recording, writer failure, double start, and each power route. |
| **P4** | `simple-point-scan.rst`, config-editor section, scripting API plus a tutorial script. Optional: a mock APD that images a synthetic cell sample from the galvo waveform, so overview → rectangle → acquisition can be demonstrated without hardware. | Sphinx `-W` build, tutorial runner, CI lanes. |
| **P5** | Rig check on STED/confocal. | Estimated time vs measured time per frame; overview frame rate; rectangle lands on the right cells; channel separation on APDs. |

## 9. Risks and open questions

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
3. **Re-arm overhead between frames** (NI-DAQ task rebuild plus detector
   threads) is not in the designer's estimate. D6 learns it from the measured
   frame period during Live; T estimates use the same measured gap (D3). P5
   records its size on the rig.
4. **T execution (D3)**: decided 2026-09-25. Bounded iterations without Save,
   and a ScanLapse with interval 0 with Save.
5. **Nyquist formula** (§5.6): still to review.
6. **Name.** `SimplePointScan` sits next to the legacy `PointScan` panel. Its
   dock title will be "Point scan" with the mode segmented control. Rename
   before P1 if you'd prefer another name.
7. **Stop cannot interrupt a running NI-DAQ iteration** (D3). This affects
   Advanced too, for example on long XYZ stacks. Decided 2026-09-25: no
   mid-iteration Stop. Stop means "after the current frame", and the panel
   says so, with the remaining time.
8. **D1 adds an `ndarray` subclass to the live image path.** Every subscriber
   still gets an array, but code that tests `type(x) is np.ndarray`, pickles
   frames, or hands them to C extensions must be checked. The D phase audits
   the eight `sigUpdateImage` subscribers.
