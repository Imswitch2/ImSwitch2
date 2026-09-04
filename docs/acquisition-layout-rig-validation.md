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

1. `git log --oneline -1` in the checkout the rig will run, and compare the
   commit with the one you mean to test.
2. The inspector's `interpreted by:` line (§2) on the first file you record.
   It says which ImSwitch actually read that file, which on a machine with a
   release install beside a working checkout is the question that matters.

If ImSwitch is installed rather than run from the checkout, reinstall it from
the checkout before the session. A session run against the wrong code produces
results that look valid and mean nothing.

---

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
python tools/inspect_acquisition_layout.py <recording> [detector]
```

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

The frame table is the check. Everything else in this branch follows from it. On the
motivating 18×18 two-line-step scan it should read:

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
the file.

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
the *same* row.

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
**Check:** each detector's frame count matches its recorded spans, and each
reconstructs only its own condition.

Pulses per position are now **declared, never defaulted**. A camera the scan
does not gate — no entry in the scan source's `getNumCamTTL()` — is refused at
arm with `DETECTOR_PULSES_UNDECLARED`. For the TriggerScope this has a visible
consequence: the basic pLS-RESOLFT and galvo-detection panels gained a
**Camera used for detection** selector (the multicolor panel already had one).
The firmware gates the camera on a fixed line in those modes, so the selector
does not choose the camera, it *declares* which detector receives that pulse.
**Do:** leave it unset once and confirm the recording is refused, with a
message naming the selector; then set it to the camera wired to the firmware's
camera line and record. If the camera is **not in the list**, that is a setup
question, not a bug: the selector lists detectors that declare a `digitalLine`,
so add the firmware's camera line to that detector's entry in the setup file
before the session rather than during it.
**Check:** `recording:discarded_frames` is 0 in the file. A non-zero value
means the detector produced more frames than the scan declared — free-running,
or pulsed more often than declared — and the surplus was dropped, with a
warning in the log.

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
| Open a bead scan in BeadRec without Scan X/Y and without a layout | Error naming the unknown raster | It used to guess `sqrt(frames)` — 25×25 for 648 frames, silently dropping 23 |
| Reconstruct a stack whose frame count disagrees with its layout | Error with expected/observed counts | It used to truncate or pad |
| Run SNOUTY on a stream that is not a whole number of timepoints | Error | It used to keep only the first timepoint |
| Run SMLM on a recording with a condition/channel/scan loop | Error naming the loops, unless you pass a selection | It used to flatten them into time |
| Analyse a STARSS file whose name has no `_h`/`_v` and no recorded role | Error | It used to assume H, silently inverting the anisotropy |
| Run Fast Gauss MoNaLISA on an interleaved line-step scan | Error pointing at the standard method | The fast path stacks contiguous X/Y blocks, which interleaved conditions are not |
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
   Confirms geometry, pitch and `geometry_source: layout`.
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
   read back from real data.
7. **A recording you stop early.** Should finalize as `stopped_early`, remain
   openable and viewable, and be refused by strict reconstructors with an exact
   count.
8. **An old recording** made before this branch. Must still open, with
   `source: *-legacy`, a lower confidence, and its assumptions listed.

## 6. What to capture

For every recording:

- the raw file (or its path, if it stays on the rig),
- `python tools/inspect_acquisition_layout.py <file>` output,
- what the hardware was actually configured to do — scan size, step, line
  steps, pulses per position, which detector on which step,
- for reconstructions: a screenshot and whether it looks right.

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
- The `stopped_early` path has never been exercised by a real interrupted
  acquisition, only by constructed metadata.
