# Scan acquisition order — specification, draft 2

**Status:** Draft 2 for review — design only, nothing implemented
**Date:** 2026-08-12
**Intent:** A portable description of *how a scan was performed*, complementing
OME rather than replacing it

---

## Changes since draft 1

Draft 1 was reviewed adversarially against techniques ImSwitch does not have,
chosen to break it. Three parts broke; the fixes below are what make the
"do not reopen this later" goal achievable.

1. **L3 rewritten.** Per-axis coordinate tables cannot describe trajectory
   scans (spiral/Lissajous: one counter drives two coordinates), skewed
   acquisition (OPM: cross-terms), or multi-ROI scans (one coordinate depends
   on two counters). L3 is now a *joint* transform from index space to
   physical space — which is exactly what the NGFF coordinate-transformations
   proposal (RFC-5) defines, so §10 now aligns with it instead of inventing a
   parallel extension.
2. **The explicit-order fallback (§3.2).** Affine order is not sufficient
   forever: adaptive sparse scanning and MINFLUX-style iterative probing are
   data-dependent per event. The fix is an explicit recorded-order array
   stored as *companion data*, not metadata. This makes the model total over
   deterministic acquisitions and is the core of the no-reopen argument (§8).
3. **Selection gets the same duality.** Pseudo-random subsets (compressed
   sensing) defeat periodic spans; an explicit selection array is the same
   escape hatch.
4. **Ragged payloads added (§4).** TCSPC/TTTR photon streams and SPAD event
   data have variable-length payloads per event. Already relevant in-tree
   (time-resolved detector workflows).
5. **Per-event timestamps made explicit (§4.3)** rather than left to be bolted
   on later.
6. **Partitions decoupled from data-dependence (§6).** Multi-view light-sheet,
   wells and tiles are *planned* partitions; event-triggered sites are
   *runtime* partitions. Same mechanism.
7. **Worked examples reorganized by mechanism (§7)** and extended to ~25
   techniques, including MINFLUX, OCT, photoacoustics, Airyscan/ISM,
   single-pixel imaging and mesoscope multi-ROI.

---

## 1. The gap this fills

OME and OME-NGFF describe the **stored array**: which axis is which, its
physical scale, its unit, where the image sits on the sample. That is a
complete description of the *result*.

They say nothing about **how the samples were visited**. For a widefield image
that is fine — the array *is* the result. For a scan it is not: the detector
produces a stream of measurements in acquisition order, and turning that
stream into an array requires knowing which measurement belongs at which
coordinate. Today every acquisition system encodes that knowledge privately
and every reader re-derives it, usually by guessing from array shape and
frame count.

This specification describes exactly that missing piece, and nothing else.

> **Non-goal.** This does not describe pixels, calibration, units, channels,
> or stage position. Those are OME's, and a conforming file uses OME for them.

## 2. The model in one view

An acquisition is a sequence of **events** (dwells, exposures). Each event
carries a **payload** (what the detector measured) and maps to a place in the
result through three layers:

| Layer | Mapping | Owner |
|---|---|---|
| **L1 — nesting** | event index → loop counters | this spec |
| **L2 — order** | loop counters → axis *indices* | this spec |
| **L3 — geometry** | axis indices → physical coordinates | **OME/NGFF transforms** |

Orthogonal to the layers:

- **Payload** — what one event contributes (§4).
- **Selection** — which events a given detector recorded (§5).
- **Partition** — which acquisitions happened (§6).

The decomposition is the load-bearing idea. Nearly everything that *looks*
like irregular order is actually irregular **geometry**: a jittered
ptychography grid, golden-angle tomography, targeted sites — all have
perfectly regular index lattices and irregular L3. Order and geometry must
not be conflated, and L3 is already OME's territory.

## 3. Order (L1 + L2)

### 3.1 Compact form: nested loops + affine map + traversal

A scan runs nested loops, outermost first — a chronological statement about
the hardware (`for z: for y: for x:`). Each loop has an identifier, a count,
a semantic kind (open vocabulary), and optionally the device that drove it.

Each loop **contributes** to axis indices through integer weights:

```
index[axis] = Σ_loops weight[axis][loop] · counter[loop] + offset[axis]
```

A loop is not required to *be* an axis. Two counters may combine into one
axis (`x = 2·k + p`, an interleaved sweep); one counter may address a folded
axis (`z = cycles·plane + cycle`, the MS-RESOLFT de-interlace).

Alternating reversal — serpentine/bidirectional sweeps — is not affine: it
depends on the parity of outer counters. It is a **traversal modifier** on
one axis, naming the ordered contiguous set of outer loops whose flattened
counter determines parity; loops outside that set are parity *reset*
boundaries.

### 3.2 Explicit form: recorded order as companion data

Some acquisitions choose their order from the data as they run: adaptive
sparse scanning, MINFLUX-style iterative probing, AI-driven acquisition. Their
order is not affine and not periodic — but after acquisition it is **known**.

The explicit form is an integer array of shape `(N_events, n_axes)`: row *i*
gives the axis indices of event *i*. It replaces L1+L2 jointly (loops may
still be declared as documentation). It is stored as a **named companion
array in the same container** as the data — Zarr/HDF5 array, TIFF appendix —
never inline in metadata, so its size is bounded by the data, not by a
metadata budget.

**Canonical-form rule.** A writer must use the compact form whenever the
recorded order admits one. Two descriptions of the same scan must compare
equal; an explicit array that happens to enumerate an affine order is
non-canonical.

### 3.3 What L2 deliberately is not

L2 outputs **indices**, never physical positions. A spiral scan is *one* loop
(sample index k) at L2; that x and y are both functions of k is geometry, and
lives in L3.

## 4. Payload

### 4.1 Rank

What one event contributes: rank 0 (point detector — a scalar), rank 1 (line
or spectral detector — OCT A-scans, photoacoustic time traces, spectra,
TCSPC histograms), rank 2 (a camera frame). Payload axes are named;
their calibration is OME's.

Payload axes need not be spatial: an Airyscan/ISM detector contributes a
vector indexed by *detector element*, whose physical geometry is instrument
metadata, out of scope here.

### 4.2 Ragged payloads

Time-tagged photon streams (TCSPC/TTTR, SPAD arrays, coincidence counting)
produce a **variable-length record list per event**. The event is the dwell
or exposure; the payload is ragged. Represented as records plus an offsets
array — companion data again. The association of records to events (line and
frame markers, pixel clocks) is the writer's problem to solve *before*
writing; a conforming file states event boundaries, not raw marker streams.

### 4.3 Timestamps

Actual event times differ from nominal ones (jitter, phased experiments such
as FRAP pre/bleach/post). The actual time of each index on the time axis is
an L3 coordinate table on that axis; in explicit form, a per-event time
array. Nominal spacing stays OME's `scale`. This is stated here so it is not
bolted on incompatibly later.

## 5. Selection

A detector may record a subset of the producer's events — gated to some
illumination conditions, enabled per line. Two forms, same duality as order:

- **Compact:** periodic spans (start, count, stride, period, repeats) over
  producer event ordinals.
- **Explicit:** an integer array mapping stored-measurement index → producer
  event ordinal, as companion data — required for pseudo-random subsets
  (compressed sensing), where spans degenerate to one span per event.

The same canonical-form rule applies.

## 6. Partition

A partition is a separately stored or separately transformed acquisition
unit. Both planned and runtime-chosen sets use the same mechanism:

- **Planned:** timepoints of a lapse, tiles of a mosaic, wells and sites of a
  plate, *views of a multi-view light-sheet* — each view a partition carrying
  its own L3/OME transform (the rotation that registration refines).
- **Runtime-chosen:** event-triggered sites, MINFLUX per-emitter iterations —
  detected, then acquired.

**The separation that keeps data-dependence tractable:** adaptive systems
overwhelmingly choose *which acquisitions happen*, not the order within one.
Detect → transform a coordinate → run an ordinary scan there. Only per-event
adaptivity (§3.2) needs the explicit form.

Within one partition, event ordinals restart at zero; a partition's index and
planned count are recorded, so lapse files are identical apart from the
index.

## 7. Worked examples, by which mechanism carries the burden

Techniques marked † are not implemented in ImSwitch and were used to stress
the model.

**Identity order, uniform geometry** — the trivial majority:

| Technique | Shape |
|---|---|
| Confocal / STED point raster | y, x loops; rank 0 |
| Widefield line-step (MoNaLISA) | y, condition, x; rank 2 |
| SIM † | angle, phase (, z); rank 2 |
| Hyperspectral point scan † | y, x; rank 1 (spectrum) |
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
| Bidirectional anything | serpentine modifier, parity over declared outer loops |

**Tabulated geometry (L3 does the work; order stays trivial):**

| Technique | Table |
|---|---|
| Ptychography † | jittered (y, x) per grid index |
| Fourier ptychography † | illumination angle per LED index |
| Golden-angle tomography † | angle per projection index |
| Spiral / Lissajous scan † | joint (x, y) trajectory per sample index — requires L3 to be a joint transform |
| Random-access AOD targeting † | position per target index |
| Mesoscope multi-ROI † | ROI origin lookup ⊕ uniform intra-ROI step — one coordinate, two index axes |
| Non-uniform / phased timelapse † | timestamp per time index |
| Sinusoidal resonant axis (unlinearized) † | position per pixel index |

**Explicit order or selection (the escape hatch):**

| Technique | Explicit array |
|---|---|
| Adaptive sparse scanning † | order: visited indices, recorded post hoc |
| MINFLUX-style iterative probing † | order within each per-emitter partition |
| Compressed-sensing subsampling † | selection: pseudo-random subset of a grid |
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
| TTTR / SPAD photon streams | ragged record list per dwell |
| Transmission PMT alongside camera | rank 0 and rank 2 detectors in one scan, each with its own description |

**Invisible by design** (not events at all): intra-exposure motion — lattice
dithering, axially swept illumination, resonant averaging within a pixel,
DMD flicker. The event is the exposure; what happens inside it is not order.

## 8. The closure claim — why this should not need reopening

**Claim.** Any acquisition that is (a) composed of discrete events, (b) whose
event order is known by the end of acquisition — planned or recorded — and
(c) whose measurements the writer can associate to events, is expressible:
compactly when periodic, explicitly otherwise.

**The boundary, stated honestly.** (a) excludes continuous, un-evented
streams until the writer defines events; (b) excludes orders the writer
failed to record, which conformance forbids (§9, W2); (c) excludes marker
association problems, which are upstream of any description.

**Evolution without structural change.** New techniques land as one of: a new
loop-kind or payload word (open vocabulary), a new L3 transform type
(delegated to NGFF), a new companion array. The versioning rules (§9) make
all three additive. A structural reopen would require an acquisition
violating (a)–(c) — none is known, and §7 is the evidence.

**One mechanism, uniformly.** Everything too large or irregular for metadata
is a named companion array in the container: explicit order, explicit
selection, coordinate tables (until NGFF carries them), ragged offsets,
timestamps. There is no second escape hatch to design later.

## 9. Conformance

A **reader** must:

- R1. Support both compact and explicit forms of order and selection.
- R2. Refuse a schema version it does not know — a version change may
  redefine existing fields — while leaving the pixel data viewable as an
  undescribed stream.
- R3. Ignore unknown *fields* within a known version, reporting each ignored
  field; an unrecognised addition must not cost access to the data.
- R4. Never infer order from array shape or measurement count when a
  description is present.

A **writer** must:

- W1. Emit a complete description or none — a partial one is worse than
  absent, because a reader cannot tell which parts were guessed.
- W2. Refuse to describe a detector whose order it cannot state.
- W3. Use the compact form whenever the order admits one (canonical form).

A conformance suite ships with the spec: §7 as fixtures, each with the
expected axis indices for every event, including one fixture per escape
hatch.

## 10. Alignment with OME/NGFF

L3 is deliberately delegated. The NGFF coordinate-transformations proposal
(RFC-5) defines exactly what L3 needs: affine with cross-terms (OPM skew),
lookup-table coordinates (jitter, trajectories, non-uniform axes), and
composition (mesoscope ROI-origin ⊕ step). This spec adds L1/L2 — which NGFF
does not address and, being about the *stored result*, should not.

Until RFC-5 (or equivalent) is adoptable, coordinate tables are carried as
companion arrays under this spec's namespace and handed back the moment OME
can express them. Nothing else in the model depends on that timing.

## 11. Deliberately out of scope

- Pixels, dtype, compression, chunking.
- Physical calibration and units — OME's.
- Reconstruction: turning indexed measurements into an output image is the
  application's, and is where modalities legitimately differ.
- Correction parameters: bidirectional phase offset, field distortion, drift.
  Calibration, not order.
- Writer lifecycle: whether a file is finished, how much is committed.
  Orthogonal, solved elsewhere.
- Instrument description — OME's.
- Intra-exposure structure (§7, "invisible by design").

## 12. Open questions for review

1. **Serialization.** OME-XML StructuredAnnotation, NGFF extension key, or
   sidecar with a registered name — decides who can adopt it. The companion-
   array mechanism constrains this: the format must be able to *name* arrays.
2. **Naming.** "Event", "loop", "axis", "partition" are this draft's terms
   and all overloaded elsewhere.
3. **Payload geometry.** Is a named-axes payload enough, or does the
   detector-element case (Airyscan) deserve a structured pointer to
   instrument geometry rather than a bare axis name?
4. **Companion-array custody.** Named per detector or shared per acquisition;
   and their integrity story (a checksum? length cross-checks only?).

Draft 1's biggest open question — *is affine enough?* — is answered: no, and
§3.2 is the consequence. It no longer gates the design.

## 13. Relationship to the current implementation

The layout contract on `codex/acquisition-layout-schema` implements the
compact form with identity weights only, plus selection spans and partitions
— a strict special case of this model. Its concepts survive; its schema need
not, because the branch is unmerged and no recording outside tests has ever
carried it. There is no legacy generation to support, which is why the schema
can still change freely — and why it should be settled before merge.

The machinery around the schema — resolver and precedence, legacy adapters
for pre-contract files, the usable/authoritative rule, transport across four
formats, the recording gate, the seam tests — is independent of the model and
is a prerequisite for it. The eight data-corruption bugs fixed along the way
are independent of both.
