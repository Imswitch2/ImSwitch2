# Image transform module — implementation plan

**Status:** Step 0 implemented (2026-08-11); Phases 1-6 still planned.
Work-in-progress document (`.md`); durable docs go to `.rst` once the module
ships.

**Goal:** one central, shared model for "how do I map image space A onto image
space B" — creation (calibration), persistence, loading, and application — used
by both imcontrol and improcess, replacing five independent implementations.

---

## 0. Why

There are currently **five** unrelated transform representations in the tree:

| Where | Model | Applies to | Calibration | Persistence |
|---|---|---|---|---|
| `imcommon/algorithms/detector_transform.py` | 3×3 affine, `(row, col, 1)` | (declared only) | none | inline in setup JSON `detectorTransforms` |
| `imcontrol/model/EtSTEDTransformService.py` | 20-coef 3rd-order polynomial | coordinate pairs | lsq + RANSAC auto-cal | `<name>.py` + `<name>.csv`, module `exec`'d |
| `improcess/analysis/multicolor.py` | `affine_2d` (pystackreg conv.) + `affine_3d` (ZYX) + int `z_shift` | volumes | 3 modes, RANSAC | bespoke HDF5 |
| `imcontrol/controller/display_transform.py` | rot90 + flips | live display | n/a | managerProperties |
| `improcess/processors/transform/processor.py` | rot90 + flips | results | n/a | n/a |

Plus `tile_mosaic.py:2987`, which holds the only real image-warping engine
(`ndimage.affine_transform` + validity masks + slab-wise assembly), welded
inside the tiling assembler.

Three coordinate conventions are live simultaneously: `(row, col)`,
`(z, y, x)`, and pystackreg's transposed `(x, y)`.

### Prior art: ImSwitch1 `testalab_scanDev`

`FociAffineController` / `FociAffineWidget` / `model/foci_affine.py` on the
ImSwitch1 branch `testalab_scanDev` is the closest thing to what we want, and it
gets several things right that nothing in our tree does:

- **The whole loop in one place**: detect → order → RANSAC fit → residuals →
  save → load → apply → *visualize*.
- **Residuals are first-class** (`residuals_px`, mean/median/max/`n_inliers` in
  metadata, surfaced in the widget). A transform with no stated error is one
  nobody can trust.
- **Convention conversion is named and isolated**: `xy_affine_to_napari_yx()`,
  `pixel_affine_to_napari_world_affine()`. The latter is `S @ H @ S⁻¹` — it
  already recognizes that a pixel-space affine and a physical-space affine are
  different objects and derives one from the other.
- **Direction is recorded** (`"transform_direction": "moving_to_reference"`).
- **Apply as a napari layer affine, not a resample.** For visual verification
  you do not want to warp pixels at all — set the layer transform and look.
  Resampling should be opt-in.
- **Startup calibration is a file pointer in SetupInfo**
  (`CalibrationsInfo.foci_affine.auto_load` + `calibration_file`), not an inline
  matrix. Better separation than our `detectorTransforms`.

Its limits (all fine for a harness, none acceptable as the final model): two
hard-coded spaces ("reference"/"moving") with no composition; affine-only, 2D-only;
`.npz` with `metadata_json` stuffed inside; correspondence hard-wired to a
regular foci grid; and it stores `matrix_xy` **and** `matrix_yx_napari` — two
representations of one thing, which is a divergence risk.

`order_grid_points()` is also fragile in a specific way worth recording: it
`np.array_split`s sorted points into equal row groups, so it needs *exactly*
`n_rows × n_cols` detected points, and the `n_cols == n_rows` tie-break on
`abs(axis0[0])` is a coin-flip for a square grid rotated near 45°. Acceptable as
one correspondence strategy; must not be the only one.

---

## Design decisions

These are settled up front because they are expensive to change later.

**D1 — Transforms are edges between named frames.** A transform is not a
property of a detector; it is an edge `source_frame → target_frame` in a small
registry that can compose paths. Calibrate each detector once against a common
frame (`sample`) instead of every pair: N calibrations, not N². `camA→camB` is
then composed, and etSTED's camera→galvo is `widefield→sample ∘ sample→galvo`
through the same machinery.

Caveat: a *directly measured* A→B beats a composed one, because error
accumulates along a path. Direct edges win over composed paths, and the composed
path is recorded in the result.

This generalizes beyond detectors. The end-of-run stage jump in the tiling work
is a Märzhäuser-tracked-frame vs `moa`-absolute-frame mismatch — a frame
mismatch. Explicit named frames make that class of bug *expressible* rather than
a surprise.

**D2 — Swappable models behind one primitive; payload wrappers built on top.**

Two independent axes: *what you are transforming* (image, point cloud, ROI,
mask, single coordinate) × *which model does it* (identity, translation,
rotation, rigid, similarity, affine, poly3, displacement field). Implementing
these as a matrix means every new model touches every payload wrapper and vice
versa. It must not work that way.

**Every model implements exactly one required primitive:**

```python
class TransformModel(Protocol):
    kind: str
    def map_points(self, pts: NDArray) -> NDArray: ...      # forward, (N, D)
    def inverse(self) -> "TransformModel": ...              # may be numerical
```

**Everything else is generic and written once**, on top of that primitive:

| Wrapper | Generic implementation |
|---|---|
| `transform_points(pts, t)` | `t.map_points(pts)` |
| `transform_image(img, t, out_shape)` | evaluate `t.inverse().map_points()` on the output grid → `ndimage.map_coordinates` |
| `transform_roi(roi, t)` | map corners/vertices, rebuild |
| `transform_mask(mask, t)` | `transform_image` with `order=0` |

That generic image path works for *any* model, including ones not yet written.

**Fast paths are optimizations, not separate semantics.** A model may
optionally declare a faster route, and the generic layer uses it when present:

- affine → `ndimage.affine_transform` (skips grid materialization)
- rotation by 90°/180°/270° and flips → `np.rot90`/`np.flip`, **lossless, no
  interpolation at all** — this is what makes "just a rotation for a simple
  detector" genuinely cheap rather than an interpolated resample
- identity → return input

Each fast path must be covered by a test asserting it agrees with the generic
path to within interpolation tolerance. A fast path that silently disagrees with
the general one is the worst possible bug in this module.

**Capability flags** let callers and the generic layer branch honestly:
`is_linear`, `is_axis_aligned`, `has_analytic_inverse`, `as_matrix() -> 3x3 |
None`.

Two consequences that are easy to miss and must be designed for, not discovered:

1. **`poly3` has no closed-form inverse.** etSTED gets away with this today
   because it only ever maps points *forward* (widefield → galvo) and never
   warps an image. The moment you warp an image with a polynomial you need the
   output→input map. `inverse()` for poly3 must therefore be numerical — fit an
   inverse polynomial over the working domain and *report its residual*, or
   iterate. It cannot silently pretend to be exact.
2. **`as_display_affine()` only exists for models that are affine.** A poly3 or
   a displacement field cannot be expressed as a napari layer transform. The
   non-resampling display path (D7) must check `as_matrix()` and degrade
   explicitly — resample instead, and say why — rather than quietly applying
   something wrong.

**Composition must survive heterogeneous chains.** `affine ∘ affine` is closed
and must collapse to a single matrix (otherwise frame-graph composition loses
the display-affine fast path). `affine ∘ poly3` falls back to a
`ComposedTransform` that simply chains `map_points`. Both are the frame
registry's problem (D1), and it must not assume everything is a matrix.

**Kinds live in a registry keyed by name**, so serialization round-trips by
`kind` and so plugins can add models — matching improcess's existing drop-in
plugin system.

Polynomial is not optional in the model set: galvo scan systems have real
pincushion, which is why etSTED has it.

**D3 — Canonical storage is physical (µm) against a frame; pixel→pixel is
derived at apply time** from each detector's *current* pixel size, ROI offset
and binning. A pixel-space calibration silently dies the moment someone changes
ROI or binning, and ROI/binning state is already a known sore spot. Every
calibration records the context it was taken under (detector, ROI, binning,
pixel size, objective/zoom); a mismatch at apply time is either corrected or
refused loudly. A calibration that quietly goes wrong is worse than one that
won't load.

**D4 — One internal convention, adapters at the edges.** Row-major `(…, y, x)`,
integer coordinates = pixel centres (matching `detector_transform.py`). Store
**forward** (source→target); invert once inside the apply layer, because
`ndimage` wants the inverse and no caller should ever have to know that. Store
**one** matrix; derive napari/vispy/pystackreg forms on demand.

**D5 — Display transforms stay out.** `display_transform.py` and the ImageJ
`TransformProcessor` are human-convenience axis permutations, not metrology.
But there is a real footgun at the boundary: if a detector has a display
rotation on, a calibration measured by clicking on screen is wrong by that
rotation. **Calibrate in raw detector coordinates, always.**

**D6 — File format: JSON canonical, HDF5 as an embedding path.** One
human-readable, diffable, git-able JSON file per transform, with an optional
`.npy` sidecar for dense payloads (displacement fields). HDF5 is *also*
supported, but as a serializer *into a group* — because transforms need to be
embeddable in OME-HDF5/Zarr recordings alongside the data they describe. JSON is
what the widget writes and reads by default.

Record contents: schema version, source/target frame, model kind, parameters,
units, acquisition context (D3), **residual RMS + inlier counts**, timestamp,
free-form provenance.

**D7 — Application has two modes.** `as_display_affine()` (non-destructive
layer transform, no resampling — the default for verification and overlay) and
`warp()` (resampling, for when warped pixel data is genuinely needed). Step 0
only needs the first.

**D8 — Correspondence strategies are pluggable; fitting is shared.** The tree
already has four ways to get corresponding points: regular foci grid
(`foci_affine`), bead RANSAC matching (etSTED auto-cal, multicolor
`descriptor_3d`), FFT phase correlation, and pystackreg intensity registration.
These are *correspondence finders*; they all feed one shared fitter/validator.
`pystackreg` is not a core dependency and must stay behind a lazy guard, as
`multicolor._require_pystackreg()` already does.

---

## Step 0 — Standalone affine calibration harness

**Rationale.** Validate the core model end-to-end with zero hardware and zero
risk to load-bearing code (etSTED, tiling, multicolor are all in production
use). Step 0 forces the file format and the coordinate conventions — the two
expensive-to-change decisions — to be settled before anything depends on them.
Nothing is hooked up until the affines demonstrably work.

Port `foci_affine` into Imswitch2, but **file-driven instead of snap-driven**:
load two already-recorded images rather than snapping from a detector.

### 0.1 Core skeleton

New package `imswitch/imcommon/algorithms/transforms/`:

- `base.py` — the D2 `TransformModel` protocol, the kind registry, and the
  generic payload wrappers (`transform_points`, `transform_image`,
  `transform_roi`, `transform_mask`) written *once* against `map_points`.
- `models/` — `identity`, `rotation` (axis-aligned lossless fast path), and
  `affine` (matrix fast path). **Three kinds, not one**, specifically so Step 0
  proves the swap actually works: the same widget, the same file format and the
  same wrappers must drive all three without branching on kind. A single-model
  Step 0 would validate the affine but not the architecture.
- `model.py` — `SpatialTransform` record wrapping a model instance with
  `source_frame`, `target_frame`, `kind`, parameters, `units`, `context`,
  `residuals`, `provenance`. `invert()`, `compose()`.
- `conventions.py` — the named adapters, ported from `foci_affine`:
  `xy_to_yx()`, `to_napari_world(scale)`, `to_ndimage_inverse()`,
  `from_pystackreg()`. One canonical matrix in, derived forms out.
- `io.py` — `save_json()` / `load_json()` per D6, with schema version and a
  round-trip guarantee.

### 0.2 Estimation

- `estimate.py` — `estimate_affine(src_pts, dst_pts, residual_threshold)`:
  skimage RANSAC (`measure.ransac` + `transform.AffineTransform`), returning
  matrix + per-point residuals + inlier mask. Lifted from
  `foci_affine.estimate_affine_from_points`, which is already sound.
- `correspondence/foci_grid.py` — `detect_spot_centers()` +
  `order_grid_points()`, ported as the *first* correspondence strategy behind
  the D8 interface. Port the fragility notes above as docstring warnings and
  raise clearly when the detected count ≠ `n_rows × n_cols` rather than
  `array_split`-ing garbage.

`scikit-image`, `scipy`, `matplotlib`, `h5py` are all already core dependencies
(`setup.cfg`), so this adds none.

### 0.3 Widget

`imswitch/imcommon/view/widgets/TransformCalibrationWidget.py` — in imcommon so
both modules can host it. Ported from `FociAffineWidget`, which already uses
`guitools.JsonEditorDialog` and `guitools.askForFilePath`, both of which exist
in Imswitch2's `imcommon/view/guitools/`. **Swap the two Snap buttons for two
Load buttons** (file → reference, file → moving).

Keep from the original: parameter JSON editor, residual summary, Calibrate /
Visualize / Load / Save / Clear, and the matplotlib side-by-side visualization
with detected points overlaid. Drop for now: `Apply Affine` to live display
layers, target-layer combo, startup-calibration wiring — those are hookup, not
core.

Note the original controller imports `imswitch.imreconstruct.model.localizer`,
which does not exist in Imswitch2 (it is `improcess` now). The autolocalize path
is optional and already guarded by `LOCALIZER_AVAILABLE`; leave it out at Step 0
rather than porting a broken import.

### 0.4 The test that matters

Synthesize a known affine, warp a real recorded image by it, recover it, assert
`recovered ≈ known` within tolerance. This is the single most valuable test in
the whole module and it needs no rig. Plus: round-trip `save → load → identical`,
and residual-threshold behaviour on deliberately corrupted correspondences.

Two more that lock in D2 while it is still cheap to change:

- **Fast path ≡ generic path.** For every model that declares one, assert the
  fast route and the generic `map_coordinates` route agree to within
  interpolation tolerance. Run it over all three Step 0 kinds.
- **Kind-agnostic wrappers.** Parametrize the wrapper tests over the registry,
  so a new kind added later is automatically held to the same contract and
  nothing in the payload layer is allowed to branch on `kind`.

### 0.5 Exit criteria

Step 0 is done when: a recorded image pair produces a calibration with
sub-pixel median residual; the saved JSON reloads byte-identically in effect;
the synthetic-ground-truth test passes; and the visualization visually confirms
the overlay. **No hookup work starts before this.**

### 0.6 Outcome (2026-08-11) — all criteria met

Shipped in `imswitch/imcommon/algorithms/transforms/` (`base`, `conventions`,
`model`, `io`, `estimate`, `models/`, `correspondence/`) plus
`imswitch/imcommon/view/widgets/TransformCalibrationWidget.py`. 84 tests, whole
imcommon suite green (175), ruff clean.

Measured on the synthetic ground-truth pair: recovered matrix matches the known
affine to ~2e-5 in the linear part and ~4e-3 px in translation, median residual
0.009 px, 16/16 inliers. The visualization shows every transformed moving point
inside its reference ring.

**A defect was found in the ported ImSwitch1 code and fixed.**
`foci_affine.order_grid_points` recovers the grid axes from the SVD of the whole
point cloud. For a *square* grid the covariance is isotropic -- both singular
values exactly equal -- so the singular vectors are arbitrary, and two images of
the same grid can be ordered along different axes. The pairing is then
transposed and the fit returns a confident, wrong transform whose tell is that
only the *second column* of the matrix is wrong. `FociAffineWidget` defaults to
a 10x10 grid, so the degenerate case was the default one.

The rewrite recovers axes from nearest-neighbour displacement vectors (local
lattice geometry, well-conditioned however square the grid is), angles
quadrupled before circular averaging to fold away the lattice's 90-degree
symmetry. `estimate_grid_angle` is public and sign-matched to the standard
rotation matrix. Because ordering still cannot distinguish rows from columns
across a near-quarter-turn, `find_grid_correspondence` measures both grids'
tilt and **refuses past 30 degrees** rather than returning a transposed
pairing.

### 0.7 Model chooser (added 2026-08-11)

The widget now has a **Model** combo box. Supporting it meant making *fitting*
per-model the way correspondence is already per-strategy, so `estimate.py` gained
an estimator registry (`estimator_specs`, `fit_transform`) alongside the shared
RANSAC/residual scoring:

| Estimator | DOF | Min pairs | Produces |
|---|---|---|---|
| affine | 6 | 3 | `affine` |
| similarity | 4 | 2 | `affine` |
| rigid | 3 | 2 | `affine` |
| translation | 2 | 1 | `affine` |
| rotation90 | 0 (discrete) | 1 | `rotation90` |
| identity | 0 | 1 | `identity` |

Two design points worth keeping:

* **Constrained fits need no new model kinds.** A rigid or similarity transform
  *is* an affine whose linear part is constrained; the constraint belongs to the
  fit, not to the stored matrix. They therefore all emit `affine`, and the
  estimator name is recorded in provenance (and shown in the widget status as
  `rigid -> affine`) so the constraint is not lost.
* **RANSAC moved in-module.** One generic RANSAC over pluggable closed-form
  solvers (least squares for affine, Umeyama with a reflection guard for
  similarity/rigid, mean offset for translation) replaced the scikit-image
  `measure.ransac` call. Every model is now fitted and scored identically, and
  the module no longer tracks scikit-image's estimator-protocol churn -- the
  `random_state`/`rng` rename had already forced one compatibility branch.

The chooser earns its place as a diagnostic. On a pair whose truth is a
similarity (rotation + 1.046 scale + shift), the median residuals are: affine
0.009 px, similarity 0.009 px, rigid 4.4 px, translation 10.4 px, identity
7.9 px. A free affine fits anything, including distortion that a constrained
model would have flagged; picking the model class is how you find that out.

Deferred from Step 0 by design, as planned: the live snap-from-detector path,
the display-layer apply, the target-layer chooser, startup-calibration wiring,
and the `imreconstruct.localizer` autolocalize path (that module does not exist
in Imswitch2).

---

## Phase 1 — Frame registry and composition

- `frames.py` — named frames, edge registry, shortest-path resolution with
  direct-edge preference (D1), cycle detection, and path recorded in the result.
- Extend `SpatialTransform` with `compose()` across a resolved path, accumulating
  and reporting combined residual estimates.
- Config surface: where frames are declared per setup, and where transform files
  live (one discoverable per-setup directory, so what imcontrol calibrates,
  improcess can find).

## Phase 2 — Model coverage

- `translation`, `rigid`, `similarity` (constrained fits — often what you
  actually want, and better conditioned than a free affine).
- `poly3`, absorbing `EtSTEDTransformService`'s polynomial as a *model kind*.
  This kills the `exec` of arbitrary `.py` files from a user data directory
  (`EtSTEDTransformService:140`); genuinely exotic transforms become registered
  plugin classes instead, matching improcess's existing drop-in plugin system.
- 3D. Decide at Phase 2 start, not later: multicolor and Snouty are 3D,
  detector↔detector is usually 2D, and designing 2D-only then retrofitting Z is
  the classic route back to two systems.

## Phase 3 — Application layer

- `as_display_affine()` per D7 — napari/vispy layer transform, no resampling.
- `warp()` — extract the warping engine currently buried in
  `tile_mosaic.py:2987` (validity masks, slab-wise assembly) into the shared
  module, and have tiling call it rather than own it.
- Physical↔pixel derivation per D3, including the mismatch check against
  recorded acquisition context.

## Phase 4 — Widgets in both modules

- imcontrol front-end: live calibration (snap on both detectors; move a
  stage/galvo and watch) — this restores the original `FociAffine` snap path on
  top of the now-general core.
- improcess front-end: file/layer-based calibration from two loaded results.
- Same core, two acquisition front-ends, one file format.

## Phase 5 — Migrations

In this order, each behind adapters that keep the old path working and write
both formats for a transition period:

1. **`detector_transform.py` / tiling.** Becomes a thin reader over
   `SpatialTransform`. **Do not silently flip the non-identity gate**
   (`SetupInfo.py:421`) — tiling's affine placement path exists but has never
   seen real calibration data. Enabling it is its own decision, with its own
   validation.
2. **etSTED.** Polynomial as a model kind; `EtSTEDAutoCalibration`'s RANSAC
   becomes a correspondence strategy. Keeps reading old `.csv` coefficients.
3. **multicolor.** Last, and possibly never fully. Its "spaces" are strips of a
   *single* array (`x_bounds` / `roi_width` / `split_axis`), not two detectors.
   It can be expressed as frames, but forcing it may be more contortion than
   value. Acceptable outcome: multicolor stays a specialized *producer* that
   emits standard transform files.

## Phase 6 — Docs

Sphinx `.rst` + toctree entry (`conf.py` has no `myst_parser`, so `.md` never
renders): the frame concept, the file format with a worked example, the
calibration workflow, and an explicit "calibrate in raw detector coordinates"
warning per D5.

---

## Open questions

1. **µm-canonical (D3) from day one**, or pixel-space v1 with a recorded-context
   mismatch warning? µm is correct but needs reliable pixel size + ROI offset
   per detector at apply time.
2. **3D from day one, or 2D with a designed-in Z axis?** (Phase 2.)
3. **Frame graph (D1) or plain pairwise detector→reference?** The graph is ~100
   lines more and covers the stage-frame bug class.

None of these block Step 0 — Step 0 is deliberately 2D, pixel-space, and
pairwise, and its file format carries the fields (`units`, `context`,
`source_frame`/`target_frame`) that let the answers land later without a
migration.
