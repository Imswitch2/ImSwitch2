# ImProcess ROI Manager 2.0 — Design & Implementation Plan

| | |
| --- | --- |
| **Status** | **Tracks A+B implemented** on `feat/improcess-roi-manager-2-0` (branched from `origin/main` @ fb6efb91). Tracks A+B are the committed scope (Q-13a). **P-0 ✅ · P-T ✅ · P-F ✅ · P-G ✅ · P-1 ✅ · P-2 ✅ · P-J ✅ · P-3 ✅ · P-4 ✅**, with a round-8 review pass folded in (§15.8). Roadmap: **P-5 ✅** (§15.9) · **P-S ✅** (§15.10) · **P-6 ✅** (§15.11, less the Fiji fixture corpus); P-U, P-P, P-7, P-R, P-F2 not started. See §15 |
| **Date** | 2026-08-08 (r1) · 2026-08-09 (r2–r5) |
| **Branch** | `Improcess-multi-recon-processing` (plan doc only; no code changed) |
| **Supersedes** | Phase 2 of [improcess-analysis-widgets.md](improcess-analysis-widgets.md) |
| **Primary scope** | `imswitch/improcess/analysis/`, `imswitch/improcess/view/ROIManagerWidget.py`, `imswitch/imcommon/algorithms/roi*.py`, `imswitch/imcommon/view/guitools/naparitools.py` |
| **Secondary scope** | Segmentation / PSF / Colocalization / Profile / ROI-stats panels, `ProcessingResult` + `DisplayLayerSpec` contracts, `ImProcessMainController` wiring, CI napari lane, `docs/improcess.rst` |
| **Committed scope** | **Tracks A + B ≈ 35 d** (Q-13a). Tracks C + D + P-F2 ≈ 25 d are approved roadmap, not a delivery commitment. See §6.0. |

---

## 0. How to review this document

Stable identifiers, never recycled or renumbered:

* **`D-nn`** — defects in the current implementation (§2.2).
* **`C-nn`** — constraints: verified facts about the codebase, not choices (§2.3).
* **`A-nn`** — architecture decisions (§4), each with rejected alternatives.
* **`F-nn`** — review findings and where they landed (§0.1). Round 1 = F-01…F-10
  plus c-1…c-4; round 2 = **F-11…F-23**.
* **`Q-nn`** — open questions (§5).
* **`P-x.y`** — phase tasks (§6).

Review order for round 3: **§0.1 (did I land your findings?) → §0.2 (the phase
restructure, which is the biggest change) → §5 Q-11…Q-14, especially Q-13
(scope) → §4.6 SpatialFrame → §4.7 tool broker**.

---

### 0.1 Round-2 findings and where they landed

All thirteen findings, all seven recommended additions, and the three
open-question calls are accepted. Each was verified against the code first; two
verifications changed the fix, and one exposed a defect in my own round-2
proposal.

| # | Finding | Verified | Landed in |
| --- | --- | --- | --- |
| **F-11** | `ROIFrame` creates a package dependency inversion — `imcommon` cannot import `improcess` | ✅ `ROIRecord` is in `imcommon/algorithms/roi.py`; r2 put the frame type in `improcess/analysis/` | **A-10 rewritten**: pure `SpatialFrame` in `imcommon/algorithms/spatial_frame.py`; the layer→frame adapter stays in `improcess` (it is the only side that knows napari layers). Enforced by `test_layering_boundaries.py` |
| **F-12** | The phase graph still contains impossible intermediate commits (6 instances) | ✅ every one | **§0.2 restructure**: all append-only record fields land in **P-G.0**, immediately after P-0.6, before any geometry, identity, overlay or capture work. Phase dependencies rewritten and now stated as a DAG |
| **F-13** | Reusing one Shapes layer does not solve multi-panel ownership | ✅ [`ProfileWidget.py:180`, `:191`](../../../imswitch/improcess/view/ProfileWidget.py) call `clear_shapes()` unconditionally; `_findFirstShape` (`:206`) takes the first match; `ROIStatsWidget:89` the same | **D-14** + new phase **P-T** (`ViewerToolService`, A-19). Critically: **P-0.3 as written in round 2 was wrong** — sharing the layer without an ownership model converts a cosmetic duplicate-layer bug into cross-panel shape erasure. P-0.3 is withdrawn and folded into P-T |
| **F-14** | `ROIFrame` cannot identify a coordinate plane | ✅ `ProcessingResult` has only a mutable `self.name` ([`result.py:148`](../../../imswitch/improcess/model/result.py)) — no stable id; no layer sets `translate`/`affine` today | **D-16**, **C-15/C-16**, **A-10 rewritten** as `SpatialFrame` (frame_uid, dataset_uid, plane_axes, full axis descriptors, pixel→world affine, component, view mode, lineage) + new phase **P-F** |
| **F-15** | A-13 is too permissive — an XY ROI on an XZ plane is not "reframed" | ✅ my own r2 text | **A-13 rewritten** as an ordered compatibility ladder: `exact` › `registered` › `pixel-compatible` › `clippable` › `incompatible`. **Q-08 answered: refuse by default**, opt in through a preflight dialog |
| **F-16** | Overlay needs ownership + multipart contract; must be read-only; overlay clicks hijack `active_image_layer()` | ✅ `roi_outline()` already returns a *list* of polygons, contradicting r2's "one shape per ROI" criterion; `active_image_layer` falls back to the first image layer when the active layer is not an image ([`layer_selection.py:38-53`](../../../imswitch/improcess/layer_selection.py)) | **A-05 rewritten**: per-part features (`roi_uid`, `part_index`, `label_anchor`), `editable=False`, and an explicit panel-held target layer (A-19 tracks it) instead of relying on the active-layer fallback |
| **F-17** | "No image-sized mask" is impossible for Make Inverse | ✅ my own r2 criterion | **A-17 (new)**: one documented full-frame exception list (Make Inverse, Create Mask, labels export) with a memory guard; the criterion now reads "no operation allocates an image-sized mask **except those on the A-17 list**" |
| **F-18** | RLE needs a worst-case policy | ✅ my r2 arithmetic was wrong in both directions — a filled 1000×1000 mask is **2** runs, not ~2000; a checkerboard is ~1e6 runs | **A-03 rewritten** as an adaptive `MaskPayload` (`rle` \| `bits`, explicit codec + local shape + validation), chosen by encoded size |
| **F-19** | Measurement execution needs a real job architecture | ✅ nothing exists today | **A-22 (new)** + new phase **P-J**: immutable `MeasurementJob` snapshot, worker, cancellation token, progress, stale-result rejection, and a defined cache-invalidation key (which r2 hand-waved — see D-15) |
| **F-20** | Measurement schema inconsistent (5 sub-points) | ✅ | **Appendix B rewritten** with `_px`/`_cal` pairs for *every* spatial measurement; **A-18 (new)** pins conventions (pixel centres, boundary inclusion, subpixel rasterisation, line width, `ddof` → **Q-12**); **A-20 (new)** mixed-unit plotting rule; threshold source defined in P-3.4; line measurements scheduled as **P-3.7** |
| **F-21** | Native format needs explicit versioning | ✅ | **A-24/P-6.2**: versioned envelope, migrations, unknown-version refusal, semantic validation, atomic write, **semantic** round-trip tests (not byte-identical — r2's criterion was the weaker one) |
| **F-22** | Style model does not support the promised UI | ✅ r2 added only `stroke_color` while P-7.4 promised width and fill | **A-16b**: an immutable shared `ROIStyle` (stroke colour/width, fill colour/opacity, label colour, label visible), on the record and on the set |
| **F-23** | napari support claim needs a test policy | ✅ `setup.cfg:32` is `napari>=0.7.0` — unbounded; CI has **no** napari version matrix, and GUI tests live in the manual xvfb `imswitch-test` workflow ([`ci.yml:36-38`](../../../.github/workflows/ci.yml)) | **C-06 rewritten** + **P-1.7**: cap at `<0.8` *or* test floor+newest (**Q-11**), plus a real-`napari.layers.Shapes` subprocess smoke test in the existing xvfb lane |

**Recommended additions — all accepted** as phases: **P-F** (spatial provenance),
**P-T** (viewer tool broker), **P-J** (background jobs), **P-U** (undo/redo &
commands), **P-S** (named ROI sets), **P-P** (points/multipoint), **P-R**
(ROI-aware processor protocol), plus interop hardening as **P-6.6**.

**Open-question calls recorded:** Q-08 refuse-by-default + preflight opt-in ·
Q-09 (a) with both pixel-domain and calibrated descriptors exposed consistently ·
Q-10 full point support as a separate **P-P**, not blocking P-2.

**File-count instruction recorded** as **C-13** and **A-25**: no new automatic
sidecar files; ROI sets live in the existing state store or inside the result
container, and user-initiated export is the only path that writes a new file.
This plan also stays a *single* document — no companion plan doc for P-F.

---

### 0.2 What changed structurally in round 3

Round 2's ordering was still wrong (F-12). The fix is to stop treating the
record contract as something that evolves across phases:

> **All append-only `ROIRecord` fields land once, in P-G.0, before anything is
> built on them.** Geometry, identity, overlay, capture, style and frames then
> all target the final contract, so there is no commit in which a helper
> promises a field that does not exist.

Second, three things round 2 treated as details are actually foundations that
other panels also need, so they became their own phases *ahead* of the ROI work:

* **P-F** — spatial provenance (`SpatialFrame`, stable ids, affine, lineage).
  Benefits every calibrated processor, not just this panel.
* **P-T** — the viewer tool broker. Required *before* the layer-sharing fix,
  not after it (F-13 / D-14).
* **P-J** — the measurement job model, required before Multi Measure claims to
  be cancellable.

The phase set is now grouped into four tracks (§6.0) so the scope decision is
explicit rather than implied.

---

### 0.3 Round-3 findings and where they landed

All five blockers, all seven refinements and the DAG corrections are accepted.
Two were verified by running against the installed napari and the persistence
service; both produced evidence that is now quoted in the design.

| # | Finding | Verified | Landed in |
| --- | --- | --- | --- |
| **F-24** | Core uses `ROISet` (P-2.7) and commands (A-23) before they exist (P-S / P-U, both outside Core) | ✅ my own §6 | **P-G.0 / P-G.6**: a *minimal* `ROISet` + one active set, `MeasurementConfig` as a pure `imcommon` type, and an Add/Update/Delete/Rename command framework. **P-S** becomes multi-set UX + merge/compare; **P-U** becomes the undo *UI*, recovery and advanced commands |
| **F-25** | Spatial identity underspecified; `lineage` carries no transform, yet `registered` says "measure via the transform" — contradicting "no automatic reprojection". IDs must survive save/reload | ✅ round-3 text contradicts itself | **A-10 rewritten**: `result_uid` / `dataset_uid` / `coordinate_space_uid` separated; `lineage` is *provenance only*; transforms are explicit `TransformEdge` records. **`registered` is deferred out of Core** — Core implements `exact` / `pixel-compatible` / `clippable` / `incompatible` only. **A-27** pins id persistence |
| **F-26** | `MeasurementJob` has no safe image source; a worker must not touch napari/Qt layers off-thread. P-J must precede P-3 | ✅ round-3 job snapshot has no reader; P-3.5 already depends on P-J's cache | **A-22 rewritten** with an `ImagePlaneSource` protocol (pure snapshot, `read_plane`, mutation token, lifetime + live-array policy). **DAG fixed: P-J → P-3 → P-4** |
| **F-27** | Read-only overlay needs an explicit hit-testing design | ✅ **and resolved with a public API**: on napari 0.7.1 a `Shapes` *instance* has `mouse_drag_callbacks` / `mouse_move_callbacks` / `mouse_double_click_callbacks`, and `layer.get_value(position, world=True)` returns `(shape_index, vertex_index)` — `(0, None)` inside shape 0, `(None, None)` outside. Setting `editable = False` also forces `mode` to `pan_zoom` | **A-05b (new)**: full hit-testing contract — effective-affine mapping, tolerance, hole behaviour, overlap tie-break, callback cleanup through the broker |
| **F-28** | `MaskPayload` placement risks a circular import; codec format imprecise | ✅ `roi.py` would need `MaskPayload`, `roi_geometry.py` needs `ROIRecord` | **A-03 rewritten**: payload moves to `imcommon/algorithms/roi_payload.py`; codec values are explicit and include compression; selection by **actual serialised size**; empty masks valid; decompression cap; bytes internally, base64 only at the JSON boundary |
| r-1 | 8-char uid is 32 bits of identity for no benefit | — | **A-14**: full `uuid4` string |
| r-2 | `_replaced()` semantics unclear (uid, revision, bounds, revision scope) | ✅ round 3 said both "every mutation" and "measurement cache key" | **A-15 rewritten** with an explicit table; **revision covers measurement-affecting mutations only** |
| r-3 | "No caller branches on `roi_type`" is too strong — I/O, applicability, overlays and points legitimately need capabilities | ✅ | Restated as "no caller independently rasterises or extracts pixels by type", plus a `roi_capabilities(roi_type)` helper |
| r-4 | `int_den` needs a `_px`/`_cal` pair like every other spatial measurement | ✅ my own Appendix B | Appendix B: `int_den_px` / `int_den_cal` |
| r-5 | ROI sets in the widget-state JSON need a size limit | ✅ state is serialised **twice** — `json.dumps(state)` as a validation pass ([`WidgetStatePersistence.py:175`](../../../imswitch/imcommon/model/WidgetStatePersistence.py)) then `json.dump(..., indent=2)` on write (`:266`) | **A-25 rewritten** with a cap and a spill policy → **Q-15** |
| r-6 | The real-napari smoke test must run on PRs | ✅ GUI tests are currently only in the manual xvfb workflow | **P-1.7** targets the PR lanes in `ci.yml`, not the manual workflow |
| r-7 | Estimates do not add up | ✅ phases summed to 53 d while the track table said 62; Track B was 16 d, not 22 | **§6.0 recomputed from the phase table**, with the round-3 additions costed |

**Open-question calls recorded:** Q-11 (a) cap `napari>=0.7,<0.8` *and* run the
Shapes smoke test on PRs · Q-12 (a) `ddof=1`, `n<2 → NaN`, changelogged ·
Q-13 (a) commit to Foundations + Core; C/D are roadmap · Q-14 (b) state storage
plus explicit export; container embedding becomes a later container-focused task.

One new question falls out of Q-14(b) + r-5: **Q-15** (§5).

---

### 0.4 Round-4 findings and where they landed

All four blockers, the P-J correction and all seven cleanups are accepted. Two
were verified by running against the installed dependencies, and both changed
the design rather than the wording.

| # | Finding | Verified | Landed in |
| --- | --- | --- | --- |
| **F-29** | The ladder can accept unrelated datasets — `pixel-compatible`/`clippable` are reachable before the unrelated-dataset rejection; `identity="derived"` is undefined; `compatibility()` has no way to see `TransformEdge`s | ✅ my own r4 table is order-independent prose, not a decision procedure | **A-13 rewritten as an explicit decision tree** with the coordinate-space check **before** any shape comparison; `identity_kind` added to `SpatialFrame`; `compatibility(..., transforms: TransformRegistry \| None)` takes the registry |
| **F-30** | Identity lifetime contradictory: `result_uid` is instance-scoped yet must survive reload; `frame_uid` is serialised with a result although it describes a display plane that may not exist yet; **Q-14(b) forbids container writes while P-F.6 requires them** | ✅ and worse than stated — there is **no shared writer**: 21 `def save(` implementations, each calling `tifffile.imwrite`/`h5py.File` directly (`improcess/processors/*/result.py`, `reconstructors/*/result.py`, `model/array_result.py`, …) | **A-27 rewritten**: `result_uid` = identity of a *logical serialised result*; **`frame_uid` is derived deterministically** from (space, plane axes, shape, affine, component, view mode) and stored **in the ROI set**, never in the result; **P-F.6 split** — Core keeps ids in memory and in the ROI set with **no container writes** (Q-14b honoured), and container-persisted ids become **P-F2**, a re-estimated roadmap task gated on one central provenance hook |
| **F-31** | The hit-testing design cannot implement its own semantics | ✅ **and I reproduced it**: with a big and a small overlapping rectangle, a point inside both returns `(1, None)` under *either* z-order — `get_value` returns the **topmost** shape (last in draw order), cannot enumerate candidates, cannot pick the smallest, cannot fall through a hole | **A-05b rewritten**: a pure `roi_hit_test(roi, position, *, tolerance)` in `roi_geometry`, evaluated over all bbox-candidate ROIs with the tie-break applied by us. **`get_value` is not used for selection at all**, which also removes a napari-semantics dependency (R-17 retired). **One coordinate representation** pinned: overlay vertices stay in **target-data coordinates** and the overlay layer carries the target's transform |
| **F-32** | The broker API takes owner strings while the prose requires tokens | ✅ | **A-19 rewritten**: `ToolToken(owner_key, generation)`; `clear`, `shapes`, `set_mode`, callback registration and `release` all take the token, and a stale generation raises |
| **F-33** | `MeasurementJob` must freeze the mutation token at creation; "disable caching" does not prevent mixed-time measurement on live arrays | ✅ | **A-22 rewritten**: token captured at job creation, compared at publication; live arrays without a reliable counter are **snapshotted or the run is refused** — never measured opportunistically |
| c-5…c-11 | layer map (`roi_payload.py`, codecs out of `roi_geometry`) · stale "every mutation"/"re-derived" wording · P-J 3 d vs 4 d · DAG omits P-S and P-P/P-5.5 edges · Q-15(a) vs "no files added automatically" and C-13 · frame table missing the four identity fields · status line overstated what Q-15 blocks | ✅ all | applied throughout; C-13 and A-25 now state the one-file exception explicitly |

**Q-15 answered — (a)**: one versioned, atomically written
`improcess_roi_sets.json`; the widget-state store keeps only a marker and
checksum when a set is spilled.

---

## 1. Context

### 1.1 What the panel is today

Shipped as "Phase 2, first rectangular-ROI slice" of the analysis-widgets plan
and untouched since (`06721def`, plus incidental changes in `71c68048`,
`4739557d`, `1b3c5bb9`, `79ae2f24`, `a1626fe7`). It is a list of rectangles or
opaque masks, a read-only table of eight fixed statistics on the current plane,
nine buttons in a row, and **nothing in the viewer** (D-02).

### 1.2 Who already depends on it

| Consumer | How | Consequence |
| --- | --- | --- |
| `SegmentationWidget.add_rois_to_manager()` ([`:218`](../../../imswitch/improcess/view/SegmentationWidget.py)) | `add_rois(analysis.rois(prefix))` | `add_rois()` signature must not change |
| `PSFResolutionWidget` ([`:103`](../../../imswitch/improcess/view/PSFResolutionWidget.py)) → `psf_resolution._extract_fit_points` | `rois()` → processor param | **own `pixels`-or-`bounds` reader (D-11)** |
| `ColocalizationWidget` ([`:123`](../../../imswitch/improcess/view/ColocalizationWidget.py)) → `colocalization._extract_pair` | same | **the same reader, duplicated (D-11)** |
| `ProfileWidget`, `ROIStatsWidget` | share the `Viewer Tools` scratch layer | **cross-panel erasure (D-14)** |
| `ImProcessMainView._wireROIManagerToDependentWidgets()` ([`:1271`](../../../imswitch/improcess/view/ImProcessMainView.py)) | late-binds the panel | keep `setRoiManagerWidget()` |
| `imcontrol` (potential) | `ROIRecord` is in `imcommon` for shared use; `test_layering_boundaries.py:98` asserts type identity | append-only fields; **no `improcess` import from `imcommon` (F-11)** |

### 1.3 Why 2.0 now

The panel has a dead control (D-01) and no viewer feedback (D-02); it is the ROI
source for three panels, two of which silently degrade non-rectangular ROIs to
bounding boxes (D-11); and it shares a scratch layer with two other panels with
no ownership model (D-14). The Fiji-parity features are the visible half; the
foundations are the half that makes the numbers trustworthy.

---

## 2. Audit

### 2.1 Component inventory

| File | Lines | Role |
| --- | --- | --- |
| [`imcommon/algorithms/roi.py`](../../../imswitch/imcommon/algorithms/roi.py) | 51 | `ROIRecord` + `to_dict`/`from_dict` |
| [`improcess/analysis/roi_manager.py`](../../../imswitch/improcess/analysis/roi_manager.py) | 222 | `ROIManagerModel`, `ROIStatsRecord`, `rectangle_roi_from_vertices` |
| [`improcess/analysis/roi_stats.py`](../../../imswitch/improcess/analysis/roi_stats.py) | 69 | `ROIStats` (8 fields), `compute_roi_stats` (`np.std`, ddof=0 — Q-12) |
| [`improcess/analysis/psf_resolution.py`](../../../imswitch/improcess/analysis/psf_resolution.py) | 165–196 | private `pixels`-or-`bounds` reader |
| [`improcess/analysis/colocalization.py`](../../../imswitch/improcess/analysis/colocalization.py) | 139–174 | the same reader, duplicated |
| [`improcess/view/ROIManagerWidget.py`](../../../imswitch/improcess/view/ROIManagerWidget.py) | 348 | the panel |
| [`improcess/view/ProfileWidget.py`](../../../imswitch/improcess/view/ProfileWidget.py) | 180, 191, 206 | `clear_shapes()` on mode change and on clear; first-match shape reads |
| [`improcess/view/ROIStatsWidget.py`](../../../imswitch/improcess/view/ROIStatsWidget.py) | 89, 133 | `clear_shapes()`; first-match rectangle read |
| [`imcommon/view/guitools/naparitools.py`](../../../imswitch/imcommon/view/guitools/naparitools.py) | 1132–1483 | `ViewerToolManager` |
| | 1484+ | `NapariCrosshairOverlay`, `NapariGridOverlay` — overlay pattern |
| [`improcess/layer_selection.py`](../../../imswitch/improcess/layer_selection.py) | 38–53 | `active_image_layer` and its first-image fallback |
| [`improcess/view/ReconstructionView.py`](../../../imswitch/improcess/view/ReconstructionView.py) | 286–307, 380–383 | where scale/unit/labels are written onto layers |
| [`improcess/model/result.py`](../../../imswitch/improcess/model/result.py) | 127–165 | `ProcessingResult.__init__` — **no stable id** |
| [`.github/workflows/ci.yml`](../../../.github/workflows/ci.yml) | 31–56 | two test lanes; no napari matrix; GUI tests deferred to the manual xvfb workflow |

### 2.2 Defect register

**S1** user-visible wrong/dead · **S2** correctness or resource hazard ·
**S3** latent, blocks a 2.0 feature.

| ID | Sev | Defect | Anchor | Fix |
| --- | --- | --- | --- | --- |
| D-01 | S1 | *Visible* checkbox does nothing — `compute_stats` called without `visible_only`, `rois()` unfiltered | `ROIManagerWidget.py:226`, `roi_manager.py:138` | P-0.1 |
| D-02 | S1 | Managed ROIs never drawn; capture ends in `clear_shapes()` | `ROIManagerWidget.py:118` | P-1 |
| D-03 | S2 | One bad ROI blanks the whole table (batch-wide `try`) | `roi_manager.py:188,198`; `roi_stats.py:43`; `ROIManagerWidget.py:225-232` | P-0.2 |
| D-04 | S2 | Duplicate `Viewer Tools` layers — three panels, three managers | `naparitools.py:1209` | **P-T** (not P-0 — see D-14) |
| D-05 | S3 | Table rows keyed by model index | `ROIManagerWidget.py:306-311` | P-0.4 → P-G.3 |
| D-06 | S2 | Measurements pixel-only although the layer carries scale **and** unit | `ROIManagerWidget.py:293-304` | P-3.3 |
| D-07 | S2 | One Python tuple per pixel, built at region construction | `segmentation.py:528-531` | P-2.3 |
| D-08 | S3 | Only one drawn shape can ever be added | `naparitools.py:1248-1261` | P-2.4 |
| D-09 | S3 | No persistence, no import | — | P-6 |
| D-10 | S3 | Measurements never reach the Results dock | `ImProcessMainView.py:821` | P-4.1 |
| D-11 | S2 | PSF & Coloc measure non-`pixels` ROIs as bounding rectangles | `psf_resolution.py:169`, `colocalization.py:150` | P-G.4 |
| D-12 | S3 | Mutations rebuild records field-by-field | `roi_manager.py:57,88,104,118` | P-0.6 |
| D-13 | S3 | Names are not a safe identity; `remove()` deletes every match | `roi_manager.py:68-71` | P-0.7 → P-G.3 |
| **D-14** | **S2** | **New (F-13). Panels erase each other's scratch shapes.** `ProfileWidget._modeChanged`/`_clearShapes` call `clear_shapes()` unconditionally and `_findFirstShape` reads the first match; `ROIStatsWidget._clear_roi` does the same. Today this is masked by the *duplicate-layer* bug (D-04) giving each panel its own layer. **Fixing D-04 alone would expose it** — and P-2.4's multi-shape capture makes it worse | `ProfileWidget.py:180,191,206`; `ROIStatsWidget.py:89,133` | **P-T** |
| **D-15** | **S3** | **New.** `ViewerToolManager.__init__` connects `viewer.camera.events.zoom` and `_ensure_shapes_layer` connects `data`/`mode` handlers; **nothing ever disconnects them**, and no dock-close teardown signal exists. Closed panels keep receiving events and keep their layer alive | `naparitools.py:1179-1182, 1220-1221` | **P-T** |
| **D-16** | **S3** | **New (F-14). `ProcessingResult` has no stable identity** — only a mutable `self.name`, which is also what `layer.metadata["source_result"]` stores. Two unrelated results with equal shape and scale are indistinguishable, so any frame-comparison built on it would report `exact` for unrelated images | `result.py:148`; `ReconstructionView.py:305` | **P-F** |

### 2.3 Constraints

* **C-01** — `viewer.add_*` banned in `improcess/view/` outside a four-file
  allowlist ([`test_result_pipeline_generality.py:87`](../../../imswitch/improcess/_test/test_result_pipeline_generality.py)).
* **C-02** — `ROIRecord` type identity is shared across layers
  (`test_layering_boundaries.py:98`).
* **C-03** — `ROIRecord` is constructed positionally; fields append with defaults.
* **C-04** — `ROIRecord` is frozen: tuples, not lists or ndarrays.
* **C-05** — panels are runtime-loaded and may not exist.
* **C-06** *(rewritten, F-23)* — `setup.cfg:32` declares `napari>=0.7.0`, an
  **unbounded** floor that admits 0.8/1.x. CI has no napari version matrix and
  runs GUI tests only in the manual xvfb `imswitch-test` workflow
  ([`ci.yml:36-38`](../../../.github/workflows/ci.yml)). Any napari API this
  plan relies on (`Shapes.text`, `Shapes.features`, `edge_color`/`edge_width`,
  `editable`) needs either a version cap or a tested range → **Q-11**.
* **C-07** — improcess suite segfaults under the napari plugin: run focused
  files with `-p no:napari` and `QT_QPA_PLATFORM=offscreen`.
* **C-08** — no point drawing mode: `mode_map` is
  pan/select/rectangle/line/ellipse/polygon/path (`naparitools.py:1295-1303`),
  and a Shapes layer has no point type. Points need a Points layer → **P-P**.
* **C-09** — `SegmentationRegion.pixels` has one production consumer (`to_roi`,
  `segmentation.py:55`), but `test_segmentation_processor.py:42` asserts
  `rois()[0].pixels` has length 12 and must change with P-2.3.
* **C-10** — calibration is already atomic on the layer: both render paths set
  `layer.scale`, `metadata["axis_labels"|"scale_unit"|"source_result"]` and (for
  display layers) `["component"]` together
  (`ReconstructionView.py:286-307, 380-383, 416-419`);
  `viewer.dims.axis_labels` is set alongside (`:293`, `:353`).
* **C-11** — measurement panels are not subscribed to the result list:
  `_wire_result_follower` (`ImProcessMainController.py:473`) connects
  `sigCurrentResultChanged` only; `sigResultsChanged` is subscribed solely by
  `ResultProcessorController:14`.
* **C-12** — results are transposed for display by the selected `ViewMode`
  (`ReconstructionViewController.py:112-117`), and display-layer results bypass
  that with their own labels/scales (`:95-105`). Axis labels are arbitrary
  strings — `"Base"`, `"Dataset"` occur, not just C/Z/T.
* **C-13** *(maintainer instruction; refined in r5 by Q-15a)* — **keep
  additional on-disk files to the necessary minimum.** No per-result sidecars,
  ever. **Exactly one** new file is sanctioned: `improcess_roi_sets.json` in the
  config root, for ROI sets too large for the widget-state store, alongside the
  existing `improcess_shortcuts.json`. See A-25.
* **C-14** *(new)* — there is no dock-close/teardown signal and no widget
  `closeEvent` in the measurement panels; viewer event handlers are never
  disconnected (D-15). Lifecycle must be owned by a service, not by widgets.
* **C-15** *(new)* — `ProcessingResult` carries no stable identifier
  (`result.py:127-165`); `DisplayLayerSpec.component` is the closest thing and
  falls back to the name.
* **C-16** *(new)* — no layer sets `translate` or `affine` today, so the origin
  is implicitly zero. A frame must still record them so the contract stays true
  if tiling/registration ever sets them.

---

## 3. ImageJ feature reference

*(Unchanged from round 2 — ImageJ User Guide §30 and §30.7, fetched
2026-08-08.)*

**Buttons:** Add [t] · Update · Delete · Rename… · Measure · Deselect ·
Properties… · Flatten [F] · ☑ Show All · ☑ Labels.
**More ▾:** Open… · Save… · Fill · Draw · AND · OR (Combine) · XOR · Split ·
Add Particles · Multi Measure · Multi Plot · Sort · Specify… · Remove Slice Info ·
Labels… · List · Help · Options….
**Options:** Associate "Show All" ROIs with slices · Restore ROIs centered ·
Use ROI names as labels.
**Set Measurements:** Area · Mean · StdDev · Modal · Min&Max · Centroid ·
Center of mass · Perimeter · Bounding rectangle · Fit ellipse · Shape
descriptors · Feret's diameter · Integrated density · Median · Skewness ·
Kurtosis · Area fraction · Stack position.
**Edit▷Selection:** Enlarge · Make Band · To Bounding Box · Convex Hull · Make
Inverse · Interpolate · Translate · Scale · Rotate · Fit Ellipse · Create Mask ·
Create Selection · Line↔Area.
**Keys:** `T` add · Shift/Ctrl-click multi-select · Alt-click re-activate.

### 3.7 Parity matrix

| ImageJ capability | Today | Target |
| --- | --- | --- |
| Add [t] / Update / Delete(multi) / Deselect | partial, single-selection | P-2.4, P-7.2, P-7.4 |
| Rename… | ✅ | — |
| Measure (selected/all) | *Refresh Stats*, all, current plane | P-4.1 |
| Properties… (stroke/fill/group) | — | A-16b, P-7.4 |
| **Show All / Labels** | inert *Visible* column | **P-1** |
| Open…/Save… (`.roi`, `RoiSet.zip`) | export-only CSV/JSON | P-6.1 |
| AND/OR/XOR/Split | — | P-5.1 |
| Add Particles | ✅ Segmentation → *Add ROIs* | — |
| Multi Measure / Multi Plot | — | P-4.2, P-4.3 |
| Sort / Specify… / Options… | — | P-7.4, P-7.5 |
| Remove Slice Info | n/a | P-2.5 |
| Set Measurements | 8 fixed columns | P-3 |
| Oval/polygon/freehand/line ROIs | rectangle + opaque mask | P-2.4 |
| **Point / multipoint** | — | **P-P** (Q-10c) |
| Selection ops | — | P-5 |
| Per-ROI stack position | — | P-2.5, axis-labelled |
| Flatten | — | §12 |

**Beyond ImageJ:** measure one ROI set across every reconstruction (P-4.4, gated
by A-13); ROI as a processor input (**P-R**, now scheduled).

---

## 4. Target architecture

### 4.1 Layer map *(revised — F-11)*

```
imcommon/algorithms/            ← pure data + numpy only; NEVER imports improcess
  roi_payload.py    MaskPayload + codecs      (A-03; imports numpy ONLY — F-28)
  roi_style.py      ROIStyle                                   (A-16b)
  roi.py            ROIRecord 2.0 (final field set from P-G.0) ──► roi_payload, roi_style
  spatial_frame.py  SpatialFrame, AxisDescriptor, TransformEdge/Registry,
                    compatibility() decision tree              (A-10/A-13)
  roi_geometry.py   rasterise / outline / hit-test / set-ops   (A-04/A-05b)
                    ──► roi, roi_payload   (codecs live in roi_payload, not here)
  roi_set.py        ROISet, MeasurementConfig                  (A-24)
  segmentation.py   SegmentationRegion carries a MaskPayload, not tuples

imcommon/view/guitools/
  naparitools.py    ViewerToolManager (internal) + NapariROIOverlay (A-05)
  viewer_tools.py   ViewerToolService — viewer-scoped broker    (A-19)

improcess/analysis/
  roi_manager.py       ROIManagerModel + command log            (A-23)
  roi_frame_adapter.py layer/viewer → SpatialFrame  ← the ONLY napari-aware side
  roi_measurements.py  measurement registry + conventions       (A-06/A-18)
  roi_stats.py         kept: shim over the registry
  roi_jobs.py          MeasurementJob + worker                  (A-22)
  roi_io.py            ImageJ .roi/.zip + versioned native envelope (A-24)
  psf_resolution.py    → shared rasteriser
  colocalization.py    → shared rasteriser

improcess/view/
  ROIManagerWidget.py  panel; no viewer.add_* (C-01)
  ROIMeasurementsDialog.py · ROISpecifyDialog.py · ROIPreflightDialog.py
```

**A-01 — the model stays UI-free.** Model, geometry, frames, measurements,
commands and jobs must be importable and testable without Qt or napari.

**A-26 (new, F-11) — the dependency rule is testable.** `test_layering_boundaries.py`
gains an assertion that nothing under `imcommon/algorithms/` imports `improcess`
or `napari`, so this class of inversion cannot recur.

---

### 4.2 `ROIRecord` 2.0 — the **final** field set, landed once (A-02, F-12)

```python
@dataclass(frozen=True)
class ROIRecord:
    # --- 1.0, unchanged, same order ------------------------------------
    name: str
    roi_type: str          # + ellipse|polygon|freehand|line|polyline|composite|point*
    bounds: tuple[int, int, int, int]      # half-open, always derived
    visible: bool = True
    source: str = "manual"
    pixels: tuple[tuple[int, int], ...] | None = None      # DEPRECATED, still read

    # --- 2.0, appended in ONE step (P-G.0) -----------------------------
    uid: str = ""                                  # A-14, assigned on ingest
    revision: int = 0                              # A-21/A-15: bumped ONLY by
                                                   # measurement-affecting changes
    vertices: tuple[tuple[float, float], ...] | None = None
    mask: MaskPayload | None = None                # A-03, adaptive codec
    position: tuple[tuple[str, int], ...] = ()     # A-11, axis-labelled
    frame_uid: str = ""                            # A-10; the frame itself lives in the set
    style: ROIStyle | None = None                  # A-16b
    group: int = 0
    properties: tuple[tuple[str, str], ...] = ()
```

Invariants: `bounds` is always the derived tight half-open box; at most one of
`vertices` / `mask` / `pixels` is populated; `to_dict()` omits defaults so
1.0-shaped JSON stays 1.0-shaped; `from_dict()` tolerates any subset.

`frame_uid` is a *reference*; the `SpatialFrame` objects live once per `ROISet`
(A-24), not duplicated on every record — which also keeps the serialised form
small (C-13).

**A-15 — one mutation helper.** `_replaced(roi, **changes)` — `dataclasses.replace`,
**preserving `uid`, incrementing `revision` when the change is
measurement-affecting, and recomputing `bounds` only when geometry changed** —
is the only way the model produces a modified record. Constructing
`ROIRecord(...)` field-by-field inside the model is forbidden, with a
parametrised preservation test per mutation. Full semantics table in §4.9.

**A-21 (F-19) — `revision`** is incremented by measurement-affecting mutations
only (geometry, `position`, `frame_uid`, `visible`) and is part of the
measurement cache key (A-22). Renames and style changes drive redraws through
the *set*'s revision instead.

**A-16b (new, F-22) — `ROIStyle`**, immutable and shared:

```python
@dataclass(frozen=True)
class ROIStyle:
    stroke_color: str | None = None
    stroke_width: float | None = None      # screen px; None → overlay default
    fill_color: str | None = None
    fill_opacity: float = 0.0
    label_color: str | None = None
    label_visible: bool = True
```

A record's `style` is `None` when it inherits the set's default style — so the
common case serialises to nothing.

---

### 4.3 Geometry (A-03 rewritten, A-04, A-17 new)

**A-03 (rewritten again in r4, F-18 + F-28) — adaptive `MaskPayload`, in its own
module.** Round 2 asserted RLE is always better; that was wrong in both
directions (a filled 1000×1000 mask is **2** runs; a checkerboard is ~1e6).

**Placement (F-28).** `ROIRecord` must reference `MaskPayload`, and every
geometry function must import `ROIRecord`; putting the payload in
`roi_geometry.py` is a circular import waiting to happen. It lives in
**`imcommon/algorithms/roi_payload.py`**, which imports nothing from the package
except numpy:

```
roi_payload.py  ──►  (numpy only)
roi.py          ──►  roi_payload
roi_geometry.py ──►  roi, roi_payload
```

```python
@dataclass(frozen=True)
class MaskPayload:
    codec: Literal["rle", "bits", "bits-zlib"]   # compression is a CODEC value,
                                                 # never an implicit flag
    shape: tuple[int, int]        # LOCAL shape — the ROI's bounding box
    data: tuple[int, ...] | bytes # rle: alternating off/on runs, C order,
                                  #      starting with an off run
                                  # bits*: raw bytes (np.packbits [+ zlib])
    nbytes: int = 0               # decoded byte budget, validated on decode
```

* **`bytes` internally, base64 only at the JSON boundary** (r-5/F-28). The
  in-memory payload never carries a base64 string; `roi_io` encodes on write and
  decodes on read, so nothing in the model pays base64's 33 % overhead.
* **Codec chosen by actual serialised size**, not by run-count arithmetic: the
  encoder produces both candidates for masks below a size threshold, compares
  `len()` of the serialised forms, and keeps the smaller; above the threshold it
  short-circuits to `bits-zlib`. The heuristic in round 3 (`runs > size/8`) was
  an estimate of the thing we can simply measure.
* **Empty masks are valid.** `AND` of disjoint ROIs legitimately produces one,
  as does an ROI clipped fully outside the image. An empty payload is
  `shape=(0, 0)`, `data=()`; measurements report an empty ROI (D-03), and it is
  *not* an error. Operations that cannot accept one (Split) say so explicitly.
* **Decode validates and caps**: run sum equals `prod(shape)`; `bits` length
  equals `ceil(prod(shape)/8)`; `shape` is non-negative and within a configured
  maximum; **zlib output is bounded by `nbytes`** so a malformed or hostile
  payload cannot expand without limit. Failures raise a typed `MaskPayloadError`
  that the measurement layer reports per ROI rather than crashing a batch.
* Codec is stored, never inferred — a fourth codec is additive.

**A-04 — the canonical rasterisation is LOCAL** (F-05, round 1):

```python
def roi_mask_local(roi, shape) -> tuple[np.ndarray, tuple[slice, slice]]:
    """(local_mask, (row_slice, col_slice)) covering only the ROI's clipped
    bounding box. THE canonical rasterisation; the one place roi_type is
    interpreted. Never raises for an out-of-bounds ROI — returns a zero-sized
    mask, reported as an empty ROI (D-03)."""
```

Measurement reads `image[rs, cs][local_mask]`; set operations work in the union
of the operands' bounding boxes.

**A-17 (new, F-17) — the full-frame exception list.** Some operations are
*defined* as image-sized and cannot be local:

| Operation | Why | Guard |
| --- | --- | --- |
| Make Inverse | the complement of a small ROI spans the image | pre-flight memory estimate; refuse above a configurable cap; result stored as a `bits` payload |
| Create Mask / labels export | the output *is* an image | published as a `ProcessingResult`, so it follows the normal memory path |
| Area fraction with *Limit to threshold* over the full frame | denominator is the frame | computed streaming, no mask materialised |

Everything else is local, and §9's criterion now reads "…except A-17
operations". A lazy complement representation was considered and rejected: it
would infect every consumer of `MaskPayload` with laziness for one operation.

```python
roi_outline(roi, *, max_vertices=256) -> list[np.ndarray]   # MULTIPART (F-16)
roi_from_vertices(...) · roi_from_mask(...) · roi_from_payload(...)
encode_mask(mask) -> MaskPayload · decode_mask(payload) -> np.ndarray
combine(rois, op) · split(roi) · enlarge(roi, n) · make_band(roi, w)
convex_hull(roi) · to_bounding_box(roi) · make_inverse(roi, shape)  # A-17
translate(roi, dr, dc)
```

`scipy`/`skimage` imported lazily inside the functions that need them.

---

### 4.4 Measurements (A-06, A-18 new, A-20 new)

```python
@dataclass(frozen=True)
class Measurement:
    id: str                 # stable, NEVER unit-suffixed (A-16)
    label: str
    group: str
    domain: Literal["pixel", "calibrated", "both"]   # F-20: pairs are explicit
    unit_kind: Literal["none", "length", "area", "intensity", "index"]
    compute: Callable[[MeasurementContext], float]
    default_on: bool = False
```

**A-16 — unit never appears in a column id.** ids are `area_px`/`area_cal`,
`centroid_r_px`/`centroid_r_cal`, … Every spatial measurement has **both**
members of the pair (F-20 — round 2 only did this for area and perimeter). A
`spatial_unit` column carries the unit; the header renders `Area (µm²)`.

**A-18 (new, F-20) — measurement conventions, pinned in one place.**

| Convention | Decision |
| --- | --- |
| Pixel centres | integer indices; pixel *i* spans `[i-0.5, i+0.5)` |
| Rectangle bounds | half-open `(r0, r1, c0, c1)`, matching numpy slicing |
| Boundary inclusion | a pixel belongs to an ROI iff its **centre** lies inside the polygon; ties on the boundary resolve by the even-odd rule |
| Subpixel vertices | preserved in `vertices`; rasterisation uses the centre test above; no anti-aliasing, no partial-pixel weighting |
| Perimeter | **vector ROIs**: exact polygon length. **Mask/composite**: `skimage.measure.perimeter_crofton` (the Crofton estimator — *not* `perimeter`) |
| Shape descriptors | pixel-grid domain (Q-09a), with `pixel_aspect` emitted and a flag when `row_scale/col_scale` deviates >1%; calibrated companions emitted from the scaled outline |
| Ellipse / Feret angles | reported in the **pixel** frame, with `pixel_aspect` alongside |
| Line ROIs | sampled with a configurable integer `line_width` (default 1); >1 averages perpendicular to the line, as ImageJ does |
| Standard deviation | **`ddof=1`** — the sample standard deviation, matching ImageJ (Q-12a). **`n<2 → NaN`**, never 0. `compute_roi_stats` currently uses `np.std` (ddof=0), so this is a deliberate numeric change, changelogged in P-3.8 |
| Limit to threshold | explicit `(lo, hi)` in the measurement options, persisted with them; optionally seeded from the Segmentation panel's current threshold; when unset, all finite pixels count |
| NaN handling | non-finite pixels excluded from intensity statistics but counted in `area_px`; `finite_px` reports the difference |

**A-20 (new, F-20) — mixed units must not be plotted numerically.** Rows from a
nm result and a µm result share `area_cal`, and the generic Graph would happily
plot them together. Rule: any plot request over a `*_cal` column inspects the
selection's `spatial_unit` values and either converts to a common unit (when
both are known length units) or refuses with a message naming the conflict.
Implemented once, in the table-plot spec path, so every panel inherits it.

**Line/profile measurement group** is a real catalogue item, not a promise:
scheduled as **P-3.7** with `length_px`/`length_cal`, `angle`, `mean`, `min`,
`max`, `std` along the line, and the sampling API shared with `ProfileWidget`.

---

### 4.5 Overlay (A-05 rewritten — F-16)

**One dedicated Shapes layer named `"ROI Manager"`, owned by
`NapariROIOverlay` in `imcommon` (C-01), read-only, multipart-aware.**

* **Multipart.** `roi_outline()` returns a list — a composite with holes or
  disconnected parts is several polygons. Each drawn feature carries
  `features = {"roi_uid": …, "part_index": …}`; clicking **any** part selects
  the one ROI. Labels are drawn once per ROI at a `label_anchor` (the centroid
  of the largest part). Round 2's "one shape per ROI" criterion is withdrawn.
* **Read-only.** The layer is created with `mode='pan_zoom'` and
  `editable=False`, and a data-change handler re-renders from the model if
  anything mutates it. Editing an ROI is a *command* (A-23) issued from the
  panel — never a side effect of dragging an overlay vertex, which would
  desynchronise the model with no undo.
* **Target-layer discipline.** Clicking the overlay makes it napari's active
  layer, after which `active_image_layer()` falls back to the *first* image
  layer ([`layer_selection.py:44-53`](../../../imswitch/improcess/layer_selection.py))
  — which may not be what is being measured. The panel therefore holds an
  explicit target image layer, tracked by the tool broker (A-19), and
  `active_image_layer()` is only ever used to *initialise* it.
* Vertices are ROI pixel coordinates mapped through the target layer's **full
  effective affine**, not just scale and translate (A-05b).
* Zoom-compensated edge width, lazy layer creation, safe re-creation after user
  deletion — following `NapariCrosshairOverlay` (`naparitools.py:1484`).
* `"ROI Manager"` joins the exclusion set in `layer_selection.py:16`.

#### A-05b (rewritten in r5, F-31) — hit-testing contract

Round 4 proposed `Shapes.get_value()` as the selection mechanism. **Measured
against napari 0.7.1, it cannot express the semantics we specified:**

```
big   = rectangle (0,0)-(100,100)      point (50,50) is inside BOTH
small = rectangle (40,40)-(60,60)
Shapes([big, small]).get_value((50,50))  →  (1, None)   # the small one — last
Shapes([small, big]).get_value((50,50))  →  (1, None)   # the BIG one — last
```

It returns the **topmost shape by draw order**, one index, with no way to
enumerate the other candidates. Smallest-area tie-break and hole fall-through
are therefore not implementable on top of it.

**Decision: hit-testing is ours, in pure geometry.** `roi_geometry` gains

```python
def roi_hit_test(roi, position, *, tolerance: float) -> bool:
    """True if `position` (in ROI pixel coordinates) is inside `roi`, or within
    `tolerance` of its boundary. Holes count as OUTSIDE. Pure; no napari."""
```

and the panel evaluates every candidate itself:

| Aspect | Decision |
| --- | --- |
| Candidate set | ROIs whose `bounds` (expanded by `tolerance`) contain the point — a cheap filter before the exact test |
| Test | `roi_hit_test` per candidate, so **all** hits are known, not just the topmost |
| Tie-break | among all hits, the **smallest `area_px`**; ties broken by later position in the set. Now implementable, because we hold the full candidate list |
| Holes | a hole is part of the ROI's geometry, not a separate shape: `roi_hit_test` returns `False` inside it, so the click falls through to whatever else contains the point |
| Tolerance | `max(2 px, stroke_width)` in **screen** space, converted to data units via the camera zoom, so thin ROIs stay clickable at any zoom |
| **Coordinate representation** *(pinned — F-31)* | **Overlay vertices are stored in target-data coordinates, and the overlay layer carries the target layer's `scale`/`translate`/`affine`.** They are *never* pre-transformed into world coordinates. Consequently `event.position` (world) → data coords happens exactly once, through the overlay layer's own transform, and the value handed to `roi_hit_test` is already in ROI pixel coordinates. There is no second representation to keep in sync |
| napari surface used | `mouse_drag_callbacks` on the overlay layer, for the event position only. **`get_value` is not used**, which also removes the dependency on its semantics (R-17 retired) |
| Modifiers | plain click = select; `Ctrl`/`Cmd`+click = toggle; `Shift`+click = range-extend, mirroring the table (P-7.2) |
| Cleanup | the callback is registered with the broker's `ToolToken` and removed by `release(token)` (A-19/D-15) |
| Non-goal | dragging, vertex editing and deletion on the overlay stay disabled; those are commands (A-23) |

Still true from the r4 verification, and still relied on: a `Shapes` instance
exposes `mouse_drag_callbacks`/`mouse_move_callbacks`, and setting
`editable = False` forces `mode` to `pan_zoom` on its own.

---

### 4.6 Spatial provenance (A-10 … A-13, rewritten — F-11, F-14, F-15)

#### A-10 — `SpatialFrame`, in `imcommon` (F-11)

> **An ROI stores pixel indices in the row/column plane of the image it was
> captured on, as displayed** — not world coordinates, not raw-array indices
> before the view-mode transposition (C-12).

Pixel indices are meaningless without saying *of what*, and round 2's `ROIFrame`
could not answer that (F-14). The replacement is a **pure** type in
`imcommon/algorithms/spatial_frame.py` — no napari, no `improcess` import:

**Four identities, not one (F-25).** Round 3 collapsed distinct things into
`dataset_uid` + `lineage`, then asked `lineage` to carry a spatial transform it
cannot express:

| Id | Answers | Lifetime |
| --- | --- | --- |
| `result_uid` | "which processing result is this?" | one `ProcessingResult` instance; minted at construction, serialised with it |
| `dataset_uid` | "which acquisition/source does this ultimately come from?" | durable across processing; inherited by derived results |
| `coordinate_space_uid` | "which shared pixel grid is this on?" | equal for two results that are pixel-aligned by construction (e.g. a filter output and its input); **this, not `dataset_uid`, is what `exact` compares** |
| `lineage` | "which results was this derived from?" | **provenance only — never a transform** |

Spatial relationships between *different* coordinate spaces are separate,
explicit records:

```python
@dataclass(frozen=True)
class TransformEdge:
    src_space: str            # coordinate_space_uid
    dst_space: str
    affine: tuple[float, ...] # 3x3 row-major, src pixel → dst pixel
    source: str               # "registration" | "manual" | "derived"
    confidence: float | None = None

class TransformRegistry(Protocol):
    def edge(self, src_space: str, dst_space: str) -> TransformEdge | None: ...
```

Edges live in a registry keyed by `(src, dst)`, and **`compatibility()` takes
that registry as an argument** (F-29) — round 4 asked it to find edges with no
way to reach them. **Nothing in Core creates or consumes edges**; they exist so
the `registered` rung is well-defined when someone implements it, rather than
being hand-waved by `lineage`. Passing `transforms=None` (the Core default)
simply means no edge is ever found.

```python
@dataclass(frozen=True)
class AxisDescriptor:
    label: str                 # "Z", "T", "Base", "Dataset", …
    size: int                  # full extent — lets "Z=12" be validated
    scale: float = 1.0
    unit: str = "px"

@dataclass(frozen=True)
class SpatialFrame:
    # --- the four identities (F-25), all present on every frame -------------
    frame_uid: str                     # DERIVED, not minted — see A-27
    coordinate_space_uid: str          # the shared pixel grid; what `exact` compares
    result_uid: str                    # the logical serialised result it came from
    dataset_uid: str                   # durable acquisition identity
    # --- the plane ----------------------------------------------------------
    plane_axes: tuple[str, str]        # which two labels form the displayed plane
    axes: tuple[AxisDescriptor, ...]   # ALL axes, in displayed order
    shape: tuple[int, int]             # displayed (rows, cols)
    affine: tuple[float, ...]          # 3x3 row-major pixel→world; identity today (C-16)
    unit: str = "px"
    component: str | None = None
    view_mode: str | None = None
    lineage: tuple[str, ...] = ()      # result_uids this derives from — provenance only
    identity_kind: Literal["minted", "derived"] = "minted"   # F-29
```

`identity_kind` (F-29) records **how the ids were obtained**: `"minted"` when
they were created with the result and carried through in memory, `"derived"`
when they were reconstructed from a content digest because the source had none
(A-27). A `"derived"` frame can never reach `exact` — an inferred identity must
not be able to claim certainty.

The napari-aware constructor `frame_from_layer(layer, viewer)` lives in
`improcess/analysis/roi_frame_adapter.py` and reads `layer.scale`,
`layer.translate`, `layer.affine` and `layer.metadata` **atomically from one
layer** (C-10) — round 2's mixing of layer scale with result unit is gone.

#### A-27 (rewritten in r5, F-30) — identity lifetime

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

#### A-11 — Positions are axis-labelled

`position: tuple[tuple[str, int], ...]`, e.g. `(("Z", 12), ("T", 3))`, captured
from `viewer.dims.axis_labels` + `current_step` for the non-displayed axes.
An absent axis means "every index on that axis" (Q-03a). Matching is **by
label**, never by index, so a transposed view still resolves. `AxisDescriptor.size`
makes `Z=12` validatable — a position beyond the target's extent is
`incompatible`, not silently clamped.

#### A-12 — Calibration is read atomically from one layer

`row_scale`, `col_scale`, `unit`, `axis_labels`, `translate`, `source` all come
from the same layer object in one call. Fallback when metadata is missing:
`unit="px"`, scales 1.0 — never a guessed unit.

#### A-13 — Compatibility, as a decision tree *(rewritten in r5, F-29)*

Round 4 presented the verdicts as an unordered table, which left
`pixel-compatible` and `clippable` reachable for **unrelated data of the same
shape** — contradicting P-F's own acceptance test. Order is the whole point, so
the contract is now a procedure, not a list:

```python
def compatibility(
    source: SpatialFrame,
    target: SpatialFrame,
    roi: ROIRecord,
    *,
    transforms: TransformRegistry | None = None,   # F-29: it must be reachable
) -> Compatibility:
```

```
1.  source.plane_axes != target.plane_axes                    → incompatible
        (an XY ROI on an XZ plane is a different physical quantity)

2.  any positioned axis of `roi` is absent from target.axes,
    or its index is outside that axis' size                   → incompatible

3.  source.coordinate_space_uid != target.coordinate_space_uid
        ├─ transforms and transforms.edge(src, dst) exists     → registered
        │        (Core: REFUSED, like clippable — see below)
        └─ otherwise                                           → incompatible
                ← UNRELATED DATA IS REJECTED HERE, before any
                  shape or scale comparison is reached (F-29)

    ── from here on, both frames are on the SAME pixel grid ──

4.  source.identity_kind == "derived" or target.identity_kind == "derived"
                                            → cap the result at pixel-compatible
        (an inferred identity may never claim `exact`; A-27)

5.  source.frame_uid == target.frame_uid,
    or (shape equal and affine equal within 1e-6 relative)     → exact

6.  shape equal, scale/unit differ                            → pixel-compatible

7.  shape differs                                             → clippable
```

| Verdict | Default behaviour | In Core? |
| --- | --- | --- |
| `exact` | measure | ✅ |
| `registered` | **refused** — see below | verdict only |
| `pixel-compatible` | measure the same pixels; calibrated columns use the **target** | ✅ |
| `clippable` | **refused by default (Q-08)**; opt in via preflight | ✅ |
| `incompatible` | never measured, never opt-in-able | ✅ |

**`registered` is deferred out of Core (F-25).** Round 3 said "measure via the
transform" two paragraphs after "no automatic reprojection" — a straight
contradiction, because measuring through a transform *is* reprojection. The
resolution: Core computes and reports `registered` as a verdict when an edge
exists, but **treats it as refused**, exactly like `clippable`. Applying a
transform stays an explicit user action (*Rescale/Reframe ROIs to target…*,
P-5.5). Automatic transformed measurement, if ever wanted, arrives later behind
the same preflight — and the ladder is already shaped for it.

**No automatic reprojection**, without exception in Core. Every measured row
carries `geometry_match` and `frame_uid`, so a compromise is visible in the
Results table.

**Q-08 answered — refuse by default.** A batch measure that would produce
`clippable` or worse stops and opens a **preflight dialog** listing, per ROI,
the verdict and what would be lost; the user opts in explicitly, and the opt-in
is recorded per row.

---

### 4.7 Viewer tool broker (A-19, new — F-13/D-14/D-15)

Sharing one `Viewer Tools` layer without ownership is worse than duplicating it:
`ProfileWidget._modeChanged` and `_clearShapes` call `clear_shapes()`
unconditionally, and both Profile and ROI stats read the *first* matching shape.
Today the duplicate-layer bug (D-04) accidentally isolates them.

**The API takes tokens, not owner strings (F-32).** Round 4's prose required
token checks that its signatures could not perform:

```python
@dataclass(frozen=True)
class ToolToken:
    owner_key: str      # stable per panel — NOT id(widget), so a reopened
                        # panel reclaims its own shapes
    generation: int     # bumped on every acquire; a stale token is detectable

class ViewerToolService:            # imcommon/view/guitools/viewer_tools.py
    """One instance per viewer (weak-keyed registry). Owns the scratch layer,
    the active tool, and the shared target image layer."""

    @classmethod
    def for_viewer(cls, viewer) -> "ViewerToolService": ...

    def acquire(self, owner_key: str, mode: str) -> ToolToken: ...

    # every operation is token-gated; a stale generation raises StaleToolToken
    def set_mode(self, token: ToolToken, mode: str) -> None: ...
    def clear(self, token: ToolToken) -> None:        # only that owner's shapes
    def shapes(self, token: ToolToken) -> list: ...   # only that owner's shapes
    def add_callback(self, token: ToolToken, layer, kind, fn) -> None: ...
    def release(self, token: ToolToken) -> None:      # idempotent; drops shapes
                                                      # AND every callback (D-15)
    def share(self, token: ToolToken, dst_owner_key: str) -> None:  # hand-off
    def is_current(self, token: ToolToken) -> bool: ...

    target_image_layer: property   # the layer measurements apply to (A-05/F-16)
    sigTargetLayerChanged: Signal
```

* Scratch shapes are **owner-tagged** through the Shapes layer's `features`, so
  `clear(token)` erases only that owner's shapes and cannot touch the ROI
  manager's in-progress rectangle.
* **Token semantics** (now enforced by the signatures, F-32):
  * `owner_key` is the panel's stable key, not `id(widget)`, so a reopened panel
    reclaims its own shapes rather than orphaning them;
  * every mutating or reading call takes the token; a token whose `generation`
    is not the service's current one for that owner raises `StaleToolToken`
    rather than acting on someone else's shapes;
  * acquiring preempts the previous holder, which receives `sigToolPreempted`
    and must stop listening — preemption bumps the generation but **never
    silently deletes shapes**;
  * `release()` is **idempotent**; releasing a stale token is a no-op, not an
    error (it is what a closing panel will often do);
  * every callback the service installs — including the overlay's hit-test
    handler (A-05b) — goes through `add_callback(token, …)` and is removed by
    `release(token)`, so no handler can outlive its panel.
* `release()` disconnects every handler that owner installed — the fix for D-15,
  which has no other home because no dock-close signal exists (C-14). The panel
  calls it from `hideEvent`/`destroyed`, and the service also drops owners whose
  widget has been garbage-collected (weak references, so a leaked widget cannot
  pin the viewer).
* `target_image_layer` is the single answer to "what am I measuring", replacing
  three panels' independent `active_image_layer()` calls and fixing the overlay
  click-steals-focus problem (F-16).
* `ViewerToolManager` is retained as the service's internal implementation, so
  `imcontrol`'s `ImageWidget` (`ImageWidget.py:25`) keeps working unchanged.

**D-04's fix (one layer) ships inside P-T, never before it.**

---

### 4.8 Results and Graph integration (A-08)

| Output | Mechanism |
| --- | --- |
| Measure / Multi Measure rows | `sigResultPushed(columns, records)` → `_onResultPushed` → `appendResultTableRecords` (precedent: `ROIStatsWidget.py:93`) |
| Multi Plot curves | `sigPlotPushed(PlotPayload)` (precedent: `ProfileWidget.py:30, 637`) |
| Plotting the table | the Results dock's `sigPlotRequested` (`ImProcessMainView.py:337`), now unit-aware per A-20 |

Requires `'roi-manager'` in the `_connectResultPusher` gate
(`ImProcessMainView.py:821`) and the eager connections at `:515-518`. Every
pushed row carries `source`, `kind`, `spatial_unit`, `geometry_match`,
`frame_uid`, `roi_uid`, `roi_revision`.

---

### 4.9 Identity and revisions (A-14, A-21)

* **`uid`** — a full `uuid4` string *(r4, r-1 — round 3's 8 hex chars reduced
  identity to 32 bits for no benefit)*. Assigned by the model on every ingestion
  path when `uid == ""`; immutable; survives rename and visibility changes;
  duplicate mints a new one. Not a `default_factory`, so positionally
  constructed records still compare equal (C-03).
* **`name`** — display only.
* Table rows, overlay features and selection key on `uid`.
* Interim before `uid` exists: unique names enforced at every ingestion path
  (P-0.7), worth keeping regardless.

**A-15 (clarified in r4, r-2) — `_replaced()` semantics.** Round 3 said both
"bumped by every mutation" and "part of the measurement cache key", which cannot
both be cheap. The contract:

| Aspect | Rule |
| --- | --- |
| `uid` | **preserved**, always — except `duplicate()`, which is the only operation that mints a new one |
| `revision` | **incremented by 1** (never "re-derived"), and **only for measurement-affecting changes**: geometry (`vertices`/`mask`/`pixels`/`bounds`), `position`, `frame_uid`, `visible`. Renames, `style`, `group` and `properties` **do not** bump it — otherwise renaming one ROI invalidates 200 cached measurements |
| `bounds` | recomputed **only when geometry changes**. A legacy rectangle has no geometry apart from its bounds, so there is nothing to derive them from and they pass through untouched |
| everything else | `dataclasses.replace` semantics — untouched fields are preserved verbatim, which is the whole point of D-12's fix |

A non-measurement mutation still needs to redraw the table and overlay; that is
driven by the *set*'s `revision` (A-24), not the record's.

---

### 4.10 Measurement jobs (A-22, new — F-19)

**A worker must never touch a napari layer or a Qt object (F-26).** Round 3's
snapshot carried ROIs, a frame and a token — but no way to read a pixel, which
would have forced the worker back onto the layer. The job therefore carries an
`ImagePlaneSource`:

```python
class ImagePlaneSource(Protocol):
    """A pure, thread-safe reader for one image's planes. Constructed on the
    GUI thread from a layer; used only off it. Holds NO napari/Qt references."""

    frame: SpatialFrame                     # immutable
    mutation_token: str                     # changes when the pixels change

    def read_plane(self, position: tuple[tuple[str, int], ...]) -> np.ndarray:
        """Return the 2D plane at an axis-labelled position (A-11). Must be
        safe to call from a worker thread and must not mutate shared state."""

    def close(self) -> None: ...            # releases any file/zarr handle

@dataclass(frozen=True)
class MeasurementJob:
    job_id: str
    rois: tuple[ROIRecord, ...]             # immutable snapshot
    source: ImagePlaneSource                # ← the missing piece (F-26)
    token_at_start: str                     # source.mutation_token, FROZEN (F-33)
    planes: tuple[tuple[tuple[str, int], ...], ...]
    measurements: tuple[str, ...]
    config: MeasurementConfig
```

**The mutation token is captured at job creation (F-33).** `token_at_start` is
what the cache key uses, and publication compares it against
`source.mutation_token` as it stands then; a mismatch discards the results
instead of publishing them. Round 4 read the token lazily, which would have let
a job cache under one value and publish under another.

Two implementations cover what exists today:

* **`ArrayPlaneSource`** — wraps an in-memory `ndarray`.
* **`LazyPlaneSource`** — wraps the existing lazy/virtual array path
  (`model/lazy_array.py`, `virtual_image.py`) for chunked reads, so a long Multi
  Measure never materialises the whole stack.

**Live-array policy (rewritten, F-33).** "Disable caching" does not prevent a
job from measuring plane 1 before an update and plane 40 after it. When the
array can be mutated in place and the source cannot supply a reliable
`mutation_token`, there are exactly two permitted behaviours, chosen by the
caller:

1. **Snapshot** — copy the planes the job needs at creation time, and measure
   the copy. Correct, bounded, and the default for single-plane *Measure*.
2. **Refuse** — decline the run with a message naming the live source. The
   default for *Multi Measure* over many planes, where snapshotting could be
   larger than memory.

Opportunistic measurement of a moving array is never permitted, cached or not.

Lifetime: the panel owns the source, calls `close()` when the job set is
finished, and never shares one source across two concurrent jobs.

* A worker runs the job off the GUI thread with a cancellation token; progress
  is reported per plane; results are published only if the job is still the
  panel's current one (**stale-result rejection** — the failure mode where a
  slow Multi Measure overwrites a newer quick Measure).
* **Cache key**: `(frame_uid, token_at_start, plane, roi_uid, roi_revision,
  measurement_config_revision)` — the **frozen** token (F-33), `roi_revision`
  bumping only for measurement-affecting changes (A-15). When a source cannot
  supply a meaningful token, caching is disabled **and** the snapshot-or-refuse
  policy above applies; disabling the cache alone is not a correctness measure.
* Chunked stack access so a long Multi Measure never loads the whole stack.

**Ordering (F-26): P-J precedes P-3**, because P-3.5's debounce/caching is
defined in terms of this job model. The DAG in §6.0 reflects it.

---

### 4.11 Commands and undo (A-23, new — P-U)

Every model-changing operation is a command object with `do`/`undo`. **Split
across phases (F-24)** — round 3 asserted this in A-23 while scheduling the
whole of P-U outside the committed scope, so Core would have violated its own
invariant:

| Lands in | What |
| --- | --- |
| **P-G.6 (Core)** | The framework and the four basic commands: **Add · Update · Delete · Rename**. Each carries `undo()` from the start — they are `_replaced`-based, so the inverse is nearly free — but there is **no undo stack and no UI**. The value in Core is that every mutation goes through one audited path |
| **P-5** | Boolean/morphological commands, written against the same framework |
| **P-U** | The bounded undo **stack and UI**, `Ctrl+Z`/`Ctrl+Shift+Z` via the config-driven shortcut catalog, transactional import with conflict resolution (skip / rename / replace), Clear, batch property changes, Rescale/reframe, and crash-recovery autosave |

Consequence: **P-U depends on the operations it wraps** (P-5, P-6), and **P-7
depends on P-U** if its shortcut set includes undo/redo. Both edges are now in
the §6.0 DAG.

Crash-recovery autosave writes into the **existing state store**, not a new
file (C-13/A-25), subject to the size policy below.

---

### 4.12 ROI sets (A-24, new — P-S)

A single global list stops making sense once results and frames differ — and
Core already needs the container, because P-2.7 registers frames on it and the
measurement config has to live somewhere (F-24).

```python
@dataclass(frozen=True)
class MeasurementConfig:              # pure imcommon type (F-24)
    selected: tuple[str, ...] = ()    # measurement ids
    decimals: int = 3
    scientific: bool = False
    threshold: tuple[float, float] | None = None   # "Limit to threshold" (A-18)
    revision: int = 0

@dataclass(frozen=True)
class ROISet:
    uid: str
    name: str
    frames: tuple[SpatialFrame, ...]       # referenced by ROIRecord.frame_uid
    rois: tuple[ROIRecord, ...]
    default_style: ROIStyle
    measurement_config: MeasurementConfig
    revision: int = 0                      # bumps on ANY change, incl. renames
    dataset_uid: str | None = None         # optional association
```

**Split across phases (F-24):**

* **P-G.0 (Core)** lands `MeasurementConfig`, `ROISet` and **exactly one active
  set** — enough for P-2.7 to register frames, for P-3 to persist a measurement
  selection, and for P-6 to have a serialisation root.
* **P-S** adds *multiple* named sets: the selector, duplicate, merge (with uid
  and name conflict resolution) and compare.

Frames are stored **once per set** and referenced by `frame_uid`, rather than
copied onto every record — which also keeps the serialised form small (C-13).

---

### 4.13 Persistence and file policy (A-09 rewritten, A-25 new — F-08, C-13)

**A-09 — controller-owned buffering adapter.** The panel is runtime-loaded and
usually absent when startup state is restored (C-05), so a widget-registered
adapter is unsound. `_ROIManagerStateAdapter` is owned by
`ImProcessMainController`, registered under `ImProcessROIManager`:

* `setWidgetState` applies immediately if the panel exists, otherwise stashes
  and applies when `ensureRuntimeAnalysisWidget` next builds it;
* `getWidgetState` returns the panel's state, or the stash verbatim if the panel
  was never opened — so a session that never opened it does not erase it;
* it deliberately does **not** force the panel open.

This mirrors `_GuiLayoutStateAdapter` (`ImProcessMainController.py:731-766`).

**A-25 (rewritten in r4 — C-13, Q-14b, r-5) — minimum on-disk files, with a
size policy.** The widget-state store is not free: `getComponentState` runs
`json.dumps(state)` purely to validate serialisability
([`WidgetStatePersistence.py:175`](../../../imswitch/imcommon/model/WidgetStatePersistence.py))
and `saveWidgetState` then writes `json.dump(..., indent=2)` (`:266`) — so every
state save **serialises the payload twice**, once thrown away, at shutdown. A
5000-ROI segmented set would make closing ImProcess visibly slow.

Storage order, with **Q-15 answered (a)**:

1. **The existing widget-state store** — measurement config, options, the active
   set's metadata, and the ROI list **while it stays under the cap**
   (default: 1 MB serialised or 2000 ROIs, whichever comes first; configurable).
2. **Over the cap → one dedicated file**, `improcess_roi_sets.json` in the
   ImSwitch config root, following the precedent set by
   `improcess_shortcuts.json`
   ([`improcess/controller/shortcuts.py:34`](../../../imswitch/improcess/controller/shortcuts.py)).
   It is **versioned and written atomically** (temp + rename), using the same
   envelope as P-6.2. When a set is spilled, **the widget-state store keeps only
   a marker and a checksum**, so the two can never silently disagree and a
   missing/corrupt spill file is detected rather than half-loaded.
   One file for all ROI sets — never one per result.
3. **User-initiated export only** — `Save ROI set…` (versioned native JSON) and
   ImageJ `.roi`/`RoiSet.zip`.

So "nothing is written automatically" is **not** literally true and should not
be claimed: the spill file is written automatically when the cap is exceeded.
What holds is the rule that matters — **no per-result files, and exactly one new
file in total** (C-13).

**Result containers stay out of scope (Q-14b).** Embedding ROI sets — or ids
(A-27) — in HDF5/Zarr would touch all 21 independent `save()` implementations,
so it becomes **P-F2**, a later container-focused task with its own estimate.
Round 2's per-result sidecar (old P-6.5) stays **withdrawn**.

Restoring ROIs still requires a stored frame (A-10/A-27) — without it the
restore is refused with a message rather than placing pixels on an unknown
image.

---

### 4.14 Data flow

```
   ViewerToolService (per viewer)  ── target_image_layer ──┐
     owner-tagged scratch shapes                            │
            │ Add [T]                                       │
            ▼                                               ▼
    ┌──────────────────────────────┐            frame_from_layer()
    │  ROIManagerModel / ROISet     │◄── commands (undo) ── SpatialFrame
    │  uid + revision keyed         │
    └──────────────────────────────┘
        │           │            │             │
 roi_mask_local  roi_outline  to_dicts      rois()
        │           │            │             │
        ▼           ▼            ▼             ▼
  MeasurementJob  NapariROIOverlay  roi_io   PSF / Coloc / P-R processors
   (worker,        read-only,      versioned      (same rasteriser)
    cancellable)   multipart        envelope
        │
        ▼
  table + sigResultPushed → Results dock (unit-aware plotting, A-20)
        + sigPlotPushed  → Graph panel
```

---

## 5. Questions

### Answered

| Q | Decision |
| --- | --- |
| Q-01 | **(a)** `roifile` as an optional extra |
| Q-02 | **(a)** long form; wide CSV export as P-4.7 |
| Q-03 | **(a)** ImageJ default (not plane-bound), after axis-labelled positions |
| Q-04 | **(b)** Visible = drawn **and** measured; **hidden ROIs keep their table row** |
| Q-05 | **(a)** options only for now; ROI-set persistence follows frames |
| Q-06 | separate task (PSF pixel-size spinbox) |
| Q-07 | first group of selection ops only |
| **Q-08** | **Refuse by default**; explicit preflight dialog to opt into clipping (A-13) |
| **Q-09** | **(a)** pixel-domain descriptors, **with calibrated companions exposed consistently** (A-18, Appendix B) |
| **Q-10** | **Full point support**, as a separate **P-P** phase; does **not** block P-2 |

### Answered in round 3

| Q | Decision | Consequence |
| --- | --- | --- |
| **Q-11** | **(a)** cap `napari>=0.7,<0.8` — **and still run the real `Shapes` smoke test on PRs** | `setup.cfg` capped in P-1.7; smoke test targets the PR lanes, not the manual workflow (r-6) |
| **Q-12** | **(a)** sample standard deviation, `ddof=1`, **`n<2 → NaN`**, changelogged | A-18 and Appendix B updated; `test_legacy_roi_stats_shim_matches_previous_values` becomes a *deliberate* diff with a recorded before/after |
| **Q-13** | **(a)** commit to **Foundations + Core**; tracks C/D approved as roadmap, not delivery | §6.0 splits committed scope from roadmap |
| **Q-14** | **(b)** state storage + explicit export; container embedding becomes a later container-focused task | A-25 rewritten; no result-container writes in this plan |

### New in round 4

**Q-15 · Blocks P-6.3 · Where does an over-cap ROI set live?** (A-25, from
Q-14b + r-5.) With result containers off the table and the widget-state store
double-serialising at shutdown, a large segmented set needs somewhere to go.

* **(a) One dedicated file in the ImSwitch config root**
  (`improcess_roi_sets.json`), following the existing
  `improcess_shortcuts.json` precedent. *(recommended)* — one file total, not
  one per result, so it honours C-13; sets stay restorable across sessions.
* (b) Refuse to persist over the cap; the user must export explicitly. Strictly
  fewer files, but silently loses an expensive segmentation on shutdown unless
  they notice the warning.
* (c) Raise the cap and keep everything in the widget-state store — simplest,
  and makes shutdown slow exactly when the user has the most to lose.

---

## 6. Phase plan

### 6.0 Tracks, estimates and the dependency DAG

**Estimates corrected (r-7).** Round 3's track table said 62 d while its phase
headings summed to 53, and Track B was listed at 22 d against 16 d of phases.
The table below is now computed *from* the phase headings, with the round-3
additions costed in (P-F +1 for the identity split and `TransformEdge`; P-G +2
for minimal `ROISet`/`MeasurementConfig`/commands; P-J +1 for
`ImagePlaneSource`; P-1 +1 for hit-testing; P-S −1 and P-U −0.5 because their
foundations moved into P-G).

| Track | Phases (days) | Total | What you get |
| --- | --- | --- | --- |
| **A — Foundations** ✅ *committed* | P-F 4 · P-T 3 · P-0 2 · P-G 8 | **17** | Correct provenance and identity, tool ownership, safe model, one rasteriser, one set, one mutation path. Nothing new visible; six existing bugs gone |
| **B — Core** ✅ *committed* | P-1 4 · P-2 3 · P-J 4 · P-3 4 · P-4 3 | **18** | Visible ROIs, real geometry, ImageJ measurements in real units, Measure / Multi Measure / Multi Plot on a cancellable worker |
| **C — Operations & data** — *roadmap* | P-5 2 · P-S 2 · P-U 2.5 · P-6 4 | 10.5 | Set ops, multiple named sets, undo/recovery, ImageJ interop |
| **D — Reach** — *roadmap* | P-P 3 · P-7 3 · P-R 4 | 10 | Points, full ImageJ-shaped UX, ROI-aware processors |
| **P-F2** — *roadmap, new in r5* | central provenance hook + container ids | 5 | Ids that survive save/reload, so a reloaded result can reach `exact` (A-27). Split out of P-F because there is **no shared writer** — 21 independent `save()` implementations (F-30) |

**Committed scope (Q-13a): tracks A + B = 35 d.** Roadmap C + D + P-F2 = 25.5 d.
Estimates are relative weights, not a schedule.

Dependency DAG — corrected again in r5 (c-8: P-S was missing, P-P/P-5.5 edges
were not drawn):

```
P-0 ──┐
P-F ──┼──► P-G ──┬──► P-1 ──► P-2 ──┬──► P-J ──► P-3 ──► P-4 ──► P-R
P-T ──┘  (record │                  │                     │
          contract│                 │                     └──► P-3.7
          + ROISet│                 │
          + cmds) │                 └──────────────┐
                  │                                │
                  ├──► P-5 ─────────────► P-5.5 ◄──┘  (needs P-2 frames)
                  │       │
                  ├──► P-S│                          (multi-set UX)
                  │       │
                  └──► P-6┴──► P-U ──► P-7
                       │
                       └──► P-P ◄── P-T, P-2, P-3

P-F ──► P-F2   (roadmap: central provenance hook + container ids)
```

Edges made explicit:

* **P-J → P-3 → P-4** — P-3.5's caching is defined in terms of P-J's job model
  and `ImagePlaneSource` (F-26).
* **P-G carries minimal `ROISet` + commands**, so P-2.7 and P-3.4 have somewhere
  to put frames and configuration (F-24).
* **P-5.5** (*Rescale to target*) needs **P-2**'s frames as well as P-5.
* **P-S** depends only on P-G (it is multi-set UX over a `ROISet` that already
  exists).
* **P-U** depends on **P-5 and P-6** — the operations it wraps.
* **P-7** depends on **P-U**, because its shortcut set includes undo/redo.
* **P-P** depends on **P-T** (new drawing mode), **P-2** (capture), **P-3**
  (point measurements) and **P-6** (ImageJ point interop).

---

### P-F — Spatial provenance foundation *(4 d)*

**Goal:** an ROI can say *which plane of which data* it belongs to, and so can
every other calibrated consumer. Fixes D-16.

| Task | Description |
| --- | --- |
| **P-F.1** | `imcommon/algorithms/spatial_frame.py`: `AxisDescriptor`, `SpatialFrame` (incl. `identity_kind`), `TransformEdge`/`TransformRegistry`, and `compatibility(source, target, roi, *, transforms=None)` as the **decision tree** in A-13. Pure; no napari, no `improcess`. |
| **P-F.2** *(revised, F-25)* | **Four separate identities**: `ProcessingResult` gains `result_uid`, `dataset_uid`, `coordinate_space_uid`; `lineage` holds **source `result_uid`s and nothing else**. `DisplayLayerSpec` propagates them. All append-only with defaults. |
| **P-F.3** | Render path: `setImage`/`setDisplayLayers` write the ids, `plane_axes`, `view_mode` and the full axis descriptors into `layer.metadata` alongside what they already write (C-10). |
| **P-F.4** | Derived results (crop/substack/projection/segmentation) set `lineage` to their source's `result_uid`, and **keep `coordinate_space_uid` only when the output is genuinely pixel-aligned with the input** — a projection is, a resample is not. |
| **P-F.5** | `test_layering_boundaries.py` gains the A-26 assertion: nothing under `imcommon/algorithms/` imports `improcess` or `napari` (F-11). |
| **P-F.6** *(rewritten, F-30)* | **In-memory identity only.** `frame_uid` is **derived** by a stable hash (A-27); ids travel with the ROI set, **nothing is written to a result container**, honouring Q-14(b). A result loaded from disk gets deterministic content-digest ids and `identity_kind="derived"`, which the ladder caps at `pixel-compatible`. |
| ~~P-F.7~~ → **P-F2** | Container-persisted ids are **split out to the roadmap** (F-30): with 21 independent `save()` implementations and no shared writer, this needs one central provenance hook first and its own estimate (5 d). |

**Acceptance:** two unrelated results with identical shape and scale compare
`incompatible` — and the tree proves it, because the coordinate-space check runs
**before** any shape comparison (F-29); a crop reports its parent's `result_uid`
in lineage; renaming a result changes no id; a result reloaded from disk reports
`identity_kind="derived"` and tops out at `pixel-compatible`, never `exact`;
`compatibility(..., transforms=None)` never returns `registered`.

**Tests:** `test_spatial_frame.py` — decision-tree truth table incl.
XY-vs-XZ → `incompatible`, **unrelated-space-same-shape → `incompatible`**
(F-29), position-out-of-range → `incompatible`, same-space-different-scale →
`pixel-compatible`, transform-edge-present → `registered` **and refused in
Core**, `transforms=None` → never `registered`;
`test_frame_uid_is_deterministic_for_identical_planes` ← A-27;
`test_derived_identity_never_reports_exact`;
`test_imcommon_algorithms_do_not_import_improcess_or_napari`.

**Risk:** medium — reduced from r4 by dropping the container writes. Append-only
fields, and nothing depends on them in this phase.

---

### P-T — Viewer tool broker *(new, 3 d)*

**Goal:** one scratch layer with real ownership. Fixes D-04 **and** D-14, D-15.

| Task | Description |
| --- | --- |
| **P-T.1** *(revised, F-32)* | `ViewerToolService` (A-19): per-viewer registry, `ToolToken(owner_key, generation)`, owner-tagged scratch shapes, and a **token-gated API** — `set_mode/clear/shapes/add_callback/release` all take the token and raise `StaleToolToken` on a stale generation; `share()`; preemption signal. |
| **P-T.2** | `ViewerToolManager` becomes the service's internal implementation; `imcontrol`'s `ImageWidget:25` keeps working unchanged. |
| **P-T.3** | Migrate `ROIStatsWidget`, `ProfileWidget`, `ROIManagerWidget` onto the service; their unconditional `clear_shapes()` calls become `clear(token)`. **This is the commit that makes one shared layer safe** — it lands together with the single-layer change, never before it. |
| **P-T.4** | Lifecycle: `release(token)` disconnects every handler that token registered and is idempotent; panels call it on hide/destroy; the service drops owners whose widget is gone, via weak references (D-15, C-14). |
| **P-T.5** | `target_image_layer` + `sigTargetLayerChanged`; the three panels initialise from `active_image_layer()` once and then follow the service (F-16). |

**Acceptance:** exactly one `Viewer Tools` layer with all three panels open;
switching Profile's mode does **not** clear ROI stats' rectangle; closing a panel
leaves no connected handlers; clicking a non-image layer does not silently
change what a panel measures.

**Tests:** `test_viewer_tool_service.py` — one service per viewer;
`test_profile_clear_does_not_erase_roi_stats_shapes` ← D-14;
`test_release_disconnects_handlers` ← D-15;
`test_target_layer_survives_overlay_click` ← F-16.

**Risk:** medium — shared with imcontrol. The `ViewerToolManager` API is
preserved verbatim for existing callers.

---

### P-0 — Defect fixes (no model change) *(2 d)*

| Task | Description |
| --- | --- |
| **P-0.1** | Fix D-01 under Q-04(b). **The table always renders one row per model ROI**; visibility controls whether an ROI is measured/drawn, never whether its row exists. `rois(visible_only=False)` is additive. |
| **P-0.2** | Fix D-03: `ROIStatsRecord.error`; per-ROI `try`; NaN measurements + a *Note* column. |
| **P-0.4** | Fix D-05 (interim): rows keyed by name in `Qt.UserRole`; sorting enabled. Switches to `uid` in P-G.3. |
| **P-0.6** | Fix D-12: `_replaced()` (A-15) and route `add`-collision/`rename`/`duplicate`/`set_visible` through it. Behaviour-neutral; lands **before** any field is appended. |
| **P-0.7** | Fix D-13 (interim): enforce unique names at every ingestion path. |
| **P-0.5** | Housekeeping: drop the double `unique_name` in `add_current_rectangle`; align `_format_value` with `ResultsTableWidget.format_table_value`. |
| ~~P-0.3~~ | **Withdrawn (F-13)** — the single-layer fix moved to **P-T.3**, because sharing a layer without ownership creates D-14. |

**Acceptance:** unchecking *Visible* blanks that ROI's statistics and **leaves
its row and checkbox in place**; an oversized ROI set measures the in-bounds ROIs
and reports the rest individually; sorting then renaming targets the clicked row;
`from_dicts` with two `"cell"` entries yields two distinct names.

**Tests:** `test_hidden_roi_keeps_its_row_and_checkbox`,
`test_out_of_bounds_roi_reports_error_without_blanking_the_batch`,
`test_selection_after_sorting_targets_the_clicked_roi`,
`test_every_mutation_preserves_all_record_fields` (parametrised; fails if a
field is added without P-0.6),
`test_duplicate_names_are_rejected_on_every_ingestion_path`.

---

### P-G — Record contract, geometry, identity, set and commands *(8 d)*

**Goal:** the final record contract, one rasteriser, one set, one mutation path —
with every existing consumer migrated, before anything is built on top.
Resolves F-12 and F-24.

| Task | Description |
| --- | --- |
| **P-G.0** *(revised, F-12/F-24/F-28)* | **Land the complete contract in one commit** (A-02): every `ROIRecord` field, plus `ROIStyle` (`roi_style.py`), `MaskPayload` (**`roi_payload.py`** — F-28's circular-import fix), `MeasurementConfig`, and a **minimal `ROISet` with exactly one active set** (A-24). Nothing consumes them yet; `to_dict` omits defaults; `from_dict` tolerates any subset. Safe because P-0.6 landed first. |
| **P-G.1** | `imcommon/algorithms/roi_geometry.py`: `roi_mask_local` (A-04), `roi_mask` (A-17 exceptions only), `roi_outline` (multipart), `roi_from_vertices`, `roi_from_mask`, `roi_from_payload`, `encode_mask`/`decode_mask` with the adaptive codec, size-based selection and validation (A-03). Plus **`roi_capabilities(roi_type)`** (r-3) — the sanctioned way for I/O, applicability and overlay code to ask what a type supports, so the "one rasteriser" rule does not force type knowledge underground. |
| **P-G.2** | `ROIManagerModel.compute_stats` moves onto `roi_mask_local`, deleting the bespoke clipping at `roi_manager.py:179-221`. Characterisation test written first. |
| **P-G.3** | `uid` (**full uuid4**, r-1) + `revision` assignment on every ingestion path, with A-15's clarified semantics (r-2); rows, overlay features and selection key on `uid`. Supersedes P-0.4. |
| **P-G.4** | **Migrate `psf_resolution._extract_fit_points` and `colocalization._extract_pair` to `roi_mask_local`** (D-11), deleting both duplicated readers. Because P-G.0 landed the whole contract, the polygon/composite tests here are real (F-12). |
| **P-G.5** | `roi_frame_adapter.frame_from_layer()` (A-12) reading scale/translate/affine/metadata atomically; replaces `ROIManagerWidget._visible_pixel_scales()`. |
| **P-G.6** *(new, F-24)* | **Command framework + Add/Update/Delete/Rename**, each with `undo()` but **no stack and no UI** (A-23). Every model mutation goes through it from here on. |

**Acceptance:** `compute_stats` is bit-identical to P-0 for rectangle and
`pixels` ROIs; a polygon ROI measured through PSF and through Colocalization
uses the same pixel set the manager reports; an out-of-bounds ROI yields a
zero-sized local mask; an **empty** mask is a valid payload, not an error; a
malformed payload fails one ROI, not the batch; zlib output is bounded by
`nbytes`; **no call site outside `roi_geometry.py` independently rasterises or
extracts pixels by type** (r-3 — capability queries are fine); renaming an ROI
does **not** bump its `revision`, moving it does.

**Tests:** `test_roi_mask_local_is_bounding_box_sized`,
`test_mask_payload_round_trip_rle_bits_and_zlib`,
`test_codec_chosen_by_serialised_size` ← F-28,
`test_empty_mask_payload_is_valid` ← F-28,
`test_decompression_is_bounded_by_nbytes` ← F-28,
`test_roi_payload_module_has_no_package_imports` ← F-28,
`test_compute_stats_values_unchanged_from_1_0` (characterisation),
`test_psf_measures_a_polygon_roi_not_its_bounding_box` ← F-01,
`test_uid_is_a_uuid_and_preserved_except_on_duplicate` ← r-1/r-2,
`test_revision_bumps_only_for_measurement_affecting_changes` ← r-2,
`test_commands_round_trip_through_undo` ← F-24.

**Risk:** medium-high — the shared type plus two kernels. Characterisation tests
land in the same commits (R-8).

---

### P-1 — Show All / Labels *(4 d)*

| Task | Description |
| --- | --- |
| **P-1.1** | `NapariROIOverlay` (A-05): multipart rendering with `features={roi_uid, part_index}`, one label per ROI at `label_anchor`, zoom-compensated width, lazy/safe layer creation. |
| **P-1.2** | `"ROI Manager"` joins the `layer_selection` exclusion set. |
| **P-1.3** | *Show All* / *Labels* checkboxes; overlay refreshed on every model change; hidden ROIs not drawn (Q-04b). |
| **P-1.4** *(rewritten, F-31)* | **Hit-testing per A-05b**: `roi_hit_test()` in `roi_geometry` (pure, hole-aware), a bbox candidate filter, smallest-area tie-break over **all** hits, screen-space tolerance, modifier handling, and callback registration/removal through the broker's `ToolToken`. **`get_value` is not used** — measured evidence shows it returns only the topmost shape. Overlay vertices stay in target-data coordinates with the target's transform on the layer (the single coordinate representation). |
| **P-1.5** | Styling from `ROIStyle` (available since P-G.0) with a default colour cycle; group members share a colour. |
| **P-1.6** | **Read-only overlay** (F-16): `editable=False` (which forces `mode='pan_zoom'`), and a data-change handler that re-renders from the model if anything mutates it. |
| **P-1.7** *(revised, Q-11/r-6)* | Cap `napari>=0.7,<0.8` in `setup.cfg`, **and add the real-`napari.layers.Shapes` smoke test to the PR test lanes** in `ci.yml` — `text`, `features`, `editable`→`pan_zoom`, and `mouse_drag_callbacks` presence — not to the manual xvfb workflow. It is a headless subprocess test, so it needs no display. (`get_value` is deliberately **not** covered: A-05b no longer uses it.) |

**Acceptance:** an added rectangle stays visible; segmentation outlines land on
the regions; a composite with a hole renders both boundaries, selects as one ROI
from any part, and **does not select when the click is inside the hole**; two
overlapping ROIs select the smaller; the overlay lands correctly under a
non-identity affine; dragging an overlay vertex does not change the model;
closing the panel leaves no callback on the layer; deleting the overlay layer
and adding an ROI recreates it.

**Tests:** `test_overlay_renders_all_parts_of_a_composite` ← F-16,
`test_clicking_any_part_selects_one_roi`,
`test_roi_hit_test_returns_false_inside_a_hole` ← F-31 (pure, no viewer),
`test_all_overlapping_candidates_are_evaluated_not_just_topmost` ← F-31,
`test_overlapping_rois_select_the_smallest` ← F-31,
`test_overlay_vertices_are_target_data_coordinates_only` ← F-31,
`test_hit_test_callback_is_removed_on_release` ← F-31/D-15,
`test_overlay_is_not_editable_and_resyncs_on_tamper`,
`test_overlay_layer_is_not_an_image_source`, plus the real-napari smoke test.

---

### P-2 — Capture, segmentation payloads, positions *(3 d)*

| Task | Description |
| --- | --- |
| **P-2.3** | Fix D-07 **at the construction site**: `SegmentationRegion` carries a `MaskPayload` built directly from the `np.nonzero` arrays (`segmentation.py:528-531`); `pixels` becomes an on-demand property. **C-09**: `test_segmentation_processor.py:42` becomes an area assertion. Record before/after peak RSS. |
| **P-2.4** | Capture every shape type the broker offers — rectangle, ellipse, polygon, path/freehand, line — and add **all** shapes, not just the first (D-08). Points are **not** here (C-08, Q-10 → P-P). |
| **P-2.5** | Axis-labelled `position` (A-11) from `dims.axis_labels` + `current_step`; *Associate with slices* (Q-03a); *Remove Slice Info*. |
| **P-2.6** | `rectangle_roi_from_vertices` becomes a wrapper, signature unchanged. |
| **P-2.7** | Attach `frame_uid` at capture and register the `SpatialFrame` on the active set — **which exists since P-G.0** (F-24); surface `geometry_match` wherever an ROI is measured on another frame. |

**Blocked by:** nothing. *(Round 2 wrongly listed Q-10 here — F-12.)*

**Acceptance:** a polygon measures as a polygon in the manager, PSF and Coloc; a
1000×1000 region never materialises pixel tuples; 1.0 JSON still loads and
measures identically; three drawn shapes add three ROIs; an ROI captured at
`Z=12` reports `(("Z", 12),)` regardless of view-mode axis order.

**Tests:** `test_segmentation_region_never_materialises_pixel_tuples` ← F-06,
`test_segmentation_roi_measures_the_same_pixels_as_before`,
`test_all_drawn_shapes_are_added`,
`test_position_matches_by_label_under_a_transposed_view_mode`,
`test_frame_is_registered_once_per_set_not_per_roi` ← C-13.

---

### P-J — Background measurement jobs *(4 d)*

| Task | Description |
| --- | --- |
| **P-J.0** *(F-26, revised by F-33)* | **`ImagePlaneSource` protocol** + `ArrayPlaneSource` and `LazyPlaneSource` (A-22): pure, thread-safe, no napari/Qt references, `read_plane(position)` on axis-labelled positions, `mutation_token`, `close()`. Constructed on the GUI thread, used only off it. **Live arrays are snapshotted or the run is refused** — never measured opportunistically (F-33). |
| **P-J.1** | `MeasurementJob` immutable snapshot (carrying the source) + worker + cancellation token. |
| **P-J.2** | Progress reporting and cancel in the panel; chunked plane access via `LazyPlaneSource`. |
| **P-J.3** *(revised, F-33)* | Stale-result rejection: only the panel's current `job_id` may publish, and publication compares the job's **frozen `token_at_start`** against the source's token at that moment — a mismatch discards the results. |
| **P-J.4** | Cache key `(frame_uid, mutation_token, plane, roi_uid, roi_revision, measurement_config_revision)`; caching disabled when no meaningful token exists. |

**Ordering:** **P-J precedes P-3** (F-26) — P-3.5's debounce and caching are
defined in terms of this model.

**Acceptance:** cancelling a Multi Measure over 500 planes stops within one
plane; a slow job started first never overwrites a fast job started later;
**the worker touches no napari or Qt object** (asserted by constructing a job
from a fake layer and running it with the viewer absent); mutating an ROI's
geometry invalidates only that ROI's cached rows while renaming it invalidates
nothing (A-15); a live array mutated mid-run discards the job rather than
publishing mixed pixels.

**Tests:** `test_plane_source_has_no_napari_or_qt_references` ← F-26,
`test_worker_runs_without_a_viewer` ← F-26,
`test_live_array_mutation_discards_the_job` ← F-33,
`test_live_source_is_snapshotted_or_refused_never_measured_live` ← F-33,
`test_cache_key_uses_the_frozen_token` ← F-33,
`test_cancelled_job_publishes_nothing`, `test_stale_job_is_rejected`,
`test_cache_invalidates_on_geometry_but_not_on_rename` ← r-2,
`test_cache_disabled_without_a_mutation_token`.

---

### P-3 — Measurement set *(4 d)*

| Task | Description |
| --- | --- |
| **P-3.1** | `roi_measurements.py`: registry covering §3.4 (Appendix B). |
| **P-3.2** | `ROIStats`/`compute_roi_stats` reimplemented as a shim; `ROIStatsWidget` untouched. |
| **P-3.3** | Calibration per A-12 with A-16 naming: `_px`/`_cal` **pairs for every spatial measurement** (F-20), plus `spatial_unit`. |
| **P-3.4** | `ROIMeasurementsDialog`: grouped checkboxes, decimals, scientific notation, *Display label*, and ***Limit to threshold* with an explicit `(lo, hi)`** persisted with the config and optionally seeded from the Segmentation panel (F-20). |
| **P-3.5** | Debounce + the P-J cache (**P-J is a prerequisite**, F-26). |
| **P-3.6** | Anisotropy policy (Q-09a): `pixel_aspect`, >1% flag, documented perimeter definitions (A-18), **with pixel-domain and calibrated descriptors both exposed** rather than only one. |
| **P-3.7** *(new, F-20)* | **Line/profile measurement group**: `length_px`/`length_cal`, `angle`, along-line `mean`/`min`/`max`/`std`, with `line_width` (A-18) and the sampling API shared with `ProfileWidget`. |
| **P-3.8** *(Q-12 answered)* | `ddof=1` (sample standard deviation), **`n<2 → NaN`**, applied to both the registry and the `compute_roi_stats` shim, with a changelog entry recording the numeric change. |

**Acceptance:** circularity ≈1.0 for a disc, ≈0.785 for a square (±0.02, r=50);
`area_cal == area_px * 0.01` at 0.1 µm/px with `spatial_unit == "µm"`; an
uncalibrated result emits `_px` only; **a nm result and a µm result produce the
same column set**, distinguished by `spatial_unit`; every spatial measurement has
both members of its pair; toggling a measurement off removes it from table, CSV
and pushed rows.

**Tests:** per-group fixtures; `test_every_spatial_measurement_has_px_and_cal` ←
F-20; `test_shape_descriptors_for_disc_and_square`;
`test_perimeter_definition_vector_vs_mask`;
`test_line_measurements_respect_line_width`;
`test_threshold_limit_changes_area_fraction`;
`test_legacy_roi_stats_shim_matches_previous_values` (with the Q-12 note).

---

### P-4 — Measure, Multi Measure, Multi Plot *(3 d)*

| Task | Description |
| --- | --- |
| **P-4.1** | *Measure* (selected, else all) → `sigResultPushed`; register `roi-manager` in `_connectResultPusher` (D-10). |
| **P-4.2** | *Multi Measure* over the stack axis, long form (Q-02a), on P-J's worker. |
| **P-4.3** | *Multi Plot* → `sigPlotPushed`, reusing the Z-profile axis selection. |
| **P-4.4** | *Measure across results*, gated by the A-13 ladder and the Q-08 preflight. |
| **P-4.5** | *(revised during implementation)* **Keep** the panel-local CSV/JSON buttons and make them export what the panel holds. Retiring them, as this task originally said, would have removed the only way to get the wide-form Multi Measure table out — the Results dock is long-form by construction (Q-02a) — and the only export that does not require the dock to be open. What is retired is the **fixed statistics list** they used to write. |
| **P-4.6** | **Result-list lifecycle wiring** (C-11): subscribe follower panels exposing `setAvailableResults` to `sigResultsChanged`, mirroring `ResultProcessorController:14`. |
| **P-4.7** | Wide-form CSV export of a Multi Measure table (Q-02). |
| **P-4.8** *(new, A-20)* | Unit-aware plotting: a plot request over `*_cal` columns converts or refuses on mixed `spatial_unit`. |

**Acceptance:** *Measure* with no selection measures all; 3 planes × 2 ROIs → 6
rows, plane-major; loading a new reconstruction updates the across-results list
without reopening the panel; a `clippable` batch **stops and opens the preflight**
rather than measuring (Q-08); plotting mixed-unit rows converts or refuses;
every pushed row carries `frame_uid`, `roi_uid` and `roi_revision` (§4.8) and a
`geometry_match` that was actually computed.

**Tests:** `test_result_list_changes_reach_the_roi_manager` ← F-07,
`test_clippable_batch_requires_preflight_optin` ← Q-08,
`test_incompatible_plane_axes_are_never_measured` ← F-15,
`test_mixed_unit_plot_is_converted_or_refused` ← F-20.

---

### P-5 — Set and selection operations *(2 d)*

P-5.1 AND/OR/XOR/Split (union-of-bboxes frame) · P-5.2 Enlarge, Make Band,
To Bounding Box, Convex Hull, **Make Inverse (A-17 full-frame exception)**,
Translate · P-5.3 *deferred by Q-07*: Scale, Rotate, Interpolate ·
P-5.4 *More ▾* menu · **P-5.5** *Rescale ROIs to target frame…* — **depends on
P-2.7** (F-12), the explicit counterpart to A-13's no-auto-reprojection rule.

**Acceptance:** truth-table pixel counts for AND/OR/XOR; Split's union equals the
original; **no operation allocates an image-sized mask except those on the A-17
list**, and Make Inverse refuses above the memory cap with a clear message.

---

### P-S — *Multiple* named ROI sets *(2 d)*

`ROISet` itself and the single active set land in **P-G.0** (F-24). This phase
adds only what genuinely needs more than one: the set selector, duplicate,
**merge** with uid/name conflict resolution, and compare.

**Acceptance:** two sets captured on different results coexist without frame
confusion; merging reports and resolves conflicts rather than silently
overwriting; frames stay stored once per set.

---

### P-U — Undo stack, recovery, advanced commands *(2.5 d)*

The command *framework* and Add/Update/Delete/Rename land in **P-G.6** (F-24).
This phase adds the bounded undo **stack and UI**, `Ctrl+Z`/`Ctrl+Shift+Z` via
the shortcut catalog, Clear, batch property changes, Rescale/reframe,
**transactional import** with skip/rename/replace conflict resolution, and
crash-recovery autosave into the existing state store (C-13/A-25).

**Depends on P-5 and P-6** — the operations it wraps must exist first.

**Acceptance:** undo restores geometry, style and position exactly; a failed
import leaves the set untouched; the stack is bounded and **retains no image
data** (commands hold records, never pixels).

---

### P-6 — Interop and persistence *(4 d)*

| Task | Description |
| --- | --- |
| **P-6.1** | ImageJ `.roi`/`RoiSet.zip` via `roifile` (Q-01a), lazily imported; buttons disabled with a tooltip when absent. |
| **P-6.2** | **Versioned native envelope** (F-21) rooted at `ROISet`: `{format, schema_version, coordinate_convention, created_with, roi_set}`; migrations; **unknown version refused**; semantic validation; atomic write (temp + rename); **semantic** round-trip tests. |
| **P-6.3** | Persistence via the buffering adapter (A-09) under A-25's storage order; ROI-set restore requires a stored frame. |
| **P-6.4** | *Create Selection* (labels/mask → ROI set) and the inverse published as a `ProcessingResult` (C-01, A-17). |
| **P-6.6** *(new)* | **Interop hardening**: a checked-in corpus of small Fiji-produced `.roi`/`.zip` fixtures — holes, disconnected composites, subpixel vertices, C/Z/T positions, groups, Unicode names, malformed files. Import returns a **structured loss/warning report** when arbitrary axis labels or styles cannot map to ImageJ. |
| ~~P-6.5~~ | **Withdrawn (C-13)** — no automatic per-result sidecar; see A-25 and **Q-14**. |

**Acceptance:** every corpus fixture imports with correct geometry or a
*reported* loss; a set we write opens in Fiji with names and positions intact;
an unknown `schema_version` is refused, not guessed; a session that never opened
the panel does not erase its saved state; ROIs without a frame are not silently
restored.

---

### P-P — Points and multipoint *(3 d — Q-10c)*

Points layer + a new broker drawing mode (C-08); `point`/`multipoint` records;
count and coordinate measurements; nearest-neighbour distances; fiducial groups;
ImageJ point import/export.

**Depends on P-T** (the new drawing mode), **P-2** (capture), **P-3** (point
measurements) and **P-6** (ImageJ point interop). Round 3 called it
"independent of P-2 by design" — that was only true of the *record contract*,
which P-G.0 now settles anyway.

---

### P-7 — UX to match the feature set *(3 d)*

P-7.1 ImageJ-shaped button row + **More ▾** · P-7.2 extended selection +
*Deselect* · P-7.3 shortcuts via the config-driven catalog (`T`, `Del`, `F2`,
undo/redo) · P-7.4 *Update*, *Sort*, *Specify…*, *Properties…* (full `ROIStyle`,
F-22), *Options…* · P-7.5 filter + sorting · P-7.6 context menu.

**Depends on P-2** (F-12) for the record contract behind Update, Specify,
Properties and the slice options — **and on P-U**, because P-7.3's shortcut set
includes undo/redo.

---

### P-R — ROI-aware processor protocol *(4 d, new)*

A standard optional processor parameter selecting an ROI set/uid;
crop-to-bounds vs mask-outside behaviour; output frame/offset propagation
(P-F); provenance recording the source ROI set uid + revision; initial
integration with filters, segmentation, PSF and colocalization.

---

## 7. Test strategy

* **Pure-model first (A-01).** Frames, geometry, payload codecs, measurements,
  commands and jobs are numpy-in/numpy-out, tested without Qt or napari.
* **Characterisation before refactor.** P-G.2, P-G.4 and P-2.3 write their
  characterisation test in the same commit that changes the implementation.
* **Fake viewers for widget tests** (`test_z_profile_and_projection_range.py:116`),
  **plus one real-napari subprocess smoke test running on every PR** (P-1.7,
  r-6). `Shapes.text` / `features` / `editable` / callback attachment are
  exactly what a fake viewer cannot vouch for (F-23/F-27), and a napari release
  that broke them would otherwise only surface in the manually-triggered
  workflow. It constructs a bare `napari.layers.Shapes` in a subprocess with no
  viewer and no display, so it fits the existing `improcess` lane rather than
  needing xvfb.
* **Threading guard**: `test_worker_runs_without_a_viewer` (P-J) is the
  executable form of "the worker touches no napari or Qt object" (F-26).
* **Architectural guards**: `test_result_pipeline_generality.py` (C-01),
  `test_layering_boundaries.py` (C-02 **+ the new A-26 assertion**),
  `test_layer_selection.py`.
* **Interop corpus** (P-6.6) as checked-in fixtures.
* **Invocation** (C-07):

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -p no:napari imswitch/improcess/_test/test_roi_manager.py imswitch/improcess/_test/test_roi_geometry.py imswitch/improcess/_test/test_spatial_frame.py imswitch/improcess/_test/test_roi_measurements.py -q
```

Estimated new tests: **~110** across the full plan (~70 for tracks A+B).

---

## 8. Migration and backwards compatibility

| Surface | Guarantee |
| --- | --- |
| `ROIRecord(...)` positional construction | unchanged (C-03) |
| 1.0 JSON exports | load unchanged; `pixels` honoured indefinitely |
| `ROIManagerWidget.rois()` / `add_rois()` | unchanged; `rois()` gains `visible_only=` |
| `setRoiManagerWidget()` | unchanged (C-05) |
| `ROIStats` / `compute_roi_stats` | shim retained; **`ddof` may change per Q-12** — the one deliberate numeric break, changelogged |
| `rectangle_roi_from_vertices` | signature kept |
| `ViewerToolManager` public API | preserved verbatim for `imcontrol` (P-T.2) |
| `SegmentationRegion.pixels` | on-demand property; `test_segmentation_processor.py:42` must change (C-09) |
| `ProcessingResult` / `DisplayLayerSpec` | append-only `dataset_uid`/`lineage` (P-F.2) |
| PSF / Coloc processor params | unchanged; only internal extraction changes |
| `"roiManagerPanel": true` | unchanged; **no new config keys** |
| On-disk files | **none added automatically** (C-13/A-25) |

---

## 9. Performance

Target: **200 ROIs on a 2048×2048 result** stays interactive.

| Concern | Mitigation | Phase |
| --- | --- | --- |
| Image-sized masks per ROI | `roi_mask_local` + slices; full expansion only for A-17 operations | P-G.1 |
| Make Inverse / Create Mask | documented exception with a memory cap and preflight estimate | P-5.2 |
| Fragmented masks blowing up RLE | adaptive `bits` codec (A-03) | P-G.1 |
| Re-measure on every `current_step` | debounce; skip when hidden | P-3.5 |
| Hidden ROIs | not measured (Q-04b) | P-0.1 |
| Repeated measurement | cache keyed per A-22, invalidated by `roi_revision` and the source's `mutation_token` | P-J.4 |
| Long Multi Measure | worker + chunked planes + cancel | P-J |
| Overlay with hundreds of parts | decimate outlines to `max_vertices`; Labels fallback above ~300 ROIs | P-1.1 |
| Segmentation construction | payload at the source, no pixel tuples | P-2.3 |

Micro-benchmark (not a CI assertion) for 200 ROIs × 2048².

---

## 10. Documentation deliverables

Rewrite the ROI-manager section of [`docs/improcess.rst:622-629`](../../../docs/improcess.rst)
("stores multiple rectangular ROIs" is wrong after P-2); extend the
*Interacting/measuring tools* paragraph (`:243`) with the overlay, the tool
broker and measure-across-results; **document the coordinate/calibration
contract and the compatibility ladder explicitly** — it is the part a user can
get wrong silently; document the measurement conventions (A-18) including the
`ddof` decision; add the parity table (§3.7); note the optional `roifile`
dependency; changelog per phase. Durable docs go in the Sphinx `.rst` tree.

---

## 11. Risk register

| # | Risk | L | I | Mitigation |
| --- | --- | --- | --- | --- |
| R-1 | `ROIRecord` change breaks an imcontrol consumer | low | high | C-02/C-03, append-only, layering test |
| R-2 | Overlay coordinates wrong on scaled results | medium | high | explicit scale+translate tests |
| R-3 | Overlay slow with many parts | medium | medium | decimation + Labels fallback |
| R-4 | Shape descriptors disagree with ImageJ | medium | medium | Appendix B fixtures; A-18; Q-09; Q-12 |
| R-5 | `roifile` unavailable | medium | low | lazy import + disabled buttons |
| R-6 | P-7 scope creep | high | low | *Properties* bounded by `ROIStyle` |
| R-7 | Phases land out of order | medium | medium | §6.0 DAG; P-G.0 lands the whole contract |
| R-8 | P-G.2/P-G.4 change existing PSF/Coloc numbers | low | high | characterisation tests first |
| R-9 | Restored ROIs land on the wrong image | medium | high | frames required before restore (A-10, A-25) |
| R-10 | Cross-result measurement compares incomparable data | medium | high | ladder + refuse-by-default + preflight (A-13) |
| **R-11** | **P-F's `ProcessingResult` change ripples into every processor** | medium | high | append-only fields; nothing depends on them in P-F |
| **R-12** | **P-T regresses imcontrol's `ImageWidget`** | low | high | `ViewerToolManager` API preserved verbatim; imcontrol UI tests in the xvfb lane |
| **R-13** | **napari minor release breaks `features`/`text`/`editable`/callbacks** | medium | medium | Q-11(a) cap `<0.8` + the real-napari smoke test on **PR lanes** (P-1.7); surface reduced by dropping `get_value` (F-31) |
| **R-14** | **Scope**: a 24-day plan is now 60.5 d incl. P-F2 | high | medium | Q-13(a): tracks A+B (**35 d**) committed, C+D+P-F2 roadmap |
| **R-15** | **P-F and P-R change contracts every processor touches, so they will not review as ROI-manager PRs** | high | low | sequence them as their own PR series with their own reviewers; the ROI work depends only on the append-only fields |
| **R-16** | **State-store cost**: a large set doubles an already double-serialised shutdown write ([`WidgetStatePersistence.py:175`](../../../imswitch/imcommon/model/WidgetStatePersistence.py) + `:266`) | medium | medium | A-25 cap + Q-15 spill policy |
| ~~R-17~~ | ~~Overlay hit-testing depends on `Shapes.get_value` semantics~~ | — | — | **Retired in r5**: hit-testing is now pure geometry (`roi_hit_test`, A-05b), so nothing depends on `get_value` at all (F-31) |
| **R-18** | **Ids do not survive save/reload until P-F2**, so a reloaded result caps at `pixel-compatible` | high | low | deliberate and visible: `identity_kind="derived"` + `geometry_match` on every row (A-27). The ROI still measures the same pixels |

---

## 12. Out of scope

Flatten/burn-in overlays · 3D/volumetric ROIs (`ROIRecord` is 2D by
construction) · ROI-based tracking over time · converging PSF's pixel-size
spinbox (Q-06, separate task — though P-G.4 already unifies those panels'
*geometry*).

---

## 13. Review log

| Round | Date | Reviewer | Decisions | Follow-ups |
| --- | --- | --- | --- | --- |
| 1 | 2026-08-09 | Lenny | Q-01 a · Q-02 a · Q-03 a · Q-04 b · Q-05 a · Q-06 separate · Q-07 first group. 10 findings + 4 corrections, all accepted | Phase P-G added; D-11…D-13; §4.6 coordinate contract; A-04/A-07/A-09 rewritten; Q-08…Q-10 raised |
| 2 | 2026-08-09 | Lenny | Q-08 refuse+preflight · Q-09 a with both domains · Q-10 full support as P-P. 13 findings + 7 recommended phases + the minimum-files instruction, all accepted | P-F/P-T/P-J/P-U/P-S/P-P/P-R added; P-G.0 lands the whole contract; P-0.3 withdrawn into P-T; D-14…D-16; C-13…C-16; A-03/A-05/A-10/A-13 rewritten; A-16b…A-26 added; Q-11…Q-14 raised |
| 3 | 2026-08-09 | Lenny | Q-11 a (+PR smoke test) · Q-12 a (`ddof=1`, n<2→NaN) · Q-13 a (**A+B committed**) · Q-14 b. 5 blockers + 7 refinements + DAG corrections, all accepted | Minimal `ROISet`/`MeasurementConfig`/commands → P-G.0/P-G.6; identity split into `result_uid`/`dataset_uid`/`coordinate_space_uid` + `TransformEdge`, **`registered` deferred out of Core**; `ImagePlaneSource` + **P-J → P-3**; A-05b hit-testing contract; `MaskPayload` → `roi_payload.py`; uuid4; A-15 semantics table; A-25 size cap; estimates recomputed (53→56.5 with r3 additions); **Q-15 raised** |
| 4 | 2026-08-09 | Lenny | **Q-15 a** (one versioned, atomically written `improcess_roi_sets.json`; state store keeps a marker + checksum when spilled). 4 blockers + the P-J correction + 7 cleanups, all accepted | A-13 became a decision tree with the coordinate-space check first + `identity_kind` + a `TransformRegistry` argument; A-27 rewritten (`result_uid` = logical serialised result, `frame_uid` derived, **no container writes** — P-F.6 split, **P-F2** created); A-05b rewritten around a pure `roi_hit_test` after measuring that `get_value` returns only the topmost shape; A-19 token-gated; A-22 freezes the mutation token and snapshots-or-refuses live arrays; DAG, estimates (35 d committed) and C-13/A-25 corrected |
| 5 | | | | |

**No open questions.** Q-01…Q-15 all answered ✅

---

## 14. Readiness

| Phase | Status |
| --- | --- |
| **P-0** | ✅ **Implemented** — see §15 |
| **P-T** | ✅ **Implemented** — see §15 |
| **P-F** | ✅ **Implemented** — see §15 |
| **P-G** | ✅ **Implemented** — see §15 |
| **P-1** | ✅ **Implemented** — see §15.2 |
| **P-2, P-J, P-3, P-4** | ✅ **Implemented** — see §15.3, §15.4, §15.6, §15.7 |
| **P-5** | ✅ **Implemented** — see §15.9 (first roadmap phase delivered) |
| **P-S** | ✅ **Implemented** — see §15.10 |
| **P-6** | ✅ **Implemented** — see §15.11 (the Fiji fixture corpus needs a Fiji run) |
| **P-U, P-P, P-7, P-R, P-F2** | Roadmap (Q-13a) — specified, not committed |

Suggested first commits, in order: **P-0.6** (the mutation helper — the guard
that makes every later field addition safe), then P-0.1/0.2/0.4/0.5/0.7, then
P-T.

---

## 15. Implementation progress

### 15.1 Track A stabilization — status correction

An earlier revision of this section claimed Track A was complete. That was
wrong: the phases were *implemented*, not *stabilized*, and review found
correctness gaps in three of the four. The honest position:

| Phase | Status | Outstanding |
| --- | --- | --- |
| **P-0** defects | ✅ complete | — |
| **P-T** tool broker | ✅ stabilized | — (`target_image_layer` wired by P-1) |
| **P-F** provenance | ✅ stabilized | multi-input processors keep only their primary input's lineage (documented, not a defect) |
| **P-G** geometry/model | ✅ stabilized | line *sampling* remains unimplemented — line ROIs refuse to be measured as areas rather than returning a wrong number (P-3.7) |

Two of these were **P0**: unrelated same-shaped layers could share an inferred
coordinate space (so they compared `pixel-compatible` when the whole point of
the ladder was to reject them), and a line or unknown ROI type was silently
measured as a filled rectangle — a plausible, wrong number, which is the exact
failure mode D-11 existed to remove.

#### Stabilization pass — what was fixed

| # | Issue | Fix |
| --- | --- | --- |
| **P0-1** | Inferred coordinate space derived from layer name + shape, so two "Reconstruction" layers at 512×512 compared `pixel-compatible` | Unknown provenance now **mints** a per-layer identity, cached on the layer. Two unknown layers are `incompatible`; the same layer keeps its identity. The View-only content fingerprint (a sampled sum that also materialised lazy data) is replaced by a **source** identity — path + size + mtime — and pathless data gets minted ids rather than an invented equivalence |
| **P0-2** | `roi_mask_local` filled the box for lines, polylines, paths, unknown types and rotated rectangles; clipped ellipses were rebuilt from clipped bounds | New `UnsupportedROIGeometry`, raised per ROI (so the batch survives and the row says why). Vertices now win over the type name, so a rotated rectangle measures rotated. Ellipses are built over their own bounds and then cropped |
| **P1-3** | Affine hand-composed (missing rotate/shear); capture divided by `scale`, ignoring translate | Both sides go through napari's public `data_to_world` / `world_to_data`. `_visible_pixel_scales` is gone from both panels |
| **P1-4** | Provenance covered 3 processors; display-layer components minted fresh ids | `Processor.preserves_grid` + central `attach_provenance` at the controller call sites, so a processor only declares intent; declared on 11 same-grid processors. `DisplayLayerProcessingResult.from_spec` inherits its parent (with a per-spec grid override). Projection's `Auto` on 2D no longer claims a shared grid |
| **P1-5** | Widget bypassed `CommandLog`; rows keyed by name; `update()` could rewrite a uid or collide | Add / Rename / Delete / Duplicate run through the log; rows key on **uid**; `update()` refuses identity changes and name collisions; `get_by_uid` added |
| **P1-6** | Ownership by mutable indices; `dims` callbacks unreleased; a preempted token could retake the tool | Ownership keyed by shape geometry (stable when the user deletes a shape in napari); `on_viewer_event` registers viewer callbacks for release; `set_mode` requires being the active owner |
| **P2-7** | Per-ROI full-image `float64` conversion undid the local-mask design | Slice → mask → cast. Colocalization casts slices; PSF batch converts once outside the loop |
| **P2-8** | `nbytes` ignored, negative runs accepted, no zlib EOF/trailing check, codec chosen by estimate | All validated and typed; codec chosen on **measured** serialised size |

The carried item — `target_image_layer` having no consumers — is closed by
**P-1** below: the panel now follows `sigTargetLayerChanged` and aligns the
overlay to it.

### 15.2 P-1 — Show All / Labels ✅

D-02 is closed: the committed ROI set is drawn in the viewer.

| Task | Outcome |
| --- | --- |
| P-1.1 | `NapariROISetOverlay` in `naparitools.py` (C-01: in `imcommon`, so the `add_*` guard stays meaningful). Multipart rendering, per-part `features={roi_uid, part_index}`, one label per ROI on its largest part, zoom-compensated edge width, lazy/safe layer creation |
| P-1.2 | `"ROI Manager"` joins `ANNOTATION_LAYER_NAMES` in `layer_selection`, so the overlay can never be measured as an image |
| P-1.3 | *Show All* / *Labels* checkboxes; the overlay redraws on every model change; hidden ROIs are not drawn |
| P-1.4 | Hit testing is **ours** (A-05b): bbox candidates → `roi_hit_test` → smallest-area tie-break. `get_value` is not used. Click selects the row; row selection highlights the shape |
| P-1.5 | `ROIStyle` overrides, otherwise a colour cycle; group members share a colour |
| P-1.6 | `editable=False` (which also forces `pan_zoom`), plus a data-change handler that re-renders from the records if anything mutates the layer |
| P-1.7 | `setup.cfg` capped to `napari>=0.7.0,<0.8`; `test_napari_shapes_contract.py` exercises a **real** `napari.layers.Shapes` in a subprocess on the ordinary PR lane — `features`, `text`, `editable`→`pan_zoom`, mouse callbacks, per-shape `edge_color` |

Two things the implementation forced that the plan had not anticipated:

* **Grab tolerance must not close holes.** The overlay derives its tolerance
  from zoom, and at 2 px a click in the middle of a small annulus was landing
  within grab distance of the hole's rim, so the hole "selected" the ring.
  `roi_hit_test` now distinguishes *inside a hole* from *just outside the
  shape* (via a filled-mask comparison) and refuses the former regardless of
  tolerance.
* **The name `NapariROIOverlay` was already taken** by imcontrol's single
  detector-ROI rectangle. Defining a second class under that name shadowed it
  and broke 28 imcontrol tests. Renamed to `NapariROISetOverlay`, with a guard
  test asserting the two remain distinct.

### 15.3 P-2 — Capture, segmentation payloads, positions ✅

D-07 and D-08 are closed.

| Task | Outcome |
| --- | --- |
| P-2.3 | `SegmentationRegion` encodes a `MaskPayload` **at the construction site** — built straight from the `np.nonzero` arrays, so a megapixel region never becomes a million tuples. `pixels` survives as an on-demand property for callers written against the old field. Per C-09, `test_segmentation_processor.py` now asserts extent through the shared rasteriser instead of a tuple count |
| P-2.4 | `roi_from_shape` captures rectangle / ellipse / polygon / freehand / line, and the panel adds **every** shape it owns rather than the first. Points are refused with a clear error (C-08 — a Shapes layer has no point type) |
| P-2.5 | Axis-labelled `position` from `plane_position()`, behind an *Associate with slices* checkbox (off by default, as in ImageJ), plus *Remove Slice Info* |
| P-2.6 | `rectangle_roi_from_vertices` keeps its signature and delegates to `roi_from_shape` |
| P-2.7 | `frame_uid` attached at capture from `frame_from_layer()` |

Two decisions worth recording:

* **An axis-aligned rectangle stores only its bounds; everything else keeps
  its vertices.** That is what makes a *rotated* rectangle measure as the
  rotated shape — dropping its vertices would silently turn it into the
  axis-aligned box that encloses it.
* **Segmentation output keeps `roi_type="mask"`.** `"composite"` is the 2.0
  vocabulary and both rasterise identically, but renaming would change
  serialised records for no behavioural gain, so the existing name stays and
  the two are treated as synonyms.

### 15.4 P-J — Background measurement jobs ✅

`analysis/roi_jobs.py`, pure and importable without Qt or napari (asserted).

| Task | Outcome |
| --- | --- |
| P-J.0 | `ImagePlaneSource` protocol + `ArrayPlaneSource` / `LazyPlaneSource`. Planes are indexed **by axis label**, never positionally. A live array is snapshotted at construction (`live=True`) or the run is refused (`refuse_live=True`) — never measured opportunistically |
| P-J.1 | `MeasurementJob` freezes `token_at_start` at creation; the measure function is injected, so the module runs a measurement without knowing what one is |
| P-J.2 | `CancellationToken` checked per ROI and per plane; progress reported per step; `LazyPlaneSource` reads one plane at a time (asserted: a 6-plane run does 6 reads) |
| P-J.3 | Supersession — a slow job started first cannot publish over a quick one started after — plus a mid-run token change marking results stale |
| P-J.4 | `MeasurementCache` keyed `(frame_uid, token_at_start, plane, roi_uid, roi_revision, config_revision)`. Moving an ROI invalidates it; **renaming one does not**. No token ⇒ no caching |

`MeasurementRunner` gained `wait()` and `shutdown()`, so a measurement thread
cannot outlive the panel that started it.

**This phase surfaced a real defect in P-T.** Adding the module made the suite
segfault in an unrelated Qt test, four runs in six. The cause was not the job
code: `ViewerToolService` held **strong references to panels' bound methods**,
so a panel destroyed without calling `release` stayed reachable and kept being
notified — and calling a Qt widget whose C++ side has been deleted is a
segfault, not an exception. Handlers are now held through `weakref.WeakMethod`
and dropped when their panel goes. Six consecutive clean runs afterwards, with
two regression tests (`test_a_destroyed_panels_handler_is_not_called`,
`test_the_broker_does_not_keep_a_panel_alive`).

The threading contract is exercised in a subprocess, for the same reason the
napari Shapes contract is: real OS threads inside a suite holding Qt, napari
and vispy destabilise teardown, and isolating them proves the same behaviour
without that cost.

### 15.5 Round-7 review — gaps closed in P-1, P-2, P-J and P-T

A review of the delivered phases found eleven gaps. One item on the list —
"P-3.1–P-3.8 are absent" — is not a defect: **P-3 has not been started**, and
`ddof=1` belongs to it. Everything else was a genuine gap in work called
complete, and all of it is now fixed.

| Gap | Fix |
| --- | --- |
| **Mutation tokens were not mutation tokens** — derived from buffer address, shape and dtype, all of which survive an in-place write, so stale rows could be served as current | Sources no longer invent tokens. The caller supplies one from something that tracks change (result uid + revision); empty means unknown, which disables caching and forces snapshot-or-refuse. A snapshot may carry one, because nothing can write to it |
| **Invalid positions silently measured another plane** — unknown labels ignored, indices clamped, unpinned axes resolved to zero | `PositionError`. An unknown label, an out-of-range index, or an unpinned non-plane axis is refused, matching what A-13 already said about incompatible positions |
| **No active `ROISet`** | The panel owns one; capture calls `with_frame()`, so frames are stored once per set and an ROI's `frame_uid` resolves |
| **`target_image_layer` never assigned** | The panel seeds the broker once and reads the target from it thereafter, so clicking the overlay cannot redirect measurement |
| **`MeasurementRunner` not Qt-ready** | `on_done` is now strictly the *publish* hook — a cancelled or superseded run goes to `on_discarded`; every worker is tracked (not just the newest), so `shutdown()` joins all of them; the worker-thread contract is documented on `submit` and asserted |
| **A run cancelled during its last measurement reported itself complete** | Found by a new test: the token is checked once more before declaring success |
| **The single-shape rule contradicted "capture every drawn shape"** | Opt-in `set_multi_shape()`; the ROI manager keeps several, Profile and ROI statistics keep the one-region limit. `test_all_drawn_shapes_are_added` now exists at the widget level |
| **Ownership keyed by a geometry hash** — two panels drawing the same rectangle collided | Per-shape ids that disambiguate duplicates, so identical shapes stay distinct |
| **Multipart labels mismatched the shape count** | napari's text is per-shape: the name sits on the ROI's largest part and other parts carry an empty string, asserted against the shape count |
| **P-1.5 implemented only `stroke_color`** | Full `ROIStyle` — stroke colour and **per-shape** width, fill colour/opacity, label colour/visibility — plus `ROISet.default_style` |
| **Overlay lifecycle incomplete** | `remove()` disconnects the zoom handler, the layer data handler and the click handler |
| **Overlay fixed at `ndim=2` copied an nD transform, failures swallowed** | Only the displayed plane's components are copied, rotation/shear via the affine's 2×2 block, and failures are recorded in `overlay.failures` instead of ignored |
| **Visibility, Clear and Remove Slice Info bypassed the command log** | `SetVisible`, `ClearROIs`, `RemoveSliceInfo` commands, all undoable |

Track B continued with **P-3** and **P-4**, both now complete (§15.6, §15.7).
Of the roadmap, **P-5** (§15.9), **P-S** (§15.10) and **P-6** (§15.11) are
done too; P-U, P-P, P-7, P-R and P-F2 have not started.

### 15.6 P-3 — The measurement set ✅

`analysis/roi_measurements.py` is a registry: 54 measurements across Basic (10),
Intensity (6), Position (16), Shape (19) and Line (3), ten on by default. A
measurement declares its id, label, group, domain (`pixel` or `calibrated`),
unit kind and an optional note; `measure()` computes the selected ones from a
`MeasurementContext` (local mask, local image, ROI, scales, unit, plane,
threshold, geometry match). Nothing else in the codebase knows what the set of
measurements is.

| Task | Outcome |
| --- | --- |
| P-3.1 | The registry, with a `@measurement` decorator. A measurement that fails takes **its own column** down, not the row: one unfittable ellipse must not cost the mean beside it |
| P-3.2 | `roi_stats.py` is now a shim over the registry, so the legacy eight statistics and the new columns cannot drift apart. **`ddof=1`, `n < 2 → NaN`** (Q-12a) — a behaviour change from the old `ddof=0`, noted below |
| P-3.3 | The panel's columns come from the selection, not a fixed list: `Visible / Name / Type`, then one column per selected measurement, then `Note`. `column_label()` is the **only** place a unit is rendered, so a cell is a bare number and the header says what it is in |
| P-3.4 | `ROIMeasurementsDialog` — ImageJ's *Set Measurements*, rendered from `groups()`. Decimals and scientific notation are display-only; the value behind a cell, which sorts and exports, is never rounded. Persisted on the set's `MeasurementConfig` |
| P-3.5 | A 120 ms coalescing timer, so dragging a dims slider costs one measurement pass rather than one per plane crossed; plus the P-J cache, consulted per ROI |
| P-3.6–3.8 | Always-on `spatial_unit` and `geometry_match` columns; a `shape_note` when pixels are anisotropic; analytic perimeters for rectangle and ellipse |

**Calibration.** `plane_scales(frame)` reads the two displayed axes **by
label**, not by position — the displayed pair is view-mode dependent, so taking
the last two descriptors would calibrate a YZ view with the Y and X scales. A
frame with no calibration reports `px` rather than claiming micrometres nobody
supplied, which keeps `area_cal` honestly equal to `area_px` instead of scaled
by an invented number.

**One shared cache key.** `roi_jobs.cache_key()` is now the single definition,
used by both the background runner and the panel's synchronous refresh. Two key
builders that agree by inspection are two key builders that will one day
disagree, and that failure mode is a stale number displayed as a fresh one.

**Where the mutation token comes from.** P-J established that a source must
never invent one. The renderer supplies it: `ReconstructionViewController`
mints a per-render-pass token into the layer's metadata, and **withholds it for
any result that has arrived as a live update** — a live array can be rewritten
in place between passes, so a token there would certify data that had changed.
No token simply switches caching off, which is the safe direction.

**Behaviour change to note in the changelog.** ROI standard deviation is now
the sample standard deviation (`ddof=1`), matching ImageJ; a single-pixel ROI
reports NaN rather than 0. A 5-pixel ROI that read 1.118 before reads 1.291
now.

*Tests:* `test_roi_measurements.py` (29) covers the registry — including the
plan's fixture criteria, disc circularity ≈ 1.0 and square ≈ π/4 —
`test_roi_panel_measurements.py` (17) covers the panel's columns, calibration,
precision, debounce and cache, and `test_roi_manager_p0.py` gained three
cache-key tests. Two tests on the controller pin the token contract.

### 15.7 P-4 — Measure, Multi Measure, Multi Plot ✅

| Task | Outcome |
| --- | --- |
| P-4.1 | *Measure* → `sigResultPushed`, and `roi-manager` registered in `_connectResultPusher` on both the startup and the runtime-dock path (D-10). No selection measures all, as in ImageJ; hidden ROIs are skipped; one unmeasurable ROI costs its own row |
| P-4.2 | *Multi Measure* over a chosen stack axis, long form (Q-02a), on P-J's runner. Progress bar and Cancel; the runner's callbacks reach the GUI through queued signals, which is the marshalling `submit()` documents as the caller's job |
| P-4.3 | *Multi Plot* → `sigPlotPushed`: one series per ROI against the stepped axis, y-axis labelled with the unit |
| P-4.4 | *Across Results*, gated by the A-13 ladder. `frame_from_result()` builds a frame for a result that was never displayed, so the verdict does not depend on what happens to be on screen |
| P-4.5 | The panel-local CSV export now writes what the panel actually holds — the registry columns, or the wide form after a Multi Measure — instead of the retired fixed statistics list |
| P-4.6 | A follower panel exposing `setAvailableResults` is subscribed to `sigResultsChanged` (C-11), so loading a reconstruction reaches a panel that is already open |
| P-4.7 | Wide form derived on export (`roi_report.wide_form`), never stored beside the long form |
| P-4.8 | `harmonize_units()` in the shared table-plot path: rows in different length units are converted to the smallest present (squared for areas), and a unit that cannot be converted is refused **by name** |

**Q-08, per row, not per batch.** `ROIPreflightDialog` lists every (result, ROI)
pair with its verdict and what would be lost. Rows that are `exact` or
`pixel-compatible` are pre-ticked; `clippable` is unticked and opt-in;
`incompatible` and `registered` are shown but cannot be ticked at all — hiding
them would leave the user wondering which ROIs went missing. The opt-in is
recorded on the row as `geometry_match`, so a clipped measurement says so in
the table it lands in.

**A row is not a number.** `analysis/roi_report.py` owns the published row
shape: identity columns first (`source`, `kind`, `roi`, `roi_uid`), then one
column per stepped axis, then the measurements. Measure, Multi Measure, Across
Results and the CSV export all go through it, so they cannot disagree about
what a row is.

*Tests:* `test_roi_report.py` (10) for the row, plot and wide-form shapes
without a viewer; `test_roi_panel_measure.py` (25) for the panel — including
the acceptance criteria (3 planes × 2 ROIs → 6 plane-major rows; a `clippable`
batch stops and asks; an `incompatible` pair is never measured even when
ticked), the worker-to-GUI marshalling, and that closing the panel stops its
measurement thread. `test_table_plots.py` gained six unit-rule tests and
`test_result_provider.py` two for the result-list wiring.

### 15.8 Round-8 review — correctness pass over P-3 and P-4

A static review of the delivered P-3/P-4 work found eight correctness problems
and a set of completion gaps. All are fixed; the two that were closest to
silently wrong are first.

| Finding | Fix |
| --- | --- |
| **Live measurements could publish mixed-time data.** A live result deliberately has no mutation token, and `_plane_source()` neither snapshotted nor refused it — so a Multi Measure could read plane 1 from before a live update and plane 40 from after it, and the end-of-run staleness check compared `"" == ""` and passed it. Exactly what A-22 exists to prevent | No token now *means* snapshot-or-refuse: the source is snapshotted (and carries the snapshot's own token, which is sound because nothing else can write to a copy), or refused above `MAX_SNAPSHOT_BYTES` with a message saying why |
| **The shared cache held two incompatible payloads.** The table's refresh cached bare measurement values; Multi Measure cached fully decorated rows — under the same keys, in the same cache. A cached row served to the table carried a `source` and an ROI name into the value dict; a cached value dict served to Multi Measure lost them. And because a rename deliberately does not bump the ROI's revision, a cached row kept the *old name* after a rename | `run_job` now takes `measure` (values, cached) and `decorate` (row, never cached) as separate injections. The cache holds only measurement values, so a rename is free *and* correct |
| **A worker exception stranded the panel busy.** `run_job` had no per-ROI guard and `MeasurementRunner._work` had no error path, so one raising ROI — every line ROI, among others — killed the thread with the other rows unpublished and the progress bar still showing | Per-ROI failures go to a new `on_error` and cost their own row only; `_work` is wrapped so exactly one of `on_done`/`on_discarded` always fires. `MeasurementResult` gained `failed`, kept apart from `cancelled` because reporting a crash as "cancelled" tells the user they did something they did not do |
| **P-3.7 was unreachable.** `measure_roi()` always called the area rasteriser, which refuses a line — so the implemented line length and angle could not be produced through Measure or Multi Measure at all | A line takes a sampling path: `imcommon/algorithms/line_sampling.py` is now the one sampler, used by the Profile panel *and* the measurements. Added `line_mean`/`line_min`/`line_max`/`line_std` and a configurable `line_width` on `MeasurementConfig`. Measurements declare `line_safe`; anything that is not returns NaN for a line, because a straight line's circularity was being reported as 0.0 |
| **Frame and slice semantics were enforced only in Across Results.** Measure and Multi Measure defaulted every row to `geometry_match="exact"` without comparing anything; Multi Measure applied a slice-bound ROI to every plane; Across Results validated `roi.position` in the preflight and then measured plane 0 regardless | The A-13 ladder runs per ROI on every path, with a new `unverified` verdict for an ROI whose capture frame the set does not know — "we did not check" must not read as "it matched". `roi_belongs_on_plane()` binds a positioned ROI to its slice, and a `measure` returning `None` is a skip rather than a failure. Across Results measures the ROI's own plane, the one the verdict was earned on |
| **Calibration was wrong for anisotropic pixels.** `major_cal`, `minor_cal`, both Feret lengths and the mask perimeter multiplied a pixel answer by the *mean* axis scale. That is not a length in any frame: for a 10×20 bar at 1.0/0.1 µm the long axis actually runs the other way in world units, which no scalar factor can express | Geometry is scaled **before** it is measured: one `_axis_lengths()` (verified to match scikit-image exactly on the pixel grid) and a `scaled=` Feret both work on scaled coordinates, and an anisotropic mask perimeter is re-measured on the scaled boundary. Added the missing `feret_x_cal`/`feret_y_cal`, and the pairing test's whitelist for them is gone |
| **`_cal` columns appeared without a calibration**, identical to the `_px` column beside them — a claim of calibration | `applicable_selection(selection, unit=…)` drops calibrated measurements on an uncalibrated frame, used for the columns and the measurement so the two agree |
| **`harmonize_units()` discarded `px` before checking for a conflict**, so a table holding px rows and µm rows plotted them on one axis | `px` is a unit like any other there, and is not convertible: a mixed table is refused by name |
| **The legacy statistics still drifted.** `roi_stats.py` used the registry, but `compute_stats(selection=None)` reached a second copy of the eight statistics in `roi_manager.py` that still used `ddof=0` | That copy is gone; the legacy path goes through `stats_from_values`, so there is one definition of standard deviation in the panel |
| **Rows did not meet §4.8.** `frame_uid` and `roi_revision` were missing | Both are identity columns now, and `label` (ImageJ's *Display label*) is an opt-in eleventh |

Completion gaps closed with them: the active `ROISet` now holds its ROIs
(`_set.rois` was always empty); the dialog gained *Display label*, line width
and threshold seeding from the Segmentation panel; an explicitly **empty**
selection stays empty instead of springing back to the defaults
(`MeasurementConfig.selected` is `None` when unconfigured and `()` when the
user unticked everything); Across Results reads planes lazily instead of
`np.asarray`-ing a whole result to take one slice out of it; the `ddof` change
is in the changelog under **Behaviour Changes**; and P-4.5's contradiction is
resolved in the task table — the panel-local exports are **kept**, because the
Results dock is long-form by construction and retiring them would have removed
the only way to export the wide form.

**Across Results stays synchronous, deliberately.** It reads one plane per
(ROI, result) rather than sweeping a stack, so the work is bounded by the
number of loaded results, and the reads are now lazy. It shows a progress bar
and a wait cursor. Moving it onto the worker would need a plane source that
fans out across results, which is a larger change than the responsiveness it
would buy — noted here rather than left as an unstated limitation.

### 15.9 P-5 — Set and selection operations ✅

`imcommon/algorithms/roi_ops.py`, pure and local by construction.

| Task | Outcome |
| --- | --- |
| P-5.1 | `combine(rois, op)` for AND / OR / XOR / SUBTRACT over the **union of the operands' boxes**, and `split()` into connected components. Combining ROIs from two different planes is refused: their pixel indices do not refer to the same grid, so the arithmetic would be on unrelated coordinates |
| P-5.2 | `enlarge`, `make_band`, `to_bounding_box`, `convex_hull`, `translate`, and `make_inverse` as the one A-17 exception |
| P-5.3 | Deferred, as Q-07 decided |
| P-5.4 | A *More* menu on the panel; every operation goes through the command log as one `ReplaceROIs`, so all of them are undoable |
| P-5.5 | `frame_mapping()` + `rescale_to_frame()`: the explicit counterpart to A-13's refusal to measure through a transform. Composed as `inv(target.affine) @ edge @ source.affine` — through world coordinates, the only place two pixel grids are comparable |

**Locality is asserted, not asserted-to.** `test_only_the_a17_operations_ever_build_an_image_sized_mask`
walks the module's AST and fails if any function other than `make_inverse`
calls `roi_mask`. The criterion in §9 is a property of the code now rather than
a claim about it.

**Enlarge grows by distance, not by dilation steps.** Iterated binary dilation
— the obvious implementation — grows in a diamond: enlarging by 2 reaches two
pixels sideways but only one diagonally, so a circle comes out a lozenge. It is
a Euclidean distance transform, which is both what the words mean and what
ImageJ does; corners come out rounded, correctly, because the pixel diagonally
two away from a corner is 2.83 pixels from the ROI.

**Rescaling is honest about what it costs.** A vector ROI maps exactly. A
rectangle whose mapping rotates or shears becomes the polygon it actually is,
rather than an axis-aligned box quietly larger than the shape. A rasterised ROI
is resampled and comes back a composite, because a mask has no sub-pixel truth
to recover and pretending otherwise would be the third way this plan has found
to report a bounding box as a measurement.

The panel's table is now **extended-selection**: a set operation cannot be
expressed on a table that only lets one row be chosen.

*Tests:* `test_roi_ops.py` (30) — the full boolean truth table, Split's union
equalling the original, the A-17 guard refusing with a size in MiB, and the
rescaling cases including a registered edge and a refused cross-plane mapping.
`test_roi_panel_measure.py` gained 9 for the menu, the command log and the
refusal messages.

### 15.10 P-S — Multiple named ROI sets ✅

The set selector, New / Duplicate / Rename / Delete, **Merge from…** and
**Compare with…**. `merge_sets()` and `compare_sets()` are pure functions on
`ROISet`; the panel holds a list of sets and `_set` is a property onto the
active one, so every existing `self._set = …` writes back into the collection
and there is no second copy to keep in step.

**Identity does the merging.** An ROI present in both sets under the same uid
*is* the same ROI: identical when its geometry matches, a conflict when it does
not — the same region edited two different ways. A different uid that happens
to share a name is only a name collision, resolved by renaming, because a name
is display text and was never the identity. Comparing by name would report a
rename as two unrelated ROIs, which is the opposite of what a comparison is
for.

**A conflict is the only case that can lose work**, so it is the only one that
asks. `skip` (the default) keeps this set's version, `replace` takes the
other's, `keep-both` admits the incoming one **under a fresh uid** — two
different regions cannot share a uid without one of them becoming unreachable.
The dialog appears only when there is a real conflict; a merge that finds none
just reports what it did. And a display-only difference — a changed colour — is
not a conflict: the comparison is over the measurement-affecting fields, so a
decision is never put in front of the user for nothing.

**Frames travel with the ROIs that point at them**, still stored once per set.
An incoming `frame_uid` that resolved to nothing would read as "no provenance"
rather than as the missing-data bug it is.

**Switching sets clears the undo log.** Commands hold ROI *names*, which mean
different things in different sets, so an undo after a switch would either fail
or — worse — succeed against the wrong ROI. A cross-set history is P-U's
problem, and pretending to have one here would be the expensive kind of wrong.

*Tests:* `test_roi_sets.py` (27) — the three conflict policies, name-vs-uid
collisions, the frame-carrying and frame-deduplication criteria, and the panel
side: two sets keeping their own frames and their own measurement
configuration, duplicate preserving ROI identity (without which comparing the
copy says nothing), and the undo log not reaching across a switch.

### 15.11 P-6 — Interop and persistence ✅

| Task | Outcome |
| --- | --- |
| P-6.1 | ImageJ `.roi` / `RoiSet.zip` through `roifile`, imported lazily. Declared as the `imagej` extra; without it the menu entries are **disabled with the reason on them** rather than absent, because a missing entry looks like the feature does not exist |
| P-6.2 | `imcommon/algorithms/roi_set_io.py`: the envelope, migrations hook, unknown-version refusal, semantic validation, atomic write |
| P-6.3 | `improcess/model/roi_persistence.py` + a controller-owned `_ROIManagerStateAdapter` (A-09), under A-25's storage order |
| P-6.4 | *Create Selection* (labels → ROIs) and *Create Mask* (ROIs → a `ProcessingResult`, the A-17 exception) |
| P-6.6 | A structured `InteropReport` on every conversion — see below |

**The version is refused, never guessed.** A file from a future schema is not
"mostly readable": loading it half-correctly puts ROIs at plausible-looking
wrong coordinates, which is worse than an error. The envelope also records the
*coordinate convention*, because pixel indices alone do not say whether `(0,0)`
is a corner or a centre — two programs disagreeing about that produce a
half-pixel offset nothing in the data reveals.

**The frame uid is recomputed on load, not read.** A stored uid that disagreed
with its own content would be the more trustworthy-looking of the two answers
and the wrong one (A-27).

**Interop reports what it could not carry.** Styles, arbitrary ROI properties,
non-C/Z/T position axes, and the parts of a disconnected composite beyond the
largest all come back as named losses rather than silence — a silent lossy
export is how someone discovers six months later that their groups never made
it. A loss is recorded once, not once per ROI.

**Two real bugs the round-trip tests caught**, both invisible without the
library present: `roiwrite` decides between a `.roi` and a zip by what it is
*given*, so passing a one-element list wrote a zip called `.roi` that Fiji then
refused; and ImageJ's type enum says `RECT`, so a map keyed on `"rectangle"`
silently fell through to the polygon default for every rectangle ever exported.

**Persistence has a cap, and the spill is one file.** Saving state serialises
the payload twice — once to check it is serialisable, then again to write it —
so a five-thousand-ROI set would make closing ImProcess visibly slow. Under the
cap everything stays in the state store; over it the sets spill to
`improcess_roi_sets.json` and the store keeps **only a marker and a checksum**.
Two copies in two places is how they come to disagree; an edited spill file is
refused rather than half-loaded, and a missing one names the sets it lost.

**Restoring is deliberately more forgiving than opening a file.** A *file* with
a dangling frame reference is malformed and refused whole; a *saved session*
that lost one frame must not cost the user the other ROIs at startup, so the
restore path drops the orphans and says how many. Writing the strict check
first made the tolerant path unreachable — the tests found it — and the
tolerance now lives in exactly one place.

**A panel can publish and follow at once.** Giving the ROI manager a
`sigResultProduced` for *Create Mask* rerouted it to the producing-panel branch
of the controller and silently cost it the follower wiring, so its
across-results list would have gone stale the moment it gained a publish path.
Producing and following are not alternatives.

**What is not here:** the P-6.6 corpus of *Fiji-produced* fixtures. Files
written by this code and read back prove the conversion is self-consistent,
which is not the same as proving it matches what Fiji writes. Generating
fixtures by hand would encode my own reading of the format as the reference —
the one thing a corpus exists to avoid. It needs a Fiji run.

*Tests:* `test_roi_set_io.py` (17), `test_roi_imagej.py` (19 + 1 skipped
without `roifile`), `test_roi_persistence.py` (17), plus P-6.4 coverage in
`test_roi_ops.py` and `test_roi_panel_measure.py`.






### P-0 — Defect fixes ✅ *(uncommitted)*

| Task | Outcome |
| --- | --- |
| P-0.6 | `replaced()` in `roi_manager.py`; `add`-collision, `rename`, `duplicate` and `set_visible` all route through it. Landed first, as planned |
| P-0.1 | `_populate_table` renders one row per **model** ROI; hidden ROIs keep their row and checkbox with blank statistics and a `hidden` note. `compute_stats` skips hidden ROIs (`measure_hidden=` opts back in); `rois(visible_only=False)` added without changing the default for the three consumers |
| P-0.2 | `ROIStatsRecord` gained `measured` / `error` / `note`; `compute_stats` catches per ROI and emits NaN statistics. `refresh_stats` no longer has a batch-wide `try`, and the table gained a **Note** column |
| P-0.4 | Rows carry the ROI key in `ROI_KEY_ROLE` on column 0; `_selected_roi()` resolves by key. Sorting enabled, with a `_TableItem` that sorts on a stored key so Area sorts 9 before 100 |
| P-0.5 | Double `unique_name` removed from `add_current_rectangle`; `_format_value` now delegates to `ResultsTableWidget.format_table_value` |
| P-0.7 | `from_dicts` **and** the constructor route through `add()`, so names are unique however the model is built |
| P-0.3 | Withdrawn as planned — shipped inside P-T |

Also added a **Type** column, and `to_row()` now carries `measured`/`note` so
exports say why a row is blank.

*Tests:* `test_roi_manager_p0.py` (14) + `test_roi_manager_widget_p0.py` (7).
The mutation-preservation test is parametrised over `dataclasses.fields`, so a
field added in P-G.0 is covered automatically.

### P-T — Viewer tool broker ✅ *(uncommitted)*

| Task | Outcome |
| --- | --- |
| P-T.1 | `imcommon/view/guitools/viewer_tools.py`: `ViewerToolService`, `ToolToken(owner_key, generation)`, `StaleToolToken`. Token-gated `set_mode`/`clear`/`shapes`/`add_callback`/`release`/`share` |
| P-T.2 | `ViewerToolManager._ensure_shapes_layer` **adopts** an existing `Viewer Tools` layer (D-04), and gained `enforce_single=` — see the note below |
| P-T.3 | ROI stats, Profile and ROI manager migrated; every `clear_shapes()` became `clear(token)` |
| P-T.4 | `release(token)` disconnects that owner's callbacks and drops its shapes; all three panels release in `closeEvent` (D-15) |
| P-T.5 | `target_image_layer` + `sigTargetLayerChanged` on the service |

**One design point worth flagging.** Sharing the layer was not sufficient on its
own: `ViewerToolManager`'s "keep only the last rectangle and line" rule is
*global*, so on a shared layer Profile drawing a rectangle would still delete
ROI statistics'. The rule is now opt-out — `enforce_single=True` remains the
default, so imcontrol's `ImageWidget` is untouched — and the service turns it
off and applies **the same limit per owner** instead. Without that, fixing D-04
would have made D-14 worse rather than better.

*Tests:* `test_viewer_tool_service.py` (12), incl. the D-14 regression
(`test_clearing_one_owner_keeps_another_owners_shapes`), per-owner limits, stale
tokens, preemption and callback teardown.

### P-T follow-up fixes ✅ *(review round 5)*

Four issues found on review of the first P-T cut, all fixed:

| Issue | Fix |
| --- | --- |
| `release()` deleted an owner's shapes, contradicting "a reopened panel reclaims its shapes" — and the test never did a real close/reopen | `release(token, *, discard_shapes=False)` keeps shapes by default; `test_closed_and_reopened_panel_reclaims_its_shapes` now releases and re-registers |
| Nothing consumed `sigToolPreempted`, so preempted panels kept reacting to other panels' drawing | Owner-scoped `on_shapes_changed(token, handler)`; only the owner holding the tool is called |
| The weak registry could not release a viewer — its value strongly owned the service, which owns the manager, which owns the viewer | The registry stores a **weakref to the service**; `test_registry_does_not_retain_the_viewer` asserts collection |
| All three panels acquired during construction, so startup panels preempted each other before any user action | `register()` (identity only) at construction; `acquire()` on user intent. The shapes layer is no longer created at startup either |

### P-F — Spatial provenance ✅ *(uncommitted)*

`imcommon/algorithms/spatial_frame.py`: `AxisDescriptor`, `SpatialFrame`,
`TransformEdge`/`TransformRegistry`, and `compatibility()` **as the decision
tree** — coordinate space checked before any shape comparison, so unrelated
same-shaped data is rejected (F-29). `identity_kind` caps inferred identities at
`pixel-compatible`. `ProcessingResult` gained `result_uid` / `dataset_uid` /
`coordinate_space_uid` / `lineage`, propagated onto layer metadata by the render
path, with `adopt_identity_from(source, same_grid=)` wired into projection,
segmentation and stack-subset. Loaded data gets deterministic content-digest ids
marked `derived` — **no result-container writes** (Q-14b honoured; P-F2 remains
roadmap).

One deviation worth flagging: `compatibility()` takes `positions=` rather than
an `ROIRecord`, so `spatial_frame` stays independent of the ROI type. It is
strictly better layering than the plan's signature and avoids a P-F→record
dependency.

*Tests:* `test_spatial_frame.py` (28), `test_result_identity.py` (10).

### P-G — Record contract, geometry, identity, set, commands ✅ *(uncommitted)*

* **P-G.0** — the whole 2.0 contract in one step: `roi_payload.py` (adaptive
  `MaskPayload`: `rle` / `bits` / `bits-zlib`, chosen by measured size, with
  validation and a bounded decompress), `roi_style.py`, the full `ROIRecord`
  field set, `roi_set.py` (`ROISet` + `MeasurementConfig`).
* **P-G.1** — `roi_geometry.py`: `roi_mask_local` (bounding-box sized),
  `roi_mask` (explicit full-frame), multipart `roi_outline`, pure
  `roi_hit_test` (holes fall through), `roi_capabilities`.
* **P-G.2/.4** — the ROI manager, **PSF resolution and colocalization** all
  read pixels through `roi_values`/`roi_mask_local`; both bespoke
  `pixels`-or-`bounds` readers deleted. D-11 closed.
* **P-G.3** — uuid4 identity assigned on every ingestion path; `revision`
  advances only for measurement-affecting changes.
* **P-G.5** — `roi_frame_adapter.frame_from_layer()`, the one napari-aware
  side, reading everything from a single layer.
* **P-G.6** — `roi_commands.py`: Add / Delete / Rename / Update with `undo()`
  and a bounded `CommandLog`. No UI yet (that is P-U), but every mutation has
  gone through one audited path from the start.

*Tests:* `test_roi_geometry.py` (28), `test_roi_commands.py` (11).

### Suite status

`imswitch/improcess/_test` — **1364 passed** (3 consecutive clean runs) (`-p no:napari`,
`QT_QPA_PLATFORM=offscreen`, `test_snouty.py` ignored as CI does).
`imswitch/imcontrol/_test/unit` + controller + no-hardware profile —
**2645 passed, 4 skipped** (re-validated after the `fb6efb91` merge). `test_layering_boundaries.py` (extended with the
A-26 guard, now enumerated from the package) and
`test_result_pipeline_generality.py` green; ruff clean.

**Track A stabilized. Next: P-1** (Show All / Labels overlay), then P-2, P-J,
P-3, P-4 to complete the committed scope.

---

## Appendix A — Defect anchors

| File | Anchor | Defect |
| --- | --- | --- |
| `ROIManagerWidget.py:118` | `clear_shapes()` after capture | D-02 |
| `ROIManagerWidget.py:226` | `compute_stats` without `visible_only` | D-01 |
| `ROIManagerWidget.py:237` | rows derived from records | D-01/F-03 |
| `ROIManagerWidget.py:293-311` | scale discarded; row-index keying | D-06/D-05 |
| `roi_manager.py:57,88,104,118` | field-by-field reconstruction | D-12 |
| `roi_manager.py:68-71` | `remove()` deletes every name match | D-13 |
| `roi_manager.py:138,179-198` | `visible_only=False`; raises on bad mask | D-01/D-03 |
| `roi_stats.py:43` | raises on empty clip | D-03 |
| `roi_stats.py:53` | `np.std` ddof=0 vs ImageJ n−1 | Q-12 |
| `psf_resolution.py:169-186` · `colocalization.py:150-171` | duplicated `pixels`-or-`bounds` readers | D-11 |
| `ProfileWidget.py:180,191,206` · `ROIStatsWidget.py:89,133` | unconditional `clear_shapes()`; first-match reads | **D-14** |
| `naparitools.py:1179-1182,1220-1221` | handlers connected, never disconnected | **D-15** |
| `naparitools.py:1209,1248-1261,1295-1303` | one layer per manager; last-shape-wins; no point mode | D-04/D-08/C-08 |
| `segmentation.py:528-531` | one tuple per pixel at construction | D-07 |
| `result.py:127-165` | no stable result identity | **D-16** |
| `ImProcessMainView.py:821` | pusher gate excludes `roi-manager` | D-10 |
| `ImProcessMainController.py:473` | follower lacks `sigResultsChanged` | C-11 |
| `ReconstructionViewController.py:112-117` | view-mode transposition | C-12 |
| `ReconstructionView.py:286-307` | where scale+unit+labels are written | C-10 |
| `layer_selection.py:44-53` | first-image fallback | F-16 |
| `ci.yml:31-56` · `setup.cfg:32` | no napari matrix; unbounded floor | C-06 |

---

## Appendix B — Measurement catalogue

Column ids never contain a unit (A-16). **Every spatial measurement has both a
`_px` and a `_cal` member** (F-20); `spatial_unit` carries the unit and the
header renders it. Conventions per A-18.

| id(s) | Label | Group | domain | default | Definition |
| --- | --- | --- | --- | --- | --- |
| `area_px` / `area_cal` | Area | Basic | both | ✅ | mask pixel count; × row_scale × col_scale |
| `finite_px` | Finite px | Basic | pixel | ✅ | non-NaN pixels (A-18) |
| `mean` · `std` · `median` · `min` · `max` · `sum` | Mean StdDev Median Min Max Sum | Basic | intensity | ✅ | `std` is the **sample** std (`ddof=1`, `n<2 → NaN`, Q-12a); `sum` = RawIntDen |
| `mode` | Mode | Basic | intensity | | most frequent value (binned for floats) |
| `centroid_r_px`/`_cal` · `centroid_c_px`/`_cal` | Y / X | Position | both | | unweighted mask centroid |
| `com_r_px`/`_cal` · `com_c_px`/`_cal` | YM / XM | Position | both | | intensity-weighted centroid |
| `bx_px`/`_cal` · `by_px`/`_cal` · `width_px`/`_cal` · `height_px`/`_cal` | Bounding rectangle | Position | both | | half-open bounds (A-18) |
| `perimeter_px`/`_cal` | Perim. | Shape | both | | **vector: exact polygon length; mask: `perimeter_crofton`** |
| `major_px`/`_cal` · `minor_px`/`_cal` · `angle` | Major Minor Angle | Shape | both / none | | second-moment ellipse; angle in the pixel frame |
| `circularity` · `aspect_ratio` · `roundness` · `solidity` | Circ. AR Round Solidity | Shape | none | | pixel-grid domain (Q-09a) |
| `feret_px`/`_cal` · `feret_min_px`/`_cal` · `feret_angle` · `feret_x_px`/`_cal` · `feret_y_px`/`_cal` | Feret group | Shape | both / none | | calipers over the convex hull |
| `pixel_aspect` | PixelAR | Shape | none | | `row_scale/col_scale`; anisotropy flag (A-18) |
| `int_den_px` / `int_den_cal` · `raw_int_den` | IntDen RawIntDen | Intensity | both / intensity | | `area_px × mean` and `area_cal × mean` *(r-4 — IntDen is area-dependent, so it takes a pair like every other spatial measurement)*; `raw_int_den` = `sum`, which is area-independent and stays single |
| `skewness` · `kurtosis` | Skew Kurt | Intensity | none | | third / fourth standardised moments |
| `area_fraction` | %Area | Intensity | none | | thresholded fraction; threshold per P-3.4 |
| `length_px`/`_cal` · `line_angle` · `line_mean` · `line_min` · `line_max` · `line_std` | Line group | Line | both / intensity | | **P-3.7**; `line_width` per A-18 |
| `position` | Position | Stack | index | | axis-labelled, `Z=12;T=3` (A-11) |
| `spatial_unit` · `geometry_match` · `frame_uid` | Unit Match Frame | Meta | none | ✅ | A-16 / A-13 / A-10 |
| `roi_type` · `group` · `source` · `uid` · `revision` | metadata | Meta | none | | from the record |
| `note` | Note | Meta | none | | per-ROI error text (D-03) |

---

## Appendix C — Glossary

* **ROI record / ROI set** — one `ROIRecord`; an `ROISet` (A-24) owns records,
  frames, default style and measurement config.
* **Composite ROI** — arbitrary pixel set (segmentation output or set-op
  result), stored as an adaptive `MaskPayload`.
* **`SpatialFrame`** — which plane of which data an ROI's pixel indices refer
  to: the four identities, plane axes, full axis descriptors, shape, affine,
  unit, lineage, `identity_kind` (A-10).
* **The four identities** — `dataset_uid` (acquisition), `result_uid` (logical
  serialised result), `coordinate_space_uid` (shared pixel grid — what `exact`
  compares), `frame_uid` (derived hash of the displayed plane) (A-10/A-27).
* **Compatibility** — the ordered decision tree in A-13, not a lookup table:
  plane axes → positions → coordinate space → derived-identity cap → shape and
  scale. Verdicts `exact` › `registered` › `pixel-compatible` › `clippable` ›
  `incompatible`.
* **`identity_kind`** — `"minted"` (ids created with the result) vs `"derived"`
  (reconstructed from a content digest); derived can never reach `exact` (A-27).
* **`roi_hit_test`** — the pure, hole-aware point-in-ROI test that selection is
  built on, because `Shapes.get_value` returns only the topmost shape (A-05b).
* **`ToolToken`** — `(owner_key, generation)`; every broker operation takes one,
  and a stale generation raises (A-19).
* **Local mask** — boolean mask over an ROI's bounding box plus the slices that
  place it. The canonical rasterisation (A-04); A-17 lists the exceptions.
* **Tool broker** — `ViewerToolService`, the per-viewer owner of the scratch
  layer, the active tool and the target image layer (A-19).
* **Managed overlay** — the read-only `"ROI Manager"` Shapes layer (A-05).
* **`ImagePlaneSource`** — the pure, thread-safe plane reader a `MeasurementJob`
  carries so the worker never touches a napari layer (A-22).
* **`mutation_token`** — data-identity + mutation counter used in the measurement
  cache key so live data cannot serve stale numbers (A-22).
