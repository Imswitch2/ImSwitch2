# ImProcess ROI Manager 2.0 (open tail)

**Status (2026-10-17):** Tracks A and B and the roadmap items P-0, P-T, P-F, P-G,
P-1, P-2, P-J, P-3, P-4, P-5, P-S, P-6, P-U, P-P, P-7 and P-R are implemented and
rig-validated on main (ROI manager, `SpatialFrame`, viewer tool broker,
measurement jobs, ROI sets, ImageJ `.roi` interop, undo stack, ROI-aware
processors, crop from an ROI). The 2,700-line design, audit, review log and
per-phase implementation notes were removed; git history has them.

**What remains (both blocked on the same missing piece, a central save path):**

1. **P-F2 - container-persisted identity.** `dataset_uid`, `result_uid` and
   `coordinate_space_uid` live only in memory, so a reloaded result gets
   content-digest ids with `identity_kind="derived"` and an ROI set measured
   against it is capped at `pixel-compatible` instead of `exact`. Fixing it needs
   one central provenance serialisation hook that every result format calls
   (about 21 independent `save()` implementations in
   `improcess/processors/*/result.py`, `reconstructors/*/result.py`,
   `model/array_result.py`, each calling tifffile/h5py directly). Estimate was 5 d.
2. **Processing footprint in result metadata** (section 2 below).
3. **Fiji fixture corpus for the P-6 `.roi` reader/writer.** Fixtures produced by
   Fiji itself are owed; hand-made ones would only encode our own reading of the
   format. Needs a Fiji run.

Maintainer note: 1 and 2 are a sizeable cross-cutting change with no scheduled
owner; decide whether they are wanted before anyone starts.

## 1. Identity lifetime (what P-F2 must preserve)


Round 4 was contradictory in three ways: `result_uid` was described as
instance-scoped yet had to survive reload; `frame_uid` was serialised with a
result although it describes a *display plane* that may not exist until someone
views the data; and P-F.6 required result-container writes that **Q-14(b)
forbids**. All three are resolved by separating what is minted from what is
derived, and by not writing to containers at all in Core.

| Id | How it is obtained | Lifetime |
| --- | --- | --- |
| `dataset_uid` | minted at acquisition/import; inherited by derived results | durable |
| `result_uid` | **identity of a *logical serialised result*, not a Python instance.** Minted when the result is first created and carried in memory; two `ProcessingResult` objects for the same logical result share it | durable in memory; see the reload rule below |
| `coordinate_space_uid` | minted with the result; **preserved by pixel-aligned derivations** (projection, crop-with-offset), re-minted by resampling ones | durable |
| `frame_uid` | **derived deterministically** — a stable hash of `(coordinate_space_uid, plane_axes, shape, affine, component, view_mode)`. Never minted, never stored on a result | recomputed on demand; identical inputs always give the identical uid |

Consequences:

* **Frames live in the ROI set, not in the result** (A-24). That is the only
  place they are needed, and it is what makes an exported ROI set
  self-describing.
* **Core writes nothing to result containers.** Verified blast radius: there is
  no shared writer — **21 `def save(` implementations**, each calling
  `tifffile.imwrite`/`h5py.File` directly (`improcess/processors/*/result.py`,
  `reconstructors/*/result.py`, `model/array_result.py`, …). Threading
  provenance through all of them is not an append-only change, so it becomes
  **P-F2** (roadmap), gated on introducing *one* central provenance
  serialisation hook that every format calls.
* **Until P-F2, a reloaded result has no stored ids.** It gets a deterministic
  content-digest `dataset_uid`/`result_uid` (path-independent, so moving the
  file does not change identity) and `identity_kind="derived"`. Per the ladder,
  that caps it at `pixel-compatible` — the ROI still measures the same pixels,
  and the row says so honestly instead of claiming `exact`.
* **Import conflict:** loading an ROI set whose `frame_uid` is unknown in this
  session is not an error; the frame travels inside the set, and the ladder
  decides what can be measured.

## 2. Processing footprint in result metadata

wants its own branch off this one, after the pull request.

**The ask.** Every reconstruction and processor applied to a result should
leave a footprint in that result's metadata: reconstruct a raw MoNaLISA
measurement and the reconstruction's parameters and the source file are
recorded; crop it and that step is appended, with the ROI and its coordinates.
A result should be able to say how it was made, end to end.

**What already exists and should be built on rather than duplicated.** Most of
the vocabulary is in place from Track A and P-R:

* `lineage`, `result_uid`, `dataset_uid` and `coordinate_space_uid` (A-10) —
  *which* results a result derives from, and whether they share a pixel grid.
* `attach_provenance` / `preserves_grid` — applied centrally in
  `normalize_processor_output`, so every processor already contributes without
  opting in.
* `roi_provenance` (P-R) — the ROI set, revision, mode and offset behind a
  restricted run, and now the ROI behind a crop.

What is missing is the **step list**: an ordered record of *what was done*, with
each step's parameters, rather than only what the result descends from. The
natural shape is a list of entries appended by the same central hook that
already attaches provenance, so a processor stays unaware of it.

**The blocker is the same one P-F2 has.** Metadata that lives only in memory
does not survive a save, and there is **no central save path** — 21 independent
`save()` implementations call tifffile/h5py directly. Q-14b put container
writes out of scope for exactly this reason. So the honest sequencing is:

1. a central save hook (the P-F2 prerequisite), then
2. the step list written through it, then
3. readers — the Metadata panel already renders arbitrary container metadata,
   so it should need little.

Doing (2) before (1) produces a footprint that is complete on screen and absent
in every file, which is the worst of the three states.








