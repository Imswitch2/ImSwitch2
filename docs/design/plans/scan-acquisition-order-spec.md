# Scan acquisition order — specification, draft 4

**Status:** Draft 4 for review — design only, nothing implemented
**Date:** 2026-08-12
**Intent:** A portable description of *how a scan was performed*, complementing
OME rather than replacing it

---

## Changes since draft 3 (resolving review #2)

All eight findings accepted. Decisions rather than clarifications are marked ⚖.

1. **(F1) Order moved to the producer domain** — §5.1: explicit order is
   `(P, n_scan_axes)` over **producer** event ordinals, exactly what selection
   composes into; draft 3's `(N, …)` could not resolve a selected ordinal at
   all. ⚖ Consequence embraced: the order array is owned by the partition and
   **shared by all its detectors**; selection, timing, and ragged data remain
   per detector. Every companion role now declares its domain (§7).
2. **(F2) Interim coordinate tables un-regressed** — §7: shape
   `(*input_axis_extents, n_output_axes)` with ordered named input scan axes
   and named output physical axes — the jittered grid is `(Ny, Nx, 2)`, and
   the contract is the one RFC-5 `coordinates` expects. Evaluated at integer
   index tuples; interpolation is inherited from the RFC-5 transform on
   migration, not defined here.
3. **(F3) Payload placement completed** — §4.1: payload axes carry declared
   **extents**, and the storage binding is normative: a fixed-shape stream is
   stored measurement-major as `(N, *payload_extents)`. §4.2/§7: ragged
   **records** are their own companion role with a declared ordered field
   schema; offsets alone were only half the contract.
4. **(F4) Compact-form obligation made testable** — §3.3: ⚖ compact is
   mandatory **iff the writer generated the acquisition from a compact plan**;
   a merely *recorded* order may be emitted explicitly, and recognising latent
   affine structure is permitted but never required. Two writers of the same
   physical scan may therefore legitimately differ in form; `same_order`
   bridges them. Unbounded recognition obligations are gone.
5. **(F5) Injectivity validation made honest** — §2.1/§12: sampling language
   deleted. Compact injectivity is exactly decidable (kernel-in-difference-box
   test; reference algorithm ships with the conformance suite). ⚖ A reader
   either validates **exactly** or treats injectivity as a writer assertion
   and **must not claim** it was validated. Explicit forms are always
   validated exactly.
6. **(F6) Closure premises corrected** — §8: new premise (d): each payload is
   an **atomic contribution at one index tuple** — a fly-scan exposure
   integrating along a trajectory is one event at one logical index, and
   sub-event structure requires finer events. The intra-exposure exclusion now
   lives *in* the boundary, not after it. "Compactly when periodic" replaced
   by "compactly when admitted by §3.1" — bit-reversed sweeps are periodic and
   deterministic yet not affine + serpentine, and are now an explicit-form
   example (§9).
7. **(F7) `SPAN_LIMIT` demoted** — §5.2: ⚖ from a canonicality constant to a
   **recommended representation cutover** (SHOULD), with a carrier-profile
   note for formats that cannot hold companion arrays. Semantic equality was
   already carried by the equivalence predicates, so a syntactic cliff at
   64/65 bought nothing; boundary fixtures at 64/65 join the conformance
   suite. One rule stays normative: never emit both forms of one role.
8. **(F8) The two equivalences separated** — §3.3: `same_order` (placement
   equality: induced mapping + axis names/extents) versus `same_description`
   (placement **and** annotations: kinds, devices, labels). Deduplication on
   `same_order` alone must not drop annotations; each comparison context names
   its predicate.

---

## 1. The gap this fills

OME and OME-NGFF describe the **stored array**: which axis is which, its
physical scale, its unit, where the image sits on the sample. That is a
complete description of the *result*.

They say nothing about **how the samples were visited**. For a widefield
image that is fine — the array *is* the result. For a scan it is not: the
detector produces a stream of measurements in acquisition order, and turning
that stream into an array requires knowing which measurement belongs at which
coordinate. Today every acquisition system encodes that knowledge privately
and every reader re-derives it, usually by guessing from array shape and
frame count.

This specification describes exactly that missing piece, and nothing else.

> **Non-goal.** This does not describe pixels, calibration, units, channels,
> or stage position. Those are OME's, and a conforming file uses OME for
> them.

## 2. The model in one view

An acquisition is a sequence of **producer events** (dwells, exposures),
`P` of them per partition, ordinals `0 … P-1` in chronological order. Each
detector records some or all of them; each recorded event carries a
**payload**. Placement goes through three layers:

| Layer | Mapping | Owner |
|---|---|---|
| **L1 — nesting** | producer ordinal → loop counters | this spec |
| **L2 — order** | loop counters → scan-axis *indices* | this spec |
| **L3 — geometry** | scan-axis indices → physical coordinates | **OME/NGFF transforms** |

Orthogonal to the layers: **payload** (§4), **selection** (§5), **partition**
(§6), **companion arrays** (§7), **timing** (§4.3).

The decomposition is the load-bearing idea. Nearly everything that *looks*
like irregular order is irregular **geometry**: jittered ptychography,
golden-angle tomography, targeted sites all have regular index lattices and
irregular L3. Order and geometry must not be conflated, and L3 is already
OME's territory.

### 2.1 The result index space

This section makes placement deterministic; without it a reader cannot
construct the result.

- **Axes.** A description declares an ordered list of **scan axes** (the L2
  outputs), each with a name and an integer **extent**. The canonical result
  index space is the scan axes in declared order, followed by the payload
  axes in declared order. One recorded event's payload occupies the hyperslab
  at its scan-axis index tuple.
- **Name uniqueness.** Axis names must be unique across the union of scan
  axes and payload axes.
- **Bounds.** Every index produced by the order description — for **all** `P`
  producer ordinals, not only the selected ones — must lie in `[0, extent)`
  for its axis. Extents are declared, not inferred; writers should choose
  offsets so the minimum mapped index is 0.
- **Injectivity.** The composite map from a detector's *recorded* events
  (§5) to scan-axis index tuples **must be injective**, per detector. Two
  recorded events never share a tuple. Writers guarantee this (W5). Readers
  validate it **exactly** — for explicit forms by sort-and-compare, for the
  compact form by the kernel test (does any nonzero solution of
  `W·d = 0` lie in the counter-difference box? — decidable; reference
  algorithm in the conformance suite) — **or** treat it as a writer assertion
  and must not report it validated. There is no sampling middle ground: a
  sampled check that misses a collision is silent data corruption with a
  validation stamp on it.

  Injectivity replaces any collision, stacking or overwrite policy, and is
  why none is needed:

  - A repeat/averaging loop either receives its own **occurrence axis** (the
    repeats are distinct results) or is **consumed by selection** (only some
    repeats were recorded). A loop contributing to no axis and not consumed
    by selection is a guaranteed collision, hence invalid.
  - A trajectory that revisits positions (Lissajous) indexes by **sample
    number**, which is injective; that revisited samples coincide
    *physically* is L3, and binning them is reconstruction.
  - Averaging, summing, or last-wins are **reductions** — reconstruction, out
    of scope, and cleanly so because the indexed form is lossless.
- **Coverage.** The index space need not be filled. A gated or subsampled
  detector defines a subset of cells; the description itself states which, so
  a reader distinguishes "not acquired" from "acquired" without sentinels.

## 3. Order (L1 + L2)

Order belongs to the **producer** — it describes how the scan visited its
positions, once, regardless of how many detectors watched. Its domain is the
producer ordinals `0 … P-1`.

### 3.1 Compact form: nested loops + affine map + traversal

A scan runs nested loops, outermost first — a chronological statement about
the hardware (`for z: for y: for x:`). Each loop has an identifier, a count
≥ 1, a semantic kind (open vocabulary), and optionally the device that drove
it. `P` is the product of the counts.

Each loop **contributes** to scan-axis indices through integer weights:

```
index[axis] = offset[axis] + Σ_loops weight[axis][loop] · counter[loop]
```

A loop is not required to *be* an axis: two counters may combine into one
axis (`x = 2·k + p`), one counter may address a folded axis
(`z = cycles·plane + cycle`).

**Traversal** models alternating reversal (serpentine/bidirectional sweeps),
which is not affine. A traversal modifier attaches to a **loop**, not an
axis, and rewrites that loop's counter **before** the affine map — matching
the hardware, where it is the mirror's sweep that reverses. Normative
semantics:

```
counters = mixed_radix_decompose(producer_ordinal, loop_counts)  # L1
for each loop L, outermost first:
    if L.traversal == reverse:
        counters[L] = L.count - 1 - counters[L]
    if L.traversal == serpentine:
        flat = 0
        for Q in L.parity_loops:           # ordered, contiguous, outer to L
            flat = flat * Q.count + counters_raw[Q]   # raw, pre-rewrite
        if flat % 2 == 1:
            counters[L] = L.count - 1 - counters[L]
index[axis] = offset[axis] + Σ_L weight[axis][L] * counters[L]
```

Parity is computed from **raw** counters, so an outer loop's traversal does
not alter an inner loop's parity; loops omitted from `parity_loops` are
parity *reset* boundaries. Because traversal targets loops, "reverse the
composite axis `x = 2k + p`" is not expressible — deliberately, since no
single piece of hardware performs it. Traversal is a bijection of the counter
space, so it never affects injectivity; the kernel test above applies to the
weights alone.

### 3.2 Explicit form: recorded order as companion data

Some acquisitions choose their order from the data as they run — adaptive
sparse scanning, MINFLUX-style iterative probing, AI-driven acquisition —
and some deterministic orders are simply not affine + serpentine
(bit-reversed sweeps). After acquisition the order is **known** either way.

The explicit form is an integer companion array of shape
`(P, n_scan_axes)`: row *e* gives the scan-axis indices of producer ordinal
*e*. It is the producer's order, shared by every detector of the partition,
and it is exactly what selection ordinals index into. It replaces L1+L2
jointly (loops may still be declared as documentation). Bounds hold for all
`P` rows; injectivity (§2.1) applies per detector after selection.

### 3.3 Equivalence, and when each form is required

Two order descriptions are **`same_order`-equivalent** iff they induce the
same function from producer ordinals to scan-axis index tuples, with the
same axis names and extents. This is *placement* equality, and it is the
predicate meant whenever this spec says descriptions "compare equal". A
second, stricter predicate, **`same_description`**, additionally requires
equal annotations — loop kinds, devices, labels, timing presence. The two
must not be conflated: deduplication or comparison on `same_order` alone
must never discard annotations, which carry chronology and instrument
identity that placement does not. Every comparison context states which
predicate it uses. Both ship as reference implementations with the
conformance suite.

**Choosing the form (normative, testable).** The obligation follows what the
writer *knew*, not what it might recognise:

- An acquisition **generated from a compact plan** — the writer possessed the
  loops, weights and traversal before running — must be emitted in compact
  form. Enumerating a plan you hold into an explicit array is forbidden.
- An acquisition whose order was only **recorded** may be emitted explicitly.
  A writer *may* canonicalize to compact form if it proves `same_order`
  equivalence, but recognition is never required — deciding whether an
  arbitrary recorded array admits an affine factorization is an unbounded
  obligation no writer can be held to.
- A description must never contain both forms of the same role.

Consequently the same physical scan may legitimately appear compactly from
its planner and explicitly from a re-writer; `same_order` bridges the two.

Loop-structure normalization is best-effort, because loop structure is
semantics:

- A loop contributing to no axis and not referenced by selection or
  traversal is invalid (it cannot be injective).
- A loop with count 1 contributes nothing to order; it is permitted only to
  carry annotation and is ignored by `same_order` (but not by
  `same_description`).
- No merging or factoring of loops is performed or required: `for y: for x:`
  with two devices is not the same statement as one fused loop even when the
  induced mapping is identical.

## 4. Payload

### 4.1 Shape and storage binding

What one recorded event contributes: an ordered list of named payload axes,
each with a declared **extent**, of any rank ≥ 0. Rank 0 is a point
detector's scalar; rank 1 a line, spectrum, depth profile or TCSPC
histogram; rank 2 a camera frame; rank 3 a snapshot hyperspectral or
time-resolved camera event (`y × x × λ` or `y × x × bin`). The rank list is
illustrative; the definition is unbounded, and the closure claim (§8)
relies on that.

**Storage binding (normative).** A fixed-shape payload stream is stored
measurement-major: the detector's raw array has shape
`(N, *payload_extents)` with payload axes in declared order, where `N` is
that detector's recorded-event count. Readers construct hyperslabs from this
binding; serializations bind axis names to storage dimensions but do not
reorder them. Ragged payloads use the records + offsets roles instead
(§4.2, §7).

Payload axes need not be spatial: an Airyscan/ISM detector contributes a
vector indexed by *detector element*, whose physical geometry is instrument
metadata, out of scope here. Payload axis calibration is OME's.

### 4.2 Ragged payloads

Time-tagged photon streams (TCSPC/TTTR, SPAD arrays, coincidence counting)
produce a **variable-length record list per event**. The event is the dwell
or exposure; the payload is ragged: a **records** array of `M` entries with
a declared ordered field schema — per field: name, primitive dtype, unit —
plus an **offsets** array of shape `(N + 1,)` slicing records to events.
Both are companion arrays (§7). The association of records to events
(line/frame markers, pixel clocks) is the writer's problem to solve *before*
writing; a conforming file states event boundaries, not raw marker streams.

### 4.3 Timing

Timing is orthogonal to everything above: a compact raster can jitter, and
per-event times matter whether or not the result has a time axis. Events are
**intervals**, not instants.

Optional per-detector companion arrays: `time_start` (shape `(N,)`) and
either `time_end` or `duration`. Times are in a declared unit against a
declared **clock**: an opaque clock identifier, a monotonicity flag, and —
when known — the UTC time of clock zero. `time_start` is the start of the
event's integration window. Nominal spacing on a time axis remains OME's
`scale`; these arrays record what actually happened.

## 5. Selection

A detector may record a subset of the producer's events — gated to some
illumination conditions, enabled per line, or subsampled.

### 5.1 Forms and composition

- **Compact:** periodic spans (start, count, stride, period, repeats) over
  producer ordinals, in stored order.
- **Explicit:** an integer companion array of shape `(N,)` mapping
  stored-measurement index → producer ordinal.

Composition is: stored index → (selection) → producer ordinal → (order,
§3) → scan-axis indices. Selection values must be strictly within
`[0, P)`. Absent selection means identity, and `N = P`. Injectivity (§2.1)
is required after this composition, per detector.

### 5.2 Choosing the form

The canonicalization algorithm for spans is normative (merge adjacent runs,
factor periodic repetitions — the v1 reference implementation defines it),
so any span list has one canonical form. Beyond that:

- Writers **should** emit compact selection when its canonical form has at
  most `SPAN_LIMIT = 64` spans, and explicit selection beyond that. ⚖ This
  is a *representation recommendation*, not a semantic boundary — semantic
  equality is carried by the equivalence predicates, so nothing breaks at
  65; the constant exists so independent writers usually agree, and the
  conformance suite carries boundary fixtures at 64 and 65 spans.
- **Carrier profile:** a format that cannot hold companion arrays (§7) must
  use compact selection regardless of span count, up to its own metadata
  capacity; if the canonical form does not fit, that format cannot carry the
  description (§7, format capability).
- A description must never contain both forms.

## 6. Partition

A partition is a separately stored or separately transformed acquisition
unit. Planned and runtime-chosen sets use the same mechanism:

- **Planned:** timepoints of a lapse; tiles of a mosaic; wells and sites of
  a plate; views of a multi-view light-sheet, each carrying its own L3/OME
  transform (the rotation that registration refines).
- **Runtime-chosen:** event-triggered sites; MINFLUX per-emitter iterations —
  detected, then acquired.

**The separation that keeps data-dependence tractable:** adaptive systems
overwhelmingly choose *which acquisitions happen*, not the order within one.
Detect → transform a coordinate → run an ordinary scan there. Only per-event
adaptivity (§3.2) needs the explicit form.

Within one partition, producer ordinals restart at zero; a partition records
its kind, index, planned count and storage mode, so lapse files are
identical apart from the index.

## 7. Companion arrays

One mechanism, uniformly, for everything too large or irregular for
metadata. There is no second escape hatch. Every role declares its
**domain**: `P` = producer events of one partition; `N` = one detector's
recorded events in that partition; `M` = one detector's ragged records.

| Role | Domain | Shape | Dtype | Owned by | Validation |
|---|---|---|---|---|---|
| explicit order | P | `(P, n_scan_axes)` | integer | partition (shared by its detectors) | all rows in bounds; per-detector injectivity after selection |
| explicit selection | N | `(N,)` | integer | detector | values in `[0, P)` |
| ragged records | M | `(M,)` structured per declared field schema | per field | detector | schema declared; length M |
| ragged offsets | N | `(N + 1,)` | integer | detector | monotone non-decreasing, first 0, last M |
| time_start / time_end / duration | N | `(N,)` | numeric | detector | end ≥ start; unit and clock declared |
| coordinate table (interim L3, §11) | index lattice | `(*input_axis_extents, n_output_axes)` | numeric | partition | ordered named input scan axes; named output physical axes; defined at integer index tuples only — interpolation is inherited from the RFC-5 transform on migration, not defined here |

A jittered 2-D ptychography grid is therefore `(Ny, Nx, 2)` — the joint
form; draft 3's per-axis-group shape could not express it and regressed
from the joint L3 model that motivated §2.

Integer values must fit in a signed 64-bit integer; storage dtype is the
container's choice. Arrays are C-ordered.

**Naming and custody.** The metadata refers to each companion array by role
and name; each serialization binds names to storage locations — in Zarr and
HDF5, arrays at a spec-defined subpath adjacent to the partition (shared
roles) or the detector's data (per-detector roles). Lengths and bounds are
cross-checked at read time; a mismatch invalidates the *description*, never
the data.

**Format capability (normative consequence).** A format that cannot store
named companion arrays — plain single-file TIFF — cannot carry the explicit
forms, ragged payloads, or interim coordinate tables. A writer targeting
such a format must either restrict itself to what the compact forms express,
or write a sidecar container and reference it.

## 8. The closure claim — why this should not need reopening

**Claim.** Any acquisition that is (a) composed of discrete events, (b)
whose event order is known by the end of acquisition — planned or recorded —
(c) whose measurements the writer can associate to events, and (d) whose
payloads are each an **atomic contribution at a single scan-index tuple**,
is expressible: compactly when admitted by §3.1, explicitly otherwise; and
its indexed result is well-defined by §2.1.

**The boundary, stated honestly.** (a) excludes continuous, un-evented
streams until the writer defines events. (b) excludes orders the writer
failed to record, which W2 forbids. (c) excludes marker-association
problems, which are upstream of any description. (d) excludes sub-event
structure: a fly-scan exposure that integrates along a trajectory is one
event at one logical index — if the structure *within* it matters, the
writer must define finer events; no description of order can recover what
the exposure already integrated away. Intra-exposure motion (lattice
dithering, swept illumination, resonant averaging within a pixel) is the
benign case of (d).

Note the compact scope is §3.1's, not "anything periodic": a bit-reversed
sweep is periodic and deterministic yet not affine + serpentine, and takes
the explicit form.

**Evolution without structural change.** New techniques land as one of: a
new loop-kind or payload word (open vocabulary), a new L3 transform type
(delegated to NGFF), a new companion-array role (feature-gated, §10). A
structural reopen would require an acquisition violating (a)–(d) — none is
known, and §9 is the evidence.

## 9. Worked examples, by which mechanism carries the burden

Techniques marked † are not implemented in ImSwitch and were used to stress
the model.

**Identity order, uniform geometry** — the trivial majority:

| Technique | Shape |
|---|---|
| Confocal / STED point raster | y, x loops; rank 0 |
| Widefield line-step (MoNaLISA) | y, condition, x; rank 2 |
| SIM † | angle, phase (, z); rank 2 |
| Hyperspectral point scan † | y, x; rank 1 (spectrum) |
| Snapshot hyperspectral / time-resolved camera † | t; rank 3 (`y × x × λ/bin`) |
| OCT † | B-scan, A-scan; rank 1 (depth profile) |
| Photoacoustic scanning † | y, x; rank 1 (time trace) |
| Airyscan / ISM † | y, x; rank 1 (detector elements) |
| FLIM, histogram mode | y, x; rank 1 (TCSPC bins) |
| Pump–probe delay series † | delay, y, x; delay calibration in L3 |

**Non-identity affine order (L2 does the work):**

| Technique | Weights |
|---|---|
| Interleaved sweep † | `x = 2·k + p` |
| MS-RESOLFT / OPM stack | `z = cycles·plane + cycle` — exists in production as a bespoke de-interlacer today |
| Bidirectional anything | serpentine on the fast **loop**, parity over declared outer loops |
| Frame averaging, kept per-repeat | repeat loop → occurrence axis (injectivity, §2.1) |

**Tabulated geometry (L3 does the work; order stays trivial):**

| Technique | Table |
|---|---|
| Ptychography † | jittered `(Ny, Nx, 2)` joint table |
| Fourier ptychography † | illumination angle per LED index |
| Golden-angle tomography † | angle per projection index |
| Spiral / Lissajous scan † | joint (x, y) trajectory per sample index — injective by sample number; physical revisits fold in reconstruction |
| Random-access AOD targeting † | position per target index |
| Mesoscope multi-ROI † | ROI origin lookup ⊕ uniform intra-ROI step |
| Non-uniform / phased timelapse † | timestamps per event (§4.3) |
| Sinusoidal resonant axis (unlinearized) † | position per pixel index |

**Explicit order or selection (the escape hatch):**

| Technique | Explicit array |
|---|---|
| Adaptive sparse scanning † | order: visited indices, recorded post hoc |
| MINFLUX-style iterative probing † | order within each per-emitter partition |
| Bit-reversed / permuted sweeps † | order: periodic and deterministic, but not affine + serpentine |
| Compressed-sensing subsampling † | selection: pseudo-random subset (canonical spans ≫ `SPAN_LIMIT`) |
| AI-driven acquisition † | order, recorded post hoc |

**Partitions:**

| Technique | Partitioning |
|---|---|
| Scan lapse | one per timepoint |
| Tiled mosaic | one per tile (existing manifest remains authoritative) |
| Multi-well / HCS † | one per well site, positions tabulated |
| Multi-view light-sheet † | one per view, per-view L3 transform |
| Event-triggered (EtSTED / EtMonalisa) | one per detected event; ordinary scan inside |

**Payload does the work:**

| Technique | Payload |
|---|---|
| TTTR / SPAD photon streams | ragged records + offsets (§4.2) |
| Transmission PMT alongside camera | rank 0 and rank 2 detectors in one scan, each with its own description; one shared order |

**Atomic by definition (§8 premise d):** fly-scan integration along a
trajectory, lattice dithering, axially swept illumination, resonant
averaging within a pixel, DMD flicker — one event, one index; finer
structure needs finer events.

## 10. Versioning and required features

Three mechanisms, with a sharp rule about which carries semantics:

- **Schema version.** A reader refuses a version it does not know — a
  version change may redefine existing fields — while the pixel data stays
  viewable as an undescribed stream.
- **`required_features`.** A list of registered feature names. A reader
  lacking any listed feature treats the *description* as not understood —
  same degradation as an unknown version — rather than computing plausible
  but wrong coordinates. Any addition that changes the interpretation of
  existing fields (a new traversal order, a new companion role that alters
  placement) must either be listed here or bump the version.
- **Advisory fields.** Unknown fields not gated by `required_features` are
  advisory **by writer obligation**: a writer must not emit meaning-changing
  information un-gated. Readers ignore and report them.

## 11. Alignment with OME/NGFF — two profiles

L3 is deliberately delegated, but "delegated" must name a version:

- **Profile A — released OME-Zarr.** L3 restricted to what released versions
  express: per-axis `scale` + `translation`. Everything beyond — coordinate
  tables, cross-terms, trajectories — is carried as companion arrays under
  this spec's namespace. Released versions also constrain image
  dimensionality and axis types; that constrains *derived images*, not this
  description: **the raw event stream is not an NGFF image and does not
  claim conformance** — it is a stream plus this description. Derived,
  reconstructed images are where NGFF conformance applies, exactly as today.
- **Profile B — NGFF coordinate-transformations line (RFC-5).** Affine with
  cross-terms, lookup-table coordinates, and composition express L3
  natively; the interim tables migrate into `coordinates` transforms —
  same joint shape, same integer-lattice evaluation, gaining RFC-5's
  interpolation semantics. Migration is mechanical and one-way (A → B); a
  Profile B writer must not also emit interim tables.

RFC-5 belongs to the development line, not the released spec; Profile A
exists so that "portable today" is true today, and Profile B so that
"aligned with OME" has a concrete target rather than a hand-wave.

## 12. Conformance

A **reader** must:

- R1. Support both compact and explicit forms of order and selection, in
  containers that support companion arrays (§7).
- R2. Refuse an unknown schema version or a missing `required_feature`,
  leaving the pixel data viewable as an undescribed stream.
- R3. Ignore and report unknown un-gated fields (advisory by W4).
- R4. Never infer order from array shape or measurement count when a
  description is present.
- R5. Validate bounds, lengths, domains and — for explicit forms — exact
  injectivity before acting on a description. For the compact form, either
  run an exact injectivity check (reference algorithm provided) or treat
  injectivity as the writer's assertion and never report it validated. An
  invalid description is refused; the data stays viewable.

A **writer** must:

- W1. Emit a complete description or none — a partial one is worse than
  absent, because a reader cannot tell which parts were guessed.
- W2. Refuse to describe a detector whose order it cannot state.
- W3. Emit the compact form for acquisitions generated from a compact plan;
  emit either form for merely recorded orders (§3.3); follow the selection
  recommendation and carrier rules (§5.2); never emit both forms of one
  role.
- W4. Never emit meaning-changing information outside `required_features` or
  a version bump (§10).
- W5. Guarantee injectivity and bounds (§2.1).

A conformance suite ships with the spec: §9 as fixtures with the expected
index tuple for every event; one fixture per escape hatch; span-boundary
fixtures at 64 and 65; and reference implementations of `same_order`,
`same_description`, the span canonicalizer, and the exact injectivity check.

## 13. Deliberately out of scope

- Pixels, dtype, compression, chunking.
- Physical calibration and units — OME's.
- Reconstruction — including every **reduction** of colliding measurements
  (averaging repeats, binning trajectory revisits). The indexed form this
  spec defines is lossless; reductions are the application's.
- Sub-event structure — §8 premise (d): what an exposure integrated away, no
  order description recovers.
- Correction parameters: bidirectional phase offset, field distortion,
  drift. Calibration, not order.
- Writer lifecycle: whether a file is finished, how much is committed.
  Orthogonal, solved elsewhere.
- Instrument description — OME's.

## 14. Open questions for review

1. **Serialization host.** OME-XML StructuredAnnotation, NGFF extension key,
   or sidecar with a registered name. Constrained by §7: the host must name
   companion arrays; plain TIFF is already excluded from the explicit forms.
2. **Naming.** "Event", "loop", "axis", "partition" are this draft's terms
   and all overloaded elsewhere.
3. **Payload geometry.** Is a named-axes payload enough, or does the
   detector-element case (Airyscan) deserve a structured pointer to
   instrument geometry rather than a bare axis name?
4. **Feature-name registry governance** — who admits names to
   `required_features`, and where the registry lives.
5. **⚖ Shared-order custody in practice.** Order arrays owned by the
   partition and shared across detectors is clean on paper; serializations
   must make the sharing concrete (one array, referenced by each detector)
   without inviting divergent copies.

Resolved since draft 3: order domain (F1, §3.2/§5.1); table shape (F2, §7);
payload binding and records (F3, §4); recognition obligation (F4, §3.3);
injectivity validation (F5, §2.1/R5); closure premises and compact scope
(F6, §8); span-limit demotion (F7, §5.2); equivalence separation (F8, §3.3).

## 15. Relationship to the current implementation

The layout contract on `codex/acquisition-layout-schema` implements the
compact form with identity weights only, plus selection spans and partitions
— a strict special case of this model. Its concepts survive; its schema need
not, because the branch is unmerged and no recording outside tests has ever
carried it. There is no legacy generation to support, which is why the
schema can still change freely — and why it should be settled before merge.

Its span-canonicalization algorithm becomes the normative one of §5.2, its
loop-targeted traversal is exactly §3.1, and its producer-lattice-plus-spans
structure is exactly the producer-domain order of §3 — draft 3's
recorded-domain explicit order contradicted it, and review #2 caught that.

One v1 field has no v2 counterpart: the per-loop `direction`. v1 settled
(2026-09-04) that it is the orientation of a loop's logical index axis
against the physical axis (index 0 at the highest physical coordinate when
`-1`), applied exactly once by one imcommon helper, and that a monotonically
stepped axis is always a `forward` traversal whatever its direction. Under
this model a v1 `direction = -1` migrates to the L2 counter-to-index map of
§3.1 as weight `-1` with offset `count - 1`: the index space stays mirrored
exactly as v1's `iter_physical_coordinates` produces it, the traversal is
unchanged, and the L3 scale (§11) stays positive. It never becomes a
`reverse` rule, which remains chronology. (The alternative — index
unmirrored, sign pushed into a negative L3 scale — would be a different
index space and is not the migration.) Nothing in a v1 file needs rewriting;
the weight is derived at read time.

The machinery around the schema — resolver and precedence, legacy adapters
for pre-contract files, the usable/authoritative rule, transport across four
formats, the recording gate, the seam tests — is independent of the model
and is a prerequisite for it. The eight data-corruption bugs fixed along the
way are independent of both.
