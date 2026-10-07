# Lifetime 2.0 — Swabian Time Tagger, calibration tutorials, and a Lifetime widget

Status: Proposed — **revision 2**, after the first review round (two
independent reviews: measurement physics / vendor API, and architecture).
Nothing is implemented. §12 lists the decisions still open; §13 the rig facts
the plan needs; §15 logs what the review changed and why.

Scope in one sentence: turn today's single-purpose FLIM detector into a
Time-Tagger subsystem — a shared device layer that exposes the card's
measurement and conditioning features, a set of ImScripting tutorials that
calibrate and debug every time-critical signal with the card itself, and a
dedicated **Lifetime** widget that makes FLIM, gated STED and tau-STED
first-class GUI modes instead of scripts.

Revision 2 in three lines: the conditional filter reverses the TCSPC
direction and the plan now models that; the frame marker collides with the
first pixel marker on the DAQ and the plan now offsets it; fitting and gating
get a background model, a pile-up guard and peak-relative gates. The device
block is flattened to fit the config editor, P1 is split in three, and the
two-colour / counter-detector / outer-axes / raw-tag work moves to a follow-up
plan.

---

## 1. Where we are

The code is sound for what it does, and it does one thing: a scan-driven FLIM
raster through a single `TimeTagger.Flim` measurement.

**What exists** (paths relative to the repo root):

| Layer | File | What it does today |
|---|---|---|
| Detector | `imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py` (1512 lines) | Follows `NidaqManager.sigScanBuilt/Started/Done`; two `EventGenerator`s turn each physical line-clock edge into Nx pixel-begin/end markers; one `Flim` with `n_pixels = Nx·Ny`; a worker polls `getCurrentFrame()` every 1 s, fits `moment` / `phasor` / `exp1` per pixel with IRF-peak compensation, and publishes the lifetime image as the detector frame. Accumulate mode averages lifetimes across scans in software. Side channel (`TimeResolvedDetectorMixin`) hands a `TimeResolvedScanProducts` (intensity, lifetime, decay, gate images, optional cube) to workflows. |
| Contract | `imswitch/imcontrol/model/timeresolved/` | `GateSpec`, `LifetimeFitConfig`, `TimeResolvedScanConfig`, `TimeResolvedScanProducts`, gate math. |
| Workflows | `imswitch/imcontrol/model/workflows/time_resolved.py` | `BinnedPhotonArrival`, `GatedSTED`, `TauSTED`: clear → configure → `facade.scan.run_once()` → wait → save HDF5/NPZ/TIFF. |
| Scripts | `imswitch/_data/user_defaults/scripts/workflows/timeresolved/01..03_*.py` | Three ~30-line scripts that run those workflows. Hardware-only, untested, hard-coded `D:/Measurements`. |
| GUI | `FLIMHistWidget` / `FLIMHistController` (`FLIMHist` key) | One pyqtgraph bar plot: per-pixel lifetime histogram (from `sigUpdateImage`), or the aggregated decay with a global τ marker (read from the manager's private `_last_*` attributes). |
| Diagnostics | `scripts/diagnostics/measure_laser_rep_rate.py` | Standalone CLI, `Countrate` on the sync channel. The only Time Tagger diagnostic. |
| Docs | `docs/devices/detectors.rst` §SwabianTimeTaggerManager, `docs/scripting-time-resolved-workflows.rst`, `docs/design/plans/time-resolved-detector-workflows.md` | Config reference, workflow cookbook, and the plan that produced the contract. |

**Swabian API actually used** — the complete list: `createTimeTagger`,
`setTriggerLevel`, `setInputDelay` (click channel, for `t0_ps`),
`EventGenerator`, `Flim(...)`, `Flim.getCurrentFrame`. Nothing else.

**What the card can do that we don't use** (the "unleash" list, with the
caveats the review added):

| Capability | Swabian API | Why it matters for us |
|---|---|---|
| Live count rates per channel | `Counter`, `Countrate` | "Is the SPAD alive? Is the sync there? Is the line clock firing?" — today answered by scanning and seeing zeros 10 s later. |
| Period / jitter of a periodic signal | `TimeDifferences` | Laser period; line-clock period against the scan design. Caveat: the card's own two-tag jitter floor (TT20 ≈ 48 ps RMS, Ultra ≈ 30 ps, X < 10 ps) is what you measure for a good laser; report "card-limited" below it. |
| Digital timing trace | `Scope` | An oscilloscope on line/frame/pixel channels with ps resolution. Needs both edges enabled (`+ch` and `−ch`); never on the sync channel. |
| Cross-channel delay | `Histogram` (`Correlation` is symmetric and noisier) | IRF and `t0`; frame→line delay; STED-pulse→excitation delay if a photodiode is cabled. |
| Frame boundary | `Flim(frame_begin_channel=…)`, `getReadyFrameEx()`/`FlimFrameInfo`, `getSummedFrames()`, `getCurrentFrameIntensity()` | The DAQ already outputs `frameStartClockLine`. Card-closed frames; cheap live intensity previews; open-ended accumulation. See §3.2 for the marker-collision caveat. |
| USB bandwidth control | `setConditionalFilter`, `setDeadtime`, `getOverflowsAndClear` | An 80 MHz sync is 80 M tags/s: over a TT20's USB budget (≈ 8.5 M/s) and an Ultra's (≈ 65–80 M/s); only an X takes it raw. The filter passes only the first sync *after* each photon — which **reverses the TCSPC direction** (start = photon, click = sync; §3.2). `setEventDivider` on the sync is *not* a FLIM remedy (the window becomes N periods and nothing folds it); it is only a tool for measuring the rep rate. |
| Hardware delay on any channel | `setInputDelay` (software, signed), pattern offsets | A ps-resolution fix for the APD-vs-TimeTagger line offset (ROADMAP M9). Sign matters: the APD *drops* leading samples, so the TimeTagger needs a *positive* delay on line and frame. |
| Self test | `setTestSignal` | ~0.8–0.9 MHz per channel on a TT20 (`setTestSignalDivider` on Ultra/X); the signals on two channels are correlated, so a cable-free `Histogram` checks the whole pipeline. |
| Electrical inputs | `setInputImpedanceHigh`, `setInputHysteresis` (Ultra/X), negative channel numbers for falling edges | All inputs are 50 Ω (TT20 fixed). An NI DO/PFI line drives ~1–1.5 V into 50 Ω: a 1.5 V threshold on the line clock may never trigger — the "line 0 Hz" symptom. Trigger range ±2.5 V. |
| Raw tags, replay | `FileWriter`, `createTimeTaggerVirtual` | Deferred to the follow-up plan (§9): the only feature that needs the real library in tests, and `.ttbin` records post-filter tags. |
| Identity | `getSerial`, `getModel`, `getConfiguration` | Card identity into every measurement's metadata; Ultra HighRes mode halves and renumbers channels, so roles resolve *after* reading the configuration. |

**Known defects and gaps that this plan absorbs:**

1. **No mock.** `_isMock` is set but never read; with the library missing or
   the card absent, every scan with an enabled FLIM detector raises and rolls
   back. No shipped setup contains the detector. The worker and the `Flim`
   path have never run in CI.
2. **Products stay armed.** `TimeResolvedScanWorkflow.run()` clears *before*
   configuring but never after `wait_for_final`, so `_tr_enabled` stays true
   after one workflow: every later scan copies products and computes gates,
   and any later z/t scan is rejected by `_validate_time_resolved_scan_shape`
   until something clears it.
3. **Line offset.** Pixel markers key off the uncompensated physical line
   clock (ROADMAP M9). APD and TimeTagger images differ by about a line.
4. **Outer axes.** `n_linesteps` is read and ignored; z/t stacks read only
   the current frame.
5. **`max_retained_products`** is accepted and not honoured (and nothing
   reads it).
6. **Config template drift.** The builtin template carries `default`
   entries `n_bins=64` (which triggers the truncation warning the code added
   to prevent exactly that) and `line_trigger=-0.5` (code: `+0.5`); it lacks
   `laser_rep_rate_mhz`. Templates are presentation overlays: delete the
   `default` entries rather than correcting them.
7. **Dead code**: module-level `_fit_phasor` and `_fit_exp1`; only the
   worker's cached variants run.
8. **Cube dtype** is float32 (cast in the worker); counts are integers.
9. **No background model, no pile-up guard.** The fitters subtract an argmax
   `t_peak` but never subtract dark counts or afterpulsing (flat over the
   period: a 5 % flat fraction pulls a 2.5 ns moment estimate up by
   ≈ 0.19 ns and the phasor toward the origin). Nothing checks the
   photons-per-sync ratio. Gates are absolute times, so a `t0` change moves
   every STED preset.
10. **IRF peak at bin 0.** `setInputDelay(click, −t0)` is documented as
    placing the peak at bin 0, which wraps the IRF rising edge and sync jitter
    into the last bins and leaves no pre-pulse window for background.
11. **FLIMHistController** docstring says seconds; the frame is in ns.
12. **Docs drift**: the roadmap and the old plan point at
    `scripts/timeresolved/`; the files live in `scripts/workflows/timeresolved/`.

---

## 2. Goals and non-goals

**Goals**

- G1. A **shared Time Tagger device layer** so that diagnostics, the FLIM
  detector and scripts use one connection and one channel configuration, with
  the card's conditioning (trigger levels, edge sign, delays, dead time,
  conditional filter, test signals) configured in one place and recorded in
  metadata.
- G2. **Calibration and debugging tutorials** in ImScripting that measure each
  time-critical signal with the card — laser sync, line clock, frame clock,
  pixel markers, photon channels, dark counts — compare them against what the
  scan designer intended, and write the results back (`laser_rep_rate_mhz`,
  `t0_ps`, trigger levels, line delay, background). They run on a simulated
  card, like every other shipped tutorial.
- G3. **Mode setup guides**: one doc page per mode (FLIM, gated STED,
  tau-STED) that says what to cable to which channel, which settings matter,
  how to verify, and gives a setup-JSON snippet.
- G4. A **Lifetime widget** that makes gated STED and tau-STED point-and-click:
  live decay with draggable, peak-relative gates, live gate images and a
  lifetime overlay in napari, phasor readout, accumulation, one-click save,
  and a signal-health strip for every Time Tagger role.
- G5. **Measurement correctness**: TCSPC direction handled, background
  subtracted, pile-up flagged, frame boundaries closed by the card, linesteps
  counted, a measured and applied line delay.
- G6. **Testability**: a mock Time Tagger good enough that the worker, the
  widget and every tutorial run in CI on a shipped mock setup.

**Non-goals (this plan)**

- Hardware-gated detection electronics or changing the STED laser timing.
  Gates stay software gates on photon arrival times. The Pulse Streamer
  manager exists in `managers/pulsegen/` but is not wired into
  `MasterController`; it stays that way here.
- A new fitting library. The three fitters stay (plus background
  subtraction); an IRF-deconvolved tail fit is a later option (§9).
- A second real time-tagger vendor. The mock is the second backend.
- Changing galvo trajectories or `GalvoScanDesigner` (M9 territory). The
  plan *measures* the line offset and applies a card-side delay.
- Two-colour FLIM, an intensity-only counter detector, z/t cubes, raw-tag
  recording and replay, a recording-manager hook, and phasor pixel selection:
  all moved to a follow-up plan (§9) after review, so this plan stays
  deliverable.

---

## 3. Architecture

```
                 ┌──────────────────────────────────────────────────────────┐
 setup JSON      │  TimeTaggerManager   (new, low-level, one per card)      │
 "timeTagger": { │  - owns the TimeTagger connection (real or Mock)         │
   roles (flat), │  - named roles → channel, edge sign, trigger, delay,     │
   filter, ... } │    dead time ; conditional filter → tcspc_direction      │
                 │  - configuration lock (writes refused during a scan)     │
                 │  - health snapshot (rates, overflows, pile-up)           │
                 └───────┬───────────────────────────────┬──────────────────┘
                         │                               │
        ┌────────────────▼───┐              ┌────────────▼────────────────┐
        │ SwabianTimeTagger  │              │ TimeTaggerFacade            │
        │ Manager (FLIM det.)│              │ (scripts/tutorials, widget  │
        │ slimmed: Flim +    │              │  Signals panel)             │
        │ worker only        │              └─────────────────────────────┘
        └────────┬───────────┘
                 │ LiveProducts (1 Hz, no cube) · TimeResolvedScanProducts (final)
        ┌────────▼───────────────────────────────────────────┐
        │ timeresolved/  types · processing · fitting (moved) │
        │                io (HDF5/NPZ/TIFF, shared)   (new)   │
        └────────┬──────────────────────────┬────────────────┘
                 │                          │
        ┌────────▼──────────┐      ┌────────▼───────────────┐
        │ workflows/        │      │ LifetimeWidget /        │
        │ time_resolved.py  │      │ LifetimeController      │
        │ (scripts)         │      │ (FLIM · Gated STED ·    │
        └───────────────────┘      │  Tau STED · Signals)    │
                                   └─────────────────────────┘
```

### 3.1 `TimeTaggerManager` — the shared device layer

New file `imswitch/imcontrol/model/managers/TimeTaggerManager.py`, constructed
by `MasterController` from a new `TimeTaggerInfo` dataclass in
`SetupInfo.py` (next to `NidaqInfo` / `TeensyPulseInfo`), stored as
`master.timeTaggerManager` (always present, `None` when the block is absent —
the `pulseGeneratorManager` pattern), passed in `lowLevelManagers`, and added
to `closeEvent`'s `manager_attrs` so `finalize()` (→ `freeTimeTagger`) runs
at shutdown.

**The block is flat.** The config editor's system-section form edits a flat
field list only (`view/configeditor/editor.py`, `_load_section_schemas`), and
the role set is closed, so roles become prefixed fields rather than a nested
map:

```json
"timeTagger": {
  "serial": null,
  "simulation": false,
  "useMockOnFailure": false,

  "photonsChannel": 1,     "photonsTriggerV": -0.25, "photonsDeadtimePs": 50000,
  "laserSyncChannel": 2,   "laserSyncTriggerV": 0.5,
  "lineClockChannel": 3,   "lineClockTriggerV": 0.5,  "lineClockDelayPs": 0,
  "frameClockChannel": null, "frameClockTriggerV": 0.5,
  "stedPulseChannel": null, "stedPulseTriggerV": 0.5,

  "filterSyncByPhotons": false,
  "peakTargetNs": 0.8,
  "backgroundWindowNs": 0.4,

  "mockSample": "beads_two_lifetimes",
  "mockFaults": []
}
```

Edge sign follows the Swabian convention: a negative channel number means the
falling edge. `simulation` forces the mock; `useMockOnFailure` falls back to
it when the library is missing or `createTimeTagger` fails — **but only when
`nidaq.simulation` is also true**. On a real rig a missing card stays a hard
error that rolls the scan back, as today.

Responsibilities:

- **Connection**: `createTimeTagger(serial)` once; read `getModel()` /
  `getConfiguration()` first and resolve roles after it (Ultra HighRes
  renumbers channels); `freeTimeTagger` in `finalize()`.
- **Roles**: `channel('laser_sync') -> int` (signed). Everything above this
  layer speaks roles, so re-cabling is one edit.
- **Conditioning**, applied at connect: trigger level, input delay, dead time
  per role; `setConditionalFilter(trigger=[photons], filtered=[laser_sync])`
  when `filterSyncByPhotons`. Every value is snapshotted into
  `metadata['time_tagger']`. There is no Settings-tree presence (that tree
  shows detector parameters only); conditioning is edited in the setup file,
  in the widget's Signals panel, or through the facade.
- **TCSPC direction** is derived, not configured: `filterSyncByPhotons`
  ⇒ `tcspc_direction = 'reverse'` (start = photons, click = sync; the time
  axis is mirrored `t = T_rep − t'` in `timeresolved/processing` before any
  fit or gate); otherwise `'forward'`. The direction is in metadata and the
  widget shows it.
- **Configuration lock**: Swabian measurements run concurrently and do not
  need serialising; what corrupts a running FLIM frame is a *conditioning
  write* mid-scan. The manager holds an `RLock` around conditioning; writes
  are **refused** (clear error) while any detector using the affected role
  holds a SCAN lease. The lock is always released in `finally`, because
  ImScripting's `OperationCancelled` is a `BaseException`.
- **Health**: `health() -> TimeTaggerHealth(rates_per_role, overflows_delta,
  pileup_fraction, sync_rate_label, tcspc_direction, model, serial, is_mock)`
  from a slow background `Counter`, and `sigHealth` for the widget. With the
  filter on, the measured sync rate ≈ photon rate by construction; the strip
  labels it "sync (filtered)" and the rep-rate check is done by the explicit
  measurement procedure in §3.3 instead.
- **Compatibility**: a setup with a `SwabianTimeTaggerManager` detector and
  no `timeTagger` block keeps working: the detector builds a private
  `TimeTaggerManager` from its own `click_channel` / `start_channel` /
  `line_channel` and trigger properties, with a deprecation warning that
  prints the equivalent block. No automatic migration action.

### 3.2 `SwabianTimeTaggerManager` slimmed

Keeps: scan lifecycle, pixel-marker generation, the `Flim` worker, the
`TimeResolvedDetectorMixin`. Loses: connection management and trigger-level
parameters (the detector's parameters become role selections: `click_role`,
`start_role`, `line_role`, `frame_role`), and the fitters, which move to
`timeresolved/fitting.py` as a `LifetimeFitter` built once per scan (trig
tables cached as today), still executed in the worker thread, never in a
controller.

Measurement correctness, new in the worker and in `timeresolved/`:

- **Direction-aware axis.** The worker receives `tcspc_direction` from the
  device manager and `processing.oriented_axis()` mirrors the cube's time
  axis for reverse mode so fits, gates, `t0` and the decay plot always read
  in forward time.
- **Peak target, not bin 0.** `t0_ps` is applied so the IRF peak lands at
  `peakTargetNs` (default 0.8 ns), leaving a pre-pulse window.
- **Background.** `backgroundWindowNs` before the peak estimates a flat
  background per pixel (and globally); all three fitters subtract it; the
  background rate is in metadata and the health strip.
- **Pile-up guard.** photons-per-sync ratio per frame: > 5 % warning,
  > 10 % red, in metadata, the checklist and the widget.
- **Peak-relative gates.** `GateSpec` gains `reference ∈ {'peak',
  'absolute'}` (default `'peak'`), so STED presets survive a `t0` change.

Frame and marker handling:

- **Frame role**, config-gated (`frameClockChannel` set): `Flim(...,
  frame_begin_channel=frame)`. **Collision**: `AdvancedScanTTLCycleDesigner`
  raises `frame_start_clock` on the same sample as line 0's `line_clock`
  edge, so the frame marker and `pixel_begin[0]` would share a timestamp with
  undefined order (the hazard the code already dodges with
  `pixel_end = begin + period − 1 ps`). Rule: **all DAQ-derived roles share
  one delay, and the pixel pattern starts at +1 ps**, so `frame_begin` leads
  `pixel_begin[0]` by ≥ 1 ps after all delays. Rig check: that `Flim` closes
  the last frame after `n_pixels` pixel ends without a following
  `frame_begin`; if not, feed `frame_end_clock` (the designer already
  generates it).
- **Final frame** via `getReadyFrameEx()` (`FlimFrameInfo.isValid()`,
  `getFrameNumber()`) when the frame role exists; today's `getCurrentFrame()`
  path stays for setups without it.
- **Live preview cost.** A 512²×391 uint32 Flim buffer is 410 MB; `Flim`
  holds current + ready copies, the worker's float32 cast is another, the
  cube product another (~1.6 GB at 512², ~6.5 GB at 1024²). Therefore: live
  preview uses `getCurrentFrameIntensity()` for intensity and the gate images
  are computed on the ready frame only; per-pixel fitting happens on the ready
  frame (or at a configurable low cadence, default off); bin decimation
  (e.g. 64 ps → 196 bins) is offered as a detector parameter; a row goes into
  `docs/design/plans/memory-budgets.md`. The old 1 s `LIVE_PREVIEW_S` becomes
  a setup parameter.
- **`LiveProducts`** (intensity, decay, gate images, background, pile-up,
  metadata — never the cube) is what the throttled `sigTimeResolvedProducts`
  carries from the worker thread; the cube appears only in the final
  `TimeResolvedScanProducts`. The signal is a queued cross-thread signal;
  the controller never deep-copies the cube on the GUI thread.
- **Accumulation.** Hardware-side `n_frame_average` is *not* used: it sums
  consecutive output frames (it would average z-planes) and needs the `Flim`
  object to outlive a scan, which `initiateScan` rebuilds today. Software
  accumulation stays in v1; `getSummedFrames()` on a persistent `Flim` with
  unchanged geometry is the follow-up once the frame role is rig-verified.
- **Linesteps.** ROADMAP M9 says the designer tiles `line_clock` Ny times
  even when S > 1. So the per-edge pattern must hold Nx·S markers spanning S
  line periods *including flyback* (`scan_samples_d2_period`), not Nx·S·dwell.
  The pattern is derived from the design values and verified by tutorial 07.
  `EventGenerator` behaviour on a trigger arriving mid-pattern is a rig check.
- **Delay sign and mechanism.** The APD drops leading samples
  (`APDManager.throwdata`), so the TimeTagger needs a *positive* delay on
  line and frame. Positive delays are applied as a pattern offset (keeps the
  raw line channel visible to tutorials); negative delays use
  `setInputDelay`. `setDelayHardware` (acts before the filter, small range) is
  not needed.
- **Overflows**: `getOverflowsAndClear()` per frame → `metadata['overflows']`;
  the frame is flagged invalid. Without a frame role the pixel index is lost
  for the rest of the scan; with one, the next `frame_begin` resyncs.
- Cube kept in the vendor's integer dtype.

### 3.3 `TimeTaggerFacade` — the scripting surface

Added to `MicroscopeFacade` as `facade.time_tagger`, built by
`build_facade_from_master` when `master.timeTaggerManager is not None`. Thin,
synchronous, cancel-aware (every blocking call polls the script's cancel
token and frees its measurement in `finally`), returns NumPy / dataclasses
with a `summary()` string and `to_dict()`:

```python
tt = facade.time_tagger
tt.channels()                                    # {role: ChannelInfo(channel, edge, trigger_v, ...)}
tt.count_rates(roles, duration_s=1.0)            # {role: Hz}
tt.rep_rate('laser_sync', duration_s=2.0)        # RepRateResult — see procedure below
tt.scope(roles, window_ns, trigger_role)         # ScopeTrace (both edges enabled)
tt.histogram(click_role, start_role, binwidth_ps, n_bins)   # decay / IRF / frame→line
tt.trigger_sweep(role, levels_v, duration_s)     # rate vs level, any role
tt.dark_rates(roles, duration_s)                 # laser blocked: dark + afterpulsing
tt.health()
tt.set_trigger_level(role, v); tt.set_delay(role, ps)        # refused during a scan
```

`rep_rate()` procedure (works on every card model): temporarily disable the
conditional filter, `setEventDivider(sync, N ≥ 16)`, measure with
`Countrate`/`TimeDifferences` (divider output is periodic; filter output is
not), restore both in `finally`. Refused while a scan lease is active.

No `raw` handle in v1 (D5 resolved: no). Nothing is exported through
`api.imcontrol` directly; the facade gets a hand-written reference page with
doctested examples, like the existing cookbooks.

### 3.4 `MockTimeTagger` — the second backend

New module `imswitch/imcontrol/model/interfaces/timetagger_mock.py` (the
`*_mock.py` convention), documented in `docs/mock-infrastructure.rst`. It
implements the subset of the `TimeTagger` module API the code and tutorials
use: `createTimeTagger`, `freeTimeTagger`, `getModel`, `getSerial`,
`getConfiguration`, `setTriggerLevel`, `setInputDelay`, `setDeadtime`,
`setEventDivider`, `setConditionalFilter`, `setTestSignal`,
`getOverflowsAndClear`, `Counter`, `Countrate`, `Histogram`,
`TimeDifferences`, `Scope`, `EventGenerator`, `Flim` (incl.
`frame_begin_channel`, `getCurrentFrame`, `getCurrentFrameIntensity`,
`getReadyFrameEx`).

Behind it, a **vectorised signal model in virtual card time** — no tag
generation, no real-time thread; each measurement computes its result
analytically or by sampling when read:

- **Line and frame edges come from the designed TTL arrays**, not from
  `mockscan`. `ScanSimulationCoordinator` only carries frame counts and a
  wall-clock duration compressed to ≤ 5 s; the real edge times are in
  `signalDict['TTLCycleSignalsDict']['line_clock' | 'frame_start_clock']`,
  which the mock receives at `sigScanBuilt` and converts to picoseconds with
  `scan.sampleRate`. The mock measures the *design* period, and says so.
- **Laser sync**: period `1/f_rep`, configurable jitter, analytic period
  histogram.
- **Photons**: a per-pixel Poisson cube drawn from a synthetic sample
  (`mockSample`: lifetime map ⊗ intensity map, e.g. two bead populations),
  convolved with a Gaussian IRF of configurable width and `t0`, plus a flat
  background; afterpulsing optional.
- **Electrical**: a sigmoid trigger-level response per role so sweeps show a
  plateau; edge-sign aware.
- **Bandwidth**: a tag-rate budget per mock model → overflows; filter-on
  behaviour (reversed direction; filtered sync rate ≈ photon rate).
- **Test signal**: correlated across channels, as on the card.
- **Faults** switchable from the setup block (`mockFaults`:
  `missing_line_clock`, `wrong_sync_polarity`, `line_delay_ps:+1200`, …) so
  the debugging tutorials have something to find without reaching into
  Python.

Shipped mock setup: **`galvo_flim_mock_scan_setup.json`** = the existing
`galvo_apd_mock_scan_setup.json` + a `timeTagger` block (`simulation: true`)
+ a `FLIM` detector + the `Lifetime` widget. All tutorials and the widget's
UI tests run on it.

---

## 4. Calibration and debugging tutorials

Location: `imswitch/_data/user_defaults/scripts/tutorial/timetagger/`, a
fourth block next to `basic/` and `scanning/`, under the shipped-tutorial
contract enforced by `imscripting/_test/test_shipped_tutorials.py`: first
line `Tutorial timetagger NN -- <title>`, sections *You will learn / Setup /
Mock setup: / It simulates: / Your own microscope: / Next:*, a helper module
with one declared `Imported by:` importer, each script run headless in its
own subprocess (120 s timeout). Thirteen new scripts roughly double that
test's wall time, so each tutorial's mock run is kept under ~10 s. The three
existing `workflows/timeresolved/` scripts get the header and move in as
steps 11–13; `tools/update_user_defaults_history.py` runs in P2 and P5, and
the changelog says that edited copies stay at the old path.

Each tutorial follows one arc — *what the signal should look like → measure
it → compare with the design value → what to change if it differs → write
back if `APPLY = True`* (D3 proposed) — and saves its numbers as `.npz`/CSV
next to the script. No text sparklines; the widget's Scope view and NumPy are
enough.

| Step | Script | Measures / teaches | Swabian API | Writes back |
|---|---|---|---|---|
| 01 | `01_meet_the_card.py` | Model, serial, resolution, channel count, HighRes group; role → channel map with edge sign; test signal on two channels + `Histogram` between them = a cable-free pipeline check. Mock vs. real. | `getModel`, `getConfiguration`, `setTestSignal`, `Counter`, `Histogram` | — |
| 02 | `02_trigger_levels_and_dead_time.py` | Trigger-level sweep on **every** role; plateau detection; polarity (negative channel numbers); 50 Ω input and the "DAQ line into 50 Ω" trap; dead time against ringing/double counts. | `Countrate`, `setTriggerLevel`, `setDeadtime` | `*TriggerV`, `photonsDeadtimePs` |
| 03 | `03_dark_counts_and_afterpulsing.py` | Laser blocked: dark rate per photon role; afterpulsing fraction from a self-histogram; stored as the background estimate the fitters and checklist use. | `Countrate`, `Histogram` | `backgroundWindowNs` sanity, background rate in metadata |
| 04 | `04_laser_sync.py` | Rep rate and period by the §3.3 procedure; jitter with the card-floor caveat; missing pulses; why the phasor fit needs the rate. | `setEventDivider`, `Countrate`, `TimeDifferences` | `laser_rep_rate_mhz` |
| 05 | `05_bandwidth_and_the_filter.py` | Tag budget per model; overflows with an unfiltered sync; enable the conditional filter, watch overflows stop — and watch the decay reverse; what `tcspc_direction` means; why the divider is not a remedy. | `getOverflowsAndClear`, `setConditionalFilter` | `filterSyncByPhotons` |
| 06 | `06_irf_and_t0.py` | Zero the existing click delay, histogram photons vs sync (direction-aware), IRF FWHM, peak position → `t0` for `peakTargetNs`; what a truncated window looks like; IRF sample notes (filter out, SPAD colour shift, or a τ ≪ IRF dye). | `Histogram`, `setInputDelay` | `t0_ps`, `binwidth_ps`/`n_bins` suggestion |
| 07 | `07_line_clock_during_a_scan.py` | `runScanAndWait` while counting line edges and measuring their period: edges = Ny (M9: not Ny·S), period = line + flyback; jitter; design-vs-measured table; what the pixel pattern must span with linesteps. | `Counter`, `TimeDifferences`, `loadScanParamsFromFile` | — |
| 08 | `08_frame_clock_and_pixel_markers.py` | `Histogram(click=line, start=frame)` → frame→line delay (design value 0 — the collision); build the detector's pixel pattern with the +1 ps rule; check the last pixel end lands before the next line edge; `Scope` trace of frame/line/pixel-begin for one line. | `Histogram`, `Scope`, `EventGenerator` | — |
| 09 | `09_line_delay_alignment.py` | Run a scan; compare the TimeTagger intensity image with the known truth: on the mock, the sample map (with an injected `line_delay_ps` fault); on the rig, the APD image (the mock APD is spatially constant, so no APD comparison on the mock). Cross-correlate → pixels → µs → `lineClockDelayPs`; apply; re-run; shift → 0. | pattern offset / `setInputDelay`, Flim intensity | `lineClockDelayPs` |
| 10 | `10_flim_preflight.py` | The checklist: rep rate matches config (by procedure), window spans a period, no overflows in 2 s, pile-up < 5 %, background fraction, line edges = Ny, frame leads pixel 0, direction consistent with filter. Same checklist the Signals panel shows. | all | — |
| 11 | `11_binned_photon_arrivals.py` | (existing, re-headed) | `Flim` cube | — |
| 12 | `12_gated_sted.py` | (existing, re-headed; gates from a JSON preset, peak-relative) | — | — |
| 13 | `13_tau_sted.py` | (existing, re-headed) | — | — |

Docs: `docs/timetagger/index.rst` (the card, roles, cabling incl. the 50 Ω
note, bandwidth budget per model, filter and direction, conditioning,
metadata, the mock), `mode-flim.rst`, `mode-gated-sted.rst`,
`mode-tau-sted.rst` (each ending with "verify with tutorials …" and a JSON
snippet), and `debugging.rst` (symptom → tutorial: "image all zeros", "line
0 Hz", "image shifted by a line", "lifetimes all ≈ 0.9 ns", "decay looks
reversed", "frame never finishes", "overflows").

`scripts/diagnostics/measure_laser_rep_rate.py` stays for no-GUI use as a
wrapper around the same `rep_rate` procedure.

---

## 5. The Lifetime widget

Widget key **`Lifetime`**; files `view/widgets/LifetimeWidget.py`,
`controller/controllers/LifetimeController.py`
(`ImConWidgetController + StatefulComponentMixin`), dock name "Lifetime
(FLIM / STED)", right dock next to Scan. `FLIMHist` becomes a one-line alias
in `ImConMainView.py` to the Lifetime widget (histogram view) and is removed
in the same release series (D4: no shipped setup uses it).

### 5.1 Layout

```
┌ Lifetime (FLIM / STED) ──────────────────────────────────────────────────────┐
│ Detector [FLIM ▾]   Mode: ( FLIM ) ( Gated STED ) ( Tau STED ) ( Signals )   │
├──────────────────────────────────────────┬───────────────────────────────────┤
│  Decay (log ▢)  direction: forward       │  mode panel                        │
│  ▲ counts        ┆STED                   │  ┌─ Gated STED ────────────────┐   │
│  │   ╭╮          ┆                       │  │ gate  from   to   ref  colour│   │
│  │   │ ╰╮      ┃early┃  ┃   late   ┃     │  │ early +0.5  +2.5  peak  ■   │   │
│  │ ▒ │  ╰─╮    ┃     ┃  ┃          ┃     │  │ late  +2.5  +8.0  peak  ■   │   │
│  │ ▒╭╯    ╰──╮ ┃     ┃  ┃          ┃     │  │ [+] [−] presets ▾ ratio ▢  │   │
│  └──┴────────┴─┴─────┴──┴──────────┴──▶ ns│  │ Live gate layers in viewer ▣│   │
│  bg window ▒ · peak 0.80 ns · 391×32 ps  │  └─────────────────────────────┘   │
├──────────────────────────────────────────┴───────────────────────────────────┤
│ ● photons 1.2 Mcps  ● sync (filtered)  ● line 0 Hz  ● frame —  pile-up 1.4 %  bg 2 %  overflows 0  [mock] │
│ [ Run once ]  [ Live ▶ ]  [ Stop ]   accumulate [ 4 ] scans   name [tau_sted_cells]  [ Save ] │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Decay plot** (always visible): aggregated decay of the last frame in
  forward time, log-y toggle, shaded background window, IRF-peak marker,
  draggable `LinearRegionItem`s for gates (Gated STED mode), and — when a
  `stedPulseChannel` photodiode role exists — a dashed **STED-pulse marker**
  from `Histogram(sted_pulse, sync)`: the one thing a gated-STED user tunes
  against, at the cost of a spare channel and no Pulse Streamer.
- **FLIM panel**: fit method, min counts, rep rate (with *measure* → the
  §3.3 procedure), window/bins/decimation, `t0` (with *find t0 from decay*),
  lifetime colour range, the two histogram views FLIMHist has today, and the
  display choice: lifetime image, intensity image, or an
  **intensity-weighted lifetime overlay** (HSV: hue = τ, value = intensity)
  as an RGB napari layer. Phasor readout: the per-frame (g, s) point on the
  universal semicircle (per-pixel cloud and selection are in the follow-up).
- **Gated STED panel**: gate table with `ref` column and colours; presets
  (JSON files, shared with tutorial 12); ratio image (late/early); *live gate
  layers*: each gate image is a napari layer `FLIM › gate:late`, updated from
  `LiveProducts` and scaled from the scan pixel size.
- **Tau STED panel**: the FLIM panel plus a τ-vs-intensity scatter and the
  software accumulate count.
- **Signals panel**: live per-role rates, the direction and filter state,
  pile-up and background fractions, overflow counter, per-role trigger-level
  spin boxes (refused with a message during a scan), *trigger sweep*, *scope
  snapshot* (digital traces of line/frame/sync — never photons), and the
  tutorial-10 checklist. "Open tutorial…" links are deferred.
- **Status strip** and **footer** as drawn. Save uses the shared
  `timeresolved/io` savers (HDF5 + TIFFs) into the Recording widget's folder.

### 5.2 Controller wiring

- **Run path threading.** `ScanWorkflowFacade.run_once(wait=True)` refuses
  to run on the GUI thread, by design. The controller therefore runs
  `TimeResolvedScanWorkflow.run()` on a dedicated worker thread (one per
  Run/Live session, stoppable), and marshals results back with
  `_invokeOnControllerThread`. Live mode re-arms from the worker, not from a
  Qt slot. This gives the widget and the scripts the same code path, and the
  same files.
- Listens to `sigTimeResolvedProducts(LiveProducts)` (queued, from the worker
  thread) for the live views; the final `TimeResolvedScanProducts` (with
  cube, if enabled) is fetched once on the worker thread after `wait_for_final`.
- **Layers.** `ImageWidget.addStaticLayer` creates a *new* layer on every
  call and `setImage` addresses live detector layers only; the
  `dynamic-layer-lifecycle` plan is not implemented. P4 therefore adds to
  `ImageWidget`: `upsertStaticLayer(name, im, scale)`, `removeStaticLayer(name)`
  and an RGB variant, used by a small `LayerSink` the controller owns so
  layer names are stable and removed on mode change.
- Mode and gate changes call `configureTimeResolvedProducts(...)` ("applies
  to next scan" shown until then). **Scans that are not the widget's own**:
  on `sigScanStarting` from another source, the controller clears product
  capture, so a plain z/t scan is not rejected by
  `_validate_time_resolved_scan_shape` while a Lifetime mode is on; the
  workflow `run()` gets the same `finally: clear()`.
- Holds a `LeasePurpose.WORKFLOW` lease on the detector only while Run/Live is
  active, like the workflow does. RECORDING and WORKFLOW leases coexist by
  design, so the widget does **not** refuse Run during a recording (rev. 1
  said otherwise).
- State persisted (gates, presets, mode, display options, accumulate N) via
  `StatefulComponentMixin`. Device conditioning is setup-file state, never
  widget state (`widget-state-persistence.md`: never restore hardware-active
  state).

### 5.3 Tests

- `_test/unit/test_lifetime_controller.py` (nohardware): fake `LiveProducts`
  → expected layer upserts; gate edits → configure calls; foreign scan start
  → clear; worker-thread Run → marshalled completion.
- `_test/ui/test_lifetime_widget.py` (ui): gate region drag ↔ table; preset
  load; log toggle; peak-relative → absolute conversion on `t0` change.
- Shipped-setup smoke: the widget constructs on
  `galvo_flim_mock_scan_setup.json` and one mock scan produces non-empty gate
  layers.

---

## 6. Data and saving

- **`LiveProducts`** (new, small) vs **`TimeResolvedScanProducts` v2** (final;
  adds `background_rate`, `pileup_fraction`, `tcspc_direction`,
  `frames_accumulated`, `overflows`, `time_tagger` metadata incl. model,
  serial, roles and conditioning, `format_version = 2`; cube in integer
  dtype). `max_retained_products` is removed (D8 resolved).
- **`GateSpec.reference`** (`'peak'` default) and `processing.resolve_gates(
  gates, peak_time_ns)`; saved gate attributes store both the relative and the
  resolved absolute bounds.
- **`timeresolved/io.py`**: the HDF5/NPZ/TIFF writers move out of
  `workflows/time_resolved.py` so the widget and the workflows share them; the
  HDF5 layout gains `time_tagger/` and `background/` groups and a
  `format_version` attribute; a reader `load_products(path)` comes with it.
- **Recording**: unchanged in this plan. A FLIM recording still saves the
  lifetime image; a *recorded product* parameter and an attached products
  file are in the follow-up (only `sigRecordingEnded` exists as a hook today).
- Changelog entries go into each phase's PR under "Unreleased", not into P7.

---

## 7. Scan integration (red zone — hardware verification before enabling)

| Change | Why | Gate |
|---|---|---|
| Frame role with the +1 ps pattern offset and shared DAQ-role delay | Card-closed frames via `getReadyFrameEx`; avoids the frame/pixel-0 timestamp collision | Config-gated (`frameClockChannel`); rig check that the last frame closes without a trailing `frame_begin` |
| Pixel pattern spans S line periods incl. flyback | Linesteps are ignored today; M9 says edges = Ny | Unit test on marker count; tutorial 07 on the rig |
| `lineClockDelayPs` (positive → pattern offset; negative → `setInputDelay`) applied to line and frame | Card-side half of the M9 detector-sync fix, measured by tutorial 09 | Default 0 → no behaviour change; roadmap M9 note |
| Conditional filter ⇒ reverse direction, mirrored axis | Bandwidth on TT20/Ultra; correctness | Default off (today's behaviour); tutorial 05 proves the need per rig |
| Background subtraction, pile-up guard, peak target | Measurement correctness (§1.9, §1.10) | Default windows chosen so a rig with `t0_ps = 0` sees a warning, not a change, until tutorial 06 is run |
| Overflow → frame flagged invalid; resync on next frame edge | Silent corruption today | Metadata only (D7 default) |
| Live preview from `getCurrentFrameIntensity`, fits on the ready frame | Memory and CPU (§3.2) | Memory-budget row; the old cadence remains selectable |
| `pixel_end = begin + period − 1 ps` | Keep; document | — |

---

## 8. Phases

Each phase lands green on CI without hardware and names the rig check the
next phase relies on. Changelog in every PR.

**P0 — Review and rig facts.** This document, revised until agreed; §13
answered.

**P1a — Bug fixes only (half-day PR).** Template `default` entries removed
(gap 6); `finally: clear()` in the workflow (gap 2); FLIMHist docstring
(gap 11); dead fitters removed (gap 7); docs paths (gap 12).

**P1b — Device layer, block, compatibility, trivial mock.**
`TimeTaggerInfo` + section template + schema; `TimeTaggerManager` with
roles, conditioning, configuration lock, health, `finalize` wired into
`closeEvent`; detector takes the manager (compatibility path with
deprecation message); fitters moved to `timeresolved/fitting.py`; a counting
mock (rates and test signal only) so the manager is unit-tested.
*Rig check*: the existing FLIM config scans unchanged; the new block connects.

**P1c — Signal-model mock, shipped setup, worker end-to-end.** Edges from
the TTL arrays, Poisson cube from `mockSample`, trigger response, tag budget,
filter behaviour, faults; `galvo_flim_mock_scan_setup.json`; the worker runs
end-to-end in CI for the first time and recovers the mock sample's lifetimes
within tolerance for each fit method (with and without background).
*Rig check*: none (software only).

**P2 — Facade and device-level tutorials 01–06, 10.** `TimeTaggerFacade`
with the `rep_rate` procedure and refusal-during-scan; `timetagger_helpers.py`;
`docs/timetagger/index.rst`, `debugging.rst`; README rows;
`update_user_defaults_history`; CLI wrapper.
*Rig check*: 02–06 on the real card; model-specific numbers for §1's table.

**P3 — Scan-aware diagnostics and scan correctness.** Tutorials 07–09;
frame role with the collision rule; linestep-aware pattern; delay mechanism;
direction-aware axis; background, pile-up, peak target; overflow flagging;
`LiveProducts` and `sigTimeResolvedProducts`; preview from intensity.
Roadmap M9 updated with the card-side half.
*Rig check*: 07 edge count/period vs design; 08 frame leads pixel 0; 09
shift → 0 after delay; last frame closes.

**P4 — Lifetime widget v1 (depends on P1 only, not on P3's frame role).**
FLIM and Tau STED panels, Signals panel, status strip, footer with the
worker-thread Run path, `ImageWidget` upsert/remove/RGB layer API and
`LayerSink`, intensity-weighted overlay, (g, s) readout, FLIMHist alias.
`docs/gui.rst` section replaces the FLIMHist one.
*Rig check*: live τ overlay on a reference dye; checklist green.

**P5 — Gated STED panel, shared io, products v2.** Gate table with
`reference`, regions, presets, live gate layers, ratio image, STED-pulse
marker (if cabled), `timeresolved/io.py` with reader, products v2; tutorials
11–13 re-headed onto the mock setup and under test;
`update_user_defaults_history`.
*Rig check*: the widget's gated image equals tutorial 12's on the same scan
(identical file contents).

**P6 — Validation campaign and release.** Roadmap 13.A FLIM row closed with
the per-fit-method convergence test on a reference dye (tutorial 10's
extended mode); FLIMHist alias removed; old plan marked superseded; the
follow-up plan (§9) opened with the rig numbers measured here.

Rough size: P1b and P4 are the big ones; P1c is medium; P2/P3 are many
small files; P5 medium. Everything before P4 is invisible to a user who
never opens the Scripting tab, except P1a's fixes.

---

## 9. Follow-up plan (recorded here, not planned here)

Moved out after review so this plan stays deliverable; each item depends on
rig numbers from P2/P3:

- **Two-colour FLIM**: two photon roles → two `Flim` objects sharing one
  `EventGenerator` pair, started under `SynchronizedMeasurements`.
- **Intensity-only counter detector** (`CountBetweenMarkers`, fixed
  `n_values`, re-armed per frame) — only if a sync-free rig exists; the Flim
  intensity product already covers the rest.
- **Outer axes** (z/t cubes): needs the designer's linestep fix (M9) and a
  decision between reading each ready frame promptly or
  `finish_after_outputframe = N_z` (N_z more buffers).
- **Hardware-side accumulation**: persistent `Flim` + `getSummedFrames()`.
- **Raw tags and replay**: `FileWriter` (records post-filter tags) and
  `createTimeTaggerVirtual` (`replay`, `setReplaySpeed(-1)`,
  `waitForCompletion`; filter emulation to be verified) — one recorded rig
  session driving the test suite with real tags.
- **Recording hook**: a *recorded product* parameter (lifetime / intensity /
  gate) and `<rec>_timeresolved.h5` attached via a `RecordingManager`
  post-scan hook, once that hook exists.
- **Phasor per-pixel cloud with cursor selection → labels layer; ROI decay**
  from the cube.
- **IRF-deconvolved tail fit / two-exponential fit** using the stored IRF.
- **Software-defined gating** (`GatedChannel` / `DelayedChannel`) with the
  STED-pulse photodiode as reference; a `software_clock` reference on a spare
  channel (it cannot share the filtered sync).
- **ImProcess**: open products files, re-gate and re-fit offline with the same
  `timeresolved/` code; lifetime overlay processor.
- `facade.time_tagger.raw`, "Open tutorial…" links.

---

## 10. Files touched (first cut)

| Area | Files |
|---|---|
| New model | `model/managers/TimeTaggerManager.py`, `model/interfaces/timetagger_mock.py`, `model/timeresolved/fitting.py`, `model/timeresolved/io.py`, `model/timeresolved/live.py` (`LiveProducts`) |
| Changed model | `SwabianTimeTaggerManager.py` (slimmed), `model/SetupInfo.py` (`TimeTaggerInfo`), `MasterController.py` (construct, `lowLevelManagers`, `closeEvent` attrs), `workflows/facade.py` (`time_tagger`), `workflows/time_resolved.py` (clear, io import), `model/timeresolved/{types,processing}.py` (v2, direction, gate reference, background) |
| Config | `view/configeditor/builtin_templates/sections/timeTagger.json` (flat), `model/configeditor/schemas/kinds/…` for the dataclass, detector template `default` removal, `docs/setupinfo-reference.rst` |
| GUI | `view/widgets/LifetimeWidget.py`, `controller/controllers/LifetimeController.py`, both `__init__.py` maps, `ImConMainView.py` dock tables + `FLIMHist` alias, `ViewSetupInfo.py` docstring, `view/widgets/ImageWidget.py` (upsert/remove/RGB static layers) |
| Scripts | `scripts/tutorial/timetagger/01..13_*.py`, `timetagger_helpers.py`, `gate_presets/*.json`, `tutorial/README.md`, `scripts/diagnostics/measure_laser_rep_rate.py` (wrapper) |
| Setups | `imcontrol_setups/galvo_flim_mock_scan_setup.json` |
| Docs | `docs/timetagger/*.rst`, `docs/gui.rst`, `docs/devices/detectors.rst`, `docs/scripting.rst` (tutorial block), `docs/mock-infrastructure.rst`, `docs/design/plans/memory-budgets.md` (row), `docs/changelog.rst` (per PR), `ROADMAP.md` (new milestone + M9 note), this plan |
| Tests | `test_timetagger_manager.py`, `test_timetagger_mock.py`, `test_swabian_flim_worker_mock.py`, `test_timetagger_facade.py`, `test_timeresolved_direction_and_gates.py`, `test_lifetime_controller.py`, `ui/test_lifetime_widget.py`; shipped-tutorial test picks up the new block automatically; `test_user_defaults_sync.py` |

---

## 11. Risks

- **Card model drives everything electrical and bandwidth-related.** The
  plan defaults to today's behaviour (no filter, forward direction) and lets
  tutorials 02 and 05 prove the need per rig. §13 asks for the model.
- **Frame role semantics on the real card** (last-frame close, mid-pattern
  triggers). Config-gated; the current path stays until rig-verified.
- **Memory.** Cube products at 1024² are multi-GB; the preview path avoids
  the cube and decimation is offered. A hard per-scan budget row is added.
- **Mock fidelity.** A too-kind mock hides rig problems; faults and the
  measured-vs-design distinction keep it honest; raw-tag replay is the
  follow-up's stronger answer.
- **Threading.** Worker-thread Run path, queued signals, configuration lock
  released on `BaseException`: each has a unit test in its phase.
- **Widget scope creep.** Phasor selection, ROI decay, recording hook are out
  of this plan, deliberately.

---

## 12. Decisions still open for this review round

Resolved by the first round (recorded, not re-asked): D5 raw handle → **no**
in v1; D8 `max_retained_products` → **drop**; D9 hardware accumulation →
**later**, software stays; D10 phase order → correctness first, widget
depends on P1 only.

- **D1 — Device block shape.** Flat prefixed role fields (proposed, fits the
  config editor today) vs a nested `channels` map plus a one-level extension
  of the section editor in P1b.
- **D2 — Tutorial home.** `scripts/tutorial/timetagger/` under the shipped-
  tutorial test (proposed) vs `scripts/workflows/`.
- **D3 — Write-back policy.** Print suggestions, apply only with
  `APPLY = True` (proposed) / always / never.
- **D4 — FLIMHist fate.** One-line alias, removed in the same release series
  (proposed).
- **D6 — Widget name.** `Lifetime` (proposed) vs `FLIM` vs `TCSPC`.
- **D7 — Overflow handling.** Flag in metadata only (proposed) vs also
  withhold the frame from a recording.
- **D11 — Direction default.** Forward with the filter off (today's
  behaviour, proposed) vs filter on by default for TT20/Ultra models once
  `getModel` identifies them.
- **D12 — Peak target and background window defaults.** 0.8 ns / 0.4 ns
  (proposed) — or derived from the measured IRF FWHM in tutorial 06.
- **D13 — STED-pulse photodiode role.** Include the optional role and decay
  marker in P5 (proposed, cheap) or leave it to the follow-up.

## 13. Rig facts the plan needs

1. Time Tagger model(s) and firmware (20 / Ultra / X); channel count; which
   channels form the Ultra HighRes group, if used.
2. Laser sync into the card: direct at the full rep rate, or divided? Any
   overflows observed (nothing reads them today)?
3. Is `frameStartClockLine` cabled to a card channel? A pixel clock, or only
   the line clock?
4. DAQ clock-line amplitude into 50 Ω (or whether a buffer is in the path).
5. Photon detector(s): SPAD/APD or PMT, pulse polarity and amplitude, dead
   time.
6. One or two photon channels; any plan for two-colour FLIM or a second card.
7. Typical scan: Nx·Ny, dwell, laser rep rate, expected photons per pixel —
   sets the mock defaults and the memory budget.
8. Where measurements should land (today's scripts hard-code
   `D:/Measurements`; the widget would use the Recording widget's folder).
9. Is a fast photodiode on the STED beam available for the `stedPulse` role?
10. Is the Pulse Streamer used on the rig at all? (The manager exists but is
    not wired in.)

---

## 14. Relationship to existing plans

- Supersedes `docs/design/plans/time-resolved-detector-workflows.md` once P1b
  lands; its contract and workflows are kept.
- Complements ROADMAP **M9** (detector-sync): M9 fixes the designer side
  (`phase_delay` into `line_clock`, linestep edge count); this plan measures
  the offset with the card and applies a card-side delay, and derives its
  pixel pattern from the edge count M9 documents.
- Uses the lease model from `detector-acquisition-selection.md` as it stands
  (`LeasePurpose.WORKFLOW`; RECORDING and WORKFLOW coexist).
- Adds the `ImageWidget` static-layer API that `dynamic-layer-lifecycle.md`
  (not implemented) would also need.
- Adds a row to `memory-budgets.md`.

---

## 15. Review log

**Round 1 (two independent reviews: measurement physics / vendor API, and
architecture).** Changes made in revision 2:

*Measurement and API*
- Conditional filter reverses TCSPC direction → `tcspc_direction` derived
  from the filter, axis mirrored in processing; `setEventDivider` removed as
  a FLIM remedy and kept only inside the rep-rate procedure; sync rate labelled
  "(filtered)".
- Frame-start clock coincides with line 0's edge (verified in
  `AdvancedScanTTLCycleDesigner.__generate_all_clocks`) → +1 ps pattern
  offset, one shared delay for DAQ roles, `frame_end_clock` fallback, rig
  check for last-frame close. `getReadyFrameEx()` named correctly;
  `n_frame_average` dropped (averages z-planes, needs a persistent `Flim`).
- Background model, pile-up guard, peak target instead of bin 0,
  peak-relative gates, dark-count tutorial added.
- Memory: preview from `getCurrentFrameIntensity`, fits on the ready frame,
  decimation, budget row; `LiveProducts` without the cube.
- Electrical: 50 Ω inputs, DAQ line amplitude, edge sign via negative channel
  numbers, dead time, trigger sweep on every role; jitter floor caveat;
  `Scope` both edges; `Histogram` instead of `Correlation` for frame→line.
- Delay sign (positive for the TimeTagger) and mechanism (pattern offset vs
  `setInputDelay`); linestep pattern spans S periods incl. flyback.
- Optional STED-pulse photodiode role and decay marker.

*Architecture*
- Mock derives edges from the designed TTL arrays, not from `mockscan`
  (which carries only frame counts and a ≤ 5 s wall clock); tutorial 09
  compares against mock truth, not the spatially constant mock APD.
- Widget Run path moved to a worker thread (`run_once(wait=True)` refuses
  the GUI thread); products never deep-copied on the GUI thread.
- Block flattened to fit the section editor; `TimeTaggerInfo` dataclass;
  `simulation` / `useMockOnFailure` naming; mock fallback only with
  `nidaq.simulation`; `finalize` wired into `closeEvent`; manager stored as
  `None`-when-absent; facade built from `master.timeTaggerManager`; no
  Settings-tree presence claimed.
- Lock is a configuration lock (refuse conditioning writes during a scan),
  not a measurement lock; `RLock` released on `BaseException`.
- Lease: RECORDING and WORKFLOW coexist, "refuse Run during recording"
  removed; clear product capture on foreign scan starts.
- `ImageWidget` upsert/remove/RGB static-layer API specified (dynamic-layer
  plan is not implemented).
- P1 split into P1a/b/c; P6 (two-colour, counter detector, outer axes,
  phasor selection, raw tags, recording hook) moved to a follow-up plan;
  widget depends on P1 only.
- Factual corrections: workflow clears before, not after; FLIMHist reads
  private attributes only in decay mode; Pulse Streamer exists but is not
  wired; template `default` entries deleted rather than corrected; shipped-
  tutorial contract spelled out; `update_user_defaults_history` in P2 and
  P5; changelog per PR.
