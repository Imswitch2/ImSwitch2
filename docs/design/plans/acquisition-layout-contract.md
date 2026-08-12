# Acquisition layout metadata contract

**Status:** Active — all seven PRs implemented on `codex/acquisition-layout-schema`; rig validation pending
**Date:** 2026-08-11
**Revision:** 8 — PR 7 landed; every reconstructor consumes the resolved layout (2026-08-12)
**Scope:** Scan producers, recording, file readers, live sources, and ImProcess reconstructors
**Related audits:** [ImProcess OME read compatibility](../../improcess_ome_read_compatibility_audit.md), [recording data flow](../../recording_dataflow_plan.md), [OME recording standardization](../../recording_ome_standardization_plan.md), [live reconstruction](../../live_reconstruction_audit.md)

---

## 0. Implementation status

The contract, its transport, the resolver, every scan producer, and every
reconstructor in the delivery sequence are implemented. What remains is
hardware validation, which no amount of software testing can substitute for.

| PR | Status | Commit |
|---|---|---|
| PR 1 — Schema and normalization | Implemented | `4f145653` |
| PR 2 — Format transport and recording lifecycle | Implemented | `ee91ba7a` |
| PR 3 — Resolver, legacy adapters, and preflight | Implemented | `0a088493` |
| PR 4 — Producers and recording integration | Implemented | `cdcc55da` |
| PR 5 — MoNaLISA and BeadRec migration | Implemented | `eee2b603`, `baf90d39`, `e2ab1b7a` |
| PR 6 — SNOUTY and SMLM migration | Implemented | `69f139af`, `66a37936` |
| PR 7 — Widefield STARSS, view-only, and fallback cleanup | Implemented | `5474fd05` |

A review of PR 1–4 produced three fixes in `56a1b0eb`: cross-span overlap
detection no longer expands the selected event set (2.25 s and 141 MB became
0.3 ms and no allocation on two spans over 2M events), the coordinate helpers
validate once per layout instead of per frame, and the scan-position
cross-check skips loop kinds it cannot classify instead of refusing to record.

Test evidence (2026-08-12, `QT_QPA_PLATFORM=offscreen`, `-p no:napari`):
`imswitch/imcommon/_test` + `imswitch/imcontrol/_test/unit` is 2792 passed /
4 skipped, and `imswitch/improcess/_test` is 1262 passed / 2 skipped.
`imcontrol/_test/unit` and `improcess/_test` must be run in separate pytest
processes; combining them with the improcess suite hangs locally.

Deviations from the delivery-sequence file lists, and the judgement calls made
along the way, recorded so the next change builds on what exists rather than on
the original sketch:

- PR 3 also touched `ReconstructorManagerController.py` and
  `reconstruction_worker.py` to carry the resolved layout into the worker.
  Neither appeared in the PR 3 file list.
- PR 4 added `imswitch/imcontrol/controller/controllers/_acquisition_layout_source.py`
  as the shared layout-builder module for every producer. RESOLFT layouts are
  published from `TriggerScopeScanGeometryMixin`, so the six controllers that
  inherit it (Scan, PLSR, PLSR-Multicolor, LSXYR, GalvoDetection,
  LightSheetMulticolor) gain layouts without individual edits;
  `TriggerScopeRasterController` keeps its own implementation, as §4.3 requires.
- The round-trip suite landed as
  `imswitch/improcess/_test/test_acquisition_layout_transport.py`, and override
  coverage lives in the resolver test module rather than a separate
  `test_acquisition_layout_overrides.py`. §9's module list is updated to match.
- No hardware validation has been done. §4.3.5 (RESOLFT firmware counters
  against real traces) remains owed regardless of which reconstructor PR lands
  next.
- PR 5 chose the six-dimensional compatibility projection of §7.1 rather than
  a seventh Condition axis. `MonalisaProcessingResult.acquisition_projection`
  records the folded components, their order, the condition labels, and names
  the axis Condition when there is no real elapsed time.
- PR 5 also fixed two contract defects first reachable once a plugin declares
  requirements: `InMemoryStackWrapper` published a bare `AcquisitionLayout`
  where `DataObj` publishes a `ResolvedAcquisitionLayout`, and
  `inspect_acquisition` raised `AttributeError` for a source without the
  property instead of reporting an undescribed acquisition.
- PR 6 read §7.4.3 literally: SMLM takes its pixel size from the source's own
  axis scales when it has them, and the widget value becomes the recorded
  fallback used when the source has none. The result always records which was
  used, and keeps the manual value alongside when the two disagree. The SMLM
  widget defaults to a plausible 100 nm rather than a sentinel, so there is no
  way to tell a deliberate entry from an untouched default; if the widget
  should win instead, that needs an explicit override control, not a heuristic.
- PR 6 gates SMLM inside `process()` rather than through
  `acquisition_requirements`. The generic `allowed_extra_loops="reject"` gate
  would also reject the `repeat` loop that generic-fallback layouts carry,
  which would break every legacy file; the plugin instead refuses only
  `recorded`/`user-override` layouts that declare non-frame loops.
- PR 7 gave the STARSS acquisition workflow (§7.5.1) an OME description rather
  than routing it through `RecordingManager`: the workflow writes its TIFF
  directly, so it builds the same `AcquisitionLayout:*` annotation through
  `build_ome_image_meta`/`build_ome_xml`. Metadata failures are logged and the
  measurement is still saved -- describing an acquisition must never cost it.
- PR 7 also taught the registry to read the modality from the resolved layout
  (§7.6, "registry selection can use recorded modality/source identity"). It
  previously read only a bare `modality` attribute, which nothing in the new
  contract writes. The bare attribute remains as the fallback.

### Legacy trailing-axis ambiguity (fixed)

The legacy scan adapters choose between two historical conventions for a stage
axis — size (`length/step`) and endpoint (`(length-start)/step + 1`) — by which
one matches the observed frame count. When they disagree about whether a
trailing axis moved at all, the choice is a coin flip: a 200-frame file with
`ScanTTL:Nx/Ny = 10` and a third axis of length 1.0 / step 1.0 resolves as
`scan_z=2` under the endpoint convention, but the same frames could equally be
two timepoints, and Z and time are not interchangeable.

The adapters now emit `AMBIGUOUS_LEGACY_TRAILING_AXIS` naming the axis and the
alternatives, and lower confidence from `high` to `medium`, whenever a
candidate activates an axis the size convention calls a single position. A
genuine Z stack, where both conventions agree the axis moved, keeps full
confidence and emits no extra issue.

Independently of that, a plugin may only refuse a reconstruction on a
`recorded` or `user-override` layout. A `legacy-adapter` layout is inference,
however well reported, so it falls through to the plugin's own older path and
files that used to open still open.

---

## 1. Goal

Every recorded detector frame must have one unambiguous semantic coordinate.
Reconstructors should consume that description instead of independently guessing
from array rank, frame count, filenames, or modality-specific metadata.

The immediate regression is an 18 × 18 Advanced Scan recording with two line
steps, later consumed by the MoNaLISA reconstructor. Its 648 camera frames are
acquired in this order:

```text
scan_y (outer) -> condition / line step -> scan_x (inner)
frame = ((scan_y * 2 + condition) * 18 + scan_x)
```

The formula uses a zero-based `frame` index. In human-facing numbering, frames
1–18 belong to condition A, 19–36 to B, 37–54 to A, and so on. The previous
interpretation treated the two conditions as two contiguous 324-frame time
blocks. PR 5 corrects this case for MoNaLISA and BeadRec by placing every frame
at its recorded coordinate, PR 6 removes the equivalent readings in SNOUTY and
SMLM, and PR 7 removes the last of them from Widefield STARSS and view-only.
(The separate `codex/hotfix-monalisa-linesteps` branch patched the
MoNaLISA case directly and is not merged here — this branch fixes it through
the contract instead.)

This plan replaces those local interpretations with a versioned,
per-detector `AcquisitionLayout` contract. Shape-based inference remains
available for old files, but only as a visible last resort.

### Success criteria

- Advanced Scan, MoNaLISA/point scan, and TriggerScope scan producers publish
  the actual frame order for every recorded detector.
- HDF5, OME-Zarr, live Zarr, and OME-TIFF preserve the same layout.
- Offline and live reconstruction resolve a layout through the same code path.
- A line step, condition, channel, or scan coordinate is never called time
  unless the producer explicitly identifies it as time.
- Ambiguous, conflicting, incomplete, or truncated acquisitions remain
  viewable, but strict reconstructors fail preflight with a useful explanation;
  they are not silently reshaped or truncated.
- Legacy data can still open, with the inferred fields and confidence clearly
  shown to the user.

## 2. Current contract and gaps

### 2.1 What producers already know

`ScanInfoContract` in
`imswitch/imcontrol/model/signaldesigners/basesignaldesigners.py` already
separates physical image axes from `n_linesteps`, provides pixel sizes and
sample counts, and is produced by both Galvo and Beta signal designers.
Advanced Scan and TriggerScope controllers also publish useful `ScanStage:*`
and `ScanTTL:*` attributes.

Those fields do not form a complete persistent contract:

- `ScanInfoContract` is runtime-only and is not serialized intact.
- `img_axes_with_linesteps` states which axes exist, not their chronological
  nesting in the camera stream.
- `ScanTTL:n_linesteps` is global, while detector TTL enable masks can make the
  recorded condition set detector-specific or vary it per expanded scan line.
- A detector may receive more than one pulse per scan position or condition.
- Raster and RESOLFT firmware have different loop structures and must not be
  interpreted through a shared shape guess.

### 2.2 Where information is currently lost

| Boundary | Current behaviour | Consequence |
|---|---|---|
| `RecordingController` | Collects frame counts and optional scan dimensions/steps | TriggerScope raster can omit standard geometry because its controller exposes BeadRec-specific accessor names |
| `RecordingManager` | Writes expected frames, frames per stack, timepoint count, detector, and source metadata | No serialized chronological loop order or detector-specific condition list |
| OME axes | Multi-frame scans without Z are commonly labelled `T` | Raw scan frames appear to be timepoints even when they are X/Y/condition samples |
| Offline OME-Zarr | Preferred NGFF path does not flatten detector `metadata/` attributes | `ScanStage:*` and `ScanTTL:*` can disappear while selected recording fields survive |
| Live Zarr | Flattens metadata separately | Offline and live reconstruction can see different contracts for the same data |
| OME-TIFF | Stores standard OME axes/calibration but drops arbitrary acquisition attributes | Scan semantics cannot round-trip |
| In-memory source | Chooses labels from array rank | A 3D camera stream can be called channel data without evidence |

### 2.3 Reconstructor-specific assumptions

| Reconstructor | Assumption to remove |
|---|---|
| MoNaLISA | Global line-step count is enough; conditions can be folded into the current result `T` axis |
| BeadRec | Leading axes can be flattened and a square raster inferred from total frames; excess frames may be silently dropped when the inferred buffer is too small |
| SNOUTY / projections | Cycle, plane, and time counts can be supplied manually and uneven data can be split or truncated |
| SMLM | Every leading axis is a chronological frame axis; source calibration can be supplied manually |
| Widefield STARSS | H/V role and signal/background state can be inferred from filenames and a manual alternating/block choice |
| View-only | Rank-based axis defaults are a sufficient semantic description |

The tiling reconstructor is the closest existing model to follow. Its
versioned `imswitch-tiling/2` manifest distinguishes captured axes, stored
axes, positions, transforms, and provenance instead of inferring them from
rank.

## 3. Proposed model

Add a shared, dependency-light model in:

```text
imswitch/imcommon/model/acquisition_layout.py
```

Both ImControl and ImProcess must use this exact model. Do not create parallel
recording and reconstruction versions.

### 3.1 Schema

The serialized schema identifier is:

```text
imswitch.acquisition-layout/1
```

Kinds are open strings rather than closed enums. ImSwitch publishes constants
and meanings for common payloads (`detector-frame-stream`, `assembled-image`,
`reconstructed-image`) and loops (`scan_x`, `scan_y`,
`scan_z`, `condition`, `time`, `repeat`, `tile`, `position`, `tcspc_bin`), but
unknown namespaced values must round-trip unchanged. A new modality should not
need a schema-version bump unless the structure of the contract changes.

`tcspc_bin` is a registered loop kind, so a dense Y × X × TCSPC-bin product can
use `payload_kind=assembled-image`. A raw photon `event-stream` payload is
deliberately not registered in schema version 1: the work in
[time-resolved-detector-workflows.md](time-resolved-detector-workflows.md) owns
its payload validator and must add it before a strict reconstructor accepts
event-stream data. Until then, view-only may preserve it through the
open-payload rule with a warning.

The first version contains:

```python
@dataclass(frozen=True)
class LayoutIssue:
    severity: str           # warning, error
    code: str               # stable machine-readable identifier
    message: str            # user-readable explanation
    field: str | None = None
    loop_id: str | None = None


@dataclass(frozen=True)
class AcquisitionLoop:
    id: str                 # stable within the layout, e.g. "scan_y"
    kind: str               # open vocabulary; common values are registered
    count: int
    step: float | None = None
    unit: str | None = None
    direction: int | None = None  # physical +1/-1, not traversal order
    labels: tuple[str, ...] = ()
    storage_axis: str | None = None  # assembled payloads map loops to array axes


@dataclass(frozen=True)
class TraversalRule:
    loop_id: str
    order: str              # forward, reverse, serpentine
    parity_loops: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordedEventSpan:
    start: int              # producer-global event ordinal, zero based
    count: int
    stride: int = 1
    period: int | None = None
    repeats: int = 1


@dataclass(frozen=True)
class AcquisitionPartition:
    kind: str               # open vocabulary: time, position, tile, series, ...
    index: int | None = None  # zero based within the partition series
    planned_count: int | None = None
    storage: str | None = None  # combined, one-file-per-item, one-group-per-item


@dataclass(frozen=True)
class AcquisitionLayout:
    schema: str
    payload_kind: str       # open string; common values are registered
    detector: str
    storage_axes: tuple[str, ...]
    event_loops: tuple[AcquisitionLoop, ...]  # producer order, outermost -> innermost
    traversal: tuple[TraversalRule, ...] = ()
    recorded_event_spans: tuple[RecordedEventSpan, ...] | None = None
    partitions: tuple[AcquisitionPartition, ...] = ()
    modality: str | None = None
    scan_source: str | None = None
    provenance: str = "recorded"
```

`provenance` is one of `recorded`, `user-override`, `legacy-adapter`,
`ome-ngff`, `shape-inference`, or `generic-fallback`. It identifies how the
semantic layout was obtained; it is not itself a confidence score.

`storage_axes` gives the canonical ImSwitch role of each ndarray dimension.
The raw OME/NGFF axis projection remains separately available from the source;
for example, an OME `T` dimension may have the canonical storage role `frame`.
For `detector-frame-stream`, `event_loops` describes the complete
producer-global event lattice and its chronological nesting; the loops jointly
unfold the canonical `frame` storage axis. For assembled/reconstructed images,
loops instead map directly to semantic array dimensions through
`storage_axis`. `partitions` describes data split across files, arrays, groups,
timepoints, positions, or tiles. These concepts must stay separate.

For scan lapse, existing `singleLapseFile=true` means one file containing
multiple detector datasets/groups, so its partition storage value is
`one-group-per-item`, not `combined`. The existing
`recording:single_lapse_file` field remains the compatibility source of truth;
the partition entry makes its semantic meaning explicit.

The event lattice is scoped to exactly one partition. Producer event ordinals
restart at zero in every partition, and `recorded_event_spans` never address a
different partition. Here “producer-global” means shared by all detectors
inside that partition, before detector selection; it never means global across
partitions. Time is an `event_loop` when multiple timepoints are
stored inside one partition's array; time is an `AcquisitionPartition` when
timepoints are split across files, groups, or detector datasets. This is what
makes an until-stop lapse expressible: its partition `planned_count` may be
`None`, while every loop inside each completed partition still has a positive
count.

For one-file-per-timepoint and one-group-per-timepoint modes, each timepoint
carries the same canonical per-partition layout and zero-based event ordinals;
only `partition.index` and recording outcome/count metadata vary. Producers
must not offset later partitions' spans by the frame counts of earlier ones.

`recorded_event_spans` maps stored detector frames to producer-global event
ordinals, in stored-frame order. `None` means the detector recorded every
producer event. One span expands as:

```text
event_ordinal(repetition, item) =
    start + repetition * period + item * stride
```

where `0 <= item < count` and `0 <= repetition < repeats`. `period` is required
when `repeats > 1` and is otherwise omitted (the period term is then zero). The
number of selected frames is `count * repeats`. This compact periodic form
makes the contract genuinely per detector and covers:

- a static subset of line-step conditions;
- complementary detector TTL masks;
- Advanced Scan's per-expanded-line enable masks, where the recorded condition
  set varies by scan row;
- reordered or skipped producer events without pretending the result is a
  dense Cartesian product.

For example, condition B in every row of the 18 × 18 × 2 scan is one span:

```json
{"start": 18, "count": 18, "stride": 1, "period": 36, "repeats": 18}
```

Span canonicalization is part of decoding, not an optional producer behavior.
The input span-list order defines stored-frame order. The canonicalizer:

1. Conceptually expands that ordered selection into producer event ordinals
   (implementations stream it; they do not need to allocate the full list).
   If it is exactly `0, 1, ..., producer_event_count - 1`, the unique canonical
   representation is `recorded_event_spans=None`.
2. Otherwise, from the first unconsumed ordinal, takes the longest arithmetic prefix with
   one constant positive stride. A single remaining ordinal becomes
   `count=1, stride=1`. It repeats until the selection is partitioned into
   maximal, non-overlapping arithmetic runs.
3. Greedily combines the maximal consecutive runs that have equal `count` and
   `stride` and whose starts advance by one constant positive period. Two or
   more matching runs become one span with the largest possible `repeats`;
   otherwise `period` is omitted and `repeats=1`.
4. Emits canonical spans in ascending first stored-frame position—that is, the
   original selection order, not sorted producer-event ordinal.

This makes the canonical representation unique: 18 separate B-line spans and
one periodic B-line span decode to the same layout. Valid non-canonical input is
accepted and normalized immediately; it is not a validation error. Encoders,
equality, hashing, and cross-format comparisons operate only on canonicalized
layouts, and encoders always emit canonical spans.

After canonical compaction, `AcquisitionLayout:json` has a fixed 1 MiB UTF-8
limit (`MAX_INLINE_LAYOUT_BYTES = 1_048_576`) shared by every writer. A larger
layout fails producer/recording preflight with `LAYOUT_METADATA_TOO_LARGE`
before a writer is opened; decoders reject an oversized raw value before JSON
parsing and also enforce the canonical limit. Schema version 1 does not add a
format-specific external event-map fallback, because that would make TIFF,
HDF5, and Zarr carry different contracts; a future schema may add one.

Every stored frame obtains its logical coordinates by expanding an event
ordinal through `event_loops` and then applying `traversal`. A repeated camera
pulse is an explicit `repeat` loop, not a second frame-count multiplier.
Traversal maps chronological producer counters to logical indices;
`direction`, `step`, and `unit` then calibrate those logical indices. A
serpentine reversal must never be encoded as a constant physical direction.

For the motivating example, a camera stream uses:

```json
{
  "schema": "imswitch.acquisition-layout/1",
  "payload_kind": "detector-frame-stream",
  "detector": "WidefieldCamera",
  "storage_axes": ["frame", "detector_y", "detector_x"],
  "event_loops": [
    {"id": "scan_y", "kind": "scan_y", "count": 18},
    {
      "id": "linestep",
      "kind": "condition",
      "count": 2,
      "labels": ["A", "B"]
    },
    {"id": "scan_x", "kind": "scan_x", "count": 18}
  ],
  "traversal": [],
  "recorded_event_spans": null,
  "modality": "monalisa",
  "scan_source": "ScanControllerAdvanced",
  "provenance": "recorded"
}
```

Here `modality` is supplied only if the acquisition workflow or recording
configuration explicitly selected MoNaLISA processing. Advanced Scan itself
must not infer a modality from the presence of line steps.

A second detector enabled only for condition B gets its own layout whose event
spans select the B line from every producer scan row. A detector whose mask
changes per expanded line gets spans for exactly those enabled lines. The
meaning of a span is therefore precise: stored-frame position maps to a
producer-global event ordinal, and that ordinal maps to coordinates through
the producer loops. There is no overloaded `source_indices` field.

For a bidirectional version of the scan, the fast loop additionally carries:

```json
{
  "loop_id": "scan_x",
  "order": "serpentine",
  "parity_loops": ["scan_y", "linestep"]
}
```

For a serpentine rule, `parity_loops` is an ordered, contiguous suffix of the
loops immediately outside `loop_id`. Its row-major flattened counter is:

```text
flat = 0
for parity_loop in parity_loops:
    flat = flat * parity_loop.count + parity_loop.index
reverse loop_id when flat % 2 == 1
```

The rightmost listed loop changes fastest. Loops omitted outside that suffix
are reset boundaries. Thus `[scan_y, linestep]` implements
`(scan_y * n_linesteps + linestep) % 2` and resets at each omitted Z-plane or
timepoint. Including Z or time in the contiguous suffix carries parity across
that boundary instead. Alternation is evaluated in producer-global coordinate
space before detector selection, so an unrecorded B traversal still
participates in hardware parity.

### 3.2 Required helpers

Implement and unit-test these functions beside the data classes:

```python
encode_acquisition_layout(layout) -> str
decode_acquisition_layout(value) -> AcquisitionLayout
canonicalize_recorded_event_spans(
    spans, *, producer_event_count
) -> tuple[RecordedEventSpan, ...] | None
validate_acquisition_layout(layout, *, shape=None) -> tuple[LayoutIssue, ...]
producer_event_coordinates(layout, event_index) -> Mapping[str, int]
recorded_frame_coordinates(layout, frame_index) -> Mapping[str, int]
iter_recorded_coordinates(layout) -> Iterator[Mapping[str, int]]
unfold_frame_axis(array, layout, *, copy_policy="forbid") -> UnfoldedArray
```

Encoding must be deterministic JSON so HDF5, Zarr, and TIFF receive identical
content. These helpers are the one implementation reconstructors use for frame
coordinates; reconstructors must not reproduce the index or serpentine
arithmetic.

The coordinate helpers operate without loading pixels. `unfold_frame_axis`
supports NumPy arrays and lazy array-like sources that expose `shape`, `ndim`,
indexing, reshape, and transpose. Dense forward traversal can be a view when
the backend permits it. Sparse selections and serpentine reordering may need a
gather/copy; `copy_policy="forbid"` must raise a structured issue rather than
materializing silently. Live and large-data paths should use the coordinate
iterator or chunked gathers instead of coercing the complete source through
`np.asarray`.

Neither validation nor coordinate resolution may expand the selected event set.
Cost must follow the canonical run count, which compaction keeps small, not the
number of selected frames. Cross-span overlap is therefore an arithmetic test
between runs, and a coordinate lookup validates its layout at most once, because
per-frame lookups are the pattern reconstructors are told to use.

### 3.3 Validation rules

Common validation rejects an explicit layout when:

- a loop count, span count/stride/repeats, or present partition count is not
  positive;
- a present partition index is negative;
- `period` is absent/non-positive when `repeats > 1`, or is present when it is
  not needed;
- loop IDs or storage-axis names are duplicated, or a unit/physical direction
  is invalid;
- the number of storage axes does not match the source ndarray rank;
- labels do not match their loop count;
- two traversal rules target the same `loop_id`, a traversal references an
  absent loop, or a non-serpentine rule declares parity loops;
- a serpentine rule's `parity_loops` is empty, out of order, or is not a
  contiguous suffix of the loops immediately outside its target loop;
- the expanded spans address an event outside the loop product or select one
  producer event more than once;
- canonical JSON exceeds `MAX_INLINE_LAYOUT_BYTES`;
- an axis is marked `time` only because OME projected a raw frame axis to `T`.

Count and span validation then dispatches by `payload_kind`:

| Payload | Required interpretation and validation |
|---|---|
| `detector-frame-stream` | Exactly one canonical `frame` storage axis. `event_loops` collectively unfold it and must not set `storage_axis`. Spans are allowed. Within the current partition, the loop product is the producer-event count; `sum(span.count * span.repeats)` (or the loop product when spans are `None`) is the authoritative planned stored-frame count and must match the completed frame-axis length. |
| `assembled-image` / `reconstructed-image` | Spans are forbidden. Within the current partition, each acquisition loop maps to a unique existing `storage_axis`, and its count must equal that array dimension. Unmapped axes may be detector-pixel/result axes. No frame-product rule is applied. |
| Other/open payload kind | Common structural validation runs, then a registered payload validator is required before a strict reconstructor may accept it. View-only may preserve/display an unknown kind with a warning. |

A completed detector-frame acquisition whose planned count differs from the
observed array is invalid. Partial-live and `stopped_early` sources instead
return a structured incompleteness issue. The layout does not repeat derived
counts as `frames_per_position`, `frames_per_stack`, or `expected_frames`.
Existing `recording:frames_per_stack` and `recording:expected_frames` remain
derived compatibility fields during the migration.

An invalid explicit layout is a hard error and never silently falls through to
legacy or shape inference. A valid, intentional user override may replace it,
but the rejected layout and its issues remain in processing provenance.

Warnings, rather than hard errors, are reserved for legacy/inferred layouts,
partial live files, and preserved early-stop recordings. Every warning must be
returned as a `LayoutIssue`; it must not exist only in a log message.

### 3.4 Serialized keys

Continue writing current `ScanStage:*`, `ScanTTL:*`, and `recording:*` fields
for compatibility. Add:

```text
AcquisitionLayout:schema = imswitch.acquisition-layout/1
AcquisitionLayout:json   = <canonical JSON>
```

Recording outcome metadata is separate from the immutable acquisition plan:

```text
recording:completion_outcome = complete | stopped_early
recording:planned_frames     = <optional planned detector-frame count>
recording:actual_frames      = <committed detector-frame count at finalize>
recording:planned_partitions = <optional planned partition count>
recording:actual_partitions  = <completed partition count at finalize>
```

Do not add another writer-liveness flag. Normalize the existing completion
mechanisms into `writer_state = writing | finalized | unknown`:

- HDF5's sibling uint8 side dataset `stream_complete[0] == 1` means finalized
  even when SWMR prevented the compatibility `writing` attribute from being
  rewritten to false;
- otherwise an existing `writing=false` means finalized;
- `writing=true` means actively writing only while a live source knows it is
  following the current writer; on a later/offline open without a completion
  marker it means unknown/stale completeness, never complete;
- missing markers on legacy/external data remain a legacy inference rather than
  a newly recorded completion claim.

`recording:frames_committed` remains a separate readable-prefix barrier. It
says which frames are safe to read during writing; it does not say that the
recording finalized. `recording:completion_outcome` is written only at graceful
finalization and is valid only with normalized `writer_state=finalized`.

Outcome is `complete` when a known plan was fulfilled, or when an until-stop
recording with no planned count ended normally. It is `stopped_early` when a
known planned frame/partition count was not reached but the user requested a
graceful, preserved stop. Unexpected acquisition/writer failure continues down
the abort-and-delete path rather than labelling suspect partial output as a
successful early stop.

`planned_*` may be absent for an until-stop acquisition. A graceful early stop
finalizes the file with `stopped_early` and actual counts; it remains readable
but strict reconstructors can reject incomplete logical loops. The existing
`abortRecording()` path continues to delete partial output and does not create
an `aborted` file. TIFF includes final writer/outcome metadata when it embeds
OME-XML after the frame count is known. A crash that leaves `writing=true`
produces an unknown-completeness issue and never masquerades as a live or
completed acquisition.

The JSON is per detector. HDF5 and Zarr store it with the detector array's
metadata. OME-TIFF embeds the same two fields in an OME StructuredAnnotation
or MapAnnotation and the TIFF reader restores them to normal attributes.

OME/NGFF axes remain an interoperability projection of the storage array. The
axes emitted by current writers do not change in schema version 1, preserving
the existing Fiji/OMERO round trips. If a format uses `T` for a semantic frame
axis, the reader adds a lossy-projection diagnostic; that `T` never overrides
the acquisition layout.

## 4. Producer contract

Introduce one optional protocol for scan controllers, following existing
ImControl naming conventions:

```python
class AcquisitionLayoutSource(Protocol):
    def getAcquisitionLayouts(
        self, detectorNames: Sequence[str]
    ) -> Mapping[str, AcquisitionLayout]: ...
```

The selected scan controller is authoritative. Recording must not obtain
geometry from a different controller through a global communication-channel
fallback.

### 4.1 Advanced Scan

Update `imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py`
and the Galvo/Beta signal-design path:

1. Convert the generated `ScanInfoContract`, analog parameters, and digital
   detector TTL parameters into a layout after signal generation succeeds.
2. Publish the producer-global line-step loop, then derive each detector's
   recorded event spans from its actual TTL enable mask and pulse count, not
   only `n_linesteps`. Cover both per-step and per-expanded-line masks supported
   by `AdvancedScanTTLCycleDesigner`.
3. Publish physical X/Y/Z steps and direction on their matching loops, plus the
   exact forward/reverse/serpentine traversal rule.
4. Represent multiple detector pulses per pixel with an explicit inner
   `repeat` loop and validate the layout against final signal edge counts.
5. Validate the complete layout before recording is enabled.
6. Add a small 3 × 2 × 2 capture test for both Galvo and Beta designers to
   establish loop order, detector selection, and serpentine parity. Do not
   derive chronology from `img_axes_with_linesteps`, because it is an axis set
   rather than a loop ordering contract.

The motivating 18 × 18 × 2 fixture belongs to this section. Its producer is
Advanced Scan; MoNaLISA is the downstream consumer.

### 4.2 MoNaLISA and point-scan controllers

Update `ScanControllerMoNaLISA.py` and `ScanControllerPointScan.py`:

1. Publish the X/Y/Z loops and traversal their own signals actually produce.
2. Do not publish a line-step/condition loop: these controllers do not expose
   Advanced Scan line-step support.
3. Include the controller identity and detector-specific event selection.
4. Add controller-specific coordinate fixtures rather than reusing the
   Advanced Scan 18 × 18 × 2 fixture.

The scanner publishes what it controls. A recording workflow or explicit user
selection may additionally publish `modality`/intended processing; a generic
scanner must not guess that a dataset is MoNaLISA, BeadRec, or SMLM from shape
or line-step count.

### 4.3 TriggerScope raster and RESOLFT

Update `TriggerScopeRasterController.py`, `TriggerScopeScanController.py`, and
the relevant PLSR/LSXYR controllers:

1. Adapt `getBeadRecScanDims` and `getBeadRecStepSizes` into the common layout
   rather than requiring the generic `getDimsScan` accessor.
2. Publish raster X-fast/Y-slow ordering from the controller/firmware contract.
3. Characterize and test the actual RESOLFT firmware order for `roSteps`,
   `cycleSteps`, and `timeLapsePoints`; do not copy the raster ordering.
4. Represent scan-driven detector output as an assembled-image payload when it
   is already condition × scan-Y × scan-X, rather than pretending it is a
   camera-frame stream. Its acquisition loops map directly to those named
   storage axes, `recorded_event_spans` is `None`, and frame-product validation
   does not apply.
5. Validate hardware frame counters against the layout during rig testing and
   retain the captured trace as a test fixture where practical.

## 5. Recording and transport

### 5.1 Recording controller

In `imswitch/imcontrol/controller/controllers/RecordingController.py`:

1. Extend the existing pinned-source plumbing to query layouts for the selected
   detectors. For layouts, never fall back to the CommunicationChannel when a
   source has been pinned; `_scanAccessor` already follows this safety rule for
   geometry.
2. Add the mapping to `recordingArgs` without removing current geometry and
   frame-count fields.
3. If the source declares layout support but cannot create a valid layout,
   prevent scan recording and show the validation issues.
4. If the source is legacy and has no layout method, allow recording through
   the current path and mark the eventual adapter result as inferred.

### 5.2 Recording manager

In `imswitch/imcontrol/model/managers/RecordingManager.py`:

1. Accept the per-detector layout mapping.
2. In the transport PR, accept and persist synthetic layouts without requiring
   a producer. In the producer PR, enable the pre-open validation gate and
   cross-check the layout-derived planned frame count with `recFrames`,
   `numCamTTL`, and detector-specific recording settings.
3. Attach the canonical layout JSON to each detector dataset/array.
4. Preserve the existing `writing`, HDF5 `stream_complete`, and
   `recording:frames_committed` responsibilities. Add final actual
   frame/partition counts and `completion_outcome`; do not introduce a second
   writer-liveness flag.
5. Persist `scan_source` and any explicitly supplied workflow modality so
   registry selection need not depend only on file extension.
6. Preserve per-partition layout identity across scan-lapse modes. Separate
   files/groups reuse zero-based spans and differ only by `partition.index`;
   only a genuinely combined time array adds time to `event_loops`.
7. Compare scan positions with `getNumScanPositions()` only when every loop
   kind can be classified as positional or not. Kinds are an open vocabulary,
   so a layout using a kind this version does not know must skip the
   comparison and log why, rather than assume the unknown loop advances the
   scan and refuse to record.

In `imswitch/imcontrol/model/managers/recording_metadata.py`, retain the
existing emitted OME axes in version 1 but mark their use as an interoperability
projection. They must no longer be treated as evidence that scan frames are
time.

### 5.3 Reader/writer parity

Add a dependency-light shared normalization helper at
`imswitch/imcommon/model/acquisition_metadata.py` and use it from all source
paths. It should operate through mapping/group protocols rather than importing
ImProcess, h5py, or Zarr-specific application layers.

Format readers first extract their native markers into this dependency-free
input; the shared helper never opens or traverses a storage object:

```python
@dataclass(frozen=True)
class RecordingLifecycleMarkers:
    writing: bool | None = None
    frames_committed: int | None = None
    stream_complete: bool | None = None
    live_writer_attached: bool = False  # runtime context, never serialized


normalize_recording_lifecycle(
    attrs: Mapping[str, Any],
    markers: RecordingLifecycleMarkers,
) -> RecordingLifecycle
```

The format mappings are explicit:

| Format | `writing` | `frames_committed` | `stream_complete` |
|---|---|---|---|
| Zarr | Detector array attribute `writing` | Detector array attribute `recording:frames_committed` | Not present |
| HDF5 | Detector data-dataset attribute `writing` | Sibling dataset `frames_committed[0]` in the detector group | Sibling uint8 dataset `stream_complete[0]` in the detector group |
| OME-TIFF | No mid-write reader marker; only finalized OME metadata is consumed | Final observed page/frame count | Not present |

`normalize_recording_lifecycle` returns `writer_state`,
`completion_outcome`, planned/actual counts, `frames_committed`, and structured
issues. Reader adapters own marker extraction; the shared helper owns marker
precedence. An attrs-only lookup is therefore insufficient and forbidden for
HDF5. This separation keeps `imcommon` free of h5py/Zarr imports while ensuring
offline and live readers cannot implement different completion rules.

| File | Concrete change |
|---|---|
| `imswitch/imcommon/model/acquisition_metadata.py` | Normalize flat/nested acquisition attributes, writer state, completion outcome, and provide the one metadata-flattening implementation |
| `imswitch/improcess/model/image_sources.py` | Use shared normalization on the preferred NGFF path; parse structured-Zarr axes; decode the layout; read TIFF StructuredAnnotations |
| `imswitch/improcess/live/sources.py` | Reuse shared normalization/decoder; expose partial-layout issues; stop maintaining a separate metadata interpretation |
| `imswitch/imcontrol/model/managers/RecordingManager.py` | Embed layout JSON in OME-TIFF StructuredAnnotations and write identical detector-local metadata to HDF5/Zarr |
| In-memory stack wrapper | Honour supplied axes/layout in the transport PR; defer changes to metadata-less rank defaults until the view-only migration |

The transport work is complete only when the canonical decoded layout is equal
across HDF5, batch OME-Zarr, live Zarr, and OME-TIFF.

## 6. Resolution and fallback policy

Add a resolver in:

```text
imswitch/improcess/model/acquisition_layout_resolver.py
```

The distinct basename is intentional: `imcommon/model/acquisition_layout.py`
owns the serialized contract, while this module only resolves explicit and
legacy inputs into that contract.

It returns both the resolved contract and how it was obtained:

```python
@dataclass(frozen=True)
class ResolvedAcquisitionLayout:
    layout: AcquisitionLayout
    source: str
    confidence: str        # certain, high, medium, low
    issues: tuple[LayoutIssue, ...]
```

Provenance supplies a default confidence, while concrete adapter assumptions
may only lower it:

| Provenance | Default confidence | Meaning |
|---|---|---|
| `recorded` | `certain` | A valid producer-authored layout |
| `user-override` | `certain` | Semantics intentionally chosen by the user; certainty describes the declaration, not hardware verification |
| `legacy-adapter` | `high` | A known producer contract; downgrade for missing/version-dependent fields |
| `ome-ngff` | `medium` | Container axes/calibration without an acquisition-loop contract |
| `shape-inference` | `medium` | One unique arithmetic interpretation; otherwise do not resolve it |
| `generic-fallback` | `low` | Opaque frame stream with no inferred acquisition meaning |

Resolution precedence is fixed:

1. A valid, explicit user override, recorded with the metadata it replaced.
2. Valid `imswitch.acquisition-layout/1` metadata.
3. A named legacy adapter for a known ImSwitch producer/version.
4. Non-conflicting OME/NGFF axes and calibration.
5. Shape/frame arithmetic, only when it yields one unique interpretation.
6. Generic frame-stream fallback with a visible low-confidence warning.

A user override is not automatic inference. It is an intentional correction
made after the original interpretation and issues have been shown, and it must
pass the same validation as producer metadata.

Rank 1 applies only when the resolver is given a persisted override. ImProcess
stores that override in its project document or, when the user explicitly
chooses **Persist layout override**, in an adjacent
`<source>.imswitch-layout.json` sidecar keyed by a source fingerprint plus the
detector/dataset path. A fingerprint mismatch invalidates the sidecar and shows
an issue. Reopening the project or source with its matching sidecar reapplies
the override; opening the original file alone does not. Raw source metadata is
never mutated silently. Saving a derived dataset embeds the effective layout
with `provenance=user-override` and retains the rejected/original interpretation
in processing provenance.

Explicit conflicts are errors. For example, if scan metadata describes
X/Y/condition loops while OME labels the physical frame axis `T`, the explicit
layout wins and the resolver reports the OME label as a lossy projection. If
two plausible loop orders remain, reconstruction does not start until the user
chooses one.

Malformed or internally inconsistent explicit layout metadata is also an
error. The resolver must not skip it and continue to legacy, OME, or shape
inference. The only recovery is a valid user override, which retains the bad
layout and its `LayoutIssue` records in provenance.

Expose the result as `DataObj.acquisition_layout` and
`StackInfo.acquisition_layout`. Preserve the container-declared OME/NGFF axes
separately for interoperability and compatibility; do not overload either
those axes or the canonical `storage_axes` with acquisition-loop nesting.

### Legacy adapters

Implement narrowly named adapters with fixtures for:

- `ScanStage:*` + `ScanTTL:*` Advanced Scan recordings;
- TriggerScope raster metadata;
- legacy MoNaLISA scan metadata;
- SNOUTY `cycleSteps`, `roSteps`, and time-lapse metadata;
- tiling manifests, where the manifest remains authoritative.

An adapter must state every assumption it made. In particular, the generic
legacy `frames_per_stack` fallback must include line steps when metadata proves
their presence; otherwise it must remain ambiguous instead of defaulting to
time. An adapter that encounters detector gating it cannot express must return
an ambiguity/error rather than collapsing it to a fixed condition subset.

## 7. Reconstructor preflight contract

Extend `imswitch/improcess/reconstructors/base.py` with declarative
requirements:

```python
@dataclass(frozen=True)
class AcquisitionRequirements:
    payload_kinds: frozenset[str]
    required_loop_kinds: frozenset[str] = frozenset()
    allowed_extra_loops: str = "reject"  # reject, select, split, reduce
    requires_calibrated_loops: frozenset[str] = frozenset()
    allow_ambiguous: bool = False
```

Generic preflight compares the resolved layout with these requirements and
adds actionable messages to `SourceInspection`. Plugin code receives validated
coordinates/arrays rather than raw metadata dictionaries.

Extend `SourceInspection` with `issues: tuple[LayoutIssue, ...]`. Retain its
current single `warning` field temporarily as a compatibility projection of
the highest-priority issue; new UI must render the structured issue list and
may link an issue to its field or loop.

The base `Reconstructor` initially declares
`acquisition_requirements: AcquisitionRequirements | None = None`. `None`
preserves current behaviour and skips the new rejection gate. A plugin opts
into strict preflight in the same PR that migrates it, so landing the resolver
alone is behaviour-neutral.

### 7.1 MoNaLISA

1. Replace the hotfix's direct `ScanTTL:n_linesteps` lookup with the shared
   recorded-coordinate/unfold helpers.
2. Use detector-specific event spans to identify recorded condition labels,
   including complementary and per-expanded-line enable masks.
3. Share the resolver between offline reconstruction and `live_session.py`.
4. Reject missing, extra, or partial frames before coefficient extraction.
5. Apply producer-global serpentine traversal before detector selection; do not
   recompute parity from the number of frames that one detector happened to
   record.
6. Keep actual time partitions separate from condition.

The current result contract is six-dimensional
`(Dataset, Base, T, Z, Y, X)` and currently folds conditions into `T`. Before
changing it, make a compatibility decision and test viewing, TIFF export, Zarr
export, saved-project reload, and processors. The preferred end state has a
separate Condition axis and reserves T for real elapsed time. A temporary
six-dimensional compatibility projection is acceptable only if its metadata
calls the axis Condition rather than Time and records the projection.

### 7.2 BeadRec offline reconstructor

This section applies to
`imswitch/improcess/reconstructors/beadrec/reconstructor.py`. The live ImControl
BeadRec wrap/streaming behaviour remains coordinated through
`beadrec-2.0.md`; shared coordinate helpers may be reused, but this plan does
not silently change its live buffer policy.

1. Obtain raster dimensions and step sizes from the resolved layout.
2. Remove square-grid guessing when explicit or adapted geometry exists.
3. If multiple conditions are present, reconstruct each separately or require
   an explicit selection; never mix them into one raster.
4. Require an exact frame count for a finalized offline source. A preserved
   `stopped_early` source reports its mismatch and requires an explicit partial
   processing policy; it is never silently padded or truncated.
5. Remove the offline buffer behaviour that can stop at an inferred square size
   and silently discard remaining frames.
6. Add a 648-frame regression proving that no 25 × 25 guess or 23-frame loss
   occurs.

### 7.3 SNOUTY and projections

1. Resolve plane, cycle, and time loops from recorded metadata.
2. Check exact divisibility and complete loop products before restacking.
3. Restack each timepoint independently instead of truncating the full stream
   to `cycles * planes`.
4. Use recorded `recording:num_timepoints` through the resolver; manual values
   become explicit user overrides.
5. Update tests that currently encode truncation as expected behaviour.

### 7.4 SMLM

1. Accept true time/frame streams.
2. Reject or request a selection for condition, channel, Z, or scan loops; do
   not flatten them into time.
3. Take pixel calibration from the source layout/OME scales, with a clearly
   recorded manual override when absent.

### 7.5 Widefield STARSS

1. Make its acquisition workflow write `modality`, polarization role (H/V),
   and signal/background state ordering.
2. Load both members of a pair through `DataObj` and the resolver rather than
   reading the counterpart directly with tifffile.
3. Validate matching shapes, calibrations, roles, and state loops before
   analysis.
4. Retain filename parsing only as a legacy adapter with low-confidence
   diagnostics.

### 7.6 View-only and tiling

- View-only displays resolved semantic coordinates when available. Unknown 3D
  data is labelled Frame × Y × X, not Time or Channel without evidence.
- Tiling keeps its existing manifest as authoritative. Adapt its inspection
  result to the generic layout/preflight surface; do not rewrite the manifest.

## 8. Delivery sequence

Keep changes reviewable by landing the work in the following pull requests.
Every PR includes tests and may land independently in order.

### PR 1 — Schema and normalization

**Status:** Implemented (`4f145653`).

**Files**

- Add `imswitch/imcommon/model/acquisition_layout.py`,
  `imswitch/imcommon/model/acquisition_metadata.py`, and unit tests.

**Acceptance**

- Canonical JSON encode/decode is deterministic and payload-specific validators
  enforce their declared count semantics.
- The B-only 18 × 18 × 2 selection canonicalizes to one periodic span.
- An equivalent non-canonical list decodes to that same canonical layout;
  an explicit all-events span canonicalizes to `None`, and duplicate traversal
  targets are rejected.
- Serpentine parity uses row-major flattening and tests both reset and
  carry-through boundaries.
- Event ordinals restart at zero per partition, while combined-array time is
  represented as an event loop.
- Span expansion, overlap detection, compaction, and the 1 MiB rejection path
  are covered without touching recording finalization.
- Metadata normalization has no ImControl/ImProcess or format-library imports.

### PR 2 — Format transport and recording lifecycle

**Status:** Implemented (`ee91ba7a`).

**Files**

- Update `RecordingManager.py`, `recording_metadata.py`, `image_sources.py`,
  and `live/sources.py`.
- Add HDF5, OME-Zarr batch/live, OME-TIFF, and in-memory round-trip tests.

**Acceptance**

- Canonical layouts round-trip without field loss in all four formats.
- `RecordingManager` accepts and persists synthetic per-detector layouts; no
  production scan source is required yet and the validation gate remains off.
- Offline and live Zarr expose identical normalized attributes.
- Zarr attribute markers and HDF5 sibling-dataset markers produce the same
  `RecordingLifecycleMarkers`/normalized lifecycle result; attrs-only HDF5
  extraction is covered by a failing regression test.
- Existing `writing`, HDF5 `stream_complete`, and `frames_committed` semantics
  remain authoritative and cannot disagree with normalized writer state.
- Streaming finalization records actual counts and completion outcome in every
  format, including a graceful `stopped_early` fixture; a stale `writing=true`
  fixture resolves to unknown completeness.
- Existing files without the new keys still open.
- Emitted OME axes and existing Fiji/OMERO round-trip expectations are
  unchanged in version 1.
- No storage axis is relabelled from shape alone when a layout is present.

### PR 3 — Resolver, legacy adapters, and preflight

**Status:** Implemented (`0a088493`).

**Files**

- Add `imswitch/improcess/model/acquisition_layout_resolver.py` and resolver
  tests.
- Expose the result through `DataObj` and `StackInfo`.
- Add `AcquisitionRequirements` and generic preflight to
  `reconstructors/base.py`.

**Acceptance**

- Resolution precedence and conflict handling are deterministic.
- Legacy recordings open with assumptions and confidence visible.
- Ambiguous loop order blocks reconstruction rather than silently choosing T.
- Invalid explicit metadata blocks automatic fallback; a validated user
  override can recover it and retains the original issues in provenance.
- A matching project/sidecar override survives reopening; a fingerprint
  mismatch is rejected and the raw source is not modified.
- Provenance/confidence defaults and allowed downgrades are deterministic.
- Reconstructors with `acquisition_requirements=None` behave exactly as before.
- Plugin inspection explains which metadata is missing or conflicting.

### PR 4 — Producers and recording integration

**Status:** Implemented (`cdcc55da`).

**Files**

- Add the producer protocol and adapters to Advanced Scan, MoNaLISA/point scan,
  TriggerScope raster, and TriggerScope RESOLFT controllers.
- Pass layouts through `RecordingController` and validate them in
  `RecordingManager`.
- Add Galvo, Beta, raster, detector-mask, scan-lapse, and multi-pulse tests.

**Acceptance**

- Every supported scan recording writes a detector-local explicit layout.
- Complementary detector enable masks produce different valid layouts.
- Per-expanded-line detector masks map every stored frame without forcing a
  rectangular condition subset.
- Separate-file/group lapse layouts are identical apart from
  `partition.index`, and every partition restarts event ordinals at zero.
- A layout exceeding the inline metadata budget blocks recording before a
  writer is opened with `LAYOUT_METADATA_TOO_LARGE`.
- Invalid planned-frame counts prevent recording before a writer is opened.
- A 3 × 2 × 2 test establishes actual loop order and serpentine parity for each
  producer family.

### PR 5 — MoNaLISA and BeadRec migration

**Status:** Implemented (`eee2b603`, `baf90d39`, `e2ab1b7a`).

**Files**

- Update MoNaLISA offline/live paths and result compatibility handling.
- Update BeadRec geometry, condition policy, and frame validation.

**Acceptance**

- The 18 × 18 × 2 example reconstructs A/B in alternating scan rows.
- A detector recording only A reconstructs all 324 frames as A.
- A row-dependent condition mask is either reconstructed through its explicit
  sparse coordinates or rejected by a declared plugin requirement; it is never
  coerced to a dense condition loop.
- HDF5, Zarr, live Zarr, and TIFF yield the same reconstructed grouping.
- Neither plugin silently truncates, squares, splits, or calls condition time.

### PR 6 — SNOUTY and SMLM migration

**Status:** Implemented (`69f139af`, `66a37936`).

**Acceptance**

- SNOUTY handles time × cycle × plane without cross-timepoint truncation.
- SNOUTY represents time as a loop for a combined array and as a partition for
  separate files/groups, with equivalent reconstructed coordinates.
- Incomplete cycles fail with an exact expected/observed count.
- SMLM refuses non-frame loops unless the user explicitly selects/reduces them.
- Source calibration is used and manual overrides are recorded.

### PR 7 — Widefield STARSS, view-only, and fallback cleanup

**Status:** Implemented (`5474fd05`).

**Acceptance**

- New STARSS recordings carry explicit roles and state order across formats.
- View-only shows Frame for unknown frame streams and presents layout issues.
- Registry selection can use recorded modality/source identity.
- Remaining generic shape/time guesses are either removed or reachable only
  through the visible low-confidence fallback.

## 9. Test matrix

At minimum, cover every checked combination below in automated tests or a
documented hardware validation run.

| Dimension | Cases |
|---|---|
| Producer | Advanced Galvo, Advanced Beta, MoNaLISA/point, TriggerScope raster, TriggerScope RESOLFT |
| Storage | HDF5, OME-Zarr batch, OME-Zarr live, OME-TIFF, in-memory |
| Recording mode | Scan once, scan lapse in one file, one file/group per timepoint, until-stop, graceful early stop, stale/crashed writer |
| Detector pattern | All conditions, complementary condition masks, per-expanded-line masks, multiple TTL pulses, scan-driven detector |
| Selection encoding | All events, one periodic condition span, compacted irregular spans, over-1-MiB rejection |
| Traversal | Forward, reverse, row-major serpentine, reset at Z/time, carry across Z/time, detector subset retaining producer parity |
| Layout | Frame stream, assembled condition/Y/X image, reconstructed image, cycle/plane/time, assembled TCSPC-bin cube, future photon event stream with registered validator, tile/position |
| Failure | Missing/extra frame, truncated live file, stale `writing=true`, stopped-early finalized file, invalid explicit layout, conflicting metadata, oversized layout, ambiguous legacy file |

Required invariant tests:

1. For 18 × 18 × 2, zero-based indices 0–17 map to A, 18–35 to B,
   and 36–53 to A; equivalently, human-facing frames 1–18 are A and 19–36
   are B.
2. The B-only detector selection canonicalizes to
   `{start=18, count=18, stride=1, period=36, repeats=18}` and selects exactly
   324 frames.
3. Eighteen valid unfactored B-line spans decode to exactly the same canonical
   layout and deterministic JSON as the periodic span; an explicit full-range
   span canonicalizes to `recorded_event_spans=None`.
4. The same canonical layout decodes identically from all supported formats.
5. A detector enabled only for source condition A has 324 frames and one
   selected producer condition; it does not receive B frames.
6. Serpentine parity equals the row-major flattened parity of the declared
   contiguous loop suffix. Separate tests prove reset at omitted Z/time
   boundaries and carry-through when those loops are included.
7. Producer-global parity is unchanged when another detector condition was not
   recorded.
8. A per-expanded-line enable mask round-trips to the exact stored-frame
   coordinates in every format.
9. Separate-file/group timepoints carry identical layouts apart from
   `partition.index`, every partition's first event ordinal is zero, and a
   combined time array instead carries an explicit time loop.
10. An assembled condition × scan-Y × scan-X image maps loops directly to
   storage axes and never runs frame-product/span validation.
11. A dense TCSPC-bin cube validates as `assembled-image`; a photon event stream
    is refused by strict preflight until its workflow registers a validator.
12. No raw scan frame is labelled time without explicit time metadata.
13. No reconstructor silently truncates frames or accepts an uneven split.
14. An explicit layout/OME conflict produces a diagnostic and preserves the
   explicit acquisition semantics.
15. Duplicate traversal rules for one `loop_id` are rejected.
16. An invalid explicit layout never falls through to shape inference; a valid
   user override is recorded with the rejected issues.
17. A matching persisted override survives reopening, while a mismatched
    fingerprint is rejected without modifying the source.
18. A graceful early stop records planned and actual counts, remains readable,
    and fails strict plugin preflight without being mistaken for a complete
    acquisition.
19. Zarr attributes and the HDF5 sibling datasets `frames_committed[0]` and
    `stream_complete[0]` normalize identically: committed frames limit the
    readable prefix but cannot finalize a source, HDF5 completion can finalize
    despite stale `writing=true`, and `writing=true` without a completion
    marker resolves to unknown completeness.
20. A layout over 1 MiB fails producer/recording preflight before opening a
    writer.
21. A legacy file remains usable only when inference is unique; otherwise it
    requires a user choice.

Test modules (as implemented in PR 1–4):

```text
imswitch/imcommon/_test/test_acquisition_layout.py
imswitch/imcommon/_test/test_acquisition_metadata.py
imswitch/imcontrol/_test/unit/test_acquisition_layout_adapters.py
imswitch/imcontrol/_test/unit/test_recording_acquisition_layout.py
imswitch/improcess/_test/test_acquisition_layout_resolver.py
imswitch/improcess/_test/test_acquisition_layout_transport.py
```

Layout-override coverage (sidecar reopen, fingerprint mismatch, override
recovering invalid file metadata) lives in the resolver module rather than a
separate `test_acquisition_layout_overrides.py`.

Add plugin regressions to their existing test directories rather than placing
all behavioural coverage in the resolver tests.

## 10. Compatibility and rollout

- Keep existing `ScanStage:*`, `ScanTTL:*`, and `recording:*` fields readable
  and writable during the migration. No removal is part of schema version 1.
- Keep emitted OME axes unchanged in version 1. The layout and projection
  diagnostic change ImSwitch's interpretation, not the file's compatibility
  axes.
- Do not rewrite old files in place. Persist user overrides only in an ImProcess
  project, an explicitly requested fingerprinted sidecar, or newly saved
  derived output.
- Once PR 4 lands, all supported scan recordings should write the new layout;
  non-scan and third-party recordings may continue without it.
- Gate changes to reconstructed result dimensionality separately from raw-data
  layout support. Saving and viewer compatibility must be proven before adding
  a seventh MoNaLISA Condition dimension.
- Show layout status in source inspection: **Recorded**, **Legacy inferred**,
  **User override**, **Ambiguous**, or **Invalid explicit metadata**.
- Log the resolver source and issues for diagnosis, but never rely on logs as
  the only user-visible warning.

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Actual loop order differs between Galvo, Beta, or firmware versions | Characterize each producer with 3 × 2 × 2 fixtures and publish from the component that builds the signal |
| OME-TIFF cannot express the semantic frame loops as axes | Store canonical JSON in StructuredAnnotations and treat OME axes as a lossy storage projection |
| Detector TTL masks vary by expanded line | Select producer-global events with recorded spans rather than forcing a static per-detector condition loop |
| Multiple pulses change frame counts/order | Represent pulses as a `repeat` loop and validate against actual detector edges |
| A result-axis change breaks viewers/exporters | Use an explicit compatibility gate and keep acquisition-layout delivery independent of the result-schema migration |
| Writer markers disagree or remain stale after a crash | Normalize existing `writing`/`stream_complete` markers, keep `frames_committed` as a prefix barrier only, and classify unresolved stale writers as unknown completeness |
| Gracefully stopped files are incomplete | Record `completion_outcome=stopped_early` plus planned/actual counts; enforce plugin-specific completeness only at preflight |
| Periodic/irregular detector selection creates oversized metadata | Canonically factor repeated spans, enforce the shared 1 MiB limit, and fail before writer open rather than diverging by format |
| Sparse/serpentine unfolding materializes a lazy source | Require an explicit copy policy and provide coordinate iteration/chunked gathers |
| Scan-driven detectors already assemble images | Use a distinct payload kind and storage axes; do not unfold them as camera streams |

## 12. Definition of done

This project is complete when:

- all supported scan producers emit an explicit, validated, per-detector
  layout;
- detector-specific static and per-expanded-line masks, repeated pulses, and
  serpentine traversal map every stored frame to producer-global coordinates;
- equivalent span encodings canonicalize identically, and event ordinals reset
  predictably at every partition boundary;
- recording and every supported format preserve it without semantic loss;
- existing writer-completion markers, committed-frame barriers, and the new
  completion outcome have one non-conflicting normalized interpretation;
- `DataObj`, live `StackInfo`, source inspection, and plugin preflight expose
  the same resolved contract;
- MoNaLISA, BeadRec, SNOUTY, SMLM, Widefield STARSS, and view-only follow their
  declared acquisition requirements;
- the motivating 648-frame recording and detector-specific variants pass in
  offline and live reconstruction;
- no reconstructed path silently invents time, guesses a square raster,
  truncates a partial loop, loses producer traversal parity, or flattens extra
  semantics;
- legacy inference is isolated, tested, reported with confidence, and used only
  after explicit metadata and known producer adapters have been exhausted.
