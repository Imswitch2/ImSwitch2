# Lifetime 2.0 — Swabian Time Tagger, calibration tutorials, and a Lifetime widget

Status: Proposed — **revision 3**, after two review rounds (each round: one
independent measurement-physics / vendor-API review and one architecture
review, both verified against the code). Nothing is implemented. Both
round-2 reviewers judged P1a ready to start now and P1b ready once the items
they named were written down; this revision writes them down. §12 lists the
decisions still open; §13 the rig facts the plan needs; §15 logs what each
round changed and why.

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
| Workflows | `imswitch/imcontrol/model/workflows/time_resolved.py` | `BinnedPhotonArrival`, `GatedSTED`, `TauSTED`: clear → configure → `facade.scan.run_once()` → wait → save HDF5/NPZ/TIFF. |
| Scripts | `imswitch/_data/user_defaults/scripts/workflows/timeresolved/01..03_*.py` | Three ~30-line scripts that run those workflows. Hardware-only, untested, hard-coded `D:/Measurements`. |
| GUI | `FLIMHistWidget` / `FLIMHistController` (`FLIMHist` key) | One pyqtgraph bar plot: per-pixel lifetime histogram (from `sigUpdateImage`), or the aggregated decay with a global τ marker (read from the manager's private `_last_*` attributes). |
| Diagnostics | `scripts/diagnostics/measure_laser_rep_rate.py` | Standalone CLI, `Countrate` on the sync channel. The only Time Tagger diagnostic. |
| Docs | `docs/devices/detectors.rst` §SwabianTimeTaggerManager, `docs/scripting-time-resolved-workflows.rst`, `docs/design/plans/time-resolved-detector-workflows.md` | Config reference, workflow cookbook, and the plan that produced the contract. |

**Swabian API actually used** — the complete list: `createTimeTagger`,
`setTriggerLevel`, `setInputDelay` (click channel, for `t0_ps`),
`EventGenerator`, `Flim(...)`, `Flim.getCurrentFrame`. Nothing else.

**What the card can do that we don't use** (the "unleash" list, with the
caveats the reviews added):

| Capability | Swabian API | Why it matters for us |
|---|---|---|
| Live count rates per channel | `Counter`, `Countrate` | "Is the SPAD alive? Is the sync there? Is the line clock firing?" — today answered by scanning and seeing zeros 10 s later. |
| Period / jitter of a periodic signal | `TimeDifferences` | Laser period; line-clock period against the scan design. The card's own two-tag jitter floor (TT20 ≈ 48 ps RMS, Ultra ≈ 30 ps, X < 10 ps) is what you measure for a good laser; report "card-limited" below it. |
| Digital timing trace | `Scope` | An oscilloscope on line/frame/pixel channels with ps resolution. Needs both edges enabled (`+ch` and `−ch`); never on the sync channel. |
| Cross-channel delay | `Histogram` (`Correlation` is symmetric and noisier) | IRF and `t0`; frame→line skew; STED-pulse→excitation delay if a photodiode is cabled. |
| Frame boundary | `Flim(frame_begin_channel=…)`, `getReadyFrameEx()` / `FlimFrameInfo`, `getFramesAcquired()`, `getCurrentFrameIntensity()`, `finish_after_outputframe` | The DAQ already outputs `frameStartClockLine`. `Flim` closes a frame after `n_pixels` pixel ends with or without a frame channel; the frame channel buys **resync** after a lost marker. `Flim` has no frame-*end* input. |
| USB bandwidth control | `setConditionalFilter`, `setDeadtime`, `getOverflowsAndClear` | An 80 MHz sync is 80 M tags/s: over a TT20's USB budget (≈ 8.5 M/s) and an Ultra's (≈ 65–80 M/s); only an X takes it raw. The filter passes only the first sync *after* each photon — which **reverses the TCSPC direction** (§3.2). `setEventDivider` on the sync is *not* a FLIM remedy (the window becomes N periods and nothing folds it); it is only a tool for measuring the rep rate. |
| Delay on any channel | `setInputDelay` (software, signed), pattern offsets | A ps-resolution fix for the APD-vs-TimeTagger line offset (ROADMAP M9). Sign: the APD *drops* leading samples, so the galvo lags and the line clock fires early; the TimeTagger needs a *positive* delay on line and frame. |
| Self test | `setTestSignal` | ~0.8–0.9 MHz per channel on a TT20 (`setTestSignalDivider` on Ultra/X); the signals on two channels are correlated, so a cable-free `Histogram` checks the whole pipeline. |
| Electrical inputs | `setInputImpedanceHigh`, `setInputHysteresis` (Ultra/X); negative channel numbers select the falling edge | All inputs are 50 Ω (TT20 fixed). An NI DO/PFI line drives ~1–1.5 V into 50 Ω: a 1.5 V threshold on the line clock may never trigger — the "line 0 Hz" symptom. Trigger range ±2.5 V. |
| Raw tags, replay | `FileWriter`, `createTimeTaggerVirtual` | Follow-up plan (§9): the only feature that needs the real library in tests, and `.ttbin` records post-filter tags. |
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
4. **Linesteps.** `n_linesteps` (S) is read and ignored. The TTL designer
   tiles the line clock Ny times even when the galvo waveform has Ny·S line
   periods (`AdvancedScanTTLCycleDesigner.__generate_all_clocks` tiles
   `period` `n_steps_dx[1]−1` times and zero-pads), so with S > 1 the first
   Ny periods carry edges and the remaining Ny·(S−1) carry none — M9's "too
   few edges". z/t stacks read only the current frame.
5. **`max_retained_products`** is accepted and not honoured (and nothing
   reads it).
6. **Config template drift.** The builtin detector template carries
   `default` entries `n_bins=64` (which triggers the truncation warning the
   code added to prevent exactly that) and `line_trigger=-0.5` (code:
   `+0.5`); it lacks `laser_rep_rate_mhz`. Templates are presentation
   overlays: delete the `default` entries rather than correcting them.
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
    into the last bins and leaves no pre-pulse window.
11. **FLIMHistController** docstring says seconds; the frame is in ns.
12. **Docs drift**: `time-resolved-detector-workflows.md` points at
    `scripts/timeresolved/`; the files live in `scripts/workflows/timeresolved/`
    (the roadmap already has the right path).

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
  `t0_ps`, trigger levels, line delay, background rate). They run on a
  simulated card, like every other shipped tutorial.
- G3. **Mode setup guides**: one doc page per mode (FLIM, gated STED,
  tau-STED) that says what to cable to which channel, which settings matter,
  how to verify, and gives a setup-JSON snippet.
- G4. A **Lifetime widget** that makes gated STED and tau-STED point-and-click:
  live decay with draggable, peak-relative gates, gate images and a lifetime
  overlay in napari (updated on each ready frame; for a one-shot run that is
  once, at the end), phasor readout, accumulation, one-click save, and a
  signal-health strip for every Time Tagger role.
- G5. **Measurement correctness**: TCSPC direction handled, background
  subtracted, pile-up flagged per pixel, frame boundaries closed by the card,
  linesteps either counted correctly or refused, a measured and applied line
  delay.
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
- Changing galvo trajectories or `GalvoScanDesigner`. The one designer
  change this plan allows itself is the TTL *clock* count for linesteps
  (§3.2), which is M9's own listed sub-item and touches no analog waveform.
- Two-colour FLIM, an intensity-only counter detector, z/t cubes, raw-tag
  recording and replay, a recording-manager hook, and phasor pixel
  selection: all in a follow-up plan (§9), so this plan stays deliverable.

---

## 3. Architecture

```
                 ┌──────────────────────────────────────────────────────────┐
 setup JSON      │  TimeTaggerManager   (new, low-level, one per card)      │
 "timeTagger": { │  - owns the TimeTagger connection (real or Mock)         │
   roles (flat), │  - named roles → channel, edge sign, trigger, delay,     │
   filter, ... } │    dead time ; conditional filter → tcspc_direction      │
                 │  - scan hold: conditioning writes refused while held     │
                 │  - health snapshot (rates, overflows, pile-up)           │
                 │  - subscribes to nidaq.sigScanBuilt (feeds mock edges)   │
                 └───────┬───────────────────────────────┬──────────────────┘
                         │ beginScanHold/endScanHold     │
        ┌────────────────▼───┐              ┌────────────▼────────────────┐
        │ SwabianTimeTagger  │              │ TimeTaggerFacade            │
        │ Manager (FLIM det.)│              │ (scripts/tutorials, widget  │
        │ slimmed: Flim +    │              │  Signals panel)             │
        │ worker only        │              └─────────────────────────────┘
        └────────┬───────────┘
                 │ LiveProducts (per ready frame / low cadence, no cube)
                 │ TimeResolvedScanProducts (final, cube optional)
        ┌────────▼───────────────────────────────────────────────┐
        │ timeresolved/  types · processing · fitting (moved)     │
        │                preflight (new) · io (new)              │
        └────────┬──────────────────────────┬────────────────────┘
                 │                          │
        ┌────────▼──────────┐      ┌────────▼───────────────┐
        │ workflows/        │      │ LifetimeWidget /        │
        │ time_resolved.py  │      │ LifetimeController      │
        │ (scripts)         │      │ (FLIM · Gated STED ·    │
        └───────────────────┘      │  Tau STED · Signals)    │
                                   └─────────────────────────┘
```

**Role vocabulary** (one mapping, used everywhere):

| Role id (code, metadata, facade) | Setup-JSON field prefix | Flim slot, forward | Flim slot, reverse | Strip label |
|---|---|---|---|---|
| `photons` | `photons…` | `click_channel` | `start_channel` | photons |
| `laser_sync` | `laserSync…` | `start_channel` | `click_channel` | sync / sync (filtered) |
| `line_clock` | `lineClock…` | `EventGenerator` trigger | same | line |
| `frame_clock` | `frameClock…` (optional) | `frame_begin_channel` | same | frame |
| `sted_pulse` | `stedPulse…` (optional) | — (diagnostic only) | — | STED |

### 3.1 `TimeTaggerManager` — the shared device layer

New file `imswitch/imcontrol/model/managers/TimeTaggerManager.py`, constructed
by `MasterController` as `TimeTaggerManager(setupInfo.timeTagger, setupInfo,
nidaqManager)` from a new `TimeTaggerInfo` dataclass in `SetupInfo.py` (next
to `NidaqInfo` / `TeensyPulseInfo`), stored as `master.timeTaggerManager`
(always present, `None` when the block is absent — the `pulseGeneratorManager`
pattern), passed in `lowLevelManagers`, and added to `closeEvent`'s
`manager_attrs` **after `'detectorsManager'`** (the detector must stop its
worker and release `Flim` before `freeTimeTagger`) and to `unsafeWhileActive`
(an unresolved detector stop fault must also veto freeing the card).

**The block is flat.** The config editor's system-section form edits a flat
field list only, and the role set is closed, so roles become prefixed fields.
Nullable channels are JSON `null` (the editor's number fields parse empty /
`null` to `None`).

```json
"timeTagger": {
  "serial": null,
  "simulation": false,
  "useMockOnFailure": false,

  "photonsChannel": -1,    "photonsTriggerV": -0.25, "photonsDeadtimePs": 50000,
  "laserSyncChannel": 2,   "laserSyncTriggerV": 0.5,
  "lineClockChannel": 3,   "lineClockTriggerV": 0.5,  "lineClockDelayPs": 0,
  "frameClockChannel": null, "frameClockTriggerV": 0.5,
  "stedPulseChannel": null, "stedPulseTriggerV": 0.5,

  "filterSyncByPhotons": false,
  "pixelPatternOffsetPs": 10000,
  "peakTargetNs": null,
  "backgroundMode": "dark_rate",

  "mockSample": "beads_two_lifetimes",
  "mockFaults": {}
}
```

Edge sign follows the Swabian convention: a negative channel number means the
falling edge (the example photon channel is `-1`: a SPAD NIM pulse is
triggered on its falling edge, not on its return). `simulation` forces the
mock. `useMockOnFailure` (default **false**, a deliberate departure from
`TeensyPulseInfo`'s `True`) falls back to the mock when the library is
missing or `createTimeTagger` fails — **only if `nidaqManager.isSimulated`
is also true**. On a real rig a missing card stays a hard error that rolls
the scan back, as today.

Responsibilities:

- **Connection**: `createTimeTagger(serial)` once; read `getModel()` /
  `getConfiguration()` first and resolve roles after it (Ultra HighRes
  renumbers channels); `finalize()` stops the health sampler and calls
  `freeTimeTagger`; a test proves no thread survives it.
- **Roles**: `channel('laser_sync') -> int` (signed), per the vocabulary
  table. Everything above this layer speaks roles.
- **Conditioning**, applied at connect: trigger level, input delay, dead time
  per role; `setConditionalFilter(trigger=[photons], filtered=[laser_sync])`
  when `filterSyncByPhotons`. Every value is snapshotted into
  `metadata['time_tagger']`. There is no Settings-tree presence (that tree
  shows detector parameters only); conditioning is edited in the setup file,
  in the widget's Signals panel, or through the facade.
- **TCSPC direction** is derived: `filterSyncByPhotons` ⇒ `'reverse'`
  (Flim slots swap per the vocabulary table; the time axis is mirrored in
  processing, §3.2) else `'forward'`. In metadata and on the strip.
- **Scan hold** (the lock that matters): the FLIM detector calls
  `beginScanHold()` in `initiateScan` and `endScanHold()` on scan done,
  abort and build-failure rollback. While held, conditioning writes and the
  facade's `rep_rate()` are **refused** with a clear error. Measurement
  objects themselves run concurrently on the card and are not serialised.
  Ordering rule: `_onScanBuilt` runs synchronously inside
  `NidaqManager.runScan` under its finalize lock, so the manager never waits
  on anything nidaq-side while holding its own lock.
- **Health**: a slow background `Counter` feeds `health()` (per-role rates,
  overflow delta, pile-up fraction, direction, model, serial, mock flag) and
  `sigHealth`; sampling is **off by default in tests** (opt-in) to avoid
  flaky teardown. With the filter on the measured sync rate ≈ photon rate by
  construction; the strip labels it "sync (filtered)" and the rep-rate check
  uses the §3.3 procedure.
- **Mock feed**: subscribes to `nidaqManager.sigScanBuilt(scanInfoDict,
  signalDict, deviceList)` (emitted for simulated runs too) and, when the
  backend is the mock, calls
  `mock.load_scan_edges(signalDict['TTLCycleSignalsDict'], setupInfo.scan.sampleRate)`.
- **Compatibility**: a setup with a `SwabianTimeTaggerManager` detector and
  no `timeTagger` block keeps working: the detector builds a private
  `TimeTaggerManager` from its own `click_channel` / `start_channel` /
  `line_channel` and trigger properties, with a deprecation warning that
  prints the equivalent block.

### 3.2 `SwabianTimeTaggerManager` slimmed

Keeps: scan lifecycle, pixel-marker generation, the `Flim` worker, the
`TimeResolvedDetectorMixin`, scan hold calls. Loses: connection management
and trigger-level parameters (its channel parameters become role selections,
defaulting to the vocabulary table), and the fitters, which move to
`timeresolved/fitting.py` as a `LifetimeFitter` built once per scan (trig
tables cached as today), still executed in the worker thread, never in a
controller.

**Direction, window and `t0`:**

- *Forward* (filter off): `Flim(start=laser_sync, click=photons)`. `t0` is
  applied as today with `setInputDelay(photons, …)` so the IRF peak lands at
  the peak target; a truncated window loses the *tail* (today's warning).
- *Reverse* (filter on): `Flim(start=photons, click=laser_sync)`. The
  histogram is periodic and early photons appear near `t' ≈ T_rep`, so the
  window **must span ≥ T_rep plus a jitter margin — a hard refusal**, not a
  warning. `t0` is applied as a **circular roll in software** (exact for a
  periodic histogram); the sync channel's delay is never touched, and the
  photon delay is never used for `t0` in this mode (delaying the start by
  `+d` makes photons with `t' < d` land after their chosen sync and vanish).
- `processing.oriented_axis()` is an axis *transform* `t = T_rep − t'` with
  the sub-bin offset carried explicitly (12 500 ps / 32 ps = 390.625 bins),
  not `np.flip`. Fits, gates, the decay plot and `t0` always read forward
  time.

**Background, peak target, pile-up:**

- `backgroundMode = 'dark_rate'` (default): background per pixel = (dark +
  afterpulsing rate from tutorial 03) × dwell, scaled by the pixel's
  exposure; subtracted by all three fitters. `'pre_pulse_window'` is a
  diagnostic (and acceptable when T_rep ≳ 5 τ); at 80 MHz a 3 ns tail is
  still ≈ 2 % of peak and falling at 11.7 ns, so treating it as flat biases
  τ short. The moment fitter gets the periodic correction for long τ; the
  phasor is periodic by construction.
- `peakTargetNs = null` ⇒ derived from the measured IRF: window end ≥ 2 × IRF
  FWHM before the peak, fallback 1.5 ns (D12 resolved).
- Pile-up per **pixel**: photons / (configured rep rate × dwell) — the
  *configured* rate, since with the filter on the measured sync ≈ photons.
  A pile-up map and its max per frame; thresholds 5 % (warn) / 10 % (red).
  In reverse mode pile-up favours the *later* photon (apparent τ longer), the
  opposite of forward; the checklist says which.
- **Peak-relative gates**: `GateSpec.reference ∈ {'peak', 'absolute'}`
  (default `'peak'`), so STED presets survive a `t0` change.

**Frames, markers, delays:**

- **One frame path.** `Flim` closes a frame after `n_pixels` pixel ends with
  or without `frame_begin_channel`; the worker always reads with
  `getReadyFrameEx()` and detects the final frame by `getFramesAcquired()`
  advancing after `sigScanDone` (with a timeout). The frame role, when
  configured, adds **resync** after a lost marker. There is no frame-end
  input on `Flim`; if the last frame does not close on the rig,
  `finish_after_outputframe=1` is the one-shot fallback. (The designer's
  `frame_end_clock` rises inside the last pixel and is useless here.)
- **Marker collision and skew.** The designer raises `frame_start_clock` on
  the same DAQ sample as line 0's edge; two DO lines, two cables and two
  comparators add ns-scale skew on top of the card's channel calibration.
  So the pixel pattern starts at `pixelPatternOffsetPs` (default 10 000 ps,
  ≪ any dwell) rather than at 0, and tutorial 08 measures the real
  frame→line skew with `Histogram(click=line, start=frame)` and asserts
  |skew| < offset. `pixel_end = begin + period − 1 ps` stays.
- **Line delay mechanism.** Positive `lineClockDelayPs = D` is applied as a
  pattern offset on the line role (keeps the raw line channel visible to
  tutorials) and as `setInputDelay(frame_clock, +D)` on the frame role (it
  has no pattern); negative values use `setInputDelay` on both. The lead of
  `pixel_begin[0]` over `frame_begin` is then the pattern offset, independent
  of D. First guess for D: `phase_delay × scan_time_step` (the APD already
  knows `phase_delay`); tutorial 09 refines it.
- **Linesteps.** Because the designer emits only Ny edges (gap 4), there is
  no pattern that can be driven for S > 1. Two options, decided in P3 (D14):
  (i) the detector *refuses* S > 1 with a clear error until M9 lands; or
  (ii) P3 includes the one-line TTL-clock fix (tile `period` Ny·S−1 times),
  after which the pattern stays Nx markers per edge, `Flim(n_pixels =
  Nx·Ny·S)`, the cube reshapes to `(Ny, S, Nx, bin)` and S is exposed the way
  `APDManager` exposes it. Both options keep the pattern simple.
- **Live preview cost.** A 512²×391 uint32 Flim buffer is 410 MB; `Flim`
  holds current, ready and summed copies, the worker's float32 cast is
  another, the cube product another — five copies, ~2 GB at 512², ~8 GB at
  1024². Therefore: live intensity from `getCurrentFrameIntensity()`;
  gate images, per-pixel fits and `LiveProducts` on each **ready frame**, or
  at an optional low-cadence `getCurrentFrame()` poll (one rule for both,
  default off); bin decimation (e.g. 64 ps → 196 bins) as a detector
  parameter; a row in `docs/design/plans/memory-budgets.md`. The 1 s
  `LIVE_PREVIEW_S` becomes a setup parameter.
- **`LiveProducts`** (intensity, decay, gate images, background, pile-up
  map max, metadata — never the cube; defined in `timeresolved/types.py`) is
  what the queued `sigTimeResolvedProducts` carries from the worker thread;
  the cube appears only in the final `TimeResolvedScanProducts`. Nothing
  deep-copies a cube on the GUI thread.
- **Accumulation** stays in software in v1 (hardware `n_frame_average` sums
  consecutive frames and needs a persistent `Flim`; follow-up §9).
- **Overflows**: `getOverflowsAndClear()` per frame → `metadata['overflows']`;
  the frame is flagged invalid. Without a frame role the pixel index is lost
  for the rest of the scan; with one, the next `frame_begin` resyncs.
- Cube kept in the vendor's integer dtype. The measured IRF histogram
  (tutorial 06) is stored in metadata and presets for the follow-up
  deconvolution.

### 3.3 `TimeTaggerFacade` — the scripting surface

Added to `MicroscopeFacade` as `facade.time_tagger`, built by
`build_facade_from_master` when `master.timeTaggerManager is not None`. Thin,
synchronous, cancel-aware: every blocking call polls the script's cancel
token and frees its measurement object in `finally` (ImScripting's
`OperationCancelled` is a `BaseException`). Returns NumPy / dataclasses with
a `summary()` string and `to_dict()`:

```python
tt = facade.time_tagger
tt.channels()                                    # {role: ChannelInfo(channel, edge, trigger_v, ...)}
tt.count_rates(roles, duration_s=1.0)            # {role: Hz}
tt.rep_rate('laser_sync', duration_s=2.0)        # RepRateResult — procedure below
tt.scope(roles, window_ns, trigger_role)         # ScopeTrace (both edges enabled)
tt.histogram(click_role, start_role, binwidth_ps, n_bins)   # decay / IRF / frame→line skew
tt.trigger_sweep(role, levels_v, duration_s)     # rate vs level, any role
tt.dark_rates(roles, duration_s)                 # laser blocked: dark + afterpulsing
tt.preflight()                                   # the shared checklist (timeresolved/preflight.py)
tt.health()
tt.set_trigger_level(role, v); tt.set_delay(role, ps)        # refused during a scan hold
```

`rep_rate()` procedure (works on every card model): temporarily disable the
conditional filter, `setEventDivider(laser_sync, N ≥ 16)` (≈ 5 M tags/s on a
TT20 at 80 MHz), measure with `Countrate` / `TimeDifferences` (divider
output is periodic; filter output is not), restore both in `finally`.
Refused during a scan hold. Caveat reported with the result: the divided
`TimeDifferences` spread is N-period jitter, card-limited on a TT20.

No `raw` handle in v1. Nothing is exported through `api.imcontrol` directly;
the facade gets a hand-written reference page with doctested examples, like
the existing cookbooks.

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
`getReadyFrameEx`, `getFramesAcquired`).

Behind it, a **vectorised signal model in virtual card time** — no tag
generation, no real-time thread; each measurement computes its result
analytically or by sampling when read:

- **Two photon regimes.** *Parked beam* (no scan running: tutorials 03, 05,
  06 and the Signals panel): a rate, a lifetime, an IRF width and `t0`, dark
  and afterpulsing rates. *Scanning*: a per-pixel Poisson cube drawn from
  `mockSample` (lifetime map ⊗ intensity map, e.g. two bead populations)
  convolved with the same IRF, plus background.
- **Line and frame edges from the designed TTL arrays** via
  `load_scan_edges(ttlDict, sampleRate)` (fed by the device manager, §3.1),
  not from `mockscan`, which carries only frame counts and a ≤ 5 s wall
  clock. The mock measures the *design* period, and says so.
- **Laser sync**: period `1/f_rep`, configurable jitter, analytic period
  histogram.
- **Electrical**: a sigmoid trigger-level response per role so sweeps show a
  plateau; edge-sign aware.
- **Bandwidth**: a tag-rate budget per mock model → overflows; filter-on
  behaviour (reversed direction; filtered sync rate ≈ photon rate).
- **Test signal**: correlated across channels, as on the card.
- **Faults** switchable from the setup block (`mockFaults` is a dict, e.g.
  `{"missing_line_clock": true, "line_delay_ps": 1200,
  "wrong_sync_polarity": true}`), so the debugging tutorials have something
  to find without reaching into Python.

Shipped mock setup: **`galvo_flim_mock_scan_setup.json`** = the existing
`galvo_apd_mock_scan_setup.json` (which already has `nidaq.simulation` true)
+ a `timeTagger` block (`simulation: true`) + a `FLIM` detector + the
`Lifetime` widget. All tutorials and the widget's UI tests run on it.

---

## 4. Calibration and debugging tutorials

Location: `imswitch/_data/user_defaults/scripts/tutorial/timetagger/`, a
fourth block next to `basic/` and `scanning/`, under the shipped-tutorial
contract enforced by `imscripting/_test/test_shipped_tutorials.py`: first
line `Tutorial timetagger NN -- <title>`, sections *You will learn / Setup /
Mock setup: / It simulates: / Your own microscope: / Next:*, a helper module
with one declared `Imported by:` importer, `tutorial/README.md` listing every
tutorial *and every setup file*, each script run headless in its own
subprocess (120 s timeout). Thirteen new scripts roughly double that test's
wall time, so each mock run is kept under ~10 s (09 runs two mock scans:
~20 s). The three existing `workflows/timeresolved/` scripts get the header,
take their output folder from the Recording folder instead of
`D:/Measurements`, and move in as steps 11–13. `tools/update_user_defaults_history.py`
runs in **P2, P3 and P5** (each adds shipped files); the changelog says that
unedited old copies go to the OS trash and edited ones stay at the old path.

Each tutorial follows one arc — *what the signal should look like → measure
it → compare with the design value → what to change if it differs → write
back if `APPLY = True`* (D3 proposed) — and saves its numbers as `.npz`/CSV
next to the script.

| Step | Script | Measures / teaches | Swabian API | Writes back |
|---|---|---|---|---|
| 01 | `01_meet_the_card.py` | Model, serial, resolution, channel count, HighRes group; role → channel map with edge sign; test signal on two channels + `Histogram` between them = a cable-free pipeline check. Mock vs. real. | `getModel`, `getConfiguration`, `setTestSignal`, `Counter`, `Histogram` | — |
| 02 | `02_trigger_levels_and_dead_time.py` | Trigger-level sweep on the **photon and sync** roles (DAQ roles have no edges outside a scan — they are swept in 07/08); plateau detection; polarity (negative channel numbers, the `-1` SPAD case); 50 Ω inputs and the "DAQ line into 50 Ω" trap; dead time against ringing. | `Countrate`, `setTriggerLevel`, `setDeadtime` | `photonsTriggerV`, `laserSyncTriggerV`, `photonsDeadtimePs` |
| 03 | `03_dark_counts_and_afterpulsing.py` | Laser blocked: dark rate per photon role; afterpulsing fraction from a self-histogram; stored as the `dark_rate` background the fitters and checklist use. | `Countrate`, `Histogram` | background rate in metadata / preset |
| 04 | `04_laser_sync.py` | Rep rate and period by the §3.3 procedure; jitter with the card-floor and N-period caveats; missing pulses; why the phasor fit needs the rate. | `setEventDivider`, `Countrate`, `TimeDifferences` | `laser_rep_rate_mhz` |
| 05 | `05_bandwidth_and_the_filter.py` | Tag budget per model; overflows with an unfiltered sync; enable the filter, watch overflows stop — and the decay reverse; what `tcspc_direction` means; the window-≥-period rule; why the divider is not a remedy. | `getOverflowsAndClear`, `setConditionalFilter` | `filterSyncByPhotons` |
| 06 | `06_irf_and_t0.py` | Zero the existing photon delay, histogram photons vs sync (direction-aware), IRF FWHM, peak position → `t0` (delay in forward mode, roll in reverse), derived peak target; what a truncated window looks like in each direction; IRF sample notes (emission filter out, SPAD colour shift, or a τ ≪ IRF dye). Saves the IRF. | `Histogram`, `setInputDelay` | `t0_ps`, `peakTargetNs`, `binwidth_ps`/`n_bins` suggestion, IRF |
| 07 | `07_line_clock_during_a_scan.py` | `runScanAndWait` in a thread while counting line edges, sweeping the line trigger level and measuring the period: edges = Ny (and *only* Ny when S > 1 — gap 4 made visible), period = line + flyback; jitter; design-vs-measured table. | `Counter`, `TimeDifferences`, `setTriggerLevel`, `loadScanParamsFromFile` | `lineClockTriggerV` |
| 08 | `08_frame_clock_and_pixel_markers.py` | Frame trigger sweep; `Histogram(click=line, start=frame)` → frame→line skew (design 0; assert |skew| < `pixelPatternOffsetPs`); build the detector's pixel pattern; check the last pixel end lands before the next line edge; `Scope` trace of frame/line/pixel-begin for one line; **last-frame-closes check** via `getFramesAcquired()`. | `Histogram`, `Scope`, `EventGenerator`, `Flim` | `frameClockTriggerV`, `pixelPatternOffsetPs` |
| 09 | `09_line_delay_alignment.py` | Run a scan; compare the TimeTagger intensity image with the known truth: on the mock, the sample map (with an injected `line_delay_ps` fault); on the rig, the APD image (the mock APD is spatially constant). Cross-correlate → pixels → ps → `lineClockDelayPs`; apply; re-run; shift → 0. Starts from `phase_delay × scan_time_step`. | pattern offset / `setInputDelay`, Flim intensity | `lineClockDelayPs` |
| 10 | `10_flim_preflight.py` | Runs `tt.preflight()`: rep rate matches config (by procedure), window spans a period (hard in reverse), no overflows in 2 s, pile-up max < 5 %, background fraction, line edges = Ny, frame leads pixel 0 by the offset, direction consistent with filter, last frame closes. The same checklist the Signals panel shows. | all | — |
| 11 | `11_binned_photon_arrivals.py` | (existing, re-headed) | `Flim` cube | — |
| 12 | `12_gated_sted.py` | (existing, re-headed; gates from a JSON preset, peak-relative) | — | — |
| 13 | `13_tau_sted.py` | (existing, re-headed) | — | — |

Docs: `docs/timetagger/index.rst` (the card, roles, cabling incl. the 50 Ω
note, bandwidth budget per model, filter and direction, conditioning,
metadata, the mock), `mode-flim.rst`, `mode-gated-sted.rst`,
`mode-tau-sted.rst` (each ending with "verify with tutorials …" and a JSON
snippet), and `debugging.rst` (symptom → tutorial: "image all zeros", "line
0 Hz", "image shifted by a line", "lifetimes all ≈ 0.9 ns", "decay looks
reversed", "frame never finishes", "overflows", "linesteps give a half-empty
image").

`scripts/diagnostics/measure_laser_rep_rate.py` stays for no-GUI use as a
wrapper around the same `rep_rate` procedure.

---

## 5. The Lifetime widget

Widget key **`Lifetime`**; files `view/widgets/LifetimeWidget.py`,
`controller/controllers/LifetimeController.py`
(`ImConWidgetController + StatefulComponentMixin`), dock name "Lifetime
(FLIM / STED)", right dock next to Scan. `FLIMHist` becomes an alias entry in
the `view/widgets/__init__.py` and `controller/controllers/__init__.py` maps
(plus the dock tables in `ImConMainView.py`) resolving to the Lifetime widget
on its histogram view, removed in the same release series (D4: no shipped
setup uses it).

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
│  └──┴────────┴─┴─────┴──┴──────────┴──▶ ns│  │ Gate layers in viewer ▣     │   │
│  bg: dark 2 % · peak 1.50 ns · 391×32 ps │  └─────────────────────────────┘   │
├──────────────────────────────────────────┴───────────────────────────────────┤
│ ● photons 1.2 Mcps  ● sync (filtered)  ● line —  ● frame —  pile-up max 3 %  overflows 0  [mock] │
│ [ Run once ]  [ Live ▶ ]  [ Stop ]   accumulate [ 4 ] scans   name [tau_sted_cells]  [ Save ] │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Decay plot** (always visible): aggregated decay of the last ready frame
  in forward time, log-y toggle, IRF-peak marker, background level, draggable
  `LinearRegionItem`s for gates (Gated STED mode), and — when a `sted_pulse`
  photodiode role exists — a dashed **STED-pulse marker** from
  `Histogram(sted_pulse, laser_sync)`: the one thing a gated-STED user tunes
  against, at the cost of a spare channel and no Pulse Streamer.
- **FLIM panel**: fit method, min counts, rep rate (with *measure* → the
  §3.3 procedure), window/bins/decimation, `t0` (with *find t0 from decay*),
  background mode, lifetime colour range, the two histogram views FLIMHist
  has today, and the display choice: lifetime image, intensity image, or an
  **intensity-weighted lifetime overlay** (HSV: hue = τ, value = intensity)
  as an RGB napari layer. Phasor readout: the per-frame (g, s) point on the
  universal semicircle (per-pixel cloud and selection are in the follow-up).
- **Gated STED panel**: gate table with `ref` column and colours; presets
  (JSON files, shared with tutorial 12); ratio image (late/early); *gate
  layers*: each gate image is a napari layer `FLIM › gate:late`, updated per
  ready frame from `LiveProducts`, scaled from the scan pixel size.
- **Tau STED panel**: the FLIM panel plus a τ-vs-intensity scatter, the
  pile-up map toggle, and the software accumulate count.
- **Signals panel**: live per-role rates, direction and filter state,
  pile-up and background, overflow counter, per-role trigger-level spin boxes
  (refused with a message during a scan hold), *trigger sweep*, *scope
  snapshot* (digital traces of line/frame/sync — never photons), and the
  `preflight()` checklist. All of these block for seconds and run on the
  controller's worker thread, like Run.
- **Status strip** and **footer** as drawn. Save uses the shared
  `timeresolved/io` savers (HDF5 + TIFFs) into the Recording widget's folder.

### 5.2 Controller wiring

- **Run path threading.** `ScanWorkflowFacade.run_once(wait=True)` refuses
  the GUI thread whenever a scan-done signal is configured (it is, through
  `WorkflowFacadeController`). The controller therefore runs
  `TimeResolvedScanWorkflow.run()` on a dedicated worker thread (one per
  Run/Live session) and marshals results back with
  `_invokeOnControllerThread`. Live mode re-arms from the worker. **Stop** =
  `sigAbortScan` plus an `Event` the worker polls between runs (`run_once` is
  blocking). The widget and the scripts thus share one code path and produce
  identical files.
- **Product configuration is run-scoped.** The controller configures
  products (gates from the table, fit, cube) only inside its own worker run,
  through the workflow, and the workflow clears in `finally` (P1a). It never
  configures on a mode or gate change, and it never clears on another
  source's scan start: `sigScanStarting` is payload-less and fires for every
  run including scripts', so a clear-on-start would wipe a running tutorial's
  configuration. Gate edits therefore "apply on next Run", and z/t scans
  from other sources are not rejected because `_tr_enabled` is false between
  runs.
- Listens to `sigTimeResolvedProducts(LiveProducts)` (queued, from the worker
  thread) for the live views; the final `TimeResolvedScanProducts` (with
  cube, if enabled) is fetched once on the worker thread after `wait_for_final`.
- **Layers.** `ImageWidget.addStaticLayer` creates a *new* layer on every
  call and `setImage` addresses live detector layers only; the
  `dynamic-layer-lifecycle` plan is not implemented. P4 therefore adds to
  `ImageWidget`: `upsertStaticLayer(name, im, scale)`, `removeStaticLayer(name)`
  and an RGB variant, used by a small layer helper the controller owns so
  layer names are stable and removed on mode change.
- The detector lease is the WORKFLOW lease the workflow's `acquisition_lease`
  already takes; the controller takes no second one. RECORDING and WORKFLOW
  leases coexist by design, so the widget does not refuse Run during a
  recording.
- State persisted (gates, presets, mode, display options, accumulate N) via
  `StatefulComponentMixin`. Device conditioning is setup-file state, never
  widget state (never restore hardware-active state).

### 5.3 Tests

- `_test/unit/test_lifetime_controller.py` (nohardware): fake `LiveProducts`
  → expected layer upserts; Run → configure inside the run and clear after;
  a concurrent script-style configure is not disturbed; worker-thread Run →
  marshalled completion; Stop aborts and joins.
- `_test/ui/test_lifetime_widget.py` (ui): gate region drag ↔ table; preset
  load; log toggle; peak-relative → absolute conversion on `t0` change.
- Shipped-setup smoke (`_test/unit/test_lifetime_mock_setup_smoke.py`): the
  widget constructs on `galvo_flim_mock_scan_setup.json` and one mock scan
  produces non-empty gate layers.

---

## 6. Data and saving

- **`LiveProducts`** (new, small, in `types.py`) vs **`TimeResolvedScanProducts`
  v2** (final; adds `background_rate`, `pileup_max`, `tcspc_direction`,
  `frames_accumulated`, `overflows`, `irf` (optional), `time_tagger`
  metadata incl. model, serial, roles and conditioning, `format_version = 2`;
  cube in integer dtype). `max_retained_products` is removed.
- **`GateSpec.reference`** (`'peak'` default) and `processing.resolve_gates(
  gates, peak_time_ns)`; saved gate attributes store both the relative and the
  resolved absolute bounds.
- **`timeresolved/preflight.py`**: the checklist as data (`PreflightItem(
  name, ok, measured, expected, hint)`), used by tutorial 10, the facade and
  the Signals panel.
- **`timeresolved/io.py`**: the HDF5/NPZ/TIFF writers move out of
  `workflows/time_resolved.py` so the widget and the workflows share them; the
  HDF5 layout gains `time_tagger/`, `background/` and `irf/` groups and a
  `format_version` attribute; a reader `load_products(path)` comes with it.
- **Recording**: unchanged in this plan (follow-up §9).
- Changelog entries go into each phase's PR under "Unreleased".

---

## 7. Scan integration (red zone — hardware verification before enabling)

| Change | Why | Gate |
|---|---|---|
| `getReadyFrameEx` always; frame role (optional) for resync; pattern offset `pixelPatternOffsetPs`; shared DAQ-role delay | Card-closed frames; avoids the frame/pixel-0 collision and real cable skew | Tutorial 08 skew check; last-frame-closes check; `finish_after_outputframe=1` fallback |
| Linesteps: refuse S > 1, or fix the TTL clock count (D14) | Designer emits only Ny edges | If (ii): unit test on edge count in the designer, tutorial 07 on the rig |
| `lineClockDelayPs` (positive → line pattern offset + frame `setInputDelay`; negative → `setInputDelay` on both) | Card-side half of the M9 detector-sync fix, measured by tutorial 09 | Default 0 → no behaviour change; roadmap M9 note |
| Conditional filter ⇒ reverse direction, slot swap, mirrored axis, window ≥ T_rep hard refusal, `t0` as a roll | Bandwidth on TT20/Ultra; correctness | Default off (today's behaviour); tutorial 05 proves the need per rig |
| Background (`dark_rate`), per-pixel pile-up, IRF-derived peak target | Measurement correctness (§1.9, §1.10) | A rig with no tutorial-03/06 data sees warnings, not changed numbers |
| Overflow → frame flagged invalid; resync on next frame edge | Silent corruption today | Metadata only (D7 default) |
| Live intensity from `getCurrentFrameIntensity`; products per ready frame or low-cadence poll | Memory and CPU (§3.2) | Memory-budget row; the old cadence remains selectable |

---

## 8. Phases

Each phase lands green on CI without hardware and names the rig check the
next phase relies on. Changelog in every PR.

**P0 — Review and rig facts.** Done for two rounds; §13 still to be answered.

**P1a — Pure fixes and moves (half-day PR).** Template `default` entries
removed (gap 6); `finally: clear()` in the workflow (gap 2); FLIMHist
docstring (gap 11); dead fitters removed and the live fitters moved to
`timeresolved/fitting.py` unchanged (gap 7, pure move); docs path in the old
plan (gap 12). Can start now.

**P1b — Device layer, block, compatibility, trivial mock.**
`TimeTaggerInfo` (modelled on `TeensyPulseInfo`, with its own test like
`test_pulse_generator_integration.py`); `builtin_templates/sections/timeTagger.json`;
`TimeTaggerManager` with roles, conditioning, scan hold, `sigScanBuilt`
subscription, `finalize`; `MasterController` construction, `lowLevelManagers`,
`closeEvent` ordering and `unsafeWhileActive`, with the shutdown test
extended (`test_master_controller_shutdown.py` gets the `timeTagger`
analogue); detector takes the manager (role parameters; compatibility path;
hold calls), **schemas regenerated** with `tools/extract_manager_schemas.py
--write` (+ fixtures) so `test_configeditor_schemas_match_source.py` passes,
and the detector template's fields updated; a counting mock (rates and test
signal only) so the manager is unit-tested. Health sampler deferred to P1c.
*Rig check*: the existing FLIM config scans unchanged; the new block connects.

**P1c — Signal-model mock, shipped setup, worker end-to-end, live products.**
Parked-beam and scanning photon regimes, edges from the TTL arrays, trigger
response, tag budget, filter behaviour, faults; `galvo_flim_mock_scan_setup.json`;
the health sampler; `LiveProducts` + queued `sigTimeResolvedProducts`;
direction-aware axis, background, pile-up, peak target and gate reference in
`timeresolved/` (pure functions with tests); the worker runs end-to-end in CI
for the first time and recovers the mock sample's lifetimes within tolerance
for each fit method, forward and reverse, with and without background.
*Rig check*: none (software only).

**P2 — Facade, preflight, device-level tutorials 01–06 and 10.**
`TimeTaggerFacade` with the `rep_rate` procedure and hold refusal;
`preflight.py`; `timetagger_helpers.py`; `docs/timetagger/index.rst`,
`debugging.rst`; README rows; `update_user_defaults_history`; CLI wrapper.
*Rig check*: 02–06 on the real card; model-specific numbers for §1's table.

**P3 — Scan-aware diagnostics and scan correctness.** Tutorials 07–09;
`getReadyFrameEx` path with final-frame detection; frame role with the
pattern offset; D14 (refuse or fix linesteps); delay mechanism; overflow
flagging; preview from intensity; `update_user_defaults_history`. Roadmap M9
updated with the card-side half (and, if D14 = fix, the clock count).
*Rig check*: 07 edge count/period vs design; 08 skew < offset and last frame
closes; 09 shift → 0 after delay.

**P4 — Lifetime widget v1 (depends on P1 and P2).** FLIM and Tau STED
panels, Signals panel (facade-backed, worker-threaded), status strip, footer
with the worker-thread Run path and Stop, `ImageWidget` upsert/remove/RGB
layer API, intensity-weighted overlay, (g, s) readout, FLIMHist alias.
`docs/gui.rst` section replaces the FLIMHist one. Live views need P1c's
`LiveProducts`; the frame role (P3) is not required.
*Rig check*: live τ overlay on a reference dye; preflight green.

**P5 — Gated STED panel, shared io, products v2.** Gate table with
`reference`, regions, presets, gate layers, ratio image, STED-pulse marker
(if cabled), `timeresolved/io.py` with reader, products v2; tutorials 11–13
re-headed onto the mock setup, Recording-folder output, under test;
`update_user_defaults_history`.
*Rig check*: the widget's gated image equals tutorial 12's on the same scan
(identical file contents).

**P6 — Validation campaign and release.** Roadmap 13.A FLIM row closed with
the per-fit-method convergence test on a reference dye (tutorial 10's
extended mode); FLIMHist alias removed; old plan marked superseded; the
follow-up plan (§9) opened with the rig numbers measured here.

Rough size: P1b and P4 are the big ones; P1c is medium-large; P2/P3 are
many small files; P5 medium. Everything before P4 is invisible to a user who
never opens the Scripting tab, except P1a's fixes.

---

## 9. Follow-up plan (recorded here, not planned here)

Moved out after review so this plan stays deliverable; each item depends on
rig numbers from P2/P3:

- **Two-colour FLIM**: two photon roles → two `Flim` objects sharing one
  `EventGenerator` pair, started under `SynchronizedMeasurements`.
- **Intensity-only counter detector** (`CountBetweenMarkers`, fixed
  `n_values`, re-armed per frame) — only if a sync-free rig exists.
- **Outer axes** (z/t cubes): after M9's linestep fix; reading each ready
  frame promptly vs `finish_after_outputframe = N_z` (N_z more buffers).
- **Hardware-side accumulation**: persistent `Flim` + `getSummedFrames()`.
- **Raw tags and replay**: `FileWriter` (records post-filter tags) and
  `createTimeTaggerVirtual` (`replay`, `setReplaySpeed(-1)`,
  `waitForCompletion`; filter emulation to be verified) — one recorded rig
  session driving the test suite with real tags.
- **Recording hook**: a *recorded product* parameter (lifetime / intensity /
  gate) and `<rec>_timeresolved.h5` attached via a `RecordingManager`
  post-scan hook, once that hook exists (only `sigRecordingEnded` exists).
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
| New model | `model/managers/TimeTaggerManager.py`, `model/interfaces/timetagger_mock.py`, `model/timeresolved/fitting.py`, `model/timeresolved/preflight.py`, `model/timeresolved/io.py` |
| Changed model | `SwabianTimeTaggerManager.py` (slimmed, hold calls, role params), `model/SetupInfo.py` (`TimeTaggerInfo`), `MasterController.py` (construct, `lowLevelManagers`, `closeEvent` order + `unsafeWhileActive`), `workflows/facade.py` (`time_tagger`), `workflows/time_resolved.py` (clear, io import), `model/timeresolved/{types,processing}.py` (v2, `LiveProducts`, direction, gate reference, background, pile-up) |
| Config | `view/configeditor/builtin_templates/sections/timeTagger.json` (flat), `view/configeditor/builtin_templates/detectors/SwabianTimeTaggerManager.json` (fields + `default` removal), `model/configeditor/schemas/managers/SwabianTimeTaggerManager.json` + `schemas/fixtures/` (regenerated), `docs/setupinfo-reference.rst` |
| GUI | `view/widgets/LifetimeWidget.py`, `controller/controllers/LifetimeController.py`, both `__init__.py` maps (+ `FLIMHist` alias), `ImConMainView.py` dock tables, `ViewSetupInfo.py` docstring, `view/widgets/ImageWidget.py` (upsert/remove/RGB static layers) |
| Scripts | `scripts/tutorial/timetagger/01..13_*.py`, `timetagger_helpers.py`, `gate_presets/*.json`, `tutorial/README.md` (tutorials + setup files), `scripts/diagnostics/measure_laser_rep_rate.py` (wrapper), `_data/user_defaults_history.json` (regenerated in P2, P3, P5) |
| Setups | `imcontrol_setups/galvo_flim_mock_scan_setup.json` |
| Docs | `docs/timetagger/*.rst`, `docs/gui.rst`, `docs/devices/detectors.rst`, `docs/scripting.rst` (tutorial block), `docs/mock-infrastructure.rst`, `docs/design/plans/memory-budgets.md` (row), `docs/changelog.rst` (per PR), `ROADMAP.md` (new milestone + M9 note), `time-resolved-detector-workflows.md` (path fix, later superseded), this plan |
| Tests | `test_timetagger_info.py`, `test_timetagger_manager.py` (hold, finalize joins the sampler, ordering), `test_timetagger_mock.py`, `test_swabian_flim_worker_mock.py` (forward/reverse, background, pile-up), `test_timeresolved_direction_and_gates.py`, `test_timeresolved_preflight.py`, `test_timetagger_facade.py`, `test_lifetime_controller.py`, `test_lifetime_mock_setup_smoke.py`, `ui/test_lifetime_widget.py`; designer edge-count test if D14 = fix; `test_master_controller_shutdown.py` and `test_configeditor_schemas_match_source.py` extended; shipped-tutorial test picks up the new block automatically; `test_user_defaults_sync.py` |

---

## 11. Risks

- **Card model drives everything electrical and bandwidth-related.** The
  plan defaults to today's behaviour (no filter, forward direction) and lets
  tutorials 02 and 05 prove the need per rig. §13 asks for the model.
- **Frame semantics on the real card** (last-frame close, mid-pattern
  triggers, resync). One code path, config-gated role, two rig checks.
- **Linesteps** are a designer defect this plan can only refuse or fix at the
  clock level (D14); the galvo waveform stays M9's.
- **Memory.** Cube products at 1024² are multi-GB; the preview path avoids
  the cube and decimation is offered. A hard per-scan budget row is added.
- **Mock fidelity.** A too-kind mock hides rig problems; two photon regimes,
  faults and the measured-vs-design distinction keep it honest; raw-tag
  replay is the follow-up's stronger answer.
- **Threading.** Worker-thread Run and Signals actions, queued signals,
  scan hold vs nidaq lock ordering, sampler shutdown: each has a unit test in
  its phase.
- **Scope creep** in the widget: phasor selection, ROI decay, recording hook
  are out of this plan, deliberately.

---

## 12. Decisions still open

Resolved by review (recorded): D5 raw handle → **no** in v1; D8
`max_retained_products` → **drop**; D9 hardware accumulation → **later**;
D10 phase order → correctness first, widget after P1 + P2; D11 direction
default → **forward, filter off**; D12 peak target → **derived from the IRF
FWHM, fallback 1.5 ns**.

- **D1 — Device block shape.** Flat prefixed role fields (proposed, fits the
  config editor today) vs a nested `channels` map plus a one-level extension
  of the section editor in P1b.
- **D2 — Tutorial home.** `scripts/tutorial/timetagger/` under the shipped-
  tutorial test (proposed) vs `scripts/workflows/`.
- **D3 — Write-back policy.** Print suggestions, apply only with
  `APPLY = True` (proposed) / always / never.
- **D4 — FLIMHist fate.** Alias, removed in the same release series
  (proposed).
- **D6 — Widget name.** `Lifetime` (proposed) vs `FLIM` vs `TCSPC`.
- **D7 — Overflow handling.** Flag in metadata only (proposed) vs also
  withhold the frame from a recording.
- **D13 — STED-pulse photodiode role.** Include the optional role and decay
  marker in P5 (proposed, cheap) or leave it to the follow-up.
- **D14 — Linesteps in P3.** Refuse S > 1 until M9 (safest), or include the
  one-line TTL-clock count fix here (proposed: fix, since it is M9's own
  listed sub-item and touches no analog waveform; needs a designer unit test
  and a rig check with tutorial 07).

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
7. Typical scan: Nx·Ny, dwell, laser rep rate, expected photons per pixel;
   whether linesteps (S > 1) are used with FLIM today.
8. Where measurements should land (today's scripts hard-code
   `D:/Measurements`; the plan uses the Recording widget's folder).
9. Is a fast photodiode on the STED beam available for the `sted_pulse` role?
10. Is the Pulse Streamer used on the rig at all? (The manager exists but is
    not wired in.)

---

## 14. Relationship to existing plans

- Supersedes `docs/design/plans/time-resolved-detector-workflows.md` once P1b
  lands; its contract and workflows are kept.
- Complements ROADMAP **M9** (detector-sync): M9 owns `phase_delay` into
  `line_clock` and the galvo trajectory; this plan measures the offset with
  the card, applies a card-side delay, and (if D14 = fix) takes M9's
  linestep clock-count sub-item.
- Uses the lease model from `detector-acquisition-selection.md` as it stands
  (`LeasePurpose.WORKFLOW`; RECORDING and WORKFLOW coexist).
- Adds the `ImageWidget` static-layer API that `dynamic-layer-lifecycle.md`
  (not implemented) would also need.
- Adds a row to `memory-budgets.md`.

---

## 15. Review log

### Round 2 → revision 3

*Measurement and API*
- **Linesteps re-modelled**: the designer emits Ny edges on the first Ny
  periods and none after (verified in `__generate_all_clocks`), so rev. 2's
  "pattern spanning S periods" could not be driven. Now: refuse S > 1 or fix
  the TTL clock count (D14); the pattern stays Nx per edge.
- **Reverse-mode `t0`**: a photon-channel delay drops early photons in
  reverse mode; `t0` is a software circular roll there, the sync delay is
  never touched, Flim slots swap with direction, the axis mirror is a
  transform with the sub-bin offset, and window ≥ T_rep is a hard refusal.
- **Frame path**: one path (`getReadyFrameEx` + `getFramesAcquired`), frame
  role only for resync; `frame_end_clock` fallback dropped (no such Flim
  input; it rises inside the last pixel); `finish_after_outputframe=1` as
  the one-shot fallback.
- **Pattern offset** is a parameter (default 10 000 ps) because real
  frame/line skew is ns-scale; tutorial 08 measures and asserts it.
- **Background** defaults to dark + afterpulsing rate × dwell (tutorial 03);
  the pre-pulse window is a diagnostic (the previous period's tail
  contaminates it at 80 MHz); peak target derived from the IRF FWHM.
- **Delay mechanism** completed for the frame role (`setInputDelay`), first
  guess `phase_delay × scan_time_step`; pile-up per pixel against the
  configured rep rate, with the reverse-mode bias direction noted.
- Tutorial 02 sweeps photons and sync only; DAQ roles are swept during the
  scans of 07/08; parked-beam photon regime added to the mock; last-frame
  check added to 08/10; example photon channel `-1`; IRF saved; tutorials
  11–13 use the Recording folder; ~20 s allowed for 09.

*Architecture*
- **Foreign-scan clear removed** — `sigScanStarting` is payload-less and
  fires for every run; a clear-on-start would wipe a running script's
  configuration. Products are configured only inside the widget's own run
  and cleared in `finally`.
- **P4 dependencies** corrected to P1 + P2; `LiveProducts` and the queued
  signal moved into P1c.
- **Scan hold** mechanism specified (`beginScanHold`/`endScanHold` from the
  detector) replacing an unimplementable "while a detector holds a SCAN
  lease"; nidaq lock-ordering rule added.
- **Mock edge feed** specified end to end: the manager takes `setupInfo` and
  `nidaqManager`, subscribes to `sigScanBuilt(scanInfoDict, signalDict,
  deviceList)`, and passes the TTL dict plus `scan.sampleRate` to the mock.
- **Shutdown**: `timeTaggerManager` after `detectorsManager` in
  `manager_attrs`, in `unsafeWhileActive`, sampler stopped in `finalize`,
  tested.
- **Generated artefacts** added: schema regeneration + fixtures, detector
  template fields, shutdown and schema tests, `TimeTaggerInfo` test,
  `user_defaults_history` in P2/P3/P5, README lists setup files, smoke test
  and designer edge-count test in §10.
- Corrections: only the old plan has the wrong scripts path; no `kinds/`
  entry for a system section; FLIMHist alias lives in the module maps; JSON
  `null` for nullable channels; `useMockOnFailure` default false is a stated
  departure; `run_once` refusal condition stated; lock/`BaseException`
  statement moved to where it matters (facade measurement cleanup); trash
  behaviour both halves; role vocabulary table; lease via the workflow only;
  fitter move pulled into P1a; health sampler deferred to P1c and off in
  tests; Stop semantics; `mockFaults` as a dict; loosened field lists and
  helper names.

### Round 1 → revision 2

*Measurement and API*
- Conditional filter reverses TCSPC direction → `tcspc_direction` derived
  from the filter, axis mirrored in processing; `setEventDivider` removed as
  a FLIM remedy and kept only inside the rep-rate procedure; sync rate labelled
  "(filtered)".
- Frame-start clock coincides with line 0's edge → pattern offset, one shared
  delay for DAQ roles. `getReadyFrameEx()` named correctly; `n_frame_average`
  dropped (averages z-planes, needs a persistent `Flim`).
- Background model, pile-up guard, peak target instead of bin 0,
  peak-relative gates, dark-count tutorial added.
- Memory: preview from `getCurrentFrameIntensity`, fits on the ready frame,
  decimation, budget row; `LiveProducts` without the cube.
- Electrical: 50 Ω inputs, DAQ line amplitude, edge sign via negative channel
  numbers, dead time, trigger sweeps per role; jitter floor caveat; `Scope`
  both edges; `Histogram` instead of `Correlation` for frame→line.
- Delay sign (positive for the TimeTagger) and mechanism; optional STED-pulse
  photodiode role and decay marker.

*Architecture*
- Mock derives edges from the designed TTL arrays, not from `mockscan`;
  tutorial 09 compares against mock truth, not the spatially constant mock
  APD.
- Widget Run path moved to a worker thread (`run_once(wait=True)` refuses
  the GUI thread); products never deep-copied on the GUI thread.
- Block flattened to fit the section editor; `TimeTaggerInfo` dataclass;
  `simulation` / `useMockOnFailure` naming; mock fallback only with
  `nidaq.simulation`; `finalize` wired into `closeEvent`; manager stored as
  `None`-when-absent; facade built from `master.timeTaggerManager`; no
  Settings-tree presence claimed.
- Configuration lock instead of a measurement lock; RECORDING and WORKFLOW
  coexist; `ImageWidget` upsert/remove/RGB API specified; P1 split into
  P1a/b/c; P6 moved to a follow-up plan; factual corrections (workflow clears
  before, not after; FLIMHist reads private attributes only in decay mode;
  Pulse Streamer exists but is not wired; template `default` entries deleted;
  shipped-tutorial contract; `update_user_defaults_history`; changelog per
  PR).
