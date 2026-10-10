# Scan engines: what to test on the rig

For `feat/scan-engines` (the plan: `scan-engines-and-expert-scan.md`). Every
item below passed in software (simulated NI-DAQ, simulated boards, mock
detectors); the rig is where timing on a wire, real stages and real cameras
answer. Ordered by what blocks a merge first.

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

## P0 -- nothing that worked breaks (must pass before merging)

| # | Rig | Do | Passes if |
|---|---|---|---|
| A1 | Galvo point scan (STED) | An Advanced scan you ran before, same file, same sample | The image matches the old one; the timing note lists what plays differently; the progress bar moves; Stop ends after the current frame |
| A2 | Stepped camera scan (MoNaLISA, Beta) | An Advanced scan with line steps, recorded | Same frame count and layout as before; reconstruction unchanged. A camera that is off in some line steps now has a repeat axis sized by the steps it takes part in -- check the file opens and reconstructs |
| A3 | Galvo + APD | SimplePointScan: Live overview, draw a region, Acquire and save with T frames | As before; the file holds T frames |
| A4 | Any | Record any scan, open the file | `ScanStage:scan_program` (Expert) or the Advanced dicts, and `ScanStage:scan_schedule`: the policy, the grid and its source, what ran differently |
| A5 | Any rig with an inverted TTL input (a shutter, a laser enable) | Set its `ttlActiveLevel: "low"`; switch by hand, then scan | On the scope, the line is high when the GUI says off -- by hand and in a scan |
| A6 | Any rig with a laser on a non-NI-DAQ line | Open the Advanced panel | That laser is greyed with the reason, and a scan that fires it is refused (it used to never fire, silently) |
| A7 | A rig with NI-DAQ lines on a `PXI…` device | Scan with that line | It now fires (it used to be dropped) |

## P1 -- new on the NI-DAQ

| # | Rig | Do | Passes if |
|---|---|---|---|
| B1 | Snouty stage, camera | Advanced, Beta axis in **interleave** order (cycles x planes), recorded | SNOUTY reconstructs it as the plain order; the stage visits cycles then planes (scope or recorded positions). This is the M2 rig gate (plan §7.3) |
| B2 | Beta stage | **Serpentine** order | Rows are not mirrored in the image |
| B3 | Beta stage | Intra-pixel positioner move (line program) | The stage moves inside the pixel as programmed (scope on the positioner AO); it used to do nothing |
| C1 | Snouty | Expert **MS-RESOLFT** preset, recorded | Frames = cycles x planes x timepoints; the first frame is sharp (the run starts with the approach span the report names, so no window opens while the stage travels); SNOUTY reconstructs |
| C2 | Snouty | MS-RESOLFT with timepoints and a **pause** | Through the pause every AO **holds** its position on the scope. Before review round 1 a timed wait drove every positioner to 0 V and back -- a safety item, check it first |
| C3 | Camera rig | Expert **pulse sequence** | Pulse widths and gaps on the scope equal the report's realized schedule; the camera exposes inside its pulse |
| C4 | Any | Stop an Expert run; then move a positioner by hand; then run again | The positioner widget shows where the run left the stage; the next run starts from there |
| C5 | A laser with `calibCsvPath` | A `level` event inside the step | The laser's AO (or measured power) follows the calibration during the event and is 0 elsewhere |
| C6 | Camera rig | Detectors tab: set **Per step** 2 where the camera fires once | Refused before anything runs, naming the step; set back to 1: the recorded frame count is what the binding says |
| C7 | Galvo + APD | **Trajectory** circle, then spiral, slow first (report's peak speed well under `vel_max`), on a known sample (beads, a grid) | The gridded image shows the sample in place; unvisited bins are empty; with **Record every measurement** the file holds windows, times and positions. Then speed up until the image smears: that is the galvo lag the commanded path does not know (plan §8) |
| C8 | Any | Load a scan file with a Python trajectory | Asked first with the code shown; Cancel runs nothing; the startup restore never asks |

## P2 -- the Teensy as an engine (pulse generator)

| # | Do | Passes if |
|---|---|---|
| D1 | Expert pulse program on a laser wired to the Teensy, with `scan.engine` left on the NI-DAQ | The report says *Runnable on teensy*; the Teensy line pulses as the report says (1 µs grid); a time off the grid is refused with its neighbours |
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
§7.10 or a new §7.11.
