# Recording replay tools

Replay a completed ImControl scan as a growing, synthetic time-lapse to test **ImProcess Session Monitor, Auto reconstruction, and live updates** without microscope hardware. Each timepoint repeats the source camera frames and retains acquisition metadata.

## Scripts

- `fake_zarr_timelapse.py` — creates a growing single-store Zarr time-lapse (`scanN` groups); accepts Zarr or HDF5 input.
- `fake_hdf5_timelapse.py` — reproduces ImControl **disk HDF5 temporary-file publication** for single- or multi-file lapses; accepts HDF5 input. Standalone: does not import ImControl or require microscope hardware.

Requires Python with `numpy`, `zarr` (Zarr replay), and `h5py` (HDF5 replay/input).

## Examples

From the repository root, with an **existing** watched directory:

```bash
python tools/recording_replay/fake_zarr_timelapse.py "source.hdf5" "watched/" --timepoints 5 --duration 10
python tools/recording_replay/fake_hdf5_timelapse.py "source.hdf5" "watched/" --mode multi-file --timepoints 5 --duration 10
python tools/recording_replay/fake_hdf5_timelapse.py "source.hdf5" "watched/" --mode single-file --timepoints 5 --duration 10
```

Open the watched directory as an ImProcess Session and enable **Monitor + Auto** *before* starting the replay. Use **Process while acquiring** to test frame-level Zarr streaming, or leave it off to test completed-timepoint Auto processing.

**Expected HDF5 behavior (Patch 1):** each growing file is called `<final>.hdf5.partial`, with a sibling `<final>.hdf5.partial.claim` reservation. Once HDF5 has been closed and its post-close metadata finalized, the script publishes the normal `.hdf5` name without overwriting another file. Multi-file timepoints publish individually; single-file HDF5 stays hidden until the whole lapse ends. The working file is in the **same watched directory**, matching ImControl's new implementation. Existing Session discovery ignores `.partial` files; the *Acquiring* placeholder/UI and Auto-recipe capture require the separate Session Patch 2.

To simulate an early STOP after two completed timepoints of a planned five-point lapse, add `--stop-after 2`. Completed timepoints retain their original planned count (5); in single-file mode the two recorded `scanN` groups are published at STOP. For a diagnostic comparison, add `--direct-write` to restore the old unsafe immediately-visible `.hdf5` writer. The old experimental `--atomic-publish` flag remains accepted as a compatibility alias for the new **default** staged protocol; it no longer creates a separate staging directory.

If publication fails, `.partial` and `.claim` are intentionally left for recovery; do not delete them while a writer is active. For other options (delays, detector, source timepoint), run either script with `--help`.

Headless replay regression tests (no ImControl startup):

```bash
python -m pytest --confcutdir=tools/recording_replay tools/recording_replay/test_fake_hdf5_timelapse.py -v
```

These are **manual integration-test utilities**, not acquisition writers intended for experimental data.
