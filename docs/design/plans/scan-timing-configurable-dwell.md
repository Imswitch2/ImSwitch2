# Configurable scan timing: findings and plan (draft 5, 2026-10-16: S0-S6 built; open items in §9)

Lenny's observation: scans at 10 µs dwell work, 15 µs produces broken
images, `scan.sampleRate` cannot be changed, and a PCIe-6363 could play a
1 µs pixel on a continuous galvo scan. This is the analysis and the plan;
nothing is implemented. Findings are verified in code and reproduced by
`imswitch/imcontrol/_test/unit/test_scan_dwell_timing_grids.py` (the failing
cases are `xfail(strict)`, so they flip when the plan is built). Everything
cited is on branch `feat/scan-engines` at e74f4a5ed; line numbers are from
that commit.

## 1. How timing flows today

One number enters, the dwell; four grids are derived from it, in four
places, with three different roundings.

| Stage | Where | What it does with the dwell |
|---|---|---|
| Panel | `view/widgets/ScanWidgetAdvanced.py:43` | A free-text field in ms (default 0.02 = 20 µs); no grid, no validation |
| Controller | `ScanControllerAdvanced.py:742-750` | `samples_per_pixel = round(dwell · sampleRate)` for its own purposes (one more rounding) |
| Galvo designer | `signaldesigners/GalvoScanDesigner.py:226` | `timestep = 1e6 / scan.sampleRate` µs: the waveform grid, 10 µs |
| | `:324` | `samples_per_pixel = round(dwell / timestep)` |
| | `:325` | `samples_per_line = round(N_x · dwell / timestep)` -- the **line** is rounded as a whole, so the sweep lasts the requested dwell per pixel even when the pixel does not |
| | `:467-491` | the sweep period is evaluated on the timestep grid (`n_eval = int(T / timestep)`); the flyback is whatever the spline needs |
| | `:160-181` | the velocity check compares `step / requested dwell` with `vel_max` |
| | `:409-424` | the contract: `samples_per_pixel`, `dwell_time` (the **request**), `scan_time_step = 1/sampleRate`, `scan_samples = [per pixel, per line, per frame, …]`, `scan_samples_d2_period` |
| TTL designer | `AdvancedScanTTLCycleDesigner.py:87, 136, 403-404` | its own `samples_per_pixel = round(dwell · Fs)`; a line is `N_x · samples_per_pixel`; pulse edges `round(t · Fs)` |
| | `:170-172, 245-261` | a period is that line plus a zero pad of `d2_period − scan_samples[1]` -- the pad is taken from the **galvo's** line, the line from the **pixel** rounding |
| NI-DAQ | `managers/NidaqManager.py:76-80` | `SCAN_CLOCK_TERMINAL = '100kHzTimebase'`, `SCAN_CLOCK_RATE_HZ = 100 000`, `TIMER_COUNTER_RATE_HZ = 1 000 000` |
| | `:1430-1434` | the AO task is clocked from the 100 kHz timebase at 100 000 Hz |
| | `:1447-1452` | the DO task is clocked from `ao/SampleClock` (from the 100 kHz timebase when there is no AO) |
| | `:1399-1406` | the timer counter makes a 1 MHz pulse train of `len(AO) · 10` pulses, started by `ao/StartTrigger` |
| | `:580-591` | `_checkScanClockRate`: any `scan.sampleRate` but 100 000 is refused on hardware, because the waveforms would be built at one rate and played at the other |
| APD | `detectors/APDManager.py:1150-1162` | `frac_det_dwell = round(dwell · 1 MHz)` (the **request** again), `frac_scan_det_rate = round(1 MHz · scan_time_step)` = 10 |
| | `:1217-1230` | its sample plan = the contract × 10: line read, line period, total, padding, settling |
| | `:1250-1258` | the counter task samples on the timer terminal at 1 MHz, armed on `ao/StartTrigger` |
| | `:1531-1539, 1385-1395` | one read per line (or per line period), then `samples_to_pixels` sums `frac_det_dwell` samples per pixel and **drops** a partial pixel silently |
| | `:1235-1238, 1476` | `phase_delay` (µs) → timer samples, thrown away at the start |
| PMT | `detectors/PMTManager.py:1050-1107` | the same arithmetic on an AI task at the timer rate |
| Metadata | `basecontrollers.py:1979-1983` | `ScanStage:*` attributes are the **request**; `RecordingManager` reads `dwell_time` from the contract |
| Expert | `managers/scan_engines/NidaqScanEngine.py:132, 145` | `rate_hz = SCAN_CLOCK_RATE_HZ`, `timing_grid_s = 10 µs`; `strict-v1` refuses an off-grid time with its neighbours; the same `runScan`, the same detectors |

Pixel boundaries are therefore defined four times: by the galvo's line
rounding, by the TTL designer's pixel rounding, by the APD's bin at the
timer rate, and by the contract's `samples_per_pixel`. They agree only when
the dwell is a whole number of 10 µs samples.

## 2. Findings

### F1 -- 15 µs: the pixel and the line are rounded separately, and the TTL follows the pixel

Reproduced (`test_the_off_grid_mismatch_in_numbers`, 20 pixels of 1 µm,
100 kHz):

| Dwell | `samples_per_pixel` (contract) | galvo line (`scan_samples[1]`) | TTL gate per line | TTL period vs galvo period |
|---|---|---|---|---|
| 10 µs | 1 | 20 | 20 | equal |
| 15 µs | 1 (= 10 µs) | 30 (= 15 µs × 20) | 20 | **10 samples short** |
| 20 µs | 2 | 40 | 40 | equal |
| 25 µs | 2 (= 20 µs) | 50 | 40 | 10 samples short |
| 12 µs | 1 | 24 | 20 | 4 samples short |

At 15 µs the galvo sweeps 15 µs per pixel (the line is 30 samples), but
the laser gate and every other TTL line are built from the rounded pixel (1
sample) and cover 20 of the 30 samples of each line; because the flyback
pad is taken from the galvo's period, the TTL period is 10 samples shorter
than the galvo's and the gate drifts a third of a line earlier on every
line. A gated laser illuminates a shrinking, shifting part of each line;
with `ttlMultiplying` the APD mask does the same to the counts. That is the
broken image. The APD's own pixel count is right (`300 / 15 = 20`), which
is why the frame has the right shape and wrong content.

Every dwell that is not a multiple of 10 µs has this defect; 10 and 20 µs
are the only ones in daily use that do not.

### F2 -- an off-grid dwell is quantized silently, twice, and recorded as requested

The contract says `dwell_time = 15 µs` and `samples_per_pixel = 1` (10 µs)
in the same dict (`test_an_off_grid_dwell_is_refused_or_realized_as_recorded`).
The recording carries the request. The velocity check uses the request
(`GalvoScanDesigner.py:160-181`), so a scan can be accepted at a speed the
realized dwell would refuse. `samples_to_pixels` drops a partial pixel
without a word. None of this is refused and none of it is reported.

### F3 -- 1 µs: impossible by construction on the 100 kHz grid

A 1 µs pixel is a tenth of a sample. `samples_per_pixel` rounds to 0 and
the contract refuses ("scan_samples values must be positive",
`test_a_dwell_below_the_sample_step_is_refused_not_played`) -- a refusal,
not silence, but with a message about the contract rather than the clock,
and only after the velocity check has had its say. What pins the grid:

- `_checkScanClockRate` refuses any other `scan.sampleRate` on hardware
  (`NidaqManager.py:580-591`), and `SCAN_CLOCK_TERMINAL` names the 100 kHz
  timebase as the AO clock source (`:76, :1430`);
- the designers evaluate every curve on `1/sampleRate` and the TTL
  designer places edges on it;
- the detectors need `frac_scan_det_rate` to be a whole number (the timer
  rate over the scan rate) and `frac_det_dwell ≥ 1` (the dwell over the
  timer period);
- the Expert engine declares `timing_grid_s = 10 µs` and `strict-v1`
  refuses everything off it.

The electronics are not the limit (§3). The pinning is in software, and
it is honest: it was put there so that waveforms built at one rate could
never be played at another.

### F4 -- the physical limit is separate and already modelled

A continuous galvo scan does not settle per pixel. The only physical rule
is the sweep speed, `step / dwell ≤ vel_max`, and the turnaround that the
spline builds within `acc_max` (`GalvoScanDesigner.py:143-181, 467`). At
1 µs and `vel_max = 0.1 µm/µs` (example_sted) the pixel is at most 0.1 µm;
the rule stays as it is and must be evaluated on the **realized** dwell. A
stepped fast axis (Beta, `smoothScan: false`) needs `move + settle` per
pixel and is refused below that already; 1 µs does not apply to it.

## 3. What the NI hardware allows (verified)

From the NI *PCIe-6363 Specifications* (377777A-01, 2018-10-23) and the
*X Series User Manual* (AO Sample Clock Timebase Signal section):

| Subsystem | Specification | Consequence for a 1 MHz pixel clock |
|---|---|---|
| Analog output | 4 channels; maximum simultaneous update rate 2.86 MS/s (1 ch), 2.00 (2 ch), 1.54 (3 ch), 1.25 (4 ch); timing resolution 10 ns; accuracy 50 ppm; FIFO 8191 samples shared; waveform modes include regeneration from the host buffer | 1 MS/s per channel is within the limit for up to four channels; the FIFO holds 8 ms of two channels at 1 MS/s, so the host streams (DMA) |
| AO sample clock timebase | Sources: **100 MHz Timebase (default)**, 20 MHz Timebase, 100 kHz Timebase, PXI_CLK10, PFI, RTSI, …; "divided down to provide a source for AO Sample Clock" | 1 MHz = 100 MHz / 100 exactly; 100 kHz = /1000; any rate `100 MHz / n` is exact, others are coerced by the driver -- the software must ask the driver for the rate it got |
| Digital output, port 0 | hardware-timed waveform generation 0–10 MHz regenerating from the 2047-sample FIFO, 0–10 MHz streaming "system and bus activity dependent"; DO sample clock and sample clock timebase can be external (`ao/SampleClock` is used today) | 1 MHz on `ao/SampleClock` is within the limit; streaming at 4 MB/s |
| Counters | 4 × 32-bit; internal base clocks **100 MHz, 20 MHz, 100 kHz**; 50 ppm; external source 0–25 MHz; DMA per counter | the 1 MHz timer and any integer multiple of the pixel clock up to 25 MHz source are available; the APD counter samples on the timer at up to the base clock |
| Analog input (PMT) | 2.00 MS/s single channel, **1.00 MS/s aggregate** multi-channel; 10 ns resolution | two PMTs at 1 MHz each exceed the aggregate limit: a constraint the rate validator must enforce |
| Triggers | AO: start, pause, sample clock, sample clock timebase from any PFI/RTSI; DO likewise; counters: gate, source, arm | the present start-trigger wiring (`ao/StartTrigger` arms the counters and starts the DO) carries over unchanged |

The DAQmx device properties `ao_max_rate`, `ao_max_multi_chan_rate` (per
channel count), `do_max_rate`, `ai_max_single_chan_rate`,
`ai_max_multi_chan_rate`, `ci_max_timebase` and `co_max_timebase` expose
these numbers at run time, and `Task.timing.samp_clk_rate` returns the rate
the driver actually configured. Those are the sources the validator reads;
nothing is restated in code (no-magic-numbers).

Sources: NI PCIe-6363 Specifications, sections *Analog Output*, *Digital
I/O/PFI -- Waveform Characteristics (Port 0 Only)*, *General-Purpose
Counters*, *Analog Input*, *External Digital Triggers*; NI X Series User
Manual, *AO Sample Clock Timebase Signal*; NI-DAQmx Key Concepts, *Clocks*
(the 100 kHz timebase is the 20 MHz timebase divided by 200).

## 4. Two ways to a 1 µs pixel, and the choice

**A. Raise the waveform rate.** `scan.sampleRate` becomes real: the AO and
DO sample clock is derived from the 100 MHz timebase at the configured
rate; the timer runs at an integer multiple of it; every grid in §1 is the
same grid. Costs: ten times the samples at 1 MHz (§7, modest); the galvo
waveform carries more samples than its bandwidth needs (harmless).
Complexity: one clock, every consumer already parametrized by
`sampleRate`; the change is to stop pinning it and to validate it.

**B. Decouple pixel timing from the waveform.** Keep the AO at a rate the
scanner's bandwidth warrants (100 kHz) and define pixels on the detector's
timer (1 MHz); clock the DO on its own sample clock at the pixel rate.
Costs: two sample clocks in one scan (AO and DO, both divided from the
100 MHz timebase, started by one trigger -- deterministic, but the phase
between them is a new thing to measure); every line and frame boundary
must fall on both grids (a least-common-multiple rule for line lengths
and flybacks); the TTL designer, the detectors and the metadata move to
the pixel grid while the galvo designer stays on the waveform grid, so
the four-grids problem of §1 becomes a two-grids problem by design.

**Recommendation: A, with the single-source rule of §5.** Correctness:
one grid is the only arrangement in which the F1 class of defect cannot
recur. Complexity: A changes the clock configuration and a validator; B
changes every consumer's notion of time. Performance: at 1 MHz the sample
counts (§7) are far below the bus and the driver's limits; the one real
cost, per-line detector reads, is the same under both. Compatibility: at
`sampleRate = 100000` A is today's behaviour sample for sample (the goldens
pin it), so a 100 kHz setup changes nothing by upgrading. B keeps the AO
waveform small, which nothing needs.

The lesser part of B worth keeping: the detector timer rate becomes a
declared setup value (`detectionRateHz`, default 1 MHz) that must be a
whole multiple of the pixel clock, so an APD can keep binning ten samples
per pixel at 100 kHz and one per pixel at 1 MHz.

## 5. Timing rules (the design)

1. **One grid.** `scan.sampleRate` (Hz) is the pixel clock, the AO and DO
   sample clock, and the Expert engine's `timing_grid_s`. The timer
   (detector) rate is `detectionRateHz`, a whole multiple `k ≥ 1` of it.
2. **The dwell is a whole number of samples.** `samples_per_pixel =
   round(dwell · sampleRate)`; a dwell not on the grid is **refused with
   its two neighbours** (the Expert `strict-v1` wording), or, in the
   Advanced panel, **snapped explicitly**: the field shows the realized
   dwell and the timing note names the change. Nothing is rounded silently.
3. **One source for every count.** `samples_per_line = N_x ·
   samples_per_pixel`; the line period, the flyback, the TTL line, the
   detector's line read and `scan_samples` are all derived from that one
   number. The galvo designer's separate line rounding (`:325`) goes; the
   TTL designer's own `samples_per_pixel` goes (it takes the contract's);
   the controller's own rounding (`ScanControllerAdvanced.py:742`) goes.
4. **The realized dwell is the dwell.** The velocity check, the contract's
   `dwell_time`, the recording's `ScanStage` attributes and the metadata
   carry the realized value, with the request beside it when it differed
   (`dwell_requested_s`), as the Expert schedule already does.
5. **Rates are validated against the device, not against a constant.**
   The NI-DAQ manager asks the driver (`Device.ao_max_multi_chan_rate` for
   the channel count in use, `do_max_rate`, `ai_max_multi_chan_rate` for
   the PMTs, `co_max_timebase`), configures the AO clock from the 100 MHz
   timebase, reads back `samp_clk_rate`, and refuses when the read-back
   differs from `scan.sampleRate` ("the driver can play 999 000 Hz, not
   999 123 Hz"). In simulation the mock declares its limits in the setup
   (`nidaq.simulatedLimits`), never defaulted.
6. **Line and frame boundaries** are sample counts, exact, on the one
   grid: `scan_samples_d2_period = samples_per_line + flyback_samples`,
   the frame `N_y · period` (+ the settling and the first-line start); the
   detectors compute their plan from these and never from the dwell.
7. **Phase delay** stays a time in µs, converted once to **timer**
   samples; at a pixel clock of 1 MHz with `k = 1` it is whole microseconds.
8. **Repeated scans** restart from the same sample plan; the timer and the
   AO are retriggered together as today.
9. **Electronic versus physical.** Rule 2 is electronic. The physical rule
   stays `step / dwell ≤ vel_max` on the realized dwell, and a stepped fast
   axis needs `dwell ≥ move + settle`. The panel says which rule refused.

## 6. Staged implementation

**S0 -- one source at 100 kHz (fixes the 15 µs rig failure). DONE 2026-10-16** (decisions taken: the rule is the policy's `ceil`, so a dwell is never shortened; the Advanced panel snaps and its timing note says so; the contract carries `dwell_requested_s`). Rules 2–4
and 9 without touching the clock: galvo designer, TTL designer, controller
and detectors take `samples_per_pixel` from one place; the Advanced panel
snaps the dwell and says so; the velocity check uses the realized dwell;
the contract records both. Goldens at 10 and 20 µs stay sample-exact; the
`xfail` cases flip. Scope: `GalvoScanDesigner.py`, `BetaScanDesigner.py`
(same two roundings, `:81, 87`), `AdvancedScanTTLCycleDesigner.py`,
`ScanControllerAdvanced.py`, `APDManager.py`/`PMTManager.py` (refuse a
partial pixel instead of dropping it), `ScanWidgetAdvanced.py`.

**S1 -- the clock is configurable. DONE 2026-10-16 (software; the driver's division of the 100 MHz timebase, the read-back and the device limits are the rig's to confirm, §8).** `NidaqManager`: the AO task's sample
clock from the 100 MHz timebase at `scan.sampleRate`
(`cfg_samp_clk_timing(source='', rate=…)` with
`samp_clk_timebase_src = '100MHzTimebase'`), DO on `ao/SampleClock` as now,
the timer at `detectionRateHz`; `_checkScanClockRate` becomes the validator
of rule 5; `SCAN_CLOCK_RATE_HZ` is no longer a constant anything reads.
The simulated NI-DAQ and the mock detectors follow the configured rates.
Setup: `scan.sampleRate` keeps its name; `nidaq.detectionRateHz` is new
(absent = 1 MHz, as today, said in the docs).

**S2 -- the Expert engine follows. DONE 2026-10-16.** `NidaqScanEngine.rate_hz` and
`timing_grid_s` from the setup; `scan_info_for` and the schedule already
take a rate. The capability report names the grid and its source.

**S3 -- fast lines. DONE 2026-10-16:** `detectors/_read_ahead.py` (`ReadAhead`, chunk = one `liveUpdateIntervalMs` of samples, never past the task's end), APD and PMT; per-line emission and the workers' bookkeeping unchanged. Measured on the simulated rig, 256 × 128 at 1 µs (162 ms of samples): 130 driver reads per detector → 4, image identical count for count with the same seed. The driver-side latency saved is the rig's to measure. At 1 µs a 512-pixel line is 0.5 ms: the detectors'
one-read-per-line loop (`APDManager.py:1531`) makes ~2000 driver reads per
second per detector. Read in chunks of a declared minimum duration
(several lines, split afterwards), keep per-line emission, and throttle
the live image to the display's rate. Measure before and after on the
simulated NI-DAQ.

**S4 -- panels and docs. DONE 2026-10-16** (the Advanced dwell label names the clock; D2 and D3 labelled in µs; SimplePointScan's snapping on the shared arithmetic; the config editor offers both rates; setup reference, GUI, Advanced and engine docs). The Advanced dwell field shows the grid and the
realized value; SimplePointScan's `snap_dwell_s` uses the same rule; the
config editor offers the two rates with the driver's limits as help;
`setupinfo-reference.rst`, `advanced-scanning.rst`, `expert-scanning.rst`,
the changelog.

**S5 -- the lag and the settle (Lenny, 2026-10-16). DONE 2026-10-16 as far as decided:** the TTL shift on the sample grid (`NidaqManager.delayed_lines`, `lag_samples`); the slice settle declared on the positioner (`managerProperties.settleTimeS`) as D3's floor, recorded and reported. Not done, for Lenny: removing the D3 field (it stays as an optional longer pause; old scan files carry it), and the start-trigger-delay variant of the shift (10 ns, needs the 6363). Two fields of the
Advanced panel are device properties typed per scan: **D2**, the galvo's
response lag (`ScanWidgetAdvanced.py:373`, `phase_delay`, applied as samples
the detector discards at the start, `APDManager.py:1232-1238, 1476`), and
**D3**, a pause before each slice (`GalvoScanDesigner.py:227, 464`,
`d3step_delay`), there so the slow axis has settled. The speed-up itself is
not a field: the designer derives the acceleration run-in and the settling
from `vel_max` and `acc_max` and tells the detector what to discard
(`scan_throw_startacc`, `scan_throw_settling`).

- **D3 goes.** The slow axis's settle time is declared on the positioner
  (the Expert model's `settle_s` per drive, the Beta setups' `settle_time`)
  and the designer reads it; a slice-to-slice gap shorter than it is
  refused with the number.
- **D2 stays a field for now.** The lag depends on the scan speed: Lenny
  needs a different D2 at 10 and at 20 µs dwell, so one declared number
  would be wrong half the time. What changes: the value in use and its
  source are shown; a positioner may declare `responseLag` as a table of
  (sweep speed, lag) measured by a calibration run (scan a structure
  forward and back with the same waveform; the shift that aligns the two
  directions is twice the lag), and the panel fills D2 from the entry
  nearest the scan's speed when one exists, else leaves it to the user.
- **The TTL lines are shifted by the lag.** Today only the readout is
  shifted (`APDManager.py:1476`): the detector's pixel *k* integrates
  command-time samples `[k·d + lag, (k+1)·d + lag)`, while a gated laser is
  on for the command-time line `[0, N·d)`. The last pixel of every line
  therefore integrates `lag` samples after the gate closed and comes out
  dark by `lag / dwell`; with the shift the other way, the first pixel.
  That is the dark edge pixel Lenny sees on fast scans, and it grows as
  the dwell shrinks: a third of a pixel at a 3 µs lag and 10 µs dwell, three
  pixels at 1 µs. With pulses inside the pixel (advanced TTL) every pixel is
  gated against the wrong part of the sweep. The fix: delay the DO stream
  by the lag so gating follows the beam, not the command. On the sample
  grid that is a shift by `round(lag · sampleRate)` samples (coarse at
  100 kHz, 10 µs steps; exact enough at 1 MHz); finer, and independent of
  the rate, is the DO task's start-trigger delay (DAQmx
  `start_trigger.delay` in timebase ticks, 10 ns on the X Series) -- to be
  verified on the 6363 (§8). The Expert engine applies the same shift to
  its digital lines; the `ttlMultiplying` mask is shifted with them.

**S6 -- a sum projection over line steps and other extra dimensions in
the live view (Lenny, 2026-10-16). DONE 2026-10-16** (sum only, per Lenny's words; the PMT's own line-step sum stays until its live buffer carries the stack; `displayAxisNames` on the APD and PMT name the dimensions). A line-step scan (and a z or time
stack) reaches the napari viewer as a stack with a slider. Lenny wants,
under the levels tool, a checkbox per displayed dimension beyond the two
image axes that sums over it and shows the reduced image. The detectors
already hold half of this with no GUI: `_linestep_view_mode` ("sum",
"max", "slice") reduces the line steps before display (`APDManager.py:
1026-1036`, `PMTManager.py:850-893`), set by nothing, and the two defaults
disagree (the PMT sums, the APD shows the stack, `PMTManager.py:191`).

- A `NapariDimReduceWidget` beside `NapariUpdateLevelsWidget`
  (`imcommon/view/guitools/naparitools.py:242`, docked by
  `ImageWidget.py:21`): for the selected layer, one row per dimension
  beyond the last two, named from the layer's axis labels (*line steps*,
  *z*, *t*), each with a **Sum** checkbox; a checked dimension is summed
  (float, so counts do not saturate) and the layer shows the reduced
  array; unchecked, the slider is back. Display only: the recording and the
  detector's frame are untouched.
- The detector-level modes are retired in favour of it (one place, every
  layer, cameras' stacks included); until then the APD's default follows
  the PMT's or both follow the checkbox.
- Levels: auto-levels run on the displayed (reduced) image, so a summed
  stack is not clipped by the single-step range.
- Persisted per detector with the panel state, so a line-step rig opens
  summed if that is how it was left.

Each stage ends green on the unit lane and on the goldens; S1, S3 and S5
end with the hardware checks of §8. S6 is independent of the clock work
and can go first.

## 7. Costs at a 1 µs pixel (512 × 512, ~20 % flyback)

| Item | Per frame | Per second at ~3 fps |
|---|---|---|
| AO samples, 2 channels | 0.31 M × 2 (float64 in Python 5 MB; int16 in the driver 1.2 MB) | 15 MB / 3.7 MB |
| DO samples, 32 lines in one port | 0.31 M × 4 B = 1.2 MB | 3.7 MB |
| APD counter samples, per detector | 0.31 M × 4 B = 1.2 MB | 3.7 MB |
| Driver reads, per detector | 512 (one per line) | ~1500 -- the item S3 batches |
| Image | 512 × 512 × 2 B = 0.5 MB | 1.5 MB; per-line emission 1500/s, to be throttled |

All of it is small beside PCIe and the driver's DMA; the designers'
spline evaluation at 0.3 M points is milliseconds. The only change in
kind is the read cadence (S3).

## 8. Validation

**Software (no hardware), in the unit lane:**

- `test_scan_dwell_timing_grids.py` at 1, 10, 15, 20 µs: galvo line =
  `N_x · samples_per_pixel`, TTL period = galvo period, detector plan =
  contract × `k`, `samples_per_pixel · scan_time_step == dwell_time`
  (realized), off-grid dwell refused or snapped and recorded;
- photon-count conservation on the simulated APD: the sum of the frame's
  pixels equals the sum of the timer samples over the lines, at each dwell;
- the rate validator against declared mock limits: a rate the device cannot
  play, a detector rate that is not a multiple, two PMTs over the AI
  aggregate, each refused by name;
- the Beta and galvo goldens unchanged at 100 kHz; new goldens at 1 MHz;
- the Expert strict policy at a 1 µs grid; `scan_info_for` periods against
  realized step starts (the round-3 boundary tests, parametrized by rate).

**Hardware (rig test plan, PCIe-6363):**

- oscilloscope on one galvo AO and one TTL line at 1, 10, 15 and 20 µs:
  line start to gate edge within one timer sample, no drift over a frame;
- a structured sample (grid or ruler) imaged at each dwell: no shear, no
  wrap, pixel size as recorded;
- counts: a steady source at 1 µs × 10 frames against 10 µs × 1 frame
  within Poisson error (conservation);
- a `phase_delay` sweep: the delay that aligns forward lines is the same at
  every dwell, scaled by nothing;
- repeated scans (Repeat, time-lapse): frame-to-frame alignment;
- two PMTs at 1 MHz: the refusal; one PMT at 1 MHz: a clean image;
- the driver's read-back rate logged for 100 kHz, 500 kHz, 1 MHz and an
  off-divisor request;
- S5: the DO start-trigger delay on the 6363 -- scope on a TTL line
  against the AO, the delay set to the lag, edge-to-edge within one
  timebase tick; then the dark edge pixel gone at 10 and 20 µs with the
  lag the rig uses today.

## 9. Decisions and open questions

Decided (Lenny, 2026-10-16): D2 stays a per-scan field for now, because the
lag changes with the scan speed; the TTL lines are shifted by the lag (S5);
the sum projection is a per-dimension checkbox in the live view (S6).

Taken as the plan's defaults while building (Lenny said "proceed until
done"), each reversible:

1. An off-grid dwell is rounded up in Advanced (the policy's `ceil`, never
   shorter) and the timing note says so; Expert refuses it (strict-v1).
2. `nidaq.detectionRateHz` exists, absent = 1 MHz as before.
3. The TTL shift is on the sample grid (exact at 1 MHz, 10 µs steps at
   100 kHz).
4. The checkbox offers *sum* only, as asked.

Still open for Lenny:

1. **D3:** the positioner's declared `settleTimeS` is now D3's floor; remove
   the D3 field altogether? (Old scan files carry `d3step_delay`.)
2. **The start-trigger delay** for the TTL shift (10 ns resolution instead of
   the sample grid) -- needs a scope on the 6363.
3. **Which rig test first:** 15 µs at 100 kHz (S0, no clock change), then
   1 MHz on the 6363 (S1), then the lag shift at 10 and 20 µs (S5).
4. **The PMT's own line-step sum** before display stays until its live
   buffer carries the stack (S6 note).
