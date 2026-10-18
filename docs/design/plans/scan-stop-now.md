# Stop a scan now, not after its frame

Branch `feat/scan-engines` (one effort, one branch). Draft 1, 2026-10-18.

## Decisions (Lenny, 2026-10-18)

1. **Scanners after a Stop** ramp back to where the scan started, within the
   speed and acceleration their positioners declare (`vel_max`, `acc_max`).
   They are not left where the Stop found them.
2. **The unfinished frame** is dropped from recordings. The complete frames are
   kept, and the file says the scan was stopped.
3. **Stop means now, everywhere.** SimplePointScan's rule "Stop after the
   current frame" (2026-09-25) is replaced; unticking Live or Repeat still
   finishes the frame.

## Today

| Panel / engine | What Stop does |
|---|---|
| Every panel that plays on the NI-DAQ (Base, PointScan, MoNaLISA, Advanced, SimplePointScan, SimpleMoNaLISA's Acquire, Expert on the NI-DAQ) | Nothing until the iteration -- one frame or volume -- has played; then no repeat. `SuperScanController.abortScan` returns while an iteration runs; `NidaqScanEngine.abort()` returns `'none'` |
| Expert on a step board, the Teensy | Stops at the next step boundary (`STOP`, the generator's stop) |
| TriggerScope panels | After the iteration: the firmware has no mid-iteration abort (`scan-lifecycle.rst`); unchanged here, it needs the firmware |
| SimpleMoNaLISA's own Stop | Ends Live screening only; disabled while Acquire runs |

What already exists and is reused:

- `NidaqManager._cleanupScanResources()` stops and closes every task of the
  run (outputs, the timer, the detectors' input tasks), stops the simulator,
  joins the waiters and marks the run terminal so no stale `sigScanDone`
  fires -- the rollback a failed run takes.
- A controller whose stop was requested ends the run's token with
  `FINISH_ABORT`; every detector's `finishScan('abort')` then abandons the
  frame in progress (`_markRawFailed` / `_abandonRaw`: "a partial volume in
  a recording is worse than a missing one") and tears its worker down.
- A recording finalizes `recording:planned_frames`, `recording:actual_frames`
  and `recording:completion_outcome` (`stopped_early` when fewer frames than
  planned arrived).
- The external-request terminal (`ScanRequestCompletion`) of an aborted run
  is `successful=False, 'Scan was aborted.'`; a recording that did not itself
  ask for the stop treats that as a failure.

## Design

### The NI-DAQ stops now (`NidaqManager.stopScan`)

Only while a run is in progress (a task registered or the simulator active,
no terminal sent yet); otherwise nothing happens.

1. **Where the outputs are.** Before anything stops, each analog channel's
   position in its waveform: `out_stream.total_samp_per_chan_generated` of the
   analog task (in simulation, the simulator's progress through the run). A
   count the driver does not report leaves that channel's position unknown.
2. **Stop.** `_cleanupScanResources()`, as a failure's rollback, with
   `scanStopRequested` set first, so a detector worker whose read the closed
   input task interrupts ends as stopped, not as failed.
3. **Safe outputs**, as one-shot writes once the run's tasks are closed:
   - every digital line of the run at its off level (`ttlActiveLevel`; clocks
     and markers low) -- first, so the lasers go dark first;
   - a laser's analog power at the level its waveform started at (its off, the
     same rule the lag shift's idle uses);
   - each scanner ramped from where it stopped to its waveform's first sample
     -- where the scan started and where a finished scan ends -- by the
     minimum-time rest-to-rest profile its `vel_max` and `acc_max` allow
     (µm/µs and µm/µs², converted by `conversionFactor`), all channels in one
     finite task at the one-shot clock, waited for by the read-timeout rule
     (twice its duration, never less than nidaqmx's default). A scanner that
     declares no limits, or whose position is unknown, is left where it is,
     and the log says so -- no speed is invented.
4. **The run's end:** `sigScanStopped`, then `sigScanDone` (the engine
   run-end contract for a stopped run). `lastStop()` keeps what the stop did:
   the positions it found and left, and what it could not do.

`NidaqScanEngine.abort()` calls it and returns `'immediate'`; its
capabilities say so, with the source.

### The panels

- `SuperScanController.abortScan`: while an NI-DAQ iteration runs, the stop
  is requested (as today) **and** the engine is told to stop now. The run then
  ends through its usual path: `FINISH_ABORT` (detectors drop the partial
  frame), no repeat, the positioners' return-to-centre, `sigScanEnded`.
- The external terminal of a stopped run says so: `stopped=True`, `'Scan was
  stopped.'`.
- SimplePointScan / the cloak base: Stop says "Stopped" (not "Stopping after
  this frame"); unticking Live still finishes the frame.
- SimpleMoNaLISA: Stop is enabled while Acquire runs, and stops it now.
- Expert on the NI-DAQ: a stopped run's positions come from the stop's
  evidence -- where the ramp left each drive (or where it was left), in µm --
  instead of "not known".

### The recording

A run that was stopped ends its recording as a user Stop would: the complete
frames are written, the writer finalizes `stopped_early`, and the recording
ends (`recordingEnded`), instead of failing with "Scan ended before
acquisition completed". Through the exact terminal (`stopped`) and through
the global lifecycle end (the scan workflow remembers that the last run was
stopped).

### The Time Tagger

A stopped run's FLIM frame in progress is not published as the final frame.

## Tests (software)

- The ramp: starts at the stop position, ends exactly at the target, never
  exceeds `vel_max` / `acc_max` (sample differences), triangular and
  trapezoidal cases, zero distance.
- `NidaqManager.stopScan` on the hardware path with stand-in tasks: every
  task stopped and closed, the off levels written (active-low too), laser
  power off, the ramp written from the generated-sample position, the
  stopped-then-done signals, busy cleared; a scanner without limits held; idle
  is a no-op; in simulation the simulator stops and the run ends stopped.
- A panel (Advanced on the simulated NI-DAQ, mock APD): Stop mid-iteration ends
  the run before the iteration would have; no repeat; the scan-lapse
  recording keeps its complete frames, ends (not fails) with `stopped_early`.
- Expert on the NI-DAQ: Z tracked where the stop's ramp left it.
- SimplePointScan's and SimpleMoNaLISA's Stop; the Time Tagger's frame.

## Rig rows

To add to `scan-engines-rig-test-plan.md` (S rows): the scope sees every laser
line go to its off level within one write of Stop and the galvo ramp back to
its start without a jump; a stopped scan-lapse file opens with its complete
frames and `stopped_early`; a stopped camera scan's file holds the frames that
were exposed.
