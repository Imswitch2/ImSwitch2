# Audit 13 — Data integrity, formats & provenance

## Summary

- **Crash-safe architecture**: RecordingManager implements robust error handling with try/finally blocks, WriterThread clean teardown (finish/abort), and explicit file closing on both normal completion and crash paths. HDF5/Zarr files are properly flushed/closed through finalizeStream; aborted recordings delete partial files via abortStream (line 755-772).

- **Minimal OME-NGFF compliance gap**: Core recording layer (HDF5Storer/ZarrStorer) lacks OME-NGFF schema adoption—no multiscales, no coordinate transforms, no standardized units schema. Zarr datasets include basic 'axes' (['T','Y','X'] at line 274) and 'element_size_um' but not OME-0.4+ mandatory fields. Post-processing layer (improcess/WatcherFrameController) uses ome-zarr.writer for reconstructed data only.

- **Partial metadata coverage**: System captures detector metadata (model, pixelSizeUm, binning, ROI) via SharedAttributes→getHDF5Attributes flow, laser state (enabled, power, frequency, dutyCycle), and positioner coordinates per axis. Missing: exposure time, software version, calibration transforms, timestamp-per-frame, and workflow provenance parameters (tiling step_units, z_stack params).

- **Multi-dimensional data model incomplete**: HDF5 ScanLapse mode structures scans as scan{N}/detector groups (line 649-656) with auto-incremented scan numbers. Z-axis and channel dimensions are NOT self-describing in file metadata—workflows save raw TIFF stacks (z_stack.py:317) or individual NPY tiles (tiling workflow) with dimension semantics lost. No stitching metadata for tiled acquisitions.

- **Provenance loss in workflows**: Tiling (tiling.py), z_stack (z_stack.py:308-318), and target_timelapse workflows do not persist acquisition parameters (laser powers, step sizes, z-range, exposure) to data files. TargetAcquisitionResult dataclass (target_timelapse.py:95) holds target_id and positions in memory but these are not serialized with HDF5/Zarr output.

- **No atomic writes**: Streaming writes directly to final file paths without temp-file-rename pattern. Writing flag (HDF5Storer:694, ZarrStorer:275) marks datasets as incomplete but partial files remain if process crashes before finalizeStream. Lapse mode overwrites existing files in append mode ('a') risking corruption if prior recording was incomplete.

## Findings

### [P1] No OME-NGFF schema compliance in core recording layer

**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:173-337` (ZarrStorer), `RecordingManager.py:562-773` (HDF5Storer)

**Risk**: Data is not interoperable with OME-ecosystem tools (napari, FIJI/BioFormats 6.0+, QuPath, cellpose). Users cannot open ImSwitch Zarr stores in napari without custom readers. No coordinate transforms means stage positions and pixel calibrations are not machine-readable for automated downstream analysis. Missing multiscales prevents efficient visualization of large tiled datasets.

**Concrete direction**: 
1. **Adopt OME-Zarr 0.4 schema** in ZarrStorer.finalizeStream:
   - Write `.zattrs` with `{"multiscales": [{"version": "0.4", "axes": [...], "datasets": [...], "coordinateTransformations": [...]}]}`
   - Embed pixel-size calibration as `{"type": "scale", "scale": [1.0, pixel_size_um[0], pixel_size_um[1]]}`
   - Include stage-position offset transforms for tiled/multi-position acquisitions
2. **Map detector metadata** to OME-0.4 `omero` field: channel names, laser wavelengths, exposure times
3. **Reference implementation**: Study ome-zarr-py `write_multiscale` and adapt for streaming writes (already imported in WatcherFrameController:8-9, line-verified)
4. **HDF5 path**: Add OME-XML metadata string to root attrs per OME-TIFF spec for HDF5 compatibility

---

### [P1] Exposure time not captured in metadata

**Site**: `imswitch/imcontrol/controller/controllers/SettingsController.py:501-506` (detector metadata population); `RecordingManager.py:1434` (attrs passed to storer)

**Risk**: Exposure time is a critical reproducibility parameter—without it, quantitative fluorescence analysis (photobleaching correction, photon budget calculations) is impossible. Users cannot verify if acquisition settings match experimental protocols. Dynamic exposure changes during time-lapses are lost.

**Concrete direction**:
1. **Add exposure tracking** to SettingsController.updateSharedAttrs (line 501):
   ```python
   self.setSharedAttr(dName, 'Exposure_ms', dManager.getExposure())  # or detector.exposure
   ```
2. **Ensure detector managers** expose `.exposure` or `.getExposure()` method—audit DetectorManager base class
3. **Store per-frame** for time-varying exposure: extend dataset attrs with `exposure_ms_per_frame` array in finalizeStream if exposure changed during recording

---

### [P1] Missing software version and config provenance

**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:334, 378, 608` (timestamp attrs); no version tracking found

**Risk**: Cannot reproduce experiments if ImSwitch version, hardware config, or calibration coefficients change between acquisitions. Debug reports from users lack version context. Data from different setups/versions cannot be safely pooled.

**Concrete direction**:
1. **Add root-level attrs** in HDF5Storer/ZarrStorer openStream or finalizeStream:
   ```python
   root.attrs['imswitch_version'] = imswitch.__version__  # Import from package
   root.attrs['config_hash'] = hashlib.sha256(json.dumps(setup_config).encode()).hexdigest()
   root.attrs['acquisition_datetime'] = datetime.now().isoformat()
   ```
2. **Serialize setup configuration**: Dump detector/laser/positioner config (names, models, pixel sizes, wavelengths) as JSON string to `root.attrs['setup_config']`
3. **Calibration coefficients**: Add affine transforms, stage->pixel mappings, and galvo calibrations to detector/positioner metadata groups

---

### [P1] Workflow parameters not persisted with data

**Site**: 
- `imswitch/imcontrol/model/workflows/z_stack.py:308-318` (no param save)
- `imswitch/imcontrol/model/workflows/tiling.py:53-99` (TilingParams dataclass not serialized)
- `imswitch/imcontrol/model/workflows/target_timelapse.py:95-115` (TargetAcquisitionResult not saved)

**Risk**: Z-stack step size, tiling grid dimensions, laser powers, and target coordinates are lost—users cannot identify which acquisition used which protocol. Tiled datasets lack stitching metadata (tile positions, overlap, grid shape). Cannot reproduce or validate automated targeting workflows.

**Concrete direction**:
1. **Z-stack workflow** (_save at line 308): Write TIFF with OME-XML or ImageJ metadata including `z_step_um`, `z_start_um`, `z_end_um`, `laser_power_mw`, `exposure_us`
2. **Tiling workflow**: Add JSON sidecar file `{savename}_tiling_params.json` with TilingParams as dict + per-tile stage positions array
3. **Target timelapse**: Serialize TargetAcquisitionResult list to HDF5 group `targets/` with datasets for `target_id`, `stage_position`, `timestamps`
4. **RecordingManager integration**: Accept optional `workflow_params` dict in snap/startRecording and store to root attrs `workflow/` group

---

### [P2] Z and channel dimensions not self-describing in files

**Site**: `imswitch/imcontrol/model/workflows/z_stack.py:161-240` (acquisition loop); `RecordingManager.py:274` (axes=['T','Y','X'])

**Risk**: Z-stacks saved as TIFF or HDF5 lack axis labels—napari/FIJI default to interpreting 3D arrays as TYX not ZYX. Multi-channel recordings flatten channels into separate detector datasets instead of a single TCZYX array. Users must manually specify dimension order and spacing in analysis tools.

**Concrete direction**:
1. **Z-stack**: Set axes to `['Z','Y','X']` instead of default `['T','Y','X']` when `recMode == RecMode.ZStack` (add enum check in HDF5Storer/ZarrStorer)
2. **Multi-channel**: Add `singleMultiChannelDataset` mode (parallel to `singleMultiDetectorFile`) that writes 5D TCZYX array with channel names in attrs
3. **OME-Zarr axes**: Replace hardcoded `['T','Y','X']` with dynamic detection:
   ```python
   axes = []
   if is_timelapse: axes.append({'name': 't', 'type': 'time', 'unit': 'second'})
   if is_zstack: axes.append({'name': 'z', 'type': 'space', 'unit': 'micrometer'})
   if is_multichannel: axes.append({'name': 'c', 'type': 'channel'})
   axes += [{'name': 'y', 'type': 'space', 'unit': 'micrometer'}, {'name': 'x', 'type': 'space', 'unit': 'micrometer'}]
   ```

---

### [P2] Positioner coordinates saved in stage units, not microns

**Site**: `imswitch/imcontrol/controller/controllers/PositionerController.py:50, 202` (setSharedAttr with position values); no unit conversion found

**Risk**: Stage positions stored in arbitrary device units (encoder steps, DAC values) are not interpretable without calibration. Stitching algorithms and coordinate-based analysis require physical units. Multi-setup data pooling fails if units differ.

**Concrete direction**:
1. **Add unit metadata** to positioner attrs: `(_attrCategory, positionerName, axis, 'position_unit')` with value 'um' or 'steps'
2. **Convert to microns** in PositionerController.setSharedAttr if positioner manager exposes `units_per_um` calibration:
   ```python
   if hasattr(pManager, 'units_per_um'):
       position_um = position_steps / pManager.units_per_um[axis]
       self.setSharedAttr(positionerName, axis, 'position_um', position_um)
   ```
3. **Store both** raw and calibrated values in metadata for debug purposes

---

### [P2] Tiling workflow lacks stitching metadata in saved files

**Site**: `imswitch/imcontrol/model/workflows/tiling.py:250-350` (tile saving loop—line numbers approximate, file has ~500 lines)

**Risk**: Individual tile NPY files contain no coordinate information. Users must reverse-engineer grid layout from filenames and spiral_moves algorithm. StitchedImage object exists in memory but per-tile positions are not serialized. Automated stitching pipelines (ASHLAR, BigStitcher) cannot process output.

**Concrete direction**:
1. **Save tile positions** as JSON sidecar: `{savefolder}/tile_positions.json` with `[{"tile_index": i, "stage_xy_um": [x, y], "filename": "tile_{i}.npy"}]`
2. **HDF5 tiling mode**: Create group structure `tiles/tile_{i}/data` with attrs `stage_x_um`, `stage_y_um`, `tile_index`, `grid_row`, `grid_col`
3. **OME-Zarr plates**: Adopt plate/well/field layout for tiled acquisitions—each tile becomes a "field" with position transform
4. **Reference**: BIAS/Microvolution stitching metadata formats

---

### [P2] Per-frame timestamps not recorded

**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:1511` (lastFrameTime watchdog only); no frame timestamp array written

**Risk**: Time-lapse analysis assumes constant frame intervals but detector delays, buffer underruns, or triggering jitter violate this. Photophysics fitting (FCS, SPT) requires millisecond-accurate timestamps. Cannot correlate frames with external event logs (stage moves, laser pulses, temperature readings).

**Concrete direction**:
1. **Capture timestamps** in WriterThread._flush_batch (line 1231): Record `time.time()` or detector hardware timestamp per frame
2. **Store as dataset**: In finalizeStream, create `{detector}/timestamps` dataset with shape (N,) and attrs `unit='seconds'`, `reference='unix_epoch'`
3. **Alternative**: Add `frame_interval_ms` attr with median interval if hardware timestamps unavailable—at least documents nominal rate
4. **OME-NGFF future**: Timestamps can be stored in coordinateTransformations as per NGFF 0.5 draft

---

### [P3] Lapse mode file append risks corruption on incomplete prior recordings

**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:644` (mode='a' for lapse files); line 649-656 (scan{N} numbering)

**Risk**: If previous lapse recording crashed before clearing `writing=True` flag (line 694), appending creates file with mixed complete/incomplete scans. Auto-incremented scan numbers (line 650-655) are correct but no validation that scan{N-1} is finalized. ScanLapse workflows may silently accumulate garbage data.

**Concrete direction**:
1. **Validate prior scans** in openStream before appending: Check `scan{N-1}/writing` attrs and log warning if True
2. **Repair mode**: Offer `repair=True` option that truncates incomplete datasets to last known-good frame count (use HDF5 resize)
3. **Atomic lapse**: Write each scan to temp file `{name}_scan{N}.tmp.hdf5`, finalize, then merge into main file—prevents corruption spread
4. **Best practice**: Document lapse mode limitations in user guide and recommend disk-based ScanOnce with post-hoc concatenation for critical experiments

---

### [P3] Zarr DirectoryStore close() may not exist in older zarr-python

**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:183-186` (ZarrStorer._close_store)

**Risk**: `getattr(store, 'close', None)` defensive pattern suggests close() is optional. In zarr-python <2.12, DirectoryStore lacks close(). Missing close may leave file handles open on Windows or fail to sync metadata on network filesystems. Not crash-safety issue but resource leak.

**Concrete direction**:
1. **Explicit flush**: Call `root.store.flush()` if available before attempting close
2. **Document zarr version**: Add `zarr>=2.12` to requirements.txt with comment explaining close() requirement
3. **Fallback**: If close not available, log debug message and rely on Python GC—acceptable for local DirectoryStore but warn for network stores

---

### [P3] TIFF streaming batch writes may produce unsorted frame order

**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:816-920` (TiffStorer streaming—approximate line range)

**Risk**: If multi-detector batch flushing is asynchronous or detectors have different frame rates, per-detector TIFF stacks may have frames in acquisition order but not sorted by global timestamp. Minor issue for single-detector but affects multi-camera alignment.

**Concrete direction**:
1. **Review TiffStorer.writeFrames**: Confirm frames are appended in acquisition order (FIFO queue guarantees this at line 1509)
2. **Add frame index attrs**: Store `frame_indices` dataset parallel to timestamps for validation
3. **Accept as limitation**: Document that TIFF streaming is frame-order-preserving per detector but cross-detector sync requires external triggering

---

## Metadata completeness table

| Dimension / Field | Written? | Standard Format? | Location / Notes |
|-------------------|----------|------------------|------------------|
| **Detector** | | | |
| - Model | ✅ Yes | Custom attrs | SettingsController.py:503 → `Detector/{name}/Model` |
| - Pixel size (μm) | ✅ Yes | Partial | SettingsController.py:504 → `Detector/{name}/Pixel size`; Zarr: `element_size_um` (line 271-272). **Gap**: not OME-NGFF `scale` transform |
| - Binning | ✅ Yes | Custom attrs | SettingsController.py:505 |
| - ROI | ✅ Yes | Custom attrs | SettingsController.py:506 as `[x_start, y_start, width, height]` |
| - Exposure time | ❌ **No** | — | **P1 gap** |
| - Bit depth / dtype | ✅ Implicit | HDF5/Zarr dtype | Dataset dtype at creation (line 685) |
| **Laser** | | | |
| - Enabled state | ✅ Yes | Custom attrs | LaserController.py:62, 102 |
| - Power setpoint | ✅ Yes | Custom attrs | LaserController.py:63, 109 (`_valueAttr`) |
| - Wavelength | ❌ **No** | — | Stored in setup config but not per-recording |
| - Frequency (pulsed) | ✅ Yes | Custom attrs | LaserController.py:120 |
| - Duty cycle | ✅ Yes | Custom attrs | LaserController.py:126 |
| **Positioner** | | | |
| - Position per axis | ✅ Yes | Custom attrs | PositionerController.py:50, 202 → `Positioner/{name}/{axis}/Position` |
| - Units | ❌ **No** | — | **P2 gap**: values in device units, no μm conversion |
| - Speed | ✅ Partial | Custom attrs | PositionerController.py:52 (only if axis is 'Velocity') |
| **Axes & Dimensions** | | | |
| - Axes labels | ✅ Partial | Custom `axes` attr | Zarr: `['T','Y','X']` (line 274); TIFF: `'TYX'` or `'YX'` (line 794). **Gap**: not OME-NGFF `axes` schema with `type`, `unit` |
| - Z-axis label | ❌ **No** | — | **P2 gap**: Z-stacks saved as T dimension |
| - Channel dimension | ❌ **No** | — | Channels saved as separate datasets, not C axis |
| - Voxel size (XYZ) | ✅ Partial | Custom | Pixel size stored; Z-step not in metadata |
| **Time** | | | |
| - Acquisition timestamp | ✅ Yes | Unix epoch | `root.attrs['timestamp'] = time.time()` (line 334, 378, 608) |
| - Per-frame timestamps | ❌ **No** | — | **P2 gap** |
| - Frame interval | ❌ **No** | — | Must be inferred from frame count and duration |
| **Provenance** | | | |
| - Software version | ❌ **No** | — | **P1 gap**: Comment at line 480 shows intent (`@imswitch_version (future)`) but not implemented |
| - Setup config | ❌ **No** | — | **P1 gap** |
| - Workflow params | ❌ **No** | — | **P1 gap**: TilingParams, ZStackParams, target lists not saved |
| - Calibration transforms | ❌ **No** | — | **P1 gap** |
| **OME-NGFF (Zarr)** | | | |
| - `multiscales` schema | ❌ **No** | — | **P1 gap**: Core OME-NGFF requirement |
| - `coordinateTransformations` | ❌ **No** | — | **P1 gap** |
| - `omero` metadata | ❌ **No** | — | Channel names, colors, wavelengths missing |
| - Plate/well layout (tiling) | ❌ **No** | — | **P2 gap** for high-content screens |
| **OME-XML (HDF5/TIFF)** | | | |
| - OME-XML string | ❌ **No** | — | **P1 gap** for BioFormats compatibility |
| **Crash Safety** | | | |
| - `writing` flag | ✅ Yes | Custom | HDF5:694, Zarr:275—cleared in finalizeStream:727 |
| - Atomic writes (temp+rename) | ❌ **No** | — | **P3 gap**: writes directly to final path |
| - Abort cleanup | ✅ Yes | N/A | abortStream deletes partial files (line 755-772) |

**Legend**: ✅ Implemented | ❌ Missing | ⚠️ Partial/Non-standard

---

## Standards gap (distance to OME-NGFF / community interoperability)

### Current state vs. OME-NGFF 0.4 specification

| OME-NGFF 0.4 Requirement | ImSwitch Implementation | Gap Severity | Effort to Close |
|--------------------------|-------------------------|--------------|-----------------|
| **Multiscales array** | ❌ Single resolution only | **High** | Medium—requires pyramid generation (can defer to post-processing) or OME-Zarr writer integration |
| **axes schema** with name/type/unit | ⚠️ Simple list `['T','Y','X']` | **High** | Low—switch to `[{"name":"t","type":"time"},...]` dict format at line 274 |
| **coordinateTransformations** | ❌ Not present | **High** | Medium—compute pixel-size scale transform from `pixelSizeUm`, stage-offset translation for tiles |
| **Physical units** (μm, seconds) | ⚠️ `element_size_um` custom attr, no axis units | Medium | Low—add `unit` to axes and use NGFF-standard `scale` |
| **Channel metadata** via `omero` | ❌ Channels as separate datasets | Medium | High—requires data model refactor to TCZYX arrays OR use OME-Zarr `labels` group |
| **Plate/well/field** (HCS extension) | ❌ Not applicable | Low | High—only needed for tiling; alternative is BIAS metadata JSON |
| **Version field** `.zattrs["version"]` | ❌ Not present | Low | Trivial—add `root.attrs["version"] = "0.4"` |

### BioFormats / OME-TIFF compatibility

| OME-TIFF 6.0 Requirement | ImSwitch HDF5/TIFF | Gap Severity | Effort to Close |
|--------------------------|---------------------|--------------|-----------------|
| **OME-XML metadata block** | ❌ Not written | **Critical** | High—requires OME-XML generation library (python-bioformats or ome-types) |
| **DimensionOrder (XYCZT)** | ⚠️ TYX implicit | Medium | Low—set explicit order in OME-XML |
| **PhysicalSizeX/Y/Z** | ⚠️ In ImageJ metadata only | Medium | Low—migrate to OME-XML `Pixels` element |
| **Channel names & colors** | ❌ Detectors saved separately | Medium | Medium—consolidate or add OME-XML `Channel` elements |
| **TimeIncrement** | ❌ Not written | Low | Low—compute from timestamps if added per **P2** finding |

### Immediate actionable steps (priority order)

1. **[Week 1] Adopt OME-Zarr axes schema** (P1, Low effort):
   - Replace `attrs['axes'] = ['T','Y','X']` with OME-NGFF 0.4 dict format at `RecordingManager.py:274`
   - Add pixel-size `scale` transform: `{"coordinateTransformations": [{"type":"scale", "scale":[1.0, pixel_y, pixel_x]}]}`
   - Test with napari OME-Zarr plugin

2. **[Week 1] Add exposure time to metadata** (P1, Low effort):
   - Extend `SettingsController.updateSharedAttrs` to read detector exposure and store via `setSharedAttr`
   - Verify in test HDF5 file attrs

3. **[Week 2] Capture software version & config** (P1, Low effort):
   - Import `imswitch.__version__` and write to root attrs in `finalizeStream`
   - Serialize setup config JSON to attrs (detectors, lasers, positioners)

4. **[Week 2] Persist workflow parameters** (P1, Medium effort):
   - Add `workflow_params` dict argument to `RecordingManager.snap()` / `startRecording()`
   - Store to HDF5 group `workflow/` or Zarr `.zattrs["workflow"]`
   - Update tiling, z_stack, target_timelapse workflows to pass params dict

5. **[Month 2] Add per-frame timestamps** (P2, Medium effort):
   - Extend `WriterThread._flush_batch` to capture timestamps
   - Write as parallel dataset `{detector}/timestamps` in `finalizeStream`

6. **[Month 2] Implement OME-Zarr multiscales** (P1, Medium-High effort):
   - Integrate `ome-zarr.writer.write_multiscale` (already imported in WatcherFrameController, line 9)
   - Generate downsampled pyramids in `finalizeStream` or delegate to post-processing
   - Add `lazy=True` option to defer pyramid creation for real-time streaming

7. **[Month 3] OME-XML for HDF5/TIFF** (P1, High effort):
   - Add dependency on `ome-types` library
   - Generate OME-XML string from metadata in `TiffStorer.finalizeStream` and `HDF5Storer.finalizeStream`
   - Write to TIFF ImageDescription tag and HDF5 root attr `OME-XML`
   - Validate with BioFormats `showinf` tool

8. **[Month 3] Positioner unit conversion** (P2, Medium effort):
   - Audit PositionerManager classes for `units_per_um` attribute
   - Add calibration to config schema if missing
   - Convert stage positions to microns in `PositionerController.setSharedAttr`

### Community interoperability validation checklist

After implementing above steps, validate with:

- [ ] **napari**: Open Zarr stores with OME-Zarr plugin—verify axes, scale, channel names display
- [ ] **FIJI/BioFormats**: `showinf` on HDF5/TIFF files—confirm metadata parsed, dimensions correct
- [ ] **QuPath**: Import multi-channel acquisitions—verify pixel calibration and overlay alignment
- [ ] **Python ecosystem**: Load with `dask.array.from_zarr()` and check chunk layout efficiency
- [ ] **OMERO**: Import test dataset—confirm image pyramid and metadata indexing
- [ ] **BigStitcher** (FIJI): Tile stitching from tiling workflow output with new position metadata

### Long-term architectural recommendations

1. **Separate metadata layer**: Extract metadata schema to standalone `imswitch.metadata` module with JSON-schema validation—enables version migration and external tooling
2. **Streaming OME-Zarr writer**: Replace custom ZarrStorer with ome-zarr-py `write_image(..., storage_options={"chunks":...})` in streaming mode—inherits NGFF compliance and community maintenance
3. **HDF5→Zarr migration**: Deprecate HDF5Storer in favor of Zarr (better cloud support, NGFF alignment)—provide conversion tool for legacy data
4. **Provenance graph**: Implement W3C PROV-O or DataLad-style lineage tracking—record processing history (acquisition→reconstruction→analysis)
5. **Validation on write**: Add optional strict mode that validates OME-NGFF schema before finalizing—fails fast on non-compliant metadata

---

## Notes on crash safety and error handling

**Robust error handling** (no P1/P2 issues found):

- **Try-finally blocks**: RecordingWorker.run (line 1347-1353) ensures `stopAcquisition` even on crash; WriterThread.run (line 1167-1229) handles exceptions without deadlocking producer
- **File closing**: HDF5Storer.finalizeStream (line 714-753) closes files in try-except; abortStream (line 755-772) handles close() failures gracefully
- **Sentinel-based teardown**: WriterThread uses `None` sentinel (line 1194) to distinguish normal finish vs. abort—prevents data loss races
- **Stall detection**: Frame watchdog (line 1528-1547) detects stuck detectors and aborts cleanly—logs diagnostics for debug
- **Writer death detection**: `enqueue_frames` (line 1267-1285) checks `is_alive()` and surfaces exceptions instead of blocking forever on dead writer

**Minor refinement opportunities** (already P3 findings):

- Atomic writes via temp-file pattern would prevent partial files on OS-level crashes
- Lapse mode append validation would catch incomplete prior scans

**Overall assessment**: Crash safety is production-grade; main gaps are in metadata completeness and standards compliance, not file integrity.
