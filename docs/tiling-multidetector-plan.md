# Tiling: multi-detector, multi-channel, and where the two halves diverge

**Status:** Phases 0, 1, 1a and 1b implemented (not rig-validated). Phases 2a
through 2e are implemented: finalized writers publish exact payload locators;
alignment-only geometry is a cached, detector-independent value keyed by tile
identity; and selected payload assembly now supports named C/Z reduction,
identity/affine composition, strict partial handling and provenance while
materializing one tile at a time. Tiling runs now open in ImProcess as
metadata-only sources, populate detector/channel/Z/completeness and memory
estimates without loading payloads, guard image-only controls, revalidate the
manifest before work, and preserve provenance in saved OME-TIFF metadata.
Tiling reconstruction now opts into generic worker dispatch with named-phase
progress, cooperative cancellation, GUI-thread memory confirmation and an
exact pre-allocation budget check; other reconstructors remain inline by
default. Phase 2f is next. Revision 9. Work in progress — durable
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
* **Rev 7** adds the Phase 2 specification, and records that the projection
  defect there is a max rather than a sum, that the reader's locator type
  collides by name with the acquisition-side one, and that reconstruction runs
  on the GUI thread — which Phase 2 makes considerably more expensive.
* **Rev 8** closes Phase 2's remaining implementation choices: the read-side
  locator is `ManifestPayloadRef`; metadata-only sources have an explicit
  `DataObj` lifecycle; affine resampling has pixel, bounds and mask semantics;
  reconstruction runs on a cancellable worker; and triggered versus
  free-running compatibility artifacts are enumerated separately.
* **Rev 9** leaves the door open for the forthcoming transform module: the
  transform becomes a schema rather than a bare string, the manifest's copy is
  descriptive rather than authoritative so a later calibration can be applied
  to an older run, and parsing lives in one shared place. Also records that a
  mosaic uses one placement path throughout, which is what makes the affine
  path's dtype rule safe to state.

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

### Phase 0 — let the manifest say what each file contains

The two artifacts are **named separately**, not merged into one
detector-keyed map. They have different lifetimes, different producers and
different locator shapes, and a single `files` map hid that:

```json
{
  "filename": "tile_x+00_y+00_APDred.ome.tiff",
  "alignment": {"detector": "APDred",
                "filename": "tile_x+00_y+00_alignment_APDred.ome.tiff",
                "axes": "YX", "shape": [273, 273], "stored_axes": "YX"},
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

Phase 2 is not just "let `load_dataset` take a detector name". The current
reader combines four jobs which have different inputs and lifetimes: parsing the
manifest, reading pixels, solving positions and assembling the result. That was
adequate while one tile file was both the alignment image and the measurement,
but it gives the wrong architecture for full payloads:

* `_read_image` searches an HDF5/Zarr container for the first array instead of
  following a locator's exact `group`;
* selecting a detector loads that detector first and `refine_layout` therefore
  correlates its payload, not the alignment images. This is live today:
  `load_dataset(..., detector=X)` returns X's arrays and the reconstructor hands
  exactly those to `refine_layout`, so **the geometry a run produces depends on
  which detector was picked** — two detectors from one acquisition can come out
  on different layouts, which is the one thing a shared stage position was
  supposed to guarantee against;
* the detector picker exists, but nothing populates it from the current run;
* **Project volumes to 2D** max-projects *every* leading axis
  (`mosaic.max(axis=tuple(range(mosaic.ndim - 2)))`), so a `CZYX` mosaic
  collapses its channels along with its planes. Not the PMT bug's summation,
  but the same class of error: an axis nobody asked to collapse is collapsed
  because it happens to precede Y/X; and
* every selected payload is materialized before the output canvas is allocated,
  which is the wrong peak-memory shape for `CZYX` tiles.

The implementation should separate **indexing**, **geometry** and **payload
assembly**. The public compatibility wrapper can remain, but those must be real
internal boundaries rather than flags on one increasingly ambiguous loader.

#### Phase 2 invariants

1. **One manifest entry is one tile identity.** Until the manifest grows an
   explicit ID, its zero-based entry index is the stable identity. Filenames are
   not identities: several tiles in a one-file lapse share a path and differ
   only by group.

   The identity is assigned while **parsing**, before anything is read and
   therefore before anything can be skipped. Today's loader drops unreadable
   tiles from its list, which silently renumbers every tile after the gap — so
   an index taken after loading is not an identity at all, and a layout keyed by
   one would shift the moment a single file went missing. Indexing must include
   entries it cannot read, marked unreadable.
2. **Geometry belongs to the run, not to a detector output.** A layout is a map
   from tile identity to an alignment-grid position. It is solved from the
   singular alignment artifacts and then frozen.
3. **Payloads never participate in correlation.** Their intensity distribution,
   rank and resolution are allowed to differ. Reading a payload must happen
   after the layout has been solved or recovered from cache.
4. **The output grid is the alignment detector's Y/X pixel grid.** A detector's
   local pixels enter that grid through its persisted
   `transform_to_alignment`; leading axes are carried along unchanged.
5. **No guessed container traversal for version-2 payloads.** `path`, `group`,
   `stored_axes`, `axes`, `shape` and `complete` are a contract. A reader may use
   legacy discovery for a legacy manifest, but must not silently substitute it
   for a malformed current locator.
6. **Partial means declared partial.** A payload marked incomplete may be skipped
   with a report. A payload marked complete but contradicting its locator or
   descriptor is corruption and fails loudly.

#### Target internal model

Add a manifest-only index in `imcommon.algorithms.tile_mosaic`; it must not open
or materialize image arrays:

```python
@dataclass(frozen=True)
class AlignmentArtifact:
    path: Path                 # resolved from alignment.filename
    detector: str
    axes: str
    stored_axes: str
    shape: tuple[int, ...]
    stored_shape: tuple[int, ...] | None

@dataclass(frozen=True)
class ManifestPayloadRef:
    path: Path                 # resolved below the manifest folder
    group: str | None
    detector: str
    axes: str                  # logical axes
    stored_axes: str           # axes in the container
    shape: tuple[int, ...]     # logical shape
    stored_shape: tuple[int, ...] | None
    generation: int | None
    complete: bool
    #: Parsed, not the raw manifest string: a 3x3 homogeneous matrix in
    #: (row, col, 1) order, so composition is matrix multiplication and the
    #: identity fast path is a property of the value rather than of the string
    #: that produced it. `"identity"` parses to the identity matrix.
    transform_to_alignment: object

@dataclass(frozen=True)
class IndexedTile:
    tile_id: int
    grid: tuple[int, int]
    stage_um: tuple[float, float]
    saved_position_yx: tuple[float, float]
    alignment: AlignmentArtifact
    payloads: dict[str, ManifestPayloadRef]

@dataclass(frozen=True)
class TilingDatasetIndex:
    manifest: Path
    alignment_detector: str
    pixel_size_yx_um: tuple[float, float]
    z_step_um: float
    tiles: tuple[IndexedTile, ...]
    detectors: tuple[str, ...]
```

`RecordingManager.PayloadLocator` remains the acquisition-side value written
when a session finalises. `ManifestPayloadRef` is deliberately a different
read-side type in imcommon; its docstring names the write-side counterpart and
the version-2 manifest fields that must stay in step. imcommon never imports
imcontrol.

Manifest parsing is the single authority for detector enumeration.
`inspect_dataset(path)` parses once and returns `index.detectors`;
`detectors_in(payload)` becomes a compatibility wrapper over that parsed index,
not a second raw-dictionary enumerator that can disagree with validation.

The chosen names and their separation are normative. `inspect_dataset(path)`
returns this index and a completeness summary without reading tile pixels.
`load_dataset` remains as the legacy/alignment convenience wrapper so current
scripts and version-1 tests do not change behavior.

The solved result is another explicit value:

```python
@dataclass(frozen=True)
class MosaicLayout:
    positions_yx: dict[int, tuple[float, float]]
    source: str                # "stage", "saved" or "refined"
    report: RefinementReport
```

It contains no detector arrays. That makes it impossible for assembling the
second detector to accidentally solve a second layout.

#### Exact locator loading

Replace `_read_image(path)` for current manifests with a locator reader that can
inspect shape/dtype and read data separately:

* TIFF requires `group is None` and reads its one declared series.
* HDF5 opens exactly `group`; when the locator names the detector group, its
  image is exactly `<group>/data`. It never walks the file looking for the first
  dataset.
* Zarr follows the same rule: the locator selects a group or array and structured
  detector groups resolve their `data` array explicitly.
* A relative `path` is resolved against the manifest directory. `..` traversal,
  symlink resolution outside the run folder and absolute paths are rejected for
  `imswitch-tiling/2`. Transitional absolute locators may be handled by a
  deliberately named compatibility option, never silently.
* A missing group is different from a missing file only diagnostically; if the
  locator says `complete: true`, either is a hard error.
* `stored_axes` must name the stored rank. Logical normalization removes only
  axes present in `stored_axes` but absent from `axes`, only when their extent is
  one. It then verifies logical `shape`. A length-one `C` or `Z` axis survives.

Keep the old recursive/first-array behavior only behind the version-1/interim
manifest path. That is compatibility, not recovery for a broken version-2 run.

#### Solve alignment geometry once

`solve_layout(index, options) -> MosaicLayout` performs these steps:

1. Derive a nominal position for **every tile identity** from stage coordinates,
   using the recorded orientation and alignment pixel size. Fall back to saved
   pixel positions under the same rule used today.
2. Read only the singular alignment artifact for each tile. If it has leading
   axes, make the alignment projection explicitly; payload selection never
   changes this projection.
3. Run the existing all-pairs correlation, outlier rejection and weighted
   least-squares solve on the readable alignment images.
4. Write solved positions back by tile identity. A tile whose alignment image is
   unavailable keeps its nominal stage/saved position, so a recoverable payload
   or alignment-file failure does not shift later identities or discard a good
   payload.
5. Freeze and return the layout. Do not retain the alignment arrays once the
   solve is complete.

Cache this value in `TilingReconstructor`, keyed by the resolved manifest path,
manifest modification identity, the alignment artifacts' size/mtime fingerprint
and every geometry-affecting option (`stage_positions`, refinement enabled and
maximum shift). Changing detector, channel, projection or blending must reuse
the cache. Changing the manifest, an alignment artifact or an alignment option
invalidates it. The cache holds only the index, positions and report — never the
full payload arrays.

If refinement is requested but no alignment image is readable, fail with an
actionable message telling the operator to disable refinement and use the
nominal layout. Do not claim that an unrefined layout was refined.

#### Compose detector geometry, do not copy positions

Represent placement internally as homogeneous matrices in **row/column** order.
For tile `i` and detector `d`:

```text
[mosaic row]                       [detector-local row]
[mosaic col] = T(layout[i]) @ A(d) [detector-local col]
[     1    ]                       [        1         ]
```

`A(d)` is the parsed `transform_to_alignment`; `T` translates the alignment
tile's origin to its solved mosaic position. The transform acts only on Y/X.
`C`, `Z`, `T` and any other leading axes are not resampled or reordered.

The output's Y/X scale is therefore the **alignment** detector's pixel size, for
every detector, because that is the grid being composed into. Under Phase 1's
enforced identity the two are equal anyway; the distinction only becomes visible
when a calibrated transform carries a scale, and stating it now is what stops
someone later labelling a resampled mosaic with the source detector's pixel
size. `Z` scale continues to come from the manifest's `z_step_um`.

The normative Phase 1 contract emits only `"identity"`, including for the
alignment detector. An interim writer may already have emitted `"reference"`
there; accept it only on the manifest's named alignment detector as a documented
compatibility alias for identity. Still route both through matrix composition.
The assembler has a bit-for-bit fast path for identity plus integer translation
and a tested affine resampling path behind the same placed-tile interface.

The affine path has these normative conventions:

* Integer Y/X coordinates denote **pixel centres**. `A(d)` maps a
  detector-local pixel centre into the alignment pixel-centre coordinate system;
  `T` then maps it into mosaic coordinates.
* Bounds come from the four outer footprint corners at half-pixel offsets
  (`-0.5` through `size - 0.5`), transformed by the composed matrix and rounded
  outward to alignment-grid pixel edges. The result records the output origin,
  so a negative or sub-pixel transform does not disappear into array indexing.
* Resampling is inverse mapped: each output pixel centre is mapped through the
  inverse composed affine to detector-local coordinates. Intensity uses linear
  interpolation; outside samples contribute zero value and zero weight.
* A unit validity mask is transformed with nearest-neighbour interpolation.
  The overlap taper is multiplied by that mask before accumulation, and the
  output is divided only by accumulated valid weight. Warped empty corners must
  therefore never darken another tile.
* Every remaining leading-axis plane uses the same spatial map. No leading axis
  is interpolated, reordered or projected by the affine implementation.
* The affine accumulator, weights and affine-path result are `float32`. The
  identity/integer fast path does no interpolation and preserves the current
  assembler's dtype and values bit for bit.
* **One mosaic uses one path, never both.** A mosaic assembles a single
  detector, and a detector has a single `A(d)`, so the choice of path is a
  property of the output rather than of a tile. That is what makes the dtype
  split above safe to state: no mosaic can contain some tiles resampled and
  others copied, and no reader has to ask which of its pixels were which.

A future calibrated transform consequently adds a manifest parser/schema case,
not a second mosaic implementation. Unknown, singular or malformed transforms
fail during header inspection, before allocation. A synthetic affine test locks
the centre convention, outward bounds, mask behavior and output origin.

#### Leaving the door open for a transform module

A separate module is expected to own detector-to-detector transforms and their
calibration. Tiling should end up a *consumer* of that rather than a second
place where such things are configured and stored. Three decisions now decide
whether that is a small change later or a migration.

**Widen the transform to a schema before anything depends on the string.**
`TilingInfo.detectorTransforms` is `Dict[str, str]` and Phase 1 accepts only
`"identity"`. A bare string cannot carry a matrix, a calibration identity or a
date, so the format has to grow — and growing it after datasets exist means
reading two shapes forever. Accept both now, with the string as shorthand:

```json
"detectorTransforms": {
  "Camera": "identity",
  "Widefield": {
    "kind": "affine",
    "matrix": [[1, 0, 12.5], [0, 1, -3.0], [0, 0, 1]],
    "source": "transform-module",
    "calibration_id": "2026-08-04-widefield-apd",
    "measured": "2026-08-04T11:03:00Z"
  }
}
```

Phase 1 keeps rejecting anything whose resolved kind is not identity; it simply
rejects it after parsing a schema instead of after comparing a string. The
manifest stores the resolved object, so a dataset says which calibration it was
written under rather than only that one existed.

**The manifest records what was believed then; the reader may be told
otherwise.** A calibration measured after a run is often better than the one in
force during it, and a rig's configuration will have moved on. So the stored
transform is provenance, not a lock: `assemble_payload` takes an optional
transform resolver, and when one is supplied its value is used *and recorded* —
`transform_source: "manifest" | "override"` alongside the calibration identity —
so a mosaic always says which geometry produced it. Absent a resolver the
manifest's own value is used, unchanged. This is the whole of the coupling: one
optional argument and one provenance field.

**Parse in exactly one place.** Everything downstream already consumes a 3x3
matrix, so a transform module plugs in by supplying that matrix — not by
teaching the assembler a second notion of geometry. Keep the parser a single
function in imcommon with no imcontrol import, so both the acquisition side
(validating a save set) and the reader (composing placement) call the same one
and cannot disagree about what a transform means.

Two things worth writing down now because they are cheap now and awkward later:

* A calibration naturally describes a **pair** of detectors, while the manifest
  describes each detector *relative to that run's alignment detector*. A module
  speaking pairs must therefore resolve against the manifest's named alignment
  detector, and two runs that aligned on different detectors will legitimately
  store different matrices for the same physical relationship.
* Resampling is lossy, so a transform is best applied once. If a module later
  offers to transform detectors onto a common frame *before* tiling, that is a
  different and better pipeline than tiling resampling afterwards — worth
  leaving room for, and worth not foreclosing by making the manifest's stored
  transform authoritative rather than descriptive.

#### Load and assemble the selected payload

`assemble_payload(index, layout, selection, options)` should:

1. Select one detector and join its locators to `layout.positions_yx` by
   `tile_id`, never by path, filename or generation.
2. Skip only locators explicitly absent or `complete: false`, recording the tile
   IDs and reasons. If no payload remains, raise a detector-specific error.
3. Validate that all remaining tiles agree on logical axis names and rank.
   Extents may differ — for example a short Z stack — but a `CYX` tile must not
   be right-aligned into a `CZYX` dataset as though its `C` were `Z`.
4. Apply channel selection after stored-to-logical normalization and before
   mosaic allocation. Selecting one `C` index removes `C`; selecting **All**
   preserves it, including a length-one channel axis.
5. Apply projection by named axis. The first UI version needs **Keep Z** and
   **Max-project Z**; it must never project `C` or `T` merely because they lead
   Y/X. If there is no `Z`, the projection option is disabled.
6. Compose each tile's spatial transform with the frozen layout, then assemble
   every remaining leading plane with the same spatial geometry.
7. Return axes, scales and a provenance summary together with the array: selected
   detector/channel, skipped tile IDs, manifest path, layout-cache key and
   refinement report.

The default output should be the **alignment detector's full payload**, when it
exists — not its small alignment snapshot. Keep a separately labelled
**Alignment images (diagnostic)** choice for reproducing the current mosaic and
debugging registration.

#### ImProcess integration and controls

The parameter widget cannot populate itself today because it is installed before
it knows the current `DataObj`. Add a small optional, generic reconstructor hook
for source inspection rather than another `if reconstructor.id == ...` branch in
the manager. `Reconstructor.accepted_source_kinds` defaults to `("image",)`;
tiling declares `("image", "tiling-manifest")`. Its optional
`inspect_source(data_obj) -> SourceInspection` hook describes dynamic choices
and estimates without loading pixels. On `sigCurrentDataChanged`, the manager
disables reconstructors that do not accept the current source kind, calls the
active reconstructor's hook and gives the returned inspection to its parameter
widget. The tiling implementation populates:

* output source: full-payload detector names plus the alignment diagnostic;
* logical axes and representative shape for the current detector;
* channel choice when `C` is present;
* Z projection choice when `Z` is present;
* complete/total payload count and an estimated output shape/footprint; and
* why refinement is unavailable when alignment artifacts are missing.

Changing the detector refreshes only selection metadata; it does not load the
payload or solve the layout. The reconstruction result name includes detector
and channel, and `TilingMosaicResult` carries the provenance summary so saving
and later inspection do not lose which payload was assembled.

Opening a tile, `tiles.json` or the run folder should all identify the same
manifest. The source-spec layer currently knows file containers and Zarr stores
but not a tiling run directory; add an explicit tiling-manifest source spec
instead of relying on a `.json` extension being mistaken for an image dataset.
That source is metadata-only, with an explicit generic lifecycle:

* `DataObj.sourceKind` defaults to `"image"`; a tiling run uses
  `"tiling-manifest"`. `DataObj.sourceReady` is the routing-ready flag: the
  existing eager image path sets it from `dataLoaded`, the virtual image path
  sets it from `sourceLoaded`, and a successfully inspected metadata source sets
  it directly. It does not change the meanings of `sourceLoaded` (an
  array-backed source is open) or `dataLoaded` (pixels are materialized).
* `DataObj.fromMetadataSource(name, path, sourceKind, sourceMetadata)` creates a
  lightweight current object. For a tiling run, `sourceMetadata` is the parsed
  `TilingDatasetIndex`; its state has `sourceReady` true, `sourceLoaded` false
  and `dataLoaded` false. It never asks `DataObj._open` to enumerate JSON as
  though it were HDF5.
* `_loadAsCurrent` emits `sigCurrentDataChanged` when `sourceReady` is true.
  Image viewers and edit/mean/frame controls must guard on image source kind;
  for metadata-only sources they clear the image and disable those
  actions instead of dereferencing `data` or `numFrames`. The reconstructor
  manager still receives the signal and offers only reconstructors whose
  optional source-inspection hook accepts the source kind.
* `Reconstructor.process(data_obj, params, ...)` remains the processing entry
  point. The tiling reconstructor consumes the already parsed index from
  `data_obj.sourceMetadata` and revalidates its fingerprint before work starts.
  Other reconstructors continue to receive ordinary image `DataObj` instances.

This requires a small metadata-source path in `FileIOController` and listener
guards in `DataFrameController` and the current-data controls; adding a suffix
to `file_extensions` alone is insufficient. Opening a tile first walks upward
to the owning manifest, while opening the manifest or run folder resolves it
directly. Ambiguous nested manifests fail with a choice rather than selecting
one arbitrarily.

#### Memory and performance contract

Full payloads make the current eager shape unacceptable. Keep alignment loading
simple, but assemble payloads with a two-pass reader:

1. inspect locator headers, logical shapes and transforms to calculate bounds,
   output shape and exact canvas/weight memory;
2. allocate once, read one payload tile (or one leading-axis slab) at a time,
   place it and release it.

Do not keep a list of materialized payload arrays beside the output canvas.
Preserve the existing cheap Y/X-only overlap weights when leading coverage is
uniform and use per-plane weights only when it is not.

**Time is the other budget, and it is already overspent.**
`ReconstructorManagerController._reconstruct_with_plugin` calls `process()`
synchronously on the calling thread, which is the GUI thread — there is no
worker and no progress channel for any reconstructor. A 100-tile alignment-only
mosaic already freezes the interface for about fifteen seconds; that is why
every stage logs. Phase 2 replaces small alignment tiles with full payloads, so
the same run becomes minutes.

Phase 2 therefore moves **tiling reconstruction** to a worker without silently
changing the execution behavior of every existing plugin. Add an
`execution_policy` capability whose default is `"inline"`; tiling opts into
`"worker"`. Add an optional `ReconstructionContext` keyword to the base
`process` contract; registered reconstructors accept it and may ignore it,
while tiling uses its progress callback, cancellation token, memory budget and
confirmed-over-budget flag. The manager owns the reusable worker dispatch,
publishes progress to the GUI and returns the result or error to the GUI thread;
a worker never calls a widget directly. Later expensive reconstructors can opt
in without another manager-specific branch.

Cancellation is cooperative and checked between header reads, alignment-tile
reads, refinement batches and every payload tile or leading-axis slab. A
cancelled job closes the current container, releases its partial canvas, emits
one cancelled terminal and publishes no partial `ProcessingResult`. Progress
has named phases (`inspect`, `align`, `allocate`, `assemble`, `finalize`) plus
completed/total work, so phase transitions do not make the percentage move
backwards.

The cheap source-inspection pass computes the estimate before the worker is
launched. ImProcess defaults the memory budget from available memory and asks
for explicit confirmation on the GUI thread before setting the context's
confirmed-over-budget flag. The worker repeats header validation immediately
before allocation; if the manifest changed or the recomputed requirement now
exceeds an unconfirmed budget, it fails instead of opening a dialog from the
worker. The error reports output shape, canvas bytes, weight bytes and the
selections that can reduce them (one channel or Z projection).
An out-of-core result/export is useful later, but is not smuggled into this phase:
`ProcessingResult` still owns an in-memory array.

#### Failure matrix

| Condition | Phase 2 behavior |
|---|---|
| Payload absent or `complete: false` | Skip that tile for that detector and report it |
| No complete payload for the selected detector | Fail with detector name and completeness summary |
| `complete: true`, but path/group is missing | Fail loudly; the manifest contradicts the run folder |
| Stored rank/axes/shape contradict descriptor | Fail loudly; never assemble a plausible partial result |
| Alignment image missing for some tiles | Refine readable subset; missing identities keep nominal positions |
| No alignment image and refinement requested | Refuse refinement; nominal assembly remains available explicitly |
| Detector axes differ between tiles | Fail before allocation |
| Unknown, singular or malformed transform | Fail before allocation |
| Legacy manifest without descriptors | Use the existing compatibility path unchanged |
| Detector selected, refinement on | Correlate the **alignment** images, never the selected payload |
| Selected detector missing from some tiles | Skip those tiles for that detector; the layout is unaffected |
| User cancels reconstruction | Close readers, discard the partial canvas and publish no result |

#### Implementation order

**Phase 2a — index and exact readers.** Add the manifest-only dataclasses,
strict version-2 validation, exact TIFF/HDF5/Zarr locator opening and cheap
inspection. Keep `load_dataset` as a compatibility wrapper.

**Phase 2b — geometry as a value.** Extract nominal layout and alignment-only
refinement into `solve_layout`; key everything by tile identity and add the
layout cache. At the end of 2b, selecting a detector cannot affect solved
positions even before payload assembly is changed.

**Phase 2c — payload selection and assembly.** Add named-axis normalization,
channel/Z selection, transform composition, the one-tile-at-a-time assembler and
strict partial/corrupt behavior.

**Phase 2d — metadata source, ImProcess UX and provenance.** Add the
`tiling-manifest` source kind and listener guards, populate the controls from
source inspection, expose estimates/completeness, reuse cached geometry and
label/save the result with its selection and layout report.

**Phase 2e — worker execution and resource contract.** Add
`ReconstructionContext`, opt-in worker dispatch, phase/tile progress,
cooperative cancellation and GUI-thread memory confirmation. Exercise large
synthetic payloads without retaining every tile or publishing partial cancelled
results; verify that default-inline reconstructors keep their existing dispatch.

**Phase 2f — compatibility hardening.** Run the legacy fixtures through the
unchanged wrapper, verify the intentional triggered/free-running artifact rules
and keep image-only reconstructors unchanged for ordinary `DataObj` sources.

#### Phase 2 acceptance criteria

1. Two tiles stored in one HDF5 or Zarr path under different groups load the
   exact groups named by their locators; changing group changes the tile read.
2. A TIFF payload is accepted only with a null group, and version-2 paths cannot
   escape the run folder.
3. `TCZYX -> CZYX` squeezes only the declared singleton `T`; a length-one `C` or
   `Z` survives. A rank or shape contradiction raises.
4. Layout refinement reads only alignment artifacts. A payload with blank,
   shifted or unrelated intensities still receives the alignment-solved
   geometry.
5. Reconstructing two detectors or two channel selections with unchanged
   geometry options performs one layout solve and produces spatially coincident
   mosaics.
6. Identity composition reproduces today's integer placement bit for bit. A
   synthetic non-identity internal affine fixture verifies pixel-centre mapping,
   outward half-pixel bounds, recorded output origin, linear intensity sampling
   and nearest-neighbour validity masking without touching leading axes or
   darkening overlap through empty warped corners.
7. `CZYX` assembles with all channels; selecting channel `k` yields `ZYX` with
   exactly that channel. Z projection yields `CYX` or `YX` and never collapses C.
8. An explicitly incomplete payload is omitted and reported; a supposedly
   complete but missing group, wrong shape or unknown transform fails loudly.
9. A tile with a missing alignment image retains its nominal position and can
   still contribute a complete payload.
10. Opening a tile, `tiles.json` or its run folder produces one metadata-only
    `DataObj` with `sourceKind == "tiling-manifest"`, `sourceReady == true` and
    no loaded pixels. Image viewers clear safely, while detector/channel controls
    populate from the selected manifest before reconstruction; the result records
    the selection and skipped tiles.
11. Peak payload memory is output canvas plus weights plus approximately one tile,
    not output plus every materialized tile; over-budget work is refused before
    allocation with an actionable estimate.
12. Existing version-1/interim manifests, alignment-only reconstruction and
    `assemble_dataset` produce the same arrays and axis labels as before.
13. Removing one tile's file from a run does not move any other tile: identities
    and solved positions are unchanged, and only the affected tile is reported
    skipped. (The index is built before reading, so it cannot renumber.)
14. Tiling reconstruction runs off the GUI thread, reports monotonically
    advancing named-phase and per-tile/slab progress, and remains cancellable.
    Cancellation closes readers, frees partial allocations, emits one cancelled
    terminal and publishes no `ProcessingResult`; a default-inline reconstructor
    still follows its existing synchronous path.
15. In free-running mode, detector snapshots are the payload locators. In
    triggered mode, finalized recording locators are the payloads and redundant
    compatibility snapshots are never selected in their place. A genuinely 2-D
    alignment snapshot may satisfy both compatibility and alignment descriptors.

**Not in Phase 2:** live assembly while acquisition is still writing, solving Z
offsets, *measuring* calibrated cross-detector transforms, non-affine warps and
out-of-core mosaic results. Phase 2 defines how a transform is **carried,
resolved and applied**; producing one is the transform module's job, and the
seam above is deliberately the whole of what tiling needs from it. Phase 2 consumes finalized artifacts and builds the
2-D spatial layout on which those later extensions depend.

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
