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
