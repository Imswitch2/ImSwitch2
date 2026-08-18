# Scan acquisition order — specification draft

**Status:** Draft for review — design only, nothing implemented
**Date:** 2026-08-12
**Intent:** A portable description of *how a scan was performed*, complementing
OME rather than replacing it
**Supersedes:** the earlier `generalized-scan-order-model.md` sketch

---

## 1. The gap this fills

OME (and OME-NGFF) describe the **stored array**: which axis is which, its
physical scale, its unit, where the image sits on the sample. That is a complete
description of the *result*.

It says nothing about **how the samples were visited**. For an image that is
fine — the array *is* the result. For a scan it is not: the detector produces a
stream of measurements in acquisition order, and turning that stream into an
array requires knowing which measurement belongs at which coordinate. Today
every acquisition system encodes that knowledge in private metadata and every
reader re-derives it, usually by guessing from array shape and frame count.

This specification describes exactly that missing piece, and nothing else.

> **Non-goal.** This does not describe pixels, calibration, units, channels, or
> stage position. Those are OME's, and a conforming file uses OME for them.

## 2. The core idea: three layers

The mapping from a measurement to its place in the result decomposes cleanly:

| Layer | Mapping | Owner |
|---|---|---|
| **L1 — nesting** | event index → loop counters | this spec |
| **L2 — order** | loop counters → axis *indices* | this spec |
| **L3 — geometry** | axis index → physical coordinate | **OME**, extended |

The insight that makes this general: **L2 is an integer affine map for every
scan we have found**, and everything that looks non-affine — jittered
ptychography, golden-angle tomography, targeted acquisition — is actually
non-uniform at **L3**, not at L2. Its *index* lattice is perfectly regular; only
the physical position of each index is irregular.

That is why this is an addition to OME rather than a competitor: L3 is already
OME's job. This spec adds L1 and L2, and asks L3 for one extension (§6).

### 2.1 L1 — nesting

A scan runs nested loops, outermost first. This is a chronological statement
about the hardware: `for z: for y: for x:`. Each loop has an identifier, a
count, and a semantic kind.

### 2.2 L2 — order

Each loop **contributes** to axis indices through integer weights:

```
index[axis] = Σ_loops weight[axis][loop] · counter[loop] + offset[axis]
```

A loop is therefore not required to *be* an axis. This is what lets two counters
combine into one axis (`x = 2·k + p`, an interleaved sweep) or one counter
address a folded axis.

Reversal that alternates — a serpentine or bidirectional sweep — is not affine:
it depends on the parity of outer counters. It is expressed separately as a
**traversal modifier** on one axis, naming the ordered, contiguous set of outer
loops whose flattened counter determines parity. Loops outside that set are
parity **reset** boundaries.

### 2.3 L3 — geometry

An axis index becomes a physical coordinate either by a **uniform rule**
(origin + step, which OME already expresses as translation + scale) or by an
**explicit table** of one coordinate per index. The table is per axis, length
equal to that axis's count — not per event — so a 10 000-position jittered scan
costs 10 000 coordinates, not 10 000 × the number of axes.

## 3. The rest of the model

Three further pieces, orthogonal to the layers above.

- **Payload rank.** What the detector contributes per event: rank 0 (point
  detector, a scalar), rank 1 (line detector, a spectrum or line), rank 2 (a
  camera frame). With its own axis names and calibration, which are OME's.
- **Selection.** Which events a given detector actually recorded. A detector may
  be gated to a subset — enabled on some illumination conditions and not others
  — so its stored stream is a subsequence of the producer's events. Expressed
  compactly as periodic spans, not as a per-event list.
- **Partition.** Which *acquisitions* happened, when the set is chosen at
  runtime: timepoints of a lapse, tiles of a mosaic, sites picked by an event
  detector. This is where data-dependence lives.

**The separation that makes data-dependent acquisition tractable:** adaptive and
event-triggered systems choose *which acquisitions happen*, not *in what order a
single acquisition visits its positions*. The former is a partition; the latter
stays affine. Every event-triggered workflow examined behaves this way — detect,
transform a coordinate, then run an ordinary scan there.

## 4. Worked examples

Evidence of generality. Modalities marked † are not implemented in ImSwitch and
were used specifically to stress the model.

| Modality | L1 nesting | L2 weights | L3 | Payload |
|---|---|---|---|---|
| Confocal point scan | y, x | identity | uniform | rank 0 |
| Resonant bidirectional † | y, x | identity + serpentine on x | uniform | rank 0 |
| Widefield line-step (MoNaLISA) | y, condition, x | identity | uniform | rank 2 |
| Interleaved sweep | y, p, k | `x = 2·k + p` | uniform | rank 0 |
| MS-RESOLFT / OPM | time, cycle, plane | `z = cycles·plane + cycle` | uniform | rank 2 |
| SIM † | y, x, angle, phase | identity | uniform | rank 2 |
| Spinning disk † | z, time | identity | uniform | rank 2 |
| Spectral line scan † | y, x | identity | uniform | **rank 1** |
| Ptychography † | j, i | identity | **table** (jitter) | rank 2 |
| Golden-angle CT † | k | identity | **table** (non-uniform angles) | rank 2 |
| Targeted sites † | target | identity | **table** (detected positions) | any |
| Multi-well plate † | well, z | identity | **table** per well | any |
| Tiled mosaic | — | — | — | **partition** per tile |
| Event-triggered scan | ordinary scan | ordinary | ordinary | **partition** per event |

The MS-RESOLFT row is the load-bearing one: the transpose
`z = cycles·plane + cycle` is a real de-interlacing that exists in production
code today as a bespoke function, precisely because no metadata could state it.

## 5. Conformance

A **minimal conforming reader** must:

1. Read L1/L2 and produce, for each stored measurement, its axis indices.
2. Refuse a file whose declared schema version it does not know, rather than
   guessing — a version change may redefine existing fields.
3. Ignore unknown *fields* within a version it does know, reporting each, rather
   than refusing — an unrecognised addition must not cost access to the data.
4. Never infer acquisition order from array shape or measurement count when a
   description is present.

A **minimal conforming writer** must:

1. Emit a complete description or none. A partial one is worse than absent,
   because a reader cannot tell which parts were guessed.
2. Refuse to describe a detector whose order it cannot state.

A conformance suite should ship with the spec: the table in §4 as fixtures, each
with the expected index for every measurement.

## 6. The one thing asked of OME

Where an axis index maps to physical coordinates **non-uniformly**, OME's
`scale` + `translation` cannot express it. Such an axis needs an explicit
coordinate table.

This is a natural extension of NGFF `coordinateTransformations` and would
benefit OME independently of this spec: ptychography, non-uniform tomography and
multi-position acquisition all currently store such coordinates privately.

Until that exists, this spec carries the table itself, in a clearly namespaced
extension, and hands it back when OME can express it.

## 7. Deliberately out of scope

- Pixels, dtype, compression, chunking.
- Physical calibration and units — OME's.
- Reconstruction. Turning indexed measurements into an output image is the
  application's, and it is where modalities legitimately differ.
- Writer lifecycle: whether a file is finished, how many measurements were
  committed. Orthogonal, and already solved elsewhere.
- Instrument description: lasers, filters, objectives — OME's.

## 8. Open questions for review

1. **Is affine + traversal genuinely enough at L2?** It covers every modality in
   §4. A counter-example would be a scan whose *index order* — not its physical
   positions — is genuinely irregular. None found; a reviewer who knows one
   should say so, because that would force an explicit per-event index and a
   different storage strategy.
2. **Should L3 tables live here or wait for OME?** Carrying them makes the spec
   self-sufficient but overlaps OME's domain; waiting makes it incomplete for
   ptychography-class acquisitions today.
3. **Serialization.** OME-XML StructuredAnnotation namespace, an NGFF extension
   key, or an independent sidecar? This determines who can adopt it.
4. **Is `payload rank` sufficient**, or does a detector need a richer
   description (e.g. a rank-1 payload that is a spectrum versus a line in
   space)? The axis *names* may carry this, or it may need its own field.
5. **Naming.** "Loop", "axis", "event", "partition" are this draft's terms and
   are all overloaded elsewhere.

## 9. Relationship to the current implementation

The layout contract on `codex/acquisition-layout-schema` implements L1, a
restricted L2 (identity weights only), selection, and partitions. It is a strict
special case of this model, so its concepts survive; the schema does not need to
be preserved, because the branch is unmerged and **no recording outside tests
has ever carried it**. There is no legacy generation to support, which is why
the schema can still change freely — and why it should be settled before merge.

The machinery around the schema — the resolver, its precedence, legacy adapters
for pre-contract files, the authority rule, transport across four formats, the
recording gate — is independent of this model and is a prerequisite for it.
