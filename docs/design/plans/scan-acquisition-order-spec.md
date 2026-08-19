# Scan acquisition order — specification, draft 3

**Status:** Draft 3 for review — design only, nothing implemented
**Date:** 2026-08-12
**Intent:** A portable description of *how a scan was performed*, complementing
OME rather than replacing it

---

## Changes since draft 2 (resolving review #1)

All nine findings accepted. Where a finding forced a design decision rather
than a clarification, the decision is marked ⚖ for re-review.

1. **(F1) Placement is now defined** — §2.1: result index space, payload
   embedding, unique axis names, declared extents, bounds, and ⚖ an
   **injectivity requirement** over recorded events in place of any
   collision/stacking policy. Repeats either get an occurrence axis or are
   consumed by selection; reduction is reconstruction and stays out of scope.
2. **(F2) Selection canonicality fixed** — §5: a normative canonicalization
   algorithm plus ⚖ a fixed span-count limit decides compact vs explicit
   deterministically. "Compact whenever possible" is gone.
3. **(F3) Equality claim corrected** — §3.3: descriptions are compared by the
   **induced event→index mapping**, via a normative equivalence predicate in
   the conformance suite, not by syntactic equality. W3 obligations are now
   testable; loop-structure normalization rules are listed but loop structure
   itself is semantic (kinds, devices) and is not collapsed.
4. **(F4) Traversal made precise** — §3.1: traversal targets a **loop
   counter**, applied before the affine map, with normative pseudocode. This
   restores the v1 semantics that draft 2's prose ("on one axis") broke;
   "reverse the composite x" is deliberately not expressible.
5. **(F5) Timing decoupled** — §4.3: per-event interval timestamps as a
   companion record, orthogonal to compact/explicit order and to whether a
   time axis exists, with declared clock and unit.
6. **(F6) Companion arrays get a contract** — §7: roles, shapes, dtypes,
   validation, ownership; and ⚖ the honest consequence that formats unable to
   name companion arrays (plain TIFF) cannot carry the explicit forms.
7. **(F7) Payload rank generalized** — §4.1: any rank ≥ 0 with an ordered
   named-axis list; snapshot hyperspectral (y×x×λ per event) added as the
   rank-3 example. The closure claim no longer exceeds the payload model.
8. **(F8) Meaning-changing additions gated** — §10: a `required_features`
   list; a reader lacking a listed feature treats the description as
   not-understood (data stays viewable). Un-gated unknown fields are advisory
   **by writer obligation**, not by hope.
9. **(F9) NGFF claim split into profiles** — §11: Profile A against released
   OME-Zarr (scale/translation only, tables carried here), Profile B against
   the coordinate-transformations line (RFC-5), with the migration rule. The
   raw event stream is not an NGFF image and does not pretend to be one.

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

An acquisition is a sequence of **events** (dwells, exposures). Each event
carries a **payload** (what the detector measured) and maps into the result
through three layers:

| Layer | Mapping | Owner |
|---|---|---|
| **L1 — nesting** | event index → loop counters | this spec |
| **L2 — order** | loop counters → axis *indices* | this spec |
| **L3 — geometry** | axis indices → physical coordinates | **OME/NGFF transforms** |

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
  axes in declared order. One event's payload occupies the hyperslab at its
  scan-axis index tuple.
- **Name uniqueness.** Axis names must be unique across the union of scan
  axes and payload axes.
- **Bounds.** Every mapped index must lie in `[0, extent)` for its axis.
  Extents are declared, not inferred; writers should choose offsets so the
  minimum mapped index is 0.
- **Injectivity.** The map from *recorded* events (after selection, §5) to
  scan-axis index tuples **must be injective**. Two recorded events never
  share a tuple. Writers must guarantee this; readers should validate it —
  exactly in the explicit form (sort/unique), arithmetically or by sampling
  in the compact form.

  This replaces any collision, stacking or overwrite policy, and it is why
  none is needed:

  - A repeat/averaging loop either receives its own **occurrence axis** (the
    repeats are distinct results) or is **consumed by selection** (only some
    repeats were recorded). A loop contributing to no axis and not consumed
    by selection is a guaranteed collision, hence invalid.
  - A trajectory that revisits positions (Lissajous) indexes by **sample
    number**, which is injective; that revisited samples coincide
    *physically* is L3, and binning them is reconstruction.
  - Averaging, summing, or last-wins are **reductions** — reconstruction, out
    of scope, and now cleanly so because the indexed form is lossless.
- **Coverage.** The index space need not be filled. A gated or subsampled
  detector defines a subset of cells; the description itself states which,
  so a reader distinguishes "not acquired" from "acquired" without
  sentinels.

## 3. Order (L1 + L2)

### 3.1 Compact form: nested loops + affine map + traversal

A scan runs nested loops, outermost first — a chronological statement about
the hardware (`for z: for y: for x:`). Each loop has an identifier, a count
≥ 1, a semantic kind (open vocabulary), and optionally the device that drove
it.

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
counters = mixed_radix_decompose(event_index, loop_counts)   # L1, outermost first
for each loop L, outermost first:
    if L.traversal == reverse:
        counters[L] = L.count - 1 - counters[L]
    if L.traversal == serpentine:
        flat = 0
        for P in L.parity_loops:            # ordered, contiguous, outer to L
            flat = flat * P.count + counters_raw[P]   # raw (pre-rewrite) counters
        if flat % 2 == 1:
            counters[L] = L.count - 1 - counters[L]
index[axis] = offset[axis] + Σ_L weight[axis][L] * counters[L]
```

Parity is computed from **raw** counters, so an outer loop's own traversal
does not alter an inner loop's parity; loops omitted from `parity_loops` are
parity *reset* boundaries. Because traversal targets loops, "reverse the
composite axis `x = 2k + p`" is not expressible — deliberately, since no
single piece of hardware performs it.

### 3.2 Explicit form: recorded order as companion data

Some acquisitions choose their order from the data as they run: adaptive
sparse scanning, MINFLUX-style iterative probing, AI-driven acquisition.
Their order is not affine and not periodic — but after acquisition it is
**known**.

The explicit form is an integer array of shape `(N_events, n_scan_axes)`:
row *i* gives the scan-axis indices of event *i*. It replaces L1+L2 jointly
(loops may still be declared as documentation). It is stored as a companion
array (§7), so its size is bounded by the data, not by a metadata budget.
Injectivity and bounds (§2.1) apply unchanged.

### 3.3 Equivalence and canonical form

Two descriptions of order are **equivalent** iff they induce the same
function from recorded events to scan-axis index tuples, with the same axis
names and extents. Comparison is semantic, not syntactic; a normative
`same_order(a, b)` predicate ships with the conformance suite. "Compare
equal" in this spec always means this predicate.

Syntactic normalization is best-effort, because loop structure carries real
semantics (kinds, devices, chronology) that must not be collapsed:

- A loop contributing to no axis and not referenced by selection or
  traversal is invalid (it cannot be injective).
- A loop with count 1 contributes nothing to order; it is permitted only to
  carry annotation (labels, device) and is ignored by the equivalence
  predicate.
- Offsets should place the minimum mapped index at 0.
- No merging or factoring of loops is performed or required: `for y: for x:`
  with two devices is not the same statement as one fused loop, even when
  the induced mapping is identical. The equivalence predicate, not syntax,
  is the arbiter.

**Choosing the form (normative).** A writer must emit the compact form iff
the order admits one (it planned the scan affinely, or recognises the
recorded order as affine + traversal); otherwise the explicit form. An
explicit array that enumerates an order admitting a compact description is
non-canonical.

## 4. Payload

### 4.1 Shape

What one event contributes: an ordered list of named payload axes of **any
rank ≥ 0**. Rank 0 is a point detector's scalar; rank 1 a line, spectrum,
depth profile or TCSPC histogram; rank 2 a camera frame; rank 3 a snapshot
hyperspectral or time-resolved camera event (`y × x × λ` or `y × x × bin`).
The rank list in earlier drafts was illustrative; this definition is the
normative one, and the closure claim (§8) relies on it being unbounded.

Payload axes need not be spatial: an Airyscan/ISM detector contributes a
vector indexed by *detector element*, whose physical geometry is instrument
metadata, out of scope here. Payload axis calibration is OME's.

### 4.2 Ragged payloads

Time-tagged photon streams (TCSPC/TTTR, SPAD arrays, coincidence counting)
produce a **variable-length record list per event**. The event is the dwell
or exposure; the payload is ragged: records plus an offsets array, both
companion data (§7). The association of records to events (line/frame
markers, pixel clocks) is the writer's problem to solve *before* writing; a
conforming file states event boundaries, not raw marker streams.

### 4.3 Timing

Timing is orthogonal to everything above: a compact raster can jitter, and
per-event times matter whether or not the result has a time axis. Events are
**intervals**, not instants.

Optional per-event companion arrays: `time_start` (shape `(N,)`) and either
`time_end` or `duration`. Times are in a declared unit against a declared
**clock**: an opaque clock identifier, monotonicity flag, and — when known —
the UTC time of clock zero. `time_start` is the start of the event's
integration window. Nominal spacing on a time axis remains OME's `scale`;
these arrays record what actually happened.

## 5. Selection

A detector may record a subset of the producer's events — gated to some
illumination conditions, enabled per line, or subsampled. Two forms:

- **Compact:** periodic spans (start, count, stride, period, repeats) over
  producer event ordinals, in stored order.
- **Explicit:** an integer companion array mapping stored-measurement index
  → producer event ordinal.

**Choosing the form (normative).** Singleton spans can express anything, so
"compact when possible" would be vacuous — the review's finding is correct.
Instead: the **canonicalization algorithm is normative** (merge adjacent
runs, factor periodic repetitions; the v1 reference implementation defines
it). Run it; if the canonical result has at most **`SPAN_LIMIT = 64`**
spans, the compact form is canonical; otherwise the explicit form is. ⚖ The
value 64 is a determinism constant, not an optimum: real gating patterns
(per-line masks, complementary detectors, pulse subsets) compact to a
handful of spans via periodicity, while pseudo-random selections jump to
thousands; any fixed value in the gap serves, and one value must be fixed
for two writers to agree.

Selection composes before order: stored index → (selection) → event ordinal
→ (order) → scan-axis indices. Injectivity (§2.1) is required after this
composition.

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

Within one partition, event ordinals restart at zero; a partition records
its kind, index, planned count and storage mode, so lapse files are
identical apart from the index.

## 7. Companion arrays

One mechanism, uniformly, for everything too large or irregular for
metadata. There is no second escape hatch.

**Roles and shapes** (N = recorded events of one detector in one partition;
M = ragged records):

| Role | Shape | Dtype | Validation |
|---|---|---|---|
| explicit order | `(N, n_scan_axes)` | integer | bounds per axis extent; injective |
| explicit selection | `(N,)` | integer | strictly within producer event count |
| ragged offsets | `(N + 1,)` | integer | monotone non-decreasing, first 0, last M |
| time_start / time_end / duration | `(N,)` | numeric | end ≥ start; unit and clock declared |
| coordinate table (interim L3, §11) | `(extent, n_phys)` per axis group | numeric | length equals extent |

Integer values must fit in a signed 64-bit integer; storage dtype is the
container's choice. Arrays are C-ordered.

**Naming and custody.** The metadata refers to each companion array **by
role and name**; each serialization binds names to storage locations — in
Zarr and HDF5, arrays at a spec-defined subpath adjacent to the detector's
data, owned per detector per partition. Lengths are cross-checked against
the recorded-event count at read time; a mismatch invalidates the
description (it does not invalidate the data).

**Format capability (normative consequence).** ⚖ A format that cannot store
named companion arrays — plain single-file TIFF — **cannot carry the
explicit forms, ragged payloads, or interim coordinate tables**. A writer
targeting such a format must either restrict itself to acquisitions the
compact forms express, or write a sidecar container and reference it. Draft
2's "TIFF appendix" was hand-waving; this is the honest rule.

## 8. The closure claim — why this should not need reopening

**Claim.** Any acquisition that is (a) composed of discrete events, (b)
whose event order is known by the end of acquisition — planned or recorded —
and (c) whose measurements the writer can associate to events, is
expressible: compactly when periodic, explicitly otherwise; and its indexed
result is well-defined by §2.1 (bounds + injectivity + declared coverage).

**The boundary, stated honestly.** (a) excludes continuous, un-evented
streams until the writer defines events; (b) excludes orders the writer
failed to record, which W2 forbids; (c) excludes marker-association
problems, which are upstream of any description.

**Evolution without structural change.** New techniques land as one of: a
new loop-kind or payload word (open vocabulary), a new L3 transform type
(delegated to NGFF), a new companion-array role (feature-gated, §10). A
structural reopen would require an acquisition violating (a)–(c) — none is
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
| Ptychography † | jittered (y, x) per grid index |
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
| TTTR / SPAD photon streams | ragged record list per dwell (§4.2) |
| Transmission PMT alongside camera | rank 0 and rank 2 detectors in one scan, each with its own description |

**Invisible by design** (not events at all): intra-exposure motion — lattice
dithering, axially swept illumination, resonant averaging within a pixel,
DMD flicker. The event is the exposure; what happens inside it is not order.

## 10. Versioning and required features

Three mechanisms, with a sharp rule about which carries semantics:

- **Schema version.** A reader refuses a version it does not know — a
  version change may redefine existing fields — while the pixel data stays
  viewable as an undescribed stream.
- **`required_features`.** A list of registered feature names. A reader
  lacking any listed feature treats the *description* as not understood —
  same degradation as an unknown version — rather than computing plausible
  but wrong coordinates. Any addition that **changes the interpretation** of
  existing fields (a new traversal order, a new companion role that alters
  placement) must either be listed here or bump the version.
- **Advisory fields.** Unknown fields not gated by `required_features` are
  advisory **by writer obligation**: a writer must not emit
  meaning-changing information un-gated. Readers ignore and report them.

This closes the hole the review named: without the gate, "ignore unknown
fields" was safe only by luck.

## 11. Alignment with OME/NGFF — two profiles

L3 is deliberately delegated, but "delegated" must name a version:

- **Profile A — released OME-Zarr.** L3 restricted to what released
  versions express: per-axis `scale` + `translation`. Everything beyond —
  coordinate tables, cross-terms, trajectories — is carried as companion
  arrays under this spec's namespace. Released versions also constrain image
  dimensionality and axis types; that constrains *derived images*, not this
  description: **the raw event stream is not an NGFF image and does not
  claim conformance** — it is a stream plus this description. Derived,
  reconstructed images are where NGFF conformance applies, exactly as
  today.
- **Profile B — NGFF coordinate-transformations line (RFC-5).** Affine with
  cross-terms, lookup-table coordinates, and composition express L3
  natively; the interim companion tables migrate into `coordinates`
  transforms. Migration is mechanical and one-way (A → B); a Profile B
  writer must not also emit interim tables.

RFC-5 belongs to the development line, not the released spec; Profile A
exists so that the claim "portable today" is true today, and Profile B so
that the claim "aligned with OME" has a concrete target rather than a
hand-wave.

## 12. Conformance

A **reader** must:

- R1. Support both compact and explicit forms of order and selection, in
  containers that support companion arrays (§7).
- R2. Refuse an unknown schema version or a missing `required_feature`,
  leaving the pixel data viewable as an undescribed stream.
- R3. Ignore and report unknown un-gated fields (advisory by W4).
- R4. Never infer order from array shape or measurement count when a
  description is present.
- R5. Validate bounds, lengths and (at least for explicit forms) injectivity
  before acting on a description; an invalid description is refused, the
  data stays viewable.

A **writer** must:

- W1. Emit a complete description or none — a partial one is worse than
  absent, because a reader cannot tell which parts were guessed.
- W2. Refuse to describe a detector whose order it cannot state.
- W3. Choose forms by the normative rules (§3.3, §5), so two writers of the
  same scan produce equivalent descriptions under `same_order`.
- W4. Never emit meaning-changing information outside `required_features`
  or a version bump (§10).
- W5. Guarantee injectivity and bounds (§2.1).

A conformance suite ships with the spec: §9 as fixtures with the expected
index tuple for every event, one fixture per escape hatch, and the
`same_order` equivalence predicate as a reference implementation.

## 13. Deliberately out of scope

- Pixels, dtype, compression, chunking.
- Physical calibration and units — OME's.
- Reconstruction — including every **reduction** of colliding measurements
  (averaging repeats, binning trajectory revisits). The indexed form this
  spec defines is lossless; reductions are the application's.
- Correction parameters: bidirectional phase offset, field distortion,
  drift. Calibration, not order.
- Writer lifecycle: whether a file is finished, how much is committed.
  Orthogonal, solved elsewhere.
- Instrument description — OME's.
- Intra-exposure structure (§9, "invisible by design").

## 14. Open questions for review

1. **Serialization host.** OME-XML StructuredAnnotation, NGFF extension
   key, or sidecar with a registered name. Now constrained by §7: the host
   must be able to *name* companion arrays; plain TIFF is already excluded
   from the explicit forms.
2. **Naming.** "Event", "loop", "axis", "partition" are this draft's terms
   and all overloaded elsewhere.
3. **Payload geometry.** Is a named-axes payload enough, or does the
   detector-element case (Airyscan) deserve a structured pointer to
   instrument geometry rather than a bare axis name?
4. **⚖ `SPAN_LIMIT = 64`** — any fixed value in the plausible gap works;
   this one needs a second pair of eyes, then freezing.
5. **Feature-name registry governance** — who admits names to
   `required_features`, and where the registry lives.

Resolved since draft 2: affine sufficiency (explicit form, §3.2); collision
semantics (injectivity, §2.1); traversal ambiguity (§3.1); timing coupling
(§4.3); companion custody (§7); payload rank bound (§4.1); NGFF version
vagueness (§11).

## 15. Relationship to the current implementation

The layout contract on `codex/acquisition-layout-schema` implements the
compact form with identity weights only, plus selection spans and partitions
— a strict special case of this model. Its concepts survive; its schema need
not, because the branch is unmerged and no recording outside tests has ever
carried it. There is no legacy generation to support, which is why the
schema can still change freely — and why it should be settled before merge.

Its span-canonicalization algorithm becomes the normative one of §5, and
its v1 traversal semantics (loop-targeted) are exactly §3.1 — draft 2's
prose had drifted from them, and review #1 caught it.

The machinery around the schema — resolver and precedence, legacy adapters
for pre-contract files, the usable/authoritative rule, transport across
four formats, the recording gate, the seam tests — is independent of the
model and is a prerequisite for it. The eight data-corruption bugs fixed
along the way are independent of both.
