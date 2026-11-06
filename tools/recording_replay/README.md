# Recording replay tools

Replay a completed ImControl scan as a growing, synthetic time-lapse to test **ImProcess Session Monitor, Auto reconstruction, and live updates** without microscope hardware. Each timepoint repeats the source camera frames and retains acquisition metadata.

## Scripts

- `fake_zarr_timelapse.py` — creates a growing single-store Zarr time-lapse (`scanN` groups); accepts Zarr or HDF5 input.
- `fake_hdf5_timelapse.py` — creates either one HDF5 file containing `scanN` groups or a separate HDF5 file per timepoint; accepts HDF5 input.

Requires Python with `numpy`, `zarr` (Zarr replay), and `h5py` (HDF5 replay/input).

## Examples

From the repository root, with an **existing** watched directory:

```bash
python tools/recording_replay/fake_zarr_timelapse.py "source.hdf5" "watched/" --timepoints 5 --duration 10
python tools/recording_replay/fake_hdf5_timelapse.py "source.hdf5" "watched/" --mode multi-file --timepoints 5 --duration 10
python tools/recording_replay/fake_hdf5_timelapse.py "source.hdf5" "watched/" --mode single-file --timepoints 5 --duration 10
```

Open the watched directory as an ImProcess Session and enable **Monitor + Auto** *before* starting the replay. Use **Process while acquiring** to test frame-level Zarr streaming, or leave it off to test completed-timepoint Auto processing.

**Expected HDF5 behavior:** multi-file timepoints can be processed after each file is finalized; single-file HDF5 waits for the whole acquisition. For additional options (delays, detector, source timepoint), run either script with `--help`.

These are **manual integration-test utilities**, not acquisition writers intended for experimental data.
