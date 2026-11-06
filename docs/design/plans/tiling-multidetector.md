# Tiling: multi-detector, multi-channel (open tail)

**Status (2026-10-17):** Phases 0, 1, 1a and 2 are implemented on main (manifest
payload descriptions, multi-detector alignment image, the fixed chunk contract,
exact payload locators, ImProcess assembly with metadata-only sources, worker
dispatch, memory budget; durable documentation is `docs/tiling.rst`). The
detailed phase 0/1/1a/2 specifications and the per-revision history were removed;
git history has them.

**OPEN - Phase 1b session entry (verified 2026-10-17).** The per-tile recording
session is never opened: `TilingController.py:~1543` finds the Recording
controller via `self._commChannel.__dict__.get('_main', None)`, but
`CommunicationChannel` stores the main controller name-mangled as `__main`
(`_CommunicationChannel__main`), so the lookup always yields `None` and tiling
always falls back to compatibility snapshots. The public accessor is
`CommunicationChannel.controllerRegistry()`. Fix the lookup **together with**
explicit session startup/ownership; changing the lookup alone hands scan dispatch
to an inactive session and stalls the run. The test
`test_tile_layout_partition.py:~185` masks the defect with
`SimpleNamespace(_main=...)` and must be changed to the real accessor. End-to-end
rig validation of the full per-tile payload is blocked on this.

**OPEN - Phase 3** (3D alignment, deferred, incl. Snouty deskew-aware overlap) and
the section-end open questions (PMT display of summed line steps, per-tile
drain time, disk-cost projection before a run).


## Goal

Let a tiling run record everything that is on — several detectors, several
channels, a Z stack — while aligning on exactly one cheap image, and let
ImProcess put the pieces back together afterwards.

## What is already true

Findings from reading the code, not assumptions.

1. **The alignment stack is isolated from tile dimensionality.** Everything
   that aligns or stitches sees one 2-D array, `displayFrame`, which
   `_displayPlane` derives by max-projecting every axis before Y/X. The
   full-dimensional `frame` goes only to `_saveTile`. **Nothing in the
   alignment path changes for any of this.**

2. **`RecordingManager.snap()` already saves N detectors** in one call, taking
   its own lease over the set. It cannot carry a per-tile stage position, which
   is the one thing tiling needs from it. `snapImagePrev` can, but takes one
   detector and a pre-captured image.

3. **`RecordingController.getDetectorNamesToCapture()` already resolves what
   the operator wants saved.** `CommunicationChannel.getRecordingFolder()` is
   the established precedent for consuming such a decision from elsewhere —
   tiling already uses it for the output folder.

4. **Detector leases are reference-counted, not exclusive.**

5. **The manifest cannot describe a channel axis.** It records
   `tile_shape_px.depth` and `z_step_um`, so a line-step run's channel axis is
   recorded as though it were Z.


## Divergence: live vs. offline

**imcontrol (live)** stays simple: max-project everything before Y/X for the
overview, whatever the tile contains. The overview is for navigation and cell
targeting, not analysis.

**improcess (offline)** gets the flexibility: choose detector and channel, keep
every plane, eventually align in Z.

## What a tile consists of

Two artifacts, with different jobs. Revision 1 described these inconsistently —
"cheap proxy" but also "read with `is_save=True`", which for a point detector
means the full raw stack. Resolved as follows.

**The alignment image** is genuinely 2-D. It is `_displayPlane`'s max
projection: one plane, always, whatever the detector produced. It feeds the
overview, is what alignment is measured on, and is what the offline solve runs
on. Calling it cheap is then accurate.

**The payload** is the full measurement at that position — every plane, every
channel, every selected detector. Never correlated against; the geometry is
already solved from the alignment image.

Offline channel selection (Phase 2) operates on the **payload**, not on the
alignment image. That was the inconsistency: an image cannot be both a
2-D projection and a stack to select channels from.

Both are saved, and they are **separate files** — this needs stating precisely,
because revision 4 said the alignment image "stays a TIFF exactly as today",
which is false. Today `_saveTile(frame, ...)` writes the *full-dimensional*
array, so today's TIFF already carries the stack. If the alignment image simply
became that file in 2-D, every 3-D run would lose its stack the moment Phase 1
shipped and before payloads existed.

The artifact set is deliberately different in the two modes:

* **Free-running:** the full-dimensional snapshot for each selected detector
  keeps being written exactly as today and is that detector's payload. The
  alignment detector additionally gets the small 2-D alignment projection.
* **Triggered:** the finalized recording locator is the authoritative payload
  for each selected detector. The existing full-dimensional snapshots are
  retained as explicitly redundant compatibility artifacts for the current
  alignment-only/offline readers, but a version-2 reader must not mistake them
  for the payload when a finalized recording locator exists. The alignment
  detector additionally gets the small 2-D alignment projection.
* **Genuinely 2-D alignment detector:** its compatibility snapshot and
  alignment projection have identical content, so they may be one physical
  file with both descriptors pointing at it.

This duplication in triggered mode is an intentional compatibility cost, not
an accidental third measurement. Removing the redundant snapshots is a later
format-compatibility decision; Phase 2 reads the recording payload and the 2-D
alignment artifact, never the redundant snapshot as a substitute for either.
In a version-2 `TileRecord`, the legacy top-level `filename` names the alignment
detector's compatibility snapshot, `alignment.filename` names the 2-D
projection, and each `payloads[detector]` names either the free-running snapshot
or finalized triggered recording according to the rules above.

The cost of the additional 2-D alignment file is trivial next to what it buys:
a 273×273 plane is ~150 kB against a 21-plane stack at ~3 MB, and reading a
hundred of the former instead of a hundred of the latter is what keeps the
offline solve at fifteen seconds. The redundant triggered compatibility
snapshots are not trivial; the pre-run disk estimate must include them until the
legacy format is retired.

The existing offline path keeps working because the file it reads today is
still there and still has the same shape.

## Phases

### Phase 1b — full payload per tile (open)

**Scoped to triggered tiling.** With the chunk contract fixed there is one
design for every *scan-driven* payload — but the protocol below starts a scan,
and free-running tiling has no scan source to start. Rather than invent a
second cadence, free-running keeps the Phase 1 path: its payload is the
multi-detector snap, which is also all a free-running camera can meaningfully
give per tile. There is no stack to stream when nothing defines one.

This is why Phase 1 still extends `snapImagePrev` to take several images even
though the artifact model otherwise has a single alignment TIFF: that path is
the free-running payload, not just a convenience.

If a free-running camera ever needs several frames per tile — averaging, or a
short burst — the way in is an externally cadenced recording point that arms
after positioning and captures fresh frames *without* `_requestScanStart()`.
That is a real extension, not a variant of the protocol below, and it should
not be smuggled in as one.

**One scan dispatcher.** Today there are two: `_TriggeredTileSource._runScan`
calls `run_scan_from()`, and `RecordingController.nextLapse` calls
`_startManagerRecording()` then `_requestScanStart()`. Both arming the same
scan is a defect, not a detail. **RecordingController should be the sole
dispatcher**, with an explicit protocol:

1. Recording *requests* positioning and gets a future back. It does not wait.
2. Tiling moves and settles on its own worker thread, then resolves the future.
3. Recording arms the manager and starts the scan **only once the future has
   resolved successfully**.
4. Recording returns an exact completion to tiling, which then does the focus
   reacquisition wait and captures the alignment image.

Tiling supplies the cadence and the position; it must stop starting scans.

**The positioning callback must be asynchronous.** `nextLapse` advances from Qt
timers on the GUI thread, while tiling's stage moves and settle waits run on a
background worker. A synchronous callback would block the event loop for the
whole move-and-settle, at every tile — starving exactly the timers the focus
lock runs on. That failure mode has already been paid for once in this widget,
when the focus estimator ran on the GUI thread and the lock silently stopped
holding for an entire run.

So the protocol is a request/future with four terminals — **resolved, failed,
cancelled, timed out** — and recording arms on the first only. Cancellation
matters because the operator can stop a tiling run mid-move, and a timeout is
needed because a stage that never reports arrival must not wedge the session
forever.

**Failure policy, corrected.** Revision 1 said "a failed recording should not
abort the run". That cannot be inherited: `_handleRecordingFailure` sets
`stopRequested = True`, aborts the owned scan and terminalises the session.
Blindly continuing past an acquisition or hardware failure is also unsafe. The
distinction to implement:

* *Writer* failures affecting one payload — recoverable. Mark that tile's
  payload missing, continue the trajectory, report at the end.
* *Acquisition, scan or hardware* failures — not recoverable. Stop, as the
  existing lifecycle already does.

**This needs typed failures to be implementable at all.** The detailed failure
signals currently carry a message and a generation, nothing more — so under the
rule above *every* failure is unclassifiable and the policy collapses to "always
stop", which is today's behaviour with extra code. A structured kind
(`WRITER`, `ACQUISITION`, `SCAN`, `HARDWARE`) has to be added at the point the
failure is raised; classifying by matching message text would be worse than not
classifying at all.

**Storage exhaustion is not recoverable by default.** Revision 2 listed disk-full
as a writer failure to carry on from, which is wrong: the next payload and the
next alignment TIFF will fail the same way, and the run would grind through
ninety-odd tiles producing nothing but errors. Continuing past a storage failure
should require either a successful storage recheck or an explicit best-effort
mode the operator asked for.

Anything that cannot be classified is treated as not recoverable.

**Recording locators.** A filename does not identify a payload: grouped
HDF5/Zarr needs the group or series, separate files may be de-duplicated, and
successful terminals currently carry only a generation number. Each tile needs

```json
{"path": ..., "group": ..., "detector": ..., "axes": ..., 
 "generation": ..., "complete": true}
```

returned **authoritatively after writer finalisation**, not guessed at
dispatch. `snapImagePrev` already had to learn this lesson — it returns the
paths it wrote because guessing them produced a manifest pointing at files that
did not exist.

**One-file lapse is HDF5/Zarr only.** TIFF cannot safely reopen a grouped lapse
file; `RecordingController` already raises exactly this for camera timelapse,
and the same rule applies here rather than a new one.


### Phase 2 - ImProcess assembles it (implemented)

See `docs/tiling.rst`. The locator, alignment-geometry, composition,
memory-budget and failure-matrix specification was removed once implemented.

### Phase 3 — 3D (deferred)

The link graph and the least-squares solve are dimension-agnostic, so confocal
tiles on a rectilinear grid mostly need a three-component shift per pair.

**SNOUTY is a different problem.** A Snouty tile is not a volume until it has
been deskewed, so correlating before deskew compares two shears; after deskew
the volumes have empty corners, and the overlap between two tiles stops being a
box intersection — which is precisely what `_overlap_box`, and therefore the
whole solver, assumes. Tractable with an overlap mask, but a different shape of
problem, and it belongs in ImProcess where the deskew already lives.

## Open questions

* **Should the PMT still *display* a summed line step?** Phase 1a stops that
  decision affecting saved data either way, so this is now only about what the
  operator sees while scanning. Summing is a defensible thing to look at; it
  was only ever harmful because it also decided what was recorded.
* **Per-tile time.** A recording session must drain before the stage may move.
  Measure what that adds per tile before committing to a hundred of them.
* **Disk cost.** N detectors × full stacks × 100 tiles is a different order of
  magnitude from today's 15 MB. Project it before a run starts, the way
  `assemble` now reports memory before allocating it.

