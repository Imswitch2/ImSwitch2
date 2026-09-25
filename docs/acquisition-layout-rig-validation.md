# Acquisition layout: rig validation guide

**Branch:** `codex/acquisition-layout-schema`
**Status of the work:** software-verified only — the ImControl/ImCommon and
ImProcess suites pass, the seven must-do conditions of the
[direction audit](design/plans/acquisition-layout-direction-audit.md) are done
(2026-09-04), and nothing has ever run against real hardware. The last
software step was a seam test that records through every real stage under the
simulated DAQ (`test_acquisition_layout_recording_seam.py`); what remains is
whether the producers' assertions match what the hardware delivers.
**What this guide is for:** settling the assumptions that only a microscope can
settle, and catching the recordings this branch will now refuse.

---

## 0. Before the session: run the right code

Everything below assumes the instrument computer is running **this branch at
its current tip**, which is not the same thing as `codex/acquisition-layout-schema`
as published. At the time of writing the published tip predates all seven audit
conditions *and* the chunk-queue fix for the failure that started this work, so
a rig fetching the branch name alone would validate none of it.

Two checks, both before the sample goes on:

1. `git log --oneline -1` **and `git status -sb`** in the checkout the rig
   will run. The tip must be the commit you mean to test, the tree must be
   clean, and the branch must not report `ahead` or `behind`: a local-only
   commit is exactly what a rig fetching the branch by name will not have.
2. The inspector's `interpreted by:` line (§2) on the first file you record.
   It says which ImSwitch actually read that file, which on a machine with a
   release install beside a working checkout is the question that matters.

If ImSwitch is installed rather than run from the checkout, reinstall it from
the checkout before the session. A session run against the wrong code produces
results that look valid and mean nothing.

---

## 0b. What the magic-number fixes will refuse on the rig

The audit fixes (`docs/design/plans/magic-number-audit-fixes.md`) turned several
silent substitutions into refusals. Check the rig's setup file against these
before the session, or the first scan will stop with a message naming the field:

- **`nidaq.timerCounterChannel` must be set** on a rig with an APD or PMT (e.g.
  `"Dev1/ctr2"`, a counter no detector's `ctrInputLine` uses). Six of seven shipped
  setups leave it `null`; the point detectors used to arm against a terminal
  nothing drove.
- **`scan.sampleRate` must be `100000`** on real NI-DAQ hardware. Another value is
  refused at startup instead of running the scan slower by the ratio.
- **Stage (Beta) scans need a dwell longer than the stage's move + settle**
  (4 ms by default; `move_time`/`settle_time` in `scanDesignerParams` declare a
  faster stage). The stage panels open at 10 ms instead of 1 ms.
- **TriggerScope firmware scans** are refused when a DAC ramp would leave the axis's
  `minVolt`/`maxVolt` — from the parked position, so park the axis first.
- **Photometrics `External "frame-trigger"`** now programs one exposure per rising
  edge (`EXT_TRIG_EDGE_RISING`), not exposure-while-high. Confirm on the camera if
  one is present.
- Every camera without `cameraPixelSizeUm` **warns at startup** and stamps
  `Camera pixel size source = assumed default` into its files. Declare the
  measured value to silence it.
- The Point Scan panel's **phase delay opens at 0 µs** (was 100); put the calibrated
  lag in `scan.scanDesignerParams.phase_delay` (µs) so both panels seed from it.

## 0c. Single-axis (1-D) scans through the Advanced panel

The 1-D designer work (period slicing at the old `np.min` crash site,
per-device `smoothScan`, the APD/PMT one-line image chain, goldens) is merged
into this branch as of the rig session. What a 1-D scan needs:

- **Galvo-only line** (one active galvo axis): nothing to configure; the smooth
  single-sweep path builds the line from one period.
- **Piezo-only profile (Z-only)**: the piezo's positioner entry needs
  `"smoothScan": false` in its `managerProperties` (the shipped
  `example_sted.json` has it on `ND-PiezoZ`; the rig's own copy under
  `~/ImSwitchConfig` must have it too). A smooth sweep on a piezo declared with
  the huge `vel_max`/`acc_max` degenerates the spline; stepped is what the
  device does.
- Any XZ scan whose Z range collapses to one step is the same 1-D case and
  works the same way.
- Expected file: one line stored as `(1, N)` `YX` with the scan step as the
  fast-axis pixel size and the physical scan device/axis recorded as
  `ScanStage:scan_axis_devices` / `scan_axis_physical` in all three formats.

Headless rehearsal of every case: `python scripts/diagnostics/repro-single-axis-scan.py`
(reads the rig's `example_sted.json`).

## 1. Why this needs a rig at all

The whole effort replaces *inferred* frame semantics with *recorded* ones. Every
inference it removed was replaced by something a producer asserts — and those
assertions were derived from the controller code and the signal designers, not
from watching hardware deliver frames.

So the software tests prove the contract is **self-consistent**. They cannot
prove it is **true**. If a producer states the wrong loop order, every layer
downstream now agrees with it confidently, and the reconstruction is wrong in a
way no test here will catch. That is the specific risk this session is for.

Two things follow:

- Prefer samples whose correct reconstruction you can recognise by eye. A bead
  grid, a known pattern, anything with obvious structure. A featureless sample
  cannot falsify a wrong loop order.
- Keep every raw file. If a reconstruction looks wrong, the raw file plus the
  layout dump below is what makes it diagnosable.

## 2. The tool

```bash
python tools/inspect_acquisition_layout.py <recording> [dataset] [--frames N]
```

The optional argument is the dataset name **exactly as the tool lists it**. For
a single-file lapse that is `scan0/Camera`, not `Camera`; the bare detector
name exits 1 and prints what the file does hold. Omitting it inspects every
dataset, which is what you want for a lapse.

`--frames` defaults to 12, which is too few for most of this session: it stops
inside the first repetition of the innermost loop, where the table cannot
disagree with anything. Ask for enough rows to cross that loop at least once —
`--frames 22` for the 10×20 RESOLFT scan, `--frames 40` for an 18-wide
two-line-step scan.

Metadata only — no pixels are read, so it is fast and safe on big files. It
prints the resolved layout and, crucially, a **frame table**: which logical
coordinate each stored frame index maps to.

Its first line of output says **which ImSwitch interpreted the file**. Check it
once at the start of the session. An instrument computer usually has more than
one copy — a release install and the branch under test — and the answer to
"what does this file say" depends on which one is asked, which is the whole
point of the layout work. If that path is not the checkout you mean to
validate, everything below it describes the wrong software.

A container holding several datasets prints one block per dataset. That is what
a lapse recorded as a single file looks like: `scan0/Camera`, `scan1/Camera`,
each with its own partition line.

The frame table is the check on **what the software recorded**. It is rendered
from the layout the producer wrote, so it reads the same whether or not the
camera delivered frames in that order; it is falsified only by comparing it
against the raw pixels, which is what §3.2's *visibly different line steps* are
for. On the motivating 18×18 two-line-step scan it should read:

```
    frame 0     -> scan_y=0  condition=0  scan_x=0
    ...
    frame 17    -> scan_y=0  condition=0  scan_x=17
    frame 18    -> scan_y=0  condition=1  scan_x=0
    frame 36    -> scan_y=1  condition=0  scan_x=0
```

Conditions alternate **per physical row**, not in two contiguous halves. If your
hardware interleaves differently, this is where it shows, before any
reconstruction is attempted.

Run it on every file you record during the session and save the output next to
the file. [What each step should print](acquisition-layout-rig-expected-output.md)
is a companion to this guide: every session step rehearsed against the
simulated rig, with the real output a correct result gives.

**One warning is expected on every HDF5 and Zarr scan recording, and is not a
fault:**

```
  issues:
    [warning] LOSSY_OME_TIME_PROJECTION: Container axis 'T' is only an
    interoperability projection; the acquisition layout records storage role 'frame'
```

The storer labels the leading axis `T` so Fiji can open the file, while the
layout calls it `frame`. What matters in that block is `usable: True`,
`authoritative: True`, and no `[error]` line. Any *other* issue is new
information. `issues : none` is equally fine.

**Two lines that look contradictory and are not.** On a lapse item the
`lifecycle` line always reads `partitions=1 of 1`, because those counters
describe one writer session — this timepoint. The lapse's own position is on
the `partition` line above it: `time index=N of M`.

## 3. The assumptions only you can settle

Ordered by how badly a wrong answer hurts.

### 3.1 TriggerScope RESOLFT loop order — highest risk

`roSteps`, `cycleSteps` and `timeLapsePoints` are combined into a loop order
taken from the controller/firmware contract, **never from a captured trace**
(contract §4.3.5, owed since PR 4). If the firmware nests them differently,
SNOUTY output is confidently wrong.

**Do:** one RESOLFT acquisition with distinguishable planes — a sample where you
know which plane is which, or a deliberate intensity ramp across cycles.
**Check:** the frame table's cycle/plane/time ordering against what the firmware
actually ran. **If it disagrees**, capture the file and the firmware settings;
the fix is a one-line reorder in the producer, but only you can say which order.

### 3.2 Advanced Scan line-step order

`scan_y → condition → scan_x` is established by a 3×2×2 fixture against the
Galvo and Beta designers. The designers are software; the delivered frame order
is hardware.

**Do:** a small scan (3×2 with 2 line steps is enough) with the two line steps
visibly different — different laser power, different exposure, anything you can
tell apart in the raw frames.
**Check:** frames 0..2 are condition 0 of row 0, frames 3..5 are condition 1 of
the *same* row. Rather than eyeballing it, take the mean of every raw frame:
it should be a square wave whose period is twice the fast-axis count, whose
edges fall on multiples of that count, and whose high half lines up with the
condition the frame table assigns. A constant offset between the two — even
half a block — is the failure this step exists to find. In the reconstruction,
the ratio of the per-`T` means should match the ratio of the raw frames' means
with background removed, and the dimmer line step must be `T=0`.

### 3.3 Serpentine parity

If you scan bidirectionally: parity is the flattened counter over the declared
outer loops, and it **resets** at boundaries not included in `parity_loops`
(a Z plane, a timepoint). This matches the existing reconstruction code, but
whether the hardware resets at those boundaries is a hardware fact.

**Do:** a bidirectional scan over ≥2 Z planes.
**Check:** no comb/shear artefact, and none that appears only after the first
plane — that second case is specifically a parity-reset mismatch.

### 3.4 Detector gating and pulse counts

Per-detector line-step masks and multi-pulse configurations are cross-checked
against the *generated* TTL signal, not against delivered frames.

**Do:** two detectors on complementary line steps; and one detector with more
than one pulse per position.
**Check, complementary detectors:** each detector's frame table shows one
constant condition index — `condition=0` for the detector on the first line
step, `condition=1` for the other — and its `detector gating` line's
`count x repeats` equals the `frames=N` on the `lifecycle` line. Each
reconstructs one dense raster; the standard MoNaLISA method keeps a `T` slot
per condition and fills only this detector's.

**Check, more than one pulse per position:** this one is an inspector check,
not a reconstruction check. The layout gains a `repeat` loop and the frame
table pairs the frames at each position. Every scan reconstructor — BeadRec,
MoNaLISA standard and Fast Gauss — deliberately **refuses** a `repeat` loop,
because folding it would average unrelated frames into one pixel. That refusal
is the correct outcome for this half of the step, not a failure of it.

Pulses per position are now **declared, never defaulted**. A camera the scan
does not gate — no entry in the scan source's `getNumCamTTL()` — is refused at
arm with `DETECTOR_PULSES_UNDECLARED`. For the TriggerScope this has a visible
consequence: the basic pLS-RESOLFT and galvo-detection panels gained a
**Camera used for detection** selector (the multicolor panel already had one).
The firmware gates the camera on a fixed line in those modes, so the selector
does not choose the camera, it *declares* which detector receives that pulse.
**Do:** leave it unset once and confirm the recording is refused, with a
message naming the selector; then set it to the camera wired to the firmware's
camera line and record. On Snouty that is `OrcaStraight`, the Hamamatsu on
TTL3 — not `WidefieldCamera`, which free-runs and is never hardware-triggered
(see `triggerscope_firmware_quirks.md` §3).

**One mode needs more than the declaration.** The basic pLS-RESOLFT and
galvo-detection panels only *name* which detector receives the firmware's
pulse, so their selector lists every detector and nothing has to change in the
setup file. The **multicolor** panel also *programs* that line into the
firmware (`CameraTTLChan`), which it can only do for a detector whose setup
entry gives it `"digitalLine": "Triggerscope/TTL<n>"` — so its selector lists
only those. If your camera is missing from the multicolor panel's list, that is
the setup file to fix before the session, not a bug. Choosing a device with no
line elsewhere now fails with a message naming the device and the fix, rather
than a bare `KeyError` part-way through uploading the scan.
**Check:** the inspector's `lifecycle` block carries no
`DISCARDED_SURPLUS_FRAMES` warning. The inspector does not print the number
itself when it is zero; to read it:

```bash
python -c "import h5py,sys; d=h5py.File(sys.argv[1])[sys.argv[2]]['data']; [print(k,'=',d.attrs[k]) for k in sorted(d.attrs) if k.startswith('recording:')]" <file> <Detector>
```

A non-zero value means the detector produced frames the scan did not account
for, and the surplus was dropped. **Zero does not mean the opposite.** The
counter only sees a surplus that arrives in the same read as the last planned
frame; once the plan is met the detector is no longer read. An over-triggered
camera can finish as a `complete` recording with `discarded_frames = 0`, and
where the number is non-zero it is a lower bound rather than the size of the
over-trigger.

### 3.5 Scan-driven detectors

Detectors that assemble their own image are recorded as `assembled-image` with
axes mapping straight to the array.
**Check:** the inspector reports `payload: assembled-image` and no frame table,
and the image is not transposed.

**Save these as HDF5 or Zarr.** OME-TIFF has no axis for line steps and
projects the condition axis onto `T`; the layout survives and the resolver
reports the projection as lossy, but the container itself no longer says what
the extra axis was. The pixels and the layout are both recoverable, so a TIFF
recorded by mistake is not lost.

## 4. Recordings this branch will now refuse

This is the list most likely to interrupt your session. Each replaces a silently
wrong answer, but each turns a previously "working" action into an error.

| You do this | What happens now | Why |
|---|---|---|
| Record a multi-line-step scan with a detector that has no TTL line-step mask | **Recording is blocked** before any file is written | The producer cannot describe that detector's frame order; it used to describe it wrongly |
| Open a bead scan in BeadRec without Scan X/Y and without a *usable* layout | Error naming the unknown raster | It used to guess `sqrt(frames)` — 25×25 for 648 frames, silently dropping 23. An old file can carry a legacy layout and still hit this, because BeadRec declines an inferred layout that carries a loop it does not place, such as the rig's inactive `scan_z`. The message says "carries no acquisition layout"; on a legacy file read that as "no layout BeadRec can use" |
| Reconstruct a stack whose frame count disagrees with its layout | Error with expected/observed counts | It used to truncate or pad |
| Run SNOUTY on a stream that is not a whole number of timepoints | Error | It used to keep only the first timepoint |
| Run SMLM on a recording with a condition/channel/scan loop | Error naming the loops, unless you pass a selection | It used to flatten them into time |
| Analyse a STARSS file whose name has no `_h`/`_v` and no recorded role | Error | It used to assume H, silently inverting the anisotropy |
| Run Fast Gauss MoNaLISA on a recording whose detector was gated to *part* of the scan (complementary line steps), or whose conditions are not interleaved per line | Error pointing at the standard method | The fast path stacks contiguous X/Y blocks. It **does** de-interleave conditions per line — a detector enabled on *every* line step reconstructs normally — but it cannot represent a partial gating |
| Open a bead scan in BeadRec whose detector was gated more than one pulse per position | Error naming the unplaced `repeat` loop; manual Scan X/Y is **not** a workaround | Averaging repeats into one pixel is a fold BeadRec does not declare, and folding silently would mix unrelated frames |
| Tick *Save all timepoints in a single file* with format TIFF | Refused at REC | A grouped lapse file is reopened per timepoint, and TIFF cannot be reopened safely |
| Start a timelapse scan on a setup with several scan widgets without choosing a *Scan source* | Refused | A lapse starts its own scan, so there is no operator gesture to infer the scanner from |
| Open an **old** multi-line-step recording whose detector was gated onto only some line steps | Resolves — as of this branch | The per-detector mask is stored as JSON behind a sentinel that only `SharedAttributes` decoded, so the resolver never saw it and the file failed to resolve at all. Fixed; if one of these still fails, capture it |
| Open the MoNaLISA scan dialog on a file with no geometry | Fields are left alone | It used to pre-fill `sqrt(frames)` on both axes |
| Record a scan from a TriggerScope panel with no camera declared (pLS-RESOLFT, galvo detection) | **Recording is blocked** at arm, with a message naming the *Camera used for detection* selector | Producer and gate both defaulted an undeclared camera to one pulse per position and compared the default with itself |
| Record any other scan whose source gates no pulse for a selected detector | **Recording is blocked** at arm: `DETECTOR_PULSES_UNDECLARED` | Same defaulting, on the paths where the fix is to gate the detector or deselect it |
| Record a TriggerScope RESOLFT scan with a missing or unreadable `roSteps`/`cycleSteps`/`timeLapsePoints` | Error naming the counter | Each was silently read as `1`, arming for a fraction of the scan |
| Record an LS-XY-RESOLFT scan | Records with **no layout** (warning at arm), frames still counted as `rasterXSteps x rasterYSteps` | The firmware's raster loop order has not been traced; a guessed layout would be a certain, wrong one |

If one of these fires on a recording you believe is valid, that is a finding —
capture the file and the inspector output rather than working around it.

## 5. Suggested session order

Each step is independent; stop and capture at the first surprise.

0. **The scan that started this: multicolor pLS-RESOLFT, 10 cycles × 20 RO
   steps**, on `main` it aborted with *"readChunk consumer 'RecordingManager'
   fell behind by more than 1000 frames"* while 1 cycle × 200 steps worked.
   The cause was a sixteen-frame raw-queue cap meant for point detectors being
   applied to cameras; the branch fixes it, and the message now quotes the cap
   actually in force. Run exactly that scan first. **Check:** it completes;
   `recording:discarded_frames` is 0; and watch the log for the new
   *producer stall* warning. It fires when the acquisition loop sits blocked
   for a second handing frames to the writer, and it names the detector: the
   writer is not draining fast enough, so frames pile up in that detector's
   bounded chunk queue. On `main` the log showed a ~2 s gap before the abort
   that could not be attributed to either the writer or the second detector,
   and this is the line that will say which. It is a warning, not a failure;
   the scan still completes. If it fires, capture the log — a stall that is
   real but no longer fatal is worth knowing about before it grows.
1. **Plain point scan**, small, known sample. Record → inspect → BeadRec.
   Confirms geometry and pitch. Nothing in the GUI displays `geometry_source`,
   so check it by behaviour: leave *Scan X pixels* and *Scan Y pixels* at 0 and
   reconstruct — a correct image with both at 0 can only have come from the
   layout. Then type a deliberately wrong Scan X/Y: it must refuse with
   *"Scan X/Y is set to AxB, but this recording declares CxD"*. Set them back
   to 0. Gate the camera exactly one pulse per position for this step: zero is
   refused at arm, and more than one records a `repeat` loop BeadRec will not
   reconstruct. (In step 8 the same reconstruction on an *old* file needs
   Scan X/Y typed in and reports `manual` — that is correct, see there.)
2. **Advanced Scan, 2 line steps** (§3.2). Record → inspect → MoNaLISA.
   This is the motivating case.
3. **Complementary detectors** (§3.4).
4. **Bidirectional over ≥2 Z planes** (§3.3), if you scan bidirectionally.
5. **TriggerScope RESOLFT** (§3.1) → SNOUTY. Highest risk; worth doing even if
   short on time.
6. **Scan lapse**, ≥2 timepoints, **both storage modes**. Each item should show
   `partition: time index=N of M` and reconstruct identically. Do the
   *single file* mode as well as one-file-per-timepoint, not only if
   convenient: single-file lapse recordings could not be opened at all until
   this was found in preparation for this session, so the mode has never been
   read back from real data. Two things worth doing while you are there,
   because both were broken until this week and neither has met hardware:
   run one lapse with **Save on disk and keep in memory** (that combination
   used to abort at the second timepoint and delete the first), and run the
   same lapse **twice without restarting ImSwitch in between** — the guard
   against the second run appending into the first run's file lives on the
   running RecordingManager, so a restart would not exercise it.

   The file names are not what you would guess, so here they are, for base
   name `lapse`, detector `Camera`, HDF5:

   | mode | files |
   |---|---|
   | single file, M timepoints | `lapse_Camera.hdf5`, holding groups `scan0/Camera` … `scanM-1/Camera` |
   | one file per timepoint | `lapse_scan0_Camera.hdf5`, `lapse_scan1_Camera.hdf5`, … — the index sits *inside* the savename, zero-padded to the digit count of the total, so a 10-point lapse gives `lapse_scan00_Camera.hdf5` |
   | the same single-file lapse run again | a new file `lapse_Camera_1.hdf5`; that suffix goes at the end |

   Each item prints `partition   : time index=N of M  storage=one-group-per-item`
   for a single file, or `storage=one-file-per-item` for one file per
   timepoint. That `storage=` field is the only line in the whole output that
   distinguishes the two modes; everything else must be identical.
7. **A recording you stop early.** **Use recording mode *Scan once*.** Two
   modes will not show you this: stopping a *timelapse* takes the abort path
   and deliberately deletes the in-progress file, and an *Until stop* or
   *Specific time* recording has no planned count, so it finalizes as
   `complete` however you stop it. If your file vanishes when you press Stop,
   check the mode before reporting it.

   Do it twice, because they are different code paths: stop it **during** a
   scan, and stop it **before the first frame**.

   **Stopped during a scan.** Finalizes as `stopped_early`, opens and views
   under *View only*, and is refused by strict reconstructors. The refusal
   itself is generic — *"The stored data does not contain the complete declared
   acquisition layout"* — and does **not** name the numbers. The numbers are in
   the Parameters dock line just above it: *"Layout selects P frames but source
   contains S"*. The inspector says the same from the file: a `lifecycle` line
   with the outcome and the counts, and a frame table bounded by what was
   stored, followed by the positions that were never recorded. Typing Scan X/Y
   by hand does not get past the refusal, by design.

   **Stopped before the first frame.** Leaves a file with no image. In HDF5 and
   Zarr the container stamps itself and the inspector reports it under *the
   container describes itself as*: the detector, `completion_outcome =
   stopped_early`, `actual_frames = 0` and the planned count. **OME-TIFF cannot
   do this** — a TIFF with no page has nowhere to carry a description — so an
   empty OME-TIFF is a bare header and the inspector prints only
   `CANNOT LIST DATASETS`. That is a known limitation, not a fault. The
   inspector exits 1 on all three empty files, which is expected.
8. **An old recording** made before this branch. It must still open, and every
   legacy reading must say `authoritative: False` — an inferred layout may
   never refuse a reconstruction. That, not the confidence word, is the stable
   check. Expect one of: `scan-stage-legacy` (medium) for a plain scan,
   `advanced-scan-legacy` (medium) or `advanced-scan-legacy-assembled` (high)
   for a line-step scan, `snouty-legacy` (high) for RESOLFT, and `ome-ngff`
   (medium, a single `time` loop) for any old OME-TIFF — old TIFFs carry no
   scan metadata at all and their geometry cannot be recovered. Each names its
   assumptions under `issues:`.

   **`ome-ngff` with a `time` loop on a file you know was a scan means the
   stage metadata was rejected and the reason was swallowed** — capture that
   file. BeadRec on an old file reports `manual` and needs Scan X/Y typed in,
   because a legacy 2D scan on a three-positioner rig carries a count-1
   `scan_z` loop BeadRec declines to place; that is correct behaviour.

## 6. What to capture

For every recording:

- the raw file (or its path, if it stays on the rig),
- `python tools/inspect_acquisition_layout.py <file>` output,
- what the hardware was actually configured to do — scan size, step, line
  steps, pulses per position, which detector on which step,
- for reconstructions: a screenshot, whether it looks right, and for step 2
  the mean of each output `T` plane, which is falsifiable where an impression
  is not;
- **for step 8 only**, a dump of the legacy metadata the adapter read, which
  the inspector does not echo — on a file that degrades to `ome-ngff` it is the
  only evidence of which numbers were rejected:

  ```bash
  python -c "import h5py,sys; f=h5py.File(sys.argv[1]); [print(g, dict(f[sys.argv[2]+'/metadata/'+g].attrs)) for g in ('ScanStage','ScanTTL')]" <file> <Detector>
  ```

For anything that fails: the full error text. These errors are deliberately
specific and name the conflicting numbers; that text is usually enough to
locate the cause without the file.

For every scan recording, also the log lines from arm to finalize: the layout
gate logs what it skipped and why (a pulse cross-check it could not run, a loop
kind it could not classify), the worker warns once per detector about surplus
frames, and the producer-stall warning names the detector and the gap.

## 7. Known-unverified, beyond the above

- No hardware has ever run this branch.
- Live/streaming reconstruction is covered by software tests only; the live
  path's stack sizing changed (`ceil` → `round` for position counts), which
  matters only for non-divisible scan-size/step ratios — worth one live
  acquisition with e.g. a 0.52 µm axis at 0.05 µm steps.
- OME-TIFF round-trip is tested with synthetic files, not with Fiji/OMERO
  opening a real recording. If Fiji interop matters to you, open one recorded
  OME-TIFF in Fiji and confirm the axes and pixel size still read correctly.
- The `stopped_early` path has been driven end to end through the real
  recording worker and all three storers under the simulated DAQ, in both the
  short-file and the empty-file case. What has never happened is a stop against
  real hardware: what the detector and the scan do between the stop and the
  writer closing, and whether frames in flight in the bounded chunk queue reach
  the file.
