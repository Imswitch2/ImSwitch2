# Generalized scan-order model (acquisition layout v2)

**Status:** Proposed — design only; land after rig validation of v1
**Date:** 2026-08-12
**Scope:** How a scan's visiting order and a detector's payload are described
**Depends on:** [acquisition layout contract](acquisition-layout-contract.md) (v1, PR 1–7 implemented)

---

## 1. What v1 got right, and where it stops

v1 replaced inferred frame semantics with recorded ones and that holds up: one
resolver, one authority rule, producers that refuse rather than approximate.
Nothing here contradicts it.

But v1 models a scan as **nested loops where each loop *is* an axis**, plus a
forward/reverse/serpentine modifier. Three things fall outside that.

### 1.1 A loop cannot contribute to an axis

A scan that visits `x = 1, 3, 5, 2, 4, 6` needs two counters to combine into one
coordinate: `x = 2k + p`. v1 can only make `p` a separate axis, so the
reconstruction comes out 2×3 instead of 1×6. Verified against the current
implementation — the model yields `scan_x = 0,1,2,0,1,2` with `parity` as its
own output axis.

This is not hypothetical. **SNOUTY already needs it.** `restack_interleaved`
stores planes at `cycle·planes + plane` and needs them at `plane·cycles + cycle`
— an affine combination of two counters. v1 could not express it, so it was
solved by a special-purpose de-interlacing function *outside* the layout. That
is the same failure the contract exists to prevent, one level up.

### 1.2 Detector rank is assumed to be 2

Producers emit `("frame", "detector_y", "detector_x")` literally. A point
detector contributes a scalar per position, a line detector an array, a camera a
frame — and all three are currently described identically. The rank is a
property of the detector, and nothing records it.

### 1.3 Placement is still per-reconstructor

Five independent implementations of "where does frame *i* go":
`unfold_frame_axis`, `restack_interleaved`, `raster_positions_from_layout`,
`placement_from_layout`, `frame_indices_for_selection`. v1 moved the *semantics*
into one place but left the *placement arithmetic* distributed.

## 2. The model

Three things v1 tangles, separated.

### 2.1 Event payload rank

What the detector contributes per event: rank 0 (point), 1 (line), 2 (frame),
with its own axis names and calibration. Additive to the schema; nothing else
changes.

### 2.2 Event → sample coordinate: an affine map

Loops stay the *chronological* nesting — hardware really does nest loops — but a
loop **contributes** to coordinates rather than **being** one:

```
coordinate[axis] = Σ_loops weight[axis][loop] · counter[loop] + offset[axis]
```

An integer matrix per layout. Verified to reproduce every current case:

| Case | Weights | Result |
|---|---|---|
| Plain raster | identity | ✅ |
| MoNaLISA line steps | identity, condition its own axis | ✅ frames 0–17 → condition 0, 18 → condition 1, 36 → row 1 |
| Strided interleave | `x = 2·k + 1·p` | ✅ visits 0,2,4,1,3,5 |
| SNOUTY restack | `z = cycles·plane + 1·cycle` | ✅ matches `restack_interleaved` exactly |

Two things stay outside the affine core, correctly:

- **Serpentine** is reversal-per-parity-class, which is not affine. It remains
  the existing `TraversalRule` modifier applied on top.
- **Detector gating** is orthogonal: `recorded_event_spans` selects *which*
  events this detector saw, and is unaffected by how counters map to
  coordinates.

### 2.3 Sample coordinate → output index stays in the plugin

This is where MoNaLISA's focus-grid interleaving genuinely differs from
BeadRec's one-pixel-per-position. That difference is real and belongs to the
reconstructor.

## 3. The point: move the boundary

v1 draws the line between §2.1 and §2.2 — the layout hands out loop counters and
every reconstructor turns them into positions. Drawing it between §2.2 and §2.3
— **the layout delivers sample coordinates** — collapses the five placement
implementations into one, leaving only the part that legitimately differs.

The shared API is close to what already exists:

```python
sample_coordinates(layout) -> (N, n_axes) int array      # vectorized, not per-frame dicts
sample_coordinates_for(layout, frame_index) -> Mapping    # the singular form
```

`iter_recorded_coordinates` is the ancestor of the first; it needs to yield
*sample* coordinates rather than loop counters, and a vectorized form so it is
usable on large scans without a dict per frame.

## 4. Relation to OME

OME/NGFF describes the **stored array** — axes, scales, units. It has no concept
of acquisition order at all. So this is not "adopt OME", it is "specify what OME
does not". What is worth borrowing is the shape of the standard: a small closed
core, an explicit extension mechanism, a reference implementation, and a
conformance suite. v1 already has the first three.

## 5. Why this waits for rig validation

The model generalizes from the special cases. Our understanding of the most
important one — the TriggerScope RESOLFT `roSteps`/`cycleSteps`/`timeLapsePoints`
nesting — comes from reading controller code, never from a captured trace
(contract §4.3.5, owed since PR 4).

Generalizing an assumption makes it *harder* to see, not easier. If the RESOLFT
nesting is wrong, an affine map derived from it would be confidently wrong in a
form that no longer looks like a special case anyone would re-examine. Validate
v1 on hardware, then generalize from measured behaviour.

The counter-argument, recorded honestly: the schema is not public, so breaking
it is free now and expensive later, and this migrates every reconstructor twice.
If the release date arrives before a rig session, that trade flips.

## 6. Open questions

- **Is affine enough?** It covers every case in this repository. A scan whose
  order is genuinely data-dependent (adaptive, event-triggered) is not affine
  and would need an explicit index list — which the 1 MiB inline budget cannot
  hold. Smart/event-triggered acquisition already exists in this codebase, so
  this needs checking before the model is called general.
- **Where do the weights come from?** A producer must emit them. For the
  existing producers they are derivable, but the RESOLFT one is exactly the
  unverified assumption above.
- **Migration.** v1 layouts are a strict subset (identity weights), so v1 files
  read under v2 without change. The reverse does not hold, which the v1
  forward-compatibility rule already handles: a v2 file refuses cleanly on a v1
  reader and stays viewable.
- **Does `payload_kind` survive?** With explicit detector rank,
  `detector-frame-stream` vs `assembled-image` may be derivable rather than
  declared.
