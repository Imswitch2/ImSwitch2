# Scan engines: what to test on the rig

For `feat/scan-engines` (the plans: `scan-engines-and-expert-scan.md` and,
for the T rows, `scan-timing-configurable-dwell.md`). Every item below
passed in software (simulated NI-DAQ, simulated boards, mock detectors); the
rig is where timing on a wire, real stages and real cameras answer. Ordered
by what blocks a merge first. The branch also carries main as of
2026-10-16 (device reconnection, #70; COMET drift, #68): their own rig items
are in their plans.

**Before the rig**

- Back up the rig's setup file and `ImSwitchConfig` (a pull updates the
  shipped defaults; edited files are kept, but keep a copy).
- Start ImSwitch on the rig's own setup and read the log for scan-engine
  errors (a channel two engines claim, an unknown `scan.engine`).
- Open **Hardware → What this setup can scan…** and check that every device
  sits on the engine it is wired to, and that nothing you scan is listed
  under "devices on no engine".
- Have an oscilloscope on the scan's AO and TTL lines for the first runs;
  lasers off or at minimum power, small scan ranges, until a test says
  otherwise.
- From the build you run today, keep an image of a known sample at 10 µs
  and at 20 µs dwell and note the D2 (phase delay) you use for each: T1
  compares against them.

## P0 -- nothing that worked breaks (must pass before merging)

| # | Rig | Do | Passes if |
|---|---|---|---|
| A1 | Galvo point scan (STED) | An Advanced scan you ran before, same file, same sample | The image matches the old one; the timing note lists what plays differently; the progress bar moves; Stop ends after the current frame |
| A2 | Stepped camera scan (MoNaLISA, Beta) | An Advanced scan with line steps, recorded | Same frame count and layout as before; reconstruction unchanged. A camera that is off in some line steps now has a repeat axis sized by the steps it takes part in -- check the file opens and reconstructs |
| A3 | Galvo + APD | SimplePointScan: Live overview, draw a region; then a **Scan-lapse** recording of T frames in the Recording panel | As before; the file holds T frames |
| A4 | Any | Record any scan, open the file | `ScanStage:scan_program` (Expert) or the Advanced dicts, and `ScanStage:scan_schedule`: the policy, the grid and its source, what ran differently |
| A5 | Any rig with an inverted TTL input (a shutter, a laser enable) | Set its `ttlActiveLevel: "low"`; switch by hand, then scan | On the scope, the line is high when the GUI says off -- by hand and in a scan |
| A6 | Any rig with a laser on a non-NI-DAQ line | Open the Advanced panel | That laser is greyed with the reason, and a scan that fires it is refused (it used to never fire, silently) |
| A7 | A rig with NI-DAQ lines on a `PXI…` device | Scan with that line | It now fires (it used to be dropped) |

## P0 -- scan timing at the rig's own 100 kHz clock

Nothing in the setup file changes for these rows. What changed for every
galvo scan: every count follows one rounding (the dwell rounds up to whole
samples), the digital lines -- lasers, cameras, line and frame clocks -- are
played D2 later so the gate follows the beam, and the detectors' timer
starts with the scan.

| # | Rig | Do | Passes if |
|---|---|---|---|
| T1 | Galvo + APD | Your 10 µs and 20 µs Advanced scans, same D2 as before | The images match the old ones, except that an edge pixel that was dark on fast scans is lit. Scope: each laser gate's edges trail the galvo line by D2 (whole 10 µs samples); the line and frame clock outputs trail by the same D2; an AOM's per-line-step power (e.g. the 775 AOM's analog channel) trails by the same D2 as its gate. If your rig used a D3 value, put it in `scan.scanDesignerParams.d3step_delay` (it settles a 2-D scan before its first line, as the field did) |
| T2 | Galvo + APD | The same at 15 µs (the original failure) | The timing note says *Dwell 15 µs runs as 20 µs*; the image is right -- no shear, no drift from line to line |
| T3 | Galvo + APD | D2 = 3 µs, or any value that is not a multiple of 10 | The timing note says the lag plays as 0 µs on the digital lines and 3 µs on the detectors; the recording's `ScanStage:lag` says the same |
| T4 | Galvo + APD | A scan with 2 or 3 line steps | A **Sum line steps** box appears under the levels tool and nothing else; ticked, the view shows the summed image and its levels follow; the file keeps every step. Then a 2-D scan: the box is gone |
| T5 | Galvo, 3-D | Declare `settleTimeS` (e.g. `0.002`) on the Z positioner; scan 3 planes | The D3 field is gone; the timing note says *Every slice starts after a 2000 µs pause: … declares settleTimeS 0.002 s*; each plane is sharp from its first line |
| T6 | Stage, Beta (MoNaLISA) | A scan you ran before; then the same at a 5.03 ms dwell | Same frames as before. At 5.03 ms the scan ends normally with one frame per pixel (a dwell whose sample count is float noise -- 503.00000000000006 -- now counted the same way by the stage and its TTL designer) |
| T7 | Galvo + APD (any `nidaq.startTrigger`) | The same scan three times, at 100 kHz | No shift between the three images: the timer starts with the scan, and the analog output's first sample a fixed time after it (it was on the free-running 100 kHz timebase). A scope check settles it: `ao/StartTrigger` routed to a PFI pin against the first AO step, over ten scans |
| T6b | Stage, Beta (Base or MoNaLISA panel) | A camera pulse from 0 to the full dwell | Refused, naming the camera: such a pulse never falls between pixels (one trigger per line). End it a sample earlier |
| T8 | Any | The first scan after starting | No refusal starting *Cannot verify the … task*. If one appears, note the task and the message: the driver does not report that property on this board, and the check is relaxed for that task (a one-line change) |

## P1 -- a faster clock on the PCIe-6363

| # | Rig | Do | Passes if |
|---|---|---|---|
| T9 | 6363 | `scan.sampleRate: 1000000` in the setup (`nidaq.detectionRateHz` absent: 1 MHz); start | Starts; the dwell label reads *(ms, 1 µs clock)*; the first scan logs *Detector timer: 1e+06 Hz from a 1e+08 Hz timebase, first pulse … s after the scan starts* |
| T10 | 6363, galvo + APD | 1 µs dwell on a small field (at 1 µs the turnaround is v²/2a: keep pixels small, the designer refuses what the galvo cannot do), then 2 and 5 µs | A clean image; the scope shows the AO updating every 1 µs and the gate edges at whole microseconds of D2; a steady source at 1 µs × 10 frames gives the counts of 10 µs × 1 frame within Poisson error |
| T11 | 6363 | `scan.sampleRate: 300000` | Refused at startup, naming 250000 and 333333 Hz as the nearest rates the timer divides |
| T12 | 6363 + PMT (if fitted) | One PMT at 1 MHz; then two on one device | One: a clean image. Two: the driver refuses the second analog input task when the scan arms, the scan rolls back, the next scan runs |
| T13 | 6363 | 512 × 512 at 1 µs | The live view keeps up; no read timeouts in the log |
| T14 | Galvo | Forward and backward lines at 2, 5, 10 and 20 µs: the D2 that aligns them at each | A table of (dwell or sweep speed, D2) for this rig -- the lag changes with speed, which is why D2 stays a field; the table is what a per-speed lag would be filled from |

## P1 -- new on the NI-DAQ

| # | Rig | Do | Passes if |
|---|---|---|---|
| B1 | Snouty stage, camera | Advanced, Beta axis in **interleave** order (cycles x planes), recorded | SNOUTY reconstructs it as the plain order; the stage visits cycles then planes (scope or recorded positions). This is the M2 rig gate (plan §7.3) |
| B2 | Beta stage | **Serpentine** order | Rows are not mirrored in the image |
| B3 | Beta stage | Intra-pixel positioner move (line program) | The stage moves inside the pixel as programmed (scope on the positioner AO); it used to do nothing |
| C1 | Snouty | Expert **MS-RESOLFT** preset, recorded | Frames = cycles x planes x timepoints; the first frame is sharp (the run starts with the approach span the report names, so no window opens while the stage travels); SNOUTY reconstructs |
| C2 | Snouty | MS-RESOLFT with timepoints and a **pause** | Through the pause every AO **holds** its position on the scope. Before review round 1 a timed wait drove every positioner to 0 V and back -- a safety item, check it first. Keep the pauses short: the NI-DAQ plays a pause as samples, so a long one makes the panel slow, and one beyond the free memory is refused with the numbers |
| C3 | Camera rig | Expert **pulse sequence** | Pulse widths and gaps on the scope equal the report's realized schedule; the camera exposes inside its pulse |
| C4 | Any | Stop an Expert run; then move a positioner by hand; then run again | The positioner widget shows where the run left the stage; the next run starts from there |
| C5 | A laser with `calibCsvPath` | A `level` event inside the step | The laser's AO (or measured power) follows the calibration during the event and is 0 elsewhere |
| C6 | Camera rig | Detectors tab: set **Per step** 2 where the camera fires once | Refused before anything runs, naming the step; set back to 1: the recorded frame count is what the binding says |
| C7 | Galvo + APD | **Trajectory** circle, then spiral, slow first (report's peak speed well under `vel_max`), on a known sample (beads, a grid) | The gridded image shows the sample in place; unvisited bins are empty; with **Record every measurement** the file holds windows, times and positions. Then speed up until the image smears: that is the galvo lag the commanded path does not know (plan §8) |
| C8 | Any | Load a scan file with a Python trajectory | Asked first with the code shown; Cancel runs nothing; the startup restore never asks |

## P2 -- the Teensy as an engine (pulse generator)

| # | Do | Passes if |
|---|---|---|
| D1 | Expert pulse program on a laser wired to the Teensy, with `scan.engine` left on the NI-DAQ | The report says *Runnable on teensy*; the Teensy line pulses as the report says (1 µs grid); a time off the grid is refused with its neighbours. First check the log: the Teensy opened its port (with a wrong port it runs on its mock, `useMockOnFailure`, and the report still says runnable) |
| D2 | Stop it mid-run; let one finish | Every Teensy line is at its off level afterwards. An active-low line on the Teensy is refused before the run: its stop drives every channel low, which would switch it on |
| D3 | Switch the Teensy laser by hand | Follows its polarity |
| D4 | A program with the NI-DAQ's galvo and the Teensy's laser | Refused, naming which device is on which engine (they cannot run one program together yet) |
| D5 | If the Teensy's trigger input is wired: a program that waits for a start trigger | Starts on the trigger |

## P3 -- event-started runs

| # | Do | Passes if |
|---|---|---|
| E1 | EtSnouty **Slow scan** set to `Scan`: let detected events run the Expert program | It runs once per event; its recording carries `ScanEvent:triggeredBy` (pipeline, detector, position, time), blank again after the run; Stop during the program ends it cleanly |

## Blocked until the TriggerScope speaks the step protocol

The step-engine protocol (`IMSWITCH_STEP/1`), its engine, leader/follower
runs, `SET` by hand, the heartbeat and DAC resolution have run only against
simulated boards. When the lab's firmware exists:

1. `python tools/step_board_conformance.py --port COM? --trigger` on the
   board, with a signal generator on its trigger input: every program
   passes, levels included.
2. The rig's setup on the board: **What this setup can scan…**, then C1 and
   C3 on the board instead of the NI-DAQ.
3. A second board wired to follow: one program on both; the scope shows the
   leader's pulse and the follower's step after its declared latency.
4. Pull the USB cable mid-run: the run fails with the board named, within
   the heartbeat time.

## Numbers to measure (they are unknown until measured, never defaulted)

- Each camera's trigger-to-exposure latency and readiness, for its
  `preparation` declaration (legacy setups use `legacy-preparation-v1`,
  which the file records).
- The Teensy's clock accuracy, from its specification, as
  `clockAccuracyPpm` -- only needed once it leads or follows.
- Each galvo's real `vel_max` and `acc_max`: C7's smear point says whether
  the declared ones hold.
- The NI-DAQ's write time for a long program (the scan-freeze work left it
  unmeasured), to know when a densified program becomes too large.

Report results per row (pass, fail with what was seen, not run) in plan
§7.10 or a new §7.11; the T rows in `scan-timing-configurable-dwell.md` §8.
