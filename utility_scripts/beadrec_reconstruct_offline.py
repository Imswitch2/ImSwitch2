#!/usr/bin/env python
"""Offline BeadRec reconstruction from an already-recorded stack.

Reproduces the BeadRec live reconstruction on a file that was saved by the
ImSwitch RecordingManager: every recorded 2D frame becomes one reconstruction
pixel via its *whole-frame* mean (i.e. no ROI), laid out into the scan grid.

The scan dimensions are read from the recording metadata. For TriggerScope
raster recordings they live under ``<detector>/metadata/ScanStage`` as
``axis_length`` and ``axis_step_size``; the per-axis pixel count is
``round(axis_length / axis_step_size)`` — exactly what the live BeadRec uses
(TriggerScopeRasterController.getBeadRecScanDims). If the metadata can't be
found, pass ``--dims NX NY`` explicitly.

Reconstruction matches ``bead_recognition.reconstruction_image``: the flat
buffer of length ``nx * ny`` is reshaped to ``(ny, nx)`` (row-major, y down).

Supports both HDF5 (.h5/.hdf5) and Zarr (.zarr) recordings.

Examples
--------
    python beadrec_reconstruct_offline.py rec_OrcaStraight.hdf5
    python beadrec_reconstruct_offline.py rec.hdf5 --detector OrcaStraight --show
    python beadrec_reconstruct_offline.py rec.hdf5 --dims 128 128 -o recon.tif
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

# Convenience defaults so the script can be run with no arguments. Override
# either on the command line (positional path / --detector) as needed.
DEFAULT_INPUT = r"E:\SnoutyData\2026-06-16\11h58m55s_rec_OrcaStraight.hdf5"
DEFAULT_DETECTOR = "OrcaStraight"


# --------------------------------------------------------------------------- #
# Loading the recorded stack + metadata (HDF5 or Zarr structured layout)
# --------------------------------------------------------------------------- #
def _load_hdf5(path, detector):
    import h5py

    f = h5py.File(path, "r")

    # The structured layout stores frames at "<detector>/data" (and
    # "scan{N}/<detector>/data" for lapse files). Collect every such dataset.
    data_paths = []

    def _visit(name, obj):
        if isinstance(obj, h5py.Dataset) and name.rsplit("/", 1)[-1] == "data":
            data_paths.append(name)

    f.visititems(_visit)
    if not data_paths:
        f.close()
        raise SystemExit(f"No '<detector>/data' dataset found in {path}")

    data_path = _pick_dataset(data_paths, detector)
    dataset = f[data_path]
    data = dataset[...]
    meta_group = dataset.parent.get("metadata")
    dims = _scan_dims_from_attrs(_collect_attr_groups_h5(meta_group))
    f.close()
    return data, dims, data_path


def _collect_attr_groups_h5(meta_group):
    """Return {groupName: {attr: value}} for every subgroup of metadata."""
    import h5py

    groups = {}
    if meta_group is None:
        return groups
    groups[""] = dict(meta_group.attrs)
    for key, sub in meta_group.items():
        if isinstance(sub, h5py.Group):
            groups[key] = dict(sub.attrs)
    return groups


def _load_zarr(path, detector):
    import zarr

    root = zarr.open(path, mode="r")
    data_paths = []

    def _walk(group, prefix=""):
        for key in group.keys():
            item = group[key]
            full = f"{prefix}{key}"
            if hasattr(item, "shape"):  # array
                if key == "data":
                    data_paths.append(full)
            else:  # group
                _walk(item, prefix=f"{full}/")

    _walk(root)
    if not data_paths:
        raise SystemExit(f"No '<detector>/data' array found in {path}")

    data_path = _pick_dataset(data_paths, detector)
    dataset = root[data_path]
    data = dataset[...]

    det_group_path = data_path.rsplit("/", 1)[0]
    det_group = root[det_group_path] if det_group_path else root
    meta = det_group["metadata"] if "metadata" in det_group else None
    dims = _scan_dims_from_attrs(_collect_attr_groups_zarr(meta))
    return data, dims, data_path


def _collect_attr_groups_zarr(meta_group):
    groups = {}
    if meta_group is None:
        return groups
    groups[""] = dict(meta_group.attrs)
    for key in meta_group.keys():
        sub = meta_group[key]
        if not hasattr(sub, "shape"):  # subgroup
            groups[key] = dict(sub.attrs)
    return groups


def _pick_dataset(data_paths, detector):
    if detector is not None:
        matches = [p for p in data_paths if detector in p]
        if not matches:
            raise SystemExit(
                f"Detector '{detector}' not found. Available data paths:\n  "
                + "\n  ".join(data_paths)
            )
        return matches[0]
    if len(data_paths) > 1:
        print(
            "Multiple detectors found; using the first. Use --detector to choose:\n  "
            + "\n  ".join(data_paths),
            file=sys.stderr,
        )
    return data_paths[0]


# --------------------------------------------------------------------------- #
# Scan-dimension discovery from metadata
# --------------------------------------------------------------------------- #
def _scan_dims_from_attrs(attr_groups):
    """Derive (nx, ny) from any metadata group exposing axis_length/step.

    Mirrors TriggerScopeRasterController.getBeadRecScanDims:
        n = round(axis_length / axis_step_size)  per axis.
    """
    for attrs in attr_groups.values():
        if "axis_length" in attrs and "axis_step_size" in attrs:
            length = np.atleast_1d(np.asarray(attrs["axis_length"], dtype=float))
            step = np.atleast_1d(np.asarray(attrs["axis_step_size"], dtype=float))
            nx = int(round(length[0] / step[0])) if step[0] else 0
            ny = (
                int(round(length[1] / step[1]))
                if len(step) > 1 and len(length) > 1 and step[1]
                else 0
            )
            if nx > 0 and ny > 0:
                return nx, ny
    return None


# --------------------------------------------------------------------------- #
# Reconstruction
# --------------------------------------------------------------------------- #
def reconstruct(data, dims):
    """Whole-frame mean per frame, laid into a (ny, nx) reconstruction image.

    `data` is the recorded stack (T, Y, X). Returns a float64 image shaped
    (ny, nx), matching bead_recognition.reconstruction_image orientation.
    """
    if data.ndim != 3:
        raise SystemExit(
            f"Expected a (T, Y, X) stack of 2D frames, got shape {data.shape}"
        )

    n_frames = data.shape[0]
    nx, ny = dims
    n_pixels = nx * ny

    # Whole-frame mean per frame, in float64 (never truncate integer frames).
    frame_means = data.reshape(n_frames, -1).mean(axis=1, dtype=np.float64)

    if n_frames == n_pixels:
        pixel_values = frame_means
    elif n_pixels and n_frames % n_pixels == 0:
        # More frames than pixels and an exact multiple: treat as
        # frames-per-pixel and average each consecutive block per pixel.
        fpp = n_frames // n_pixels
        print(
            f"{n_frames} frames for {n_pixels} pixels -> averaging "
            f"{fpp} frames/pixel.",
            file=sys.stderr,
        )
        pixel_values = frame_means.reshape(n_pixels, fpp).mean(axis=1)
    else:
        n = min(n_frames, n_pixels)
        print(
            f"WARNING: frame count {n_frames} != scan pixels {n_pixels} "
            f"({nx}x{ny}); using the first {n} frames and zero-filling the rest.",
            file=sys.stderr,
        )
        pixel_values = np.zeros(n_pixels, dtype=np.float64)
        pixel_values[:n] = frame_means[:n]

    return pixel_values.reshape(ny, nx)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", nargs="?", default=DEFAULT_INPUT,
                        help=f"Recorded .hdf5/.h5 or .zarr file (default: {DEFAULT_INPUT})")
    parser.add_argument("--detector", default=DEFAULT_DETECTOR,
                        help=f"Detector name to reconstruct (default: {DEFAULT_DETECTOR})")
    parser.add_argument("--dims", type=int, nargs=2, metavar=("NX", "NY"),
                        default=None,
                        help="Scan pixel dimensions, overriding the metadata")
    parser.add_argument("-o", "--output", default=None,
                        help="Output image path (.tif). Default: <input>_beadrec.tif")
    parser.add_argument("--show", action="store_true",
                        help="Preview the reconstruction with matplotlib")
    args = parser.parse_args(argv)

    path = args.input
    ext = os.path.splitext(path)[1].lower()
    if ext in (".h5", ".hdf5"):
        data, meta_dims, data_path = _load_hdf5(path, args.detector)
    elif ext == ".zarr" or os.path.isdir(path):
        data, meta_dims, data_path = _load_zarr(path, args.detector)
    else:
        raise SystemExit(f"Unsupported input '{path}' (expected .hdf5/.h5/.zarr)")

    dims = tuple(args.dims) if args.dims else meta_dims
    if dims is None:
        raise SystemExit(
            "Could not read scan dimensions from metadata; pass --dims NX NY."
        )

    print(f"Reconstructing '{data_path}': stack {data.shape} ({data.dtype}) "
          f"-> grid {dims[0]}x{dims[1]}")
    image = reconstruct(data, dims)

    out = args.output or (os.path.splitext(path)[0] + "_beadrec.tif")
    try:
        import tifffile
        tifffile.imwrite(out, image.astype(np.float32))
        print(f"Saved reconstruction (float32) to {out}")
    except ImportError:
        np.save(os.path.splitext(out)[0] + ".npy", image)
        print(f"tifffile not available; saved {os.path.splitext(out)[0]}.npy instead")

    if args.show:
        import matplotlib.pyplot as plt
        plt.imshow(image, cmap="gray")
        plt.title(os.path.basename(data_path))
        plt.colorbar(label="ROI mean (whole frame)")
        plt.show()

    return image


if __name__ == "__main__":
    main()
