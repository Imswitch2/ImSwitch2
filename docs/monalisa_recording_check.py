"""Reconstruct a MoNaLISA recording with the lattice pipeline and say how it went.

The pipeline of ``docs/monalisa_optimal_reconstruction.md`` is not wired into
ImSwitch yet. This script runs it on a recording, so that it can be tried on
data before it is::

    python docs/monalisa_recording_check.py recording.hdf5 --pixel-nm 77

It reads the scan from the recording's ``ScanStage`` attributes (step, and
the number of steps from the scanned length), or takes it from the command
line. Without ``--pixel-nm`` it takes the scan to cover the cell of the
lattice once and says what pixel size that makes. It reconstructs with the
pipeline's defaults and with what the pipeline replaces, and prints for each

* what the pipeline read from the frames: lattice, spot width, scan step and
  pixel size, orientation, the gain of the frames, the shift factor;
* the measures of :mod:`imswitch.improcess.reconstructors.monalisa.quality`:
  the trace of the lattice in the image (``tiling``, ``seams``; 1 is none),
  the noise of the image and the width of its thin filaments, and the
  Fourier ring correlation of two reconstructions from disjoint halves of the
  camera pixels.

The last line is the default image after the sharpening filter
(``--sharpen SIGMA REGULARIZATION``, in output pixels): how sharp an image
looks is a filter's choice, how much noise it has at a given sharpness is the
estimator's.

With ``--out`` it writes the images as TIFF and a comparison as PNG.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from imswitch.improcess.reconstructors.monalisa import quality
from imswitch.improcess.reconstructors.monalisa.pipeline import (
    PipelineParams,
    reconstruct_scan,
)

CONFIGURATIONS = {
    "fast-Gauss-like": PipelineParams(
        joint=False, reach_sigma=1.5, reassignment="off",
        frame_gain="off", cell_offsets="off",
    ),
    "joint fit": PipelineParams(
        reassignment="off", frame_gain="off", cell_offsets="off"
    ),
    "+ frame gain": PipelineParams(reassignment="off", cell_offsets="off"),
    "+ cell offsets": PipelineParams(reassignment="off"),
    "+ reassignment (default)": PipelineParams(),
}
SHARPENED = "default, sharpened"


def load(path: Path, dataset: str | None):
    import h5py

    with h5py.File(path, "r") as file:
        names = []
        file.visititems(
            lambda name, item: names.append(name)
            if isinstance(item, h5py.Dataset) and item.ndim >= 3 else None
        )
        if not names:
            raise SystemExit(f"No image stack in {path}")
        name = dataset or names[0]
        data = file[name]
        attrs = dict(data.attrs)
        frames = np.asarray(data, dtype=np.float32)
    return frames.reshape(-1, *frames.shape[-2:]), attrs, name


def scan_from_attributes(attrs: dict, num_frames: int):
    """``(num_fast, num_slow, step_nm)`` from the ScanStage attributes."""
    step = np.asarray(attrs.get("ScanStage:axis_step_size", []), dtype=float).ravel()
    length = np.asarray(attrs.get("ScanStage:axis_length", []), dtype=float).ravel()
    if step.size < 2 or length.size < 2 or not np.all(step[:2] > 0):
        return None
    counts = np.floor(length[:2] / step[:2] + 1e-6).astype(int) + 1
    if counts[0] * counts[1] != num_frames:
        return None
    return int(counts[0]), int(counts[1]), float(step[0] * 1000.0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("recording", type=Path)
    parser.add_argument("--dataset", help="name of the stack in the file")
    parser.add_argument("--pixel-nm", type=float,
                        help="nominal camera pixel size in the sample; without it "
                             "the scan is taken to cover the cell once")
    parser.add_argument("--steps", type=int, nargs=2, metavar=("FAST", "SLOW"))
    parser.add_argument("--step-nm", type=float)
    parser.add_argument("--orientation", help='e.g. "-x+y"; read from the data if not given')
    parser.add_argument("--sharpen", type=float, nargs=2, default=(1.5, 0.1),
                        metavar=("SIGMA", "REGULARIZATION"),
                        help="the sharpening filter of the last line")
    parser.add_argument("--pinholes", choices=("raw", "shifted"),
                        help="with --out: also write the raw frames assembled "
                             "into one image per pixel of the footprint")
    parser.add_argument("--out", type=Path, help="prefix of the files to write")
    args = parser.parse_args(argv)

    frames, attrs, name = load(args.recording, args.dataset)
    print(f"{args.recording.name}: '{name}', {frames.shape[0]} frames of "
          f"{frames.shape[1]} x {frames.shape[2]}")
    scan = scan_from_attributes(attrs, frames.shape[0])
    if args.steps:
        num_fast, num_slow = args.steps
        step_nm = args.step_nm or (scan[2] if scan else None)
    elif scan:
        num_fast, num_slow, step_nm = scan
        step_nm = args.step_nm or step_nm
    else:
        raise SystemExit("The scan is not in the attributes: give --steps and --step-nm")
    if step_nm is None:
        raise SystemExit("Give --step-nm")
    print(f"scan: {num_fast} x {num_slow} steps of {step_nm:g} nm, nominal pixel "
          + (f"{args.pixel_nm:g} nm" if args.pixel_nm else "not given"))
    step = np.diag([step_nm / args.pixel_nm] * 2) if args.pixel_nm else None
    shape = (num_fast, num_slow)

    started = time.perf_counter()
    default = reconstruct_scan(frames, step, shape, orientation=args.orientation)
    elapsed = time.perf_counter() - started
    geometry, found = default.geometry.diagnostics, default.diagnostics
    used = np.array(geometry["step_px"])
    pixel_nm = step_nm / np.sqrt(abs(np.linalg.det(used)))
    print(f"lattice: {geometry['lattice']}")
    if geometry["coverage_holes"] > 0 and geometry["step_given"]:
        positions = shape[0] * shape[1]
        covering = float(np.sqrt(default.geometry.lattice.cell_area / positions))
        ratio = covering / np.sqrt(abs(np.linalg.det(used)))
        print(f"the scan leaves holes in the cell: a step {ratio:.2f} x the one used "
              f"({ratio * step_nm:.1f} nm) would cover it once; check the recorded step")
    print(f"calibration: {geometry['calibration_image']} frame; foci contrast: " + ", ".join(
        f"{name} {value:.2f}" for name, value in geometry["calibration_contrast"].items()
        if np.isfinite(value)))
    print(f"spot: sigma {geometry['spot_sigma_px']:.3f} px, FWHM "
          f"{2.355 * geometry['spot_sigma_px'] * pixel_nm:.0f} nm; "
          f"{geometry['num_foci']} foci, "
          f"{geometry['spot_model']['num_measured']} measured")
    if not geometry["step_given"]:
        print(f"step: from the cell of the lattice; pixel size {pixel_nm:.2f} nm "
              f"if the step is {step_nm:g} nm")
    else:
        locked = "locked to the lattice" if geometry["step_locked"] else "as given"
        print(f"step: {locked}, change {100 * geometry['step_lock_change']:.2f} %; "
              f"pixel size if the step is right {pixel_nm:.2f} nm")
    print(f"coverage of the cell: {geometry['coverage_holes']} holes, "
          f"{geometry['coverage_overlaps']} overlaps of "
          f"{geometry['coverage_cell_pixels']} positions")
    if "orientation_margin" in found:
        print(f"orientation: {found['orientation']}, ahead of the next by "
              f"{found['orientation_margin']:.2f} of the range")
    print(f"frame gain: {100 * found['frame_gain_rms']:.1f} % rms, "
          f"{found['frame_gain_range'][0]:.2f} to {found['frame_gain_range'][1]:.2f}")
    print(f"cell offsets: {found['cell_offset_rms']:.2f} counts rms applied; "
          f"{100 * found['cell_offset_share']:.0f} % of what the borders show is offset")
    print(f"shift factor: {found['shift_factor']:.3f} "
          f"(x {found['shift_factor_xy'][0]:.3f}, y {found['shift_factor_xy'][1]:.3f}) "
          f"-> {found['placement']}")
    print(f"reconstructed in {elapsed:.1f} s\n")

    orientation = found["orientation"]
    factor = default.geometry.shift_factor
    masks = quality.split_masks(frames.shape[1:])
    images = {}
    filaments = quality.find_filaments(default.amplitude.image)
    thin = filaments.thinnest()
    print(f"{len(filaments)} isolated filaments to measure the width on, "
          f"{len(thin)} of them thin")
    configurations = dict(CONFIGURATIONS)
    configurations[SHARPENED] = PipelineParams(
        sharpen_sigma_px=args.sharpen[0], sharpen_regularization=args.sharpen[1]
    )
    header = None
    for label, params in configurations.items():

        def image_of(result):
            return result.sharpened if label == SHARPENED else result.amplitude.image

        whole = reconstruct_scan(
            frames, step, shape, params, orientation=orientation, shift_factor=factor
        )
        halves = [
            quality.central_part(image_of(
                reconstruct_scan(
                    frames, step, shape, params, orientation=orientation,
                    shift_factor=factor, pixel_mask=mask,
                )
            ))
            for mask in masks
        ]
        frequency, correlation = quality.ring_correlation(*halves)
        if header is None:
            header = " ".join(f"{step_nm / f:5.0f}" for f in frequency[1:14:2])
            print(f"{'':26s} {'tiling':>6s} {'seams':>6s} {'noise':>7s} {'width':>6s} "
                  f"{'thin':>6s} | ring correlation at periods (nm)")
            print(f"{'':26s} {'':6s} {'':6s} {'counts':>7s} {'nm':>6s} {'nm':>6s} | {header}")
        image = image_of(whole)
        images[label] = image
        tiling = quality.tiling_contrast(image, whole.geometry.index_matrix)
        seams = quality.seam_contrast(image, whole.owner)
        print(f"{label:26s} {tiling:6.2f} {seams:6.2f} "
              f"{quality.split_noise(*halves):7.2f} "
              f"{step_nm * quality.filament_width(image, filaments):6.0f} "
              f"{step_nm * quality.filament_width(image, thin):6.0f} | "
              + " ".join(f"{c:5.2f}" for c in correlation[1:14:2]))

    if args.out:
        write(args.out, images)
        if args.pinholes:
            stack = reconstruct_scan(
                frames, step, shape, PipelineParams(pinhole_stack=args.pinholes),
                orientation=orientation, shift_factor=factor,
            ).pinholes
            write_pinholes(args.out, stack)


def busiest_part(image: np.ndarray, size: int) -> tuple[int, int]:
    """Corner of the square of ``size`` pixels with the most structure in it."""
    from scipy.ndimage import uniform_filter

    filled = np.nan_to_num(image, nan=float(np.nanmedian(image)))
    local = uniform_filter(filled, 5)
    busy = uniform_filter((local - uniform_filter(local, size // 4)) ** 2, size)
    half = size // 2
    inner = busy[half:-half, half:-half]
    row, col = np.unravel_index(np.argmax(inner), inner.shape)
    return int(row), int(col)


def write_pinholes(prefix: Path, stack):
    import tifffile

    kind = "shifted" if stack.shifted else "raw"
    path = f"{prefix}_pinholes_{kind}.tiff"
    labels = [f"dx={dx:+d} dy={dy:+d}" for dx, dy in zip(stack.dx, stack.dy)]
    tifffile.imwrite(
        path, np.nan_to_num(stack.images), imagej=True,
        metadata={"axes": "ZYX", "Labels": labels},
    )
    print(f"written: {path}, {len(labels)} virtual pinholes, "
          f"the central one first")


def write(prefix: Path, images: dict):
    import tifffile

    prefix.parent.mkdir(parents=True, exist_ok=True)
    for label, image in images.items():
        name = label.strip("+ ").replace(" (default)", "").replace(",", "")
        name = name.replace(" ", "_")
        tifffile.imwrite(f"{prefix}_{name}.tiff", np.nan_to_num(image).astype(np.float32))
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    shown = ["fast-Gauss-like", "+ reassignment (default)", SHARPENED]
    figure, axes = plt.subplots(2, 3, figsize=(27, 18))
    reference = images[shown[-1]]
    size = min(220, reference.shape[0] // 3)
    row, col = busiest_part(reference, size)
    for column, label in enumerate(shown):
        image = images[label]
        low, high = np.nanpercentile(image, [1, 99.7])
        axes[0, column].imshow(image, cmap="gray", vmin=low, vmax=high)
        axes[0, column].set_title(label, fontsize=18)
        part = image[row:row + size, col:col + size]
        axes[1, column].imshow(
            part, cmap="gray", vmin=np.nanpercentile(part, 1),
            vmax=np.nanpercentile(part, 99.7), interpolation="nearest",
        )
        axes[1, column].set_title(f"{label}, {size} pixels", fontsize=18)
    for axis in axes.ravel():
        axis.axis("off")
    figure.tight_layout()
    figure.savefig(f"{prefix}_comparison.png", dpi=50)
    print(f"\nwritten: {prefix}_*.tiff, {prefix}_comparison.png")


if __name__ == "__main__":
    main(sys.argv[1:])
