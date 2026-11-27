"""A synthetic recording, so every example here runs without a microscope.

Writes an HDF5 file the way ImControl would (a ``data`` dataset with
``element_size_um``), containing a few blurred blobs on a background.

Without ``axes`` the file names no axes, so ImProcess reads it as
``Frame, Y, X``. A workflow that splits or projects over channels needs them
named: ``axes="CYX"`` records them on the dataset (the ``axes`` attribute).
"""

from pathlib import Path

import h5py
import numpy as np


def write_synthetic_recording(path, *, frames: int = 4, size: int = 96, seed: int = 0, axes: str | None = None) -> Path:
    path = Path(path)
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    stack = np.zeros((frames, size, size), dtype=np.float32)
    for frame in range(frames):
        image = np.full((size, size), 20.0, dtype=np.float32)
        for _ in range(6):
            cy, cx = rng.uniform(10, size - 10, size=2)
            image += 200.0 * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 3.0 ** 2))
        stack[frame] = image + rng.normal(0, 3.0, image.shape)
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=stack)
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
        if axes is not None:
            if len(axes) != stack.ndim:
                raise ValueError(f"axes {axes!r} name {len(axes)} axes; the recording has {stack.ndim}")
            dataset.attrs["axes"] = axes
    return path


def write_side_by_side_snouty(path, *, timepoints: int = 2, planes: int = 8, size: int = 64, seed: int = 0):
    """A SNOUTY time-lapse filed the awkward way: one 3D array, the timepoints'
    skewed stacks placed next to each other along X.

    ``(planes, size, timepoints * size)``; the volumes are returned too, so a
    caller can check that the reshape found them again.
    """
    path = Path(path)
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    volumes = []
    for _ in range(timepoints):
        volume = rng.normal(101.0, 2.0, (planes, size, size)).astype(np.float32)
        for plane in range(planes):
            for _ in range(3):
                cy, cx = rng.uniform(6, size - 6, size=2)
                volume[plane] += 150.0 * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 2.0 ** 2))
        volumes.append(volume)
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=np.concatenate(volumes, axis=-1))
        dataset.attrs["axes"] = "ZYX"
    return path, volumes


def write_tiling_timelapse(root, *, timepoints: int = 4, drift_px: float = 2.0, noise: float = 0.5, seed: int = 0):
    """A timelapse of tilings, laid out as the Timelapse panel writes it.

    ``timelapse.json`` plus ``t0/ t1/ ...``, each an ``imswitch-tiling/2``
    dataset of a 3 x 3 grid over one textured scene that drifts ``drift_px``
    pixels (rows) per timepoint. Returns the path of ``timelapse.json``.
    """
    from imswitch.improcess._test import _tiling_timelapse_fixtures as fx

    scene = fx.smooth_scene((360, 360), seed=3)
    drifts = {t: (drift_px * t, -drift_px * t / 2) for t in range(timepoints)}
    return fx.write_timelapse(
        root, scene, timepoints, nx=3, ny=3, tile=(96, 96), step_px=(72, 72),
        jitter=fx.random_jitter(9, amplitude=4, seed=5), noise=noise, seed=seed, drifts=drifts,
    )
