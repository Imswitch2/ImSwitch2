# Tiling: multi-detector, multi-channel, and where the two halves diverge

**Status:** reviewed and approved; implementation started at Phase 0. Revision 6. Work in progress — durable
documentation belongs in `docs/tiling.rst` once this lands.

Revision history worth keeping, because two claims here were wrong before they
were right:

* **Rev 3** corrected rev 2's claim that a streaming recording of a point
  detector retains only line step 0. It does not; see the chunk audit.
* **Rev 4** corrects rev 3's compatibility check, which proposed validating an
  identity transform by comparing properties that cannot establish one, and
  scopes Phase 1b to triggered tiling — its protocol always started a scan,
  which free-running tiling has none of.
* **Rev 5** corrects rev 4's claim that the alignment image "stays a TIFF
  exactly as today" — today's tile file holds the *full-dimensional* frame, so
  the alignment image is an additional file rather than a redefinition of that
  one. It also adds the free-running multi-camera capture contract, which
  earlier revisions solved the saving half of and not the acquiring half.
* **Rev 6** corrects revs 3-5's claim that a recording always writes a leading
  `T`. `axes_for_recording` labels a scan with `Nz > 1` as `ZYX` with no `T`;
  `stored_axes` is whatever the finalized writer reports.

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

### 6. The chunk contract is the constraint everything else bends around

Streaming recording does not read `getLatestFrame`. It reads `getChunk()`,
which for both point detectors returns whatever is in `_image_display`:

```python
# APDManager.getChunk / PMTManager.getChunk — identical
return np.expand_dims(self._image_display, axis=0).copy()
```

**What that buffer holds differs per detector, and is a side effect of a
display setting.** `_image_display` is allocated from
`ScanWorker._output_image_dims`, which *does* append the line-step axis —
`(Nx, Ny, S)` for a 2-D line-step scan — so the reduction loop in
`_onFrameBoundary` has nothing to truncate. What survives is decided by
`_linestep_view_mode`:

* **APD never assigns it.** It defaults to `None`, which falls through to
  `im = raw`, so every line step reaches `getChunk()` intact. No loss.
* **PMT sets `"sum"` in its constructor.** Every line step is summed into one
  plane before any consumer sees it. The channels are not selectable
  afterwards — they are added together, irreversibly.

So the problem is not that the payload is universally lost. It is that **what a
chunk contains is a per-detector display preference that no consumer can ask
about or override.** A recording gets the APD's channels by luck and loses the
PMT's by default, and neither outcome is expressed anywhere in the contract.

**The PMT's summing is a display setting that leaked into the record path.**
The two managers' reduction blocks in `_onFrameBoundary` are identical; the
sole difference is one line in PMT's constructor, under the comment
*"linestep settings (kept for manager-side display logic)"*. The setting exists
for `_compute_display_frame`, documented as *"Build a 2D plane (Ny, Nx) from the
raw buffer. Keeps future flexibility: sum/max/slice over linestep."* — a display
concern. `_onFrameBoundary` then consults the same attribute to fill
`_image_display`, and `getChunk()` publishes that to recordings. Note that
`_onFrameBoundary` reads it as `getattr(self, "_linestep_view_mode", None)`,
whose `None` branch keeps the raw stack: the intended default there was to keep
everything, and only the constructor assignment overrides it.

Separating the two representations fixes this structurally — raw stays raw
whatever the view mode is set to. Whether the PMT should also *display* a sum
is then a separate and much smaller question, since it would no longer decide
what gets saved.

### The full `getChunk` audit

Every implementation, checked rather than assumed:

| Manager | Returns | Frame axis | Note |
| --- | --- | --- | --- |
| APD | `expand_dims(_image_display, 0).copy()` | yes | view mode unset → raw stack survives |
| PMT | `expand_dims(_image_display, 0).copy()` | yes | view mode `"sum"` → line steps collapsed |
| SwabianTimeTagger | `_image_display.copy()` | yes | buffer is allocated `(1, Ny, Nx)`, so already 3-D |
| Hamamatsu | `camera.getFrames()[0]` | yes | opaque below the manager |
| Basler | `expand_dims(getLastChunk(), 0)` | yes | may return `None`; broker guards it |
| JetsonCam | `expand_dims(getLastChunk(), 0)` | yes | |
| TIS | `frame[np.newaxis, :, :]` | yes | empty case shaped from the last frame |
| ThorCamTSI | drained frames | yes | documents the 3-D contract explicitly |
| AV, ESP32Cam, GXPIPY, PiCam | `camera.getLastChunk()` | opaque | rank owned by the camera layer |
| Photometrics | a Python **list** of 2-D frames | n/a | declared `ndarray`; works because the broker only uses `len()`, `extend` and `[-1]` |

**Only APD and PMT need a `drainChunk` override** *for the ndarray payload*.
For every camera the chunk already *is* the frames, so the default
implementation returning the same object for both kinds is correct, not merely
convenient.

**The Swabian is a deliberate exception, not a camera.** Its `getChunk()`
publishes `_image_display` like the other point detectors, but its actual
measurement — the binned TCSPC cube, the gates, the intensity and lifetime
products — lives behind a separate contract entirely
(`waitForFinalTimeResolvedProducts()` returning a structured
`TimeResolvedScanProducts`, not an array). A `ChunkPayload` of two ndarrays
cannot represent that, and pretending otherwise would silently record the
intensity image and drop the time-resolved data that is the whole point of the
detector.

So: **time-resolved products are explicitly out of scope for "full payload"**,
and a tiling run using the Swabian records its intensity image and says so. If a
time-resolved tiling run is wanted later, the way in is a detector-supplied
payload contract — `TimeResolvedScanProducts` is already the model for what one
looks like — not a wider ndarray.

Two contract drifts found, neither a live defect: Photometrics returns a list
where the abstract method promises an array, and Swabian's *empty* chunk is
`np.empty((0, 0))` where the others use `(0, 0, 0)` — harmless only because the
broker short-circuits on `len() == 0`. Worth tightening while the contract is
open, since Phase 1a is the moment it gets specified properly.

### Which consumers want which kind

| Consumer | Reads | Wants | Change |
| --- | --- | --- | --- |
| `RecordingManager._getNewFrames` | `readChunk` + `np.stack` | **RAW** | the only switch |
| BeadRec | `readChunk` + `_toDisplayedFrames` | DISPLAY | none — it applies the display transform on purpose |
| Workflow facade `get_frames` | `readChunk` + `asarray` | DISPLAY | none |
| Tiling `_grabSettledFrame` | `readChunk`, counts and takes newest | DISPLAY | none — freshness handshake only |

So exactly one consumer moves to `RAW`, and every other caller keeps today's
behaviour by default. Worth noting that BeadRec's comment already claims
"readChunk returns raw frames", which is not true for a point detector today —
after Phase 1a that comment becomes a choice rather than a misdescription.

The save hint has a harder problem — it does not survive a chunk consumer at
all:

```python
# DetectorManager.getLatestFrameShared
if self._chunkConsumers:
    newFrames = self.getChunk()        # is_save never consulted
    ...
    return self.__image
frame = self.getLatestFrame(is_save=is_save)
```

A recording registers a chunk consumer. So starting a recording on a detector
also degrades what *tiling* reads from that detector, in the same run. The
light tile and the payload cannot be reasoned about independently.

**The write side is already correct.** Both storers derive their dataset shape
from the frames and explicitly expect the extra axis:

```python
# ZarrStorer / HDF5Storer
# Scan-driven point detectors may include a linestep plane axis: (N, S, Y, X).
spatialShape = frames.shape[1:]
```

So the gap is entirely at the chunk source: the storers were built for raw
multi-axis chunks that the detectors never send. Fixing the contract needs no
storer work, which is what makes doing it properly affordable.

**The hard constraint on any fix:** `getChunk()` is a *destructive* drain — that
is the whole reason `readChunk()` exists. So a raw representation cannot be
obtained by calling it a second time. One drain must produce every
representation that is wanted, and the fan-out must route each consumer the one
it asked for.

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

So:

* The **full-dimensional tile keeps being written exactly as today.** Nothing
  regresses, and it doubles as the payload wherever no recording is made — which
  is the whole free-running case.
* The **alignment image is an additional small file**, the 2-D projection, in
  the same OME-TIFF format. For a genuinely 2-D detector it is the same content
  as the tile, so one file is written and both descriptors point at it.

The cost of the extra file is trivial next to what it buys: a 273×273 plane is
~150 kB against a 21-plane stack at ~3 MB, and reading a hundred of the former
instead of a hundred of the latter is what keeps the offline solve at fifteen
seconds.

The existing offline path keeps working because the file it reads today is
still there and still has the same shape.

## Phases

### Phase 0 — let the manifest say what each file contains

The two artifacts are **named separately**, not merged into one
detector-keyed map. They have different lifetimes, different producers and
different locator shapes, and a single `files` map hid that:

```json
{
  "alignment": {"detector": "APDred", "filename": "tile_x+00_y+00.ome.tiff",
                "axes": "YX"},
  "payloads": {
    "APDred": {"path": "payloads/tile_000.h5", "group": "scan0/APDred",
               "axes": "CZYX", "shape": [4, 21, 273, 273],
               "stored_axes": "TCZYX", "generation": 17, "complete": true,
               "transform_to_alignment": "identity"},
    "Camera": {"path": "payloads/tile_000.h5", "group": "scan0/Camera",
               "axes": "ZYX", "shape": [21, 512, 512],
               "stored_axes": "ZYX", "generation": 17, "complete": true,
               "transform_to_alignment": "identity"}
  }
}
```

The rule the example follows: **`axes`/`shape` describe what a reader should
end up with; `stored_axes` describes what is on disk.** The two differ only
when something must be squeezed — here the APD's leading `T` — and are equal
otherwise.

**`stored_axes` is reported by the finalized writer, never assumed.** Revisions
3 and 4 both asserted that a recording always writes a leading `T`; it does not.
`axes_for_recording` already decides this from mode and dimensions, and a scan
with `Nz > 1` is labelled `ZYX` with no `T` at all — a behaviour its test matrix
pins deliberately. So the descriptor records what the writer says it wrote,
which is also why the locator is only authoritative after finalisation.

* Exactly one alignment image per tile, from one named detector. It is the
  thing the layout is solved on, so it cannot be a map.
* Payloads are keyed by detector and carry a **locator**, not a filename —
  see Phase 1b.
* `axes`/`shape` describe the **logical** array a reader should expect;
  `stored_axes` describes what is actually on disk, as reported by the finalized
  writer. The two are equal unless something must be squeezed, and a reader is
  told which rather than left to guess from a length of one.
* **`path` is relative to the manifest**, and both artifacts live under the run
  folder. Today tiling creates its own run folder while RecordingController
  writes into the Recording widget's folder directly — if that is left alone,
  every payload locator becomes an absolute path out of the dataset, and moving
  or archiving the run breaks all of them. The run folder must therefore be
  allocated *before* the first recording starts, and the session pointed into
  it, rather than tiling adopting whatever the recording chose.
* `MosaicDataset` carries the descriptors; the reconstructor labels result axes
  from them instead of assuming the leading axis is Z.
* `z_step_um` stays meaningful only when a Z axis is present.
* Back-compat: a manifest with a bare `filename` and no descriptors reads
  exactly as today.

**N-dimensional assembly belongs to this phase**, not to a later "either/or".
Phase 2 requires CZYX payloads and channel selection, so it is mandatory rather
than optional, and Phase 0 is where the data model that needs it is defined.
Today `_squeeze_leading` drops leading singleton axes — silently destroying a
length-1 channel axis, and destroying a recording's leading `T` without being
asked — and `assemble()` supports exactly one optional leading axis:

```python
shape = (depth, height, width) if depth > 1 else (height, width)
```

**Acceptance criteria for Phase 0:**

1. `assemble()` places tiles of arbitrary leading rank, with the mosaic's
   leading axes taken from the descriptor rather than inferred.
2. A length-1 channel or Z axis survives a load/assemble round trip.
3. A `CZYX` dataset assembles, and its result carries `["C", "Z", "Y", "X"]`.
4. A manifest with no descriptors produces byte-identical output to today.
5. A descriptor whose `stored_axes` equals its `axes` is read with no squeeze —
   the camera case, `ZYX -> ZYX`.
6. A descriptor whose `stored_axes` carries a leading axis its `axes` does not
   is squeezed on exactly that axis — the APD case, `TCZYX -> CZYX` — and fails
   loudly rather than silently if that axis is not length 1.

### Phase 1 — the alignment image, corrected and multi-detector aware

* `TilingController` gains a *save set* from `getDetectorNamesToCapture()` via a
  new `CommunicationChannel.getRecordingDetectors()`, alongside its existing
  single **alignment** detector (relabel the Detector combo to say so).
* Per-tile multi-detector save carrying `stagePositionUm` — extend
  `snapImagePrev` to take `{detector: image}` rather than re-reading frames.
  **This is the free-running payload path**, not merely a convenience: in
  free-running mode there is no recording, so these images *are* the payload.

**Saving multi-detector is the easy half; capturing it is the contract.**
Extending `snapImagePrev` says nothing about where those images come from.
Today exactly one detector — the alignment camera — is leased
(`acquire([camera], LeasePurpose.WORKFLOW)`) and given the fresh-frame
handshake. A second camera would have neither, so it would hand back whatever
was last in its buffer: a frame from the previous tile, or one exposed while
the stage was moving. Silently misregistered, exactly the failure the
handshake exists to prevent.

The free-running capture contract:

1. **Pin the save set at run start.** It must not change mid-run — the manifest
   describes one set of detectors for the whole dataset.
2. **Reject what cannot participate.** A scan-driven detector cannot be tiled
   free-running and already says so for the alignment detector; the same
   refusal must cover the save set, along with externally triggered detectors
   whose clock the run does not control.
3. **Lease every selected camera for the whole run**, not just the alignment
   one. Leases are reference-counted, so this composes with anything else
   holding them.
4. **Open a fresh-frame boundary per camera after each move**, then require the
   same two-frames-past-the-boundary proof the alignment camera gets.
5. **Capture every image while the stage is still stationary**, then hand the
   completed dictionary to `snapImagePrev`. No saving may begin before the last
   camera has produced its frame.

The per-tile cost is set by the slowest camera in the set, which is worth
saying out loud before someone selects a 5 fps camera alongside a 100 fps one.
* Manifest bumps to `imswitch-tiling/2` with the per-file descriptors above.

**Compatible detectors only, enforced not assumed.** Equal pixel size does not
establish registration: sensor origin, crop, ROI, orientation, rotation and
optical-path offsets all differ independently, and a shared stage position
establishes the *tile grid*, not pixel-level agreement between detectors.
Revision 3 then proposed checking pixel size, Y/X shape and orientation — which
does not follow from that paragraph and does not prove what it claims. Two
detectors can pass all three and still be offset by a different sensor origin,
a different ROI within the sensor, or an optical-path offset. Equality of those
three properties is necessary and nowhere near sufficient.

**So the transform must be declared, not inferred.** Each detector in a save set
carries a configured transform relative to the alignment detector, and a
declaration of `identity` is a statement the rig owner makes — one that can be
wrong, and that they are responsible for. Absence of a declaration **fails
closed**: the save set is refused rather than assumed aligned. The three
equality checks stay as a cheap contradiction test — a detector declaring
identity while reporting a different pixel size is certainly misconfigured — but
they are a guard against obvious error, not evidence of registration.

**The declaration is persisted, not just checked.** Each payload descriptor
carries `transform_to_alignment`, so a dataset records how its detectors were
believed to relate rather than leaving a reader to re-derive it from a rig
config that may since have changed. Concretely:

* **Phase 1 accepts only `"identity"`.** Anything else is refused at run start,
  before a single tile is acquired.
* **A missing declaration fails closed** — refused, not assumed identity.
* **Phase 2 composes** the solved tile placement with each detector's persisted
  transform. Under Phase 1 that composition is with identity and therefore a
  no-op, but writing it as a composition from the start is what makes calibrated
  transforms a fill-in later rather than a rewrite of the assembly path.

That is the whole point of declaring it: the calibrated cross-detector
registration that comes later supplies a value, and nothing else has to move.

### Phase 1a — fix the chunk contract (prerequisite for 1b)

Decided: do this properly rather than route around it. It removes the
detector-kind split, makes `is_save` mean something everywhere, and repairs
line-step recordings whether or not tiling ever uses them.

**One drain, both representations.** A new optional method on
`DetectorManager`, with a default that preserves today's behaviour exactly:

```python
class ChunkKind(enum.Enum):
    DISPLAY = 'display'      # what the viewer and the overview want
    RAW = 'raw'              # what a recording wants: every axis, no reduction

def drainChunk(self) -> ChunkPayload:
    """One destructive drain, every representation a consumer may want."""
    frames = self.getChunk()
    return ChunkPayload(display=frames, raw=frames)   # identical for cameras
```

* **Every camera is unchanged and needs no override** — for a camera the chunk
  really is the frames, so display and raw are the same object, not a copy.
* **APD and PMT override it.** `getChunk()` stays abstract and stays the
  display path, so any direct caller keeps working.

**RAW and DISPLAY must publish on different schedules.** This is the subtlest
part of the contract and the easiest to get wrong. `_onFrameBoundary` is
connected to `ScanWorker.d3Step`, which fires at `dim == 3` — **once per Z
plane**, not once per scan. `_image` is therefore a volume being progressively
filled, and a naive `drainChunk` would publish it half-written.

That is not a theoretical race. `_framesToRecordFor` returns **1** for a
scan-driven detector in `ScanOnce`/`ScanLapse`, so the recording would accept
the very first partial volume, consider itself complete, and ignore every later
update. The result is a payload with plane 0 written and the rest zeros — and
nothing anywhere would report an error.

So the contract is asymmetric:

* **DISPLAY** publishes at every boundary. That is what makes the live view fill
  in plane by plane, and it must not change.
* **RAW** publishes *nothing* until the final boundary of the scan, then exactly
  one complete `(1, C, Z, Y, X)` frame. A drain before that returns an empty
  chunk, which the broker already handles.

There is precedent for exactly this shape in the tree: `SwabianTimeTaggerManager`
already distinguishes provisional updates from a final authoritative product
set, via `_tr_final_event` and `waitForFinalTimeResolvedProducts()`. The raw
chunk should generalise that pattern rather than invent a second one.

**It cannot be driven off a frame event, because there may not be another one.**
`_completedScanGenerations.add(generation)` runs inside a `finally:` during scan
teardown — *after* the last `_onFrameBoundary`. Waiting for "the next boundary
after completion" would wait forever on the final volume, which is the only one
that matters.

So the raw side needs its own small state machine, not a flag:

```
rawGeneration   # which scan the buffered volume belongs to
rawReady        # that generation reached a terminal state; the volume is whole
rawDelivered    # it has already been handed out
```

`drainChunk()` emits the completed raw buffer **exactly once**, when its
generation becomes terminal — independently of whether the display chunk is
empty, and independently of any further frame arriving. Repeated polling after
delivery yields empty, not a duplicate: a recording that receives the same
volume twice is as wrong as one that receives it half-written.

Cancellation is part of the contract, not an afterthought: a generation that
ends by abort must set `rawDelivered` without ever publishing, so a stopped scan
cannot leak a partially filled volume into a recording.

**Phase 1a acceptance tests** — this is the part most likely to look correct and
be wrong, so it is specified as tests rather than prose:

1. RAW is empty at every intermediate Z boundary of a multi-plane scan.
2. Exactly one complete `(1, C, Z, Y, X)` RAW frame appears after completion,
   with no zero-filled planes.
3. A RAW consumer and a DISPLAY consumer registered concurrently each receive
   their own representation, and neither starves the other.
4. Polling RAW repeatedly after delivery yields empty — never a second copy.
5. After an empty drain, `getLatestFrameShared(is_save=True)` still returns the
   last raw frame and `is_save=False` the last display frame; the two caches do
   not overwrite each other.
6. A cancelled or aborted scan publishes no RAW frame at all.

**The raw chunk must carry a frame axis.** Returning `_image` as-is would be a
bug: `_distributeChunkLocked` fans out with `list.extend()`, so axis 0 is the
*frame* axis by convention, while the point detectors allocate `_image` with no
such axis — `(S, Ny, Nx)` for a line-step scan. Extended into a consumer queue
that becomes **S separate frames**, and a downstream reader would take the
first as a whole frame. The display path already adds the axis
(`np.expand_dims(self._image_display, axis=0)`); the raw path must do the same:

```python
raw = np.expand_dims(np.array(self._image, copy=True), axis=0)
```

The copy is not optional either — `_image` is the live accumulation buffer the
scan is still writing into.

**Two latest-frame caches, not one.** `getLatestFrameShared` currently caches
one frame from the drain (`self.__image = newFrames[-1]`). With two
representations it needs one cache per kind, or a subsequent empty drain
answers `is_save=True` with whatever the display consumer last saw. Same bug as
the bypass, one level down.

**Consumers declare what they want**, defaulting to today's behaviour:

```python
startChunkConsumer(key, kind=ChunkKind.DISPLAY)
```

`_distributeChunkLocked` routes each consumer the representation it registered
for, from the single drain. The recording worker registers `RAW`; the viewer,
the focus lock and tiling's fresh-frame handshake stay `DISPLAY`.

**`getLatestFrameShared(is_save=True)` stops lying.** In the chunk-consumer
branch it returns the raw frame when asked to, instead of silently handing back
a display plane because someone else happens to be recording.

Two things to get right, neither of which is a blocker:

* **Queue memory.** A raw frame for a line-step 3-D APD is `S x Nz` planes where
  the display frame is one. `MAX_QUEUED_CONSUMER_FRAMES` caps frame *count*, so
  the same cap means a very different number of bytes for a raw consumer. Cap
  raw consumers by bytes, or lower their frame cap and say why.
* **Rank heterogeneity.** Raw frames may be 4-D where display frames are 3-D;
  `readChunk` returns a list that is `np.stack`ed by the caller, and at least
  one manager documents a 3-D assumption. The storers already handle it; the
  broker path needs checking rather than assuming.

### Phase 1b — full payload per tile

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

### Phase 2 — ImProcess assembles it

* `load_dataset` groups tiles by detector and resolves each to its alignment
  image or its payload.
* **Solve the layout once**, on the alignment images, then apply that geometry
  **composed with each detector's persisted `transform_to_alignment`** to every
  payload. With Phase 1's declared identity the composition is a no-op; writing
  it as a composition anyway is what lets calibrated transforms drop in without
  touching assembly. Everything at one tile shares one
  stage position, so it shares one layout by construction. Solving on the
  alignment images is also what keeps this affordable — correlating full stacks
  would multiply the current fifteen seconds for no additional information.
* Channel and detector selection live here, on the payload.

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
