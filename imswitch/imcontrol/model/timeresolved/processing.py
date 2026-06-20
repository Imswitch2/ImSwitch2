"""Pure NumPy helpers for time-resolved detector products."""

from __future__ import annotations

import numpy as np

from .types import GateSpec


def validate_gates(gates: tuple[GateSpec, ...] | list[GateSpec]) -> tuple[GateSpec, ...]:
    """Return gates as a tuple after checking name uniqueness."""

    gates = tuple(gates or ())
    names = [gate.name for gate in gates]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            "Duplicate time gate names: " + ", ".join(repr(n) for n in duplicates)
        )
    return gates


def _validate_cube_and_axis(
    cube_counts: np.ndarray,
    t_axis_ns: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray | None]:
    cube = np.asarray(cube_counts)
    if cube.ndim < 1:
        raise ValueError("cube_counts must have at least one dimension")
    axis = None if t_axis_ns is None else np.asarray(t_axis_ns, dtype=np.float64)
    if axis is not None:
        if axis.ndim != 1:
            raise ValueError("t_axis_ns must be 1D")
        if axis.shape[0] != cube.shape[-1]:
            raise ValueError(
                f"t_axis_ns length {axis.shape[0]} does not match cube bin axis "
                f"{cube.shape[-1]}"
            )
    return cube, axis


def aggregate_decay(cube_counts: np.ndarray) -> np.ndarray:
    """Sum a time-resolved cube over all non-time axes."""

    cube, _ = _validate_cube_and_axis(cube_counts)
    if cube.ndim == 1:
        return np.array(cube, copy=True)
    axes = tuple(range(cube.ndim - 1))
    return cube.sum(axis=axes)


def intensity_from_cube(cube_counts: np.ndarray) -> np.ndarray:
    """Sum photon counts over the time axis."""

    cube, _ = _validate_cube_and_axis(cube_counts)
    return cube.sum(axis=-1)


def compute_gate_images(
    cube_counts: np.ndarray,
    t_axis_ns: np.ndarray,
    gates: tuple[GateSpec, ...] | list[GateSpec],
) -> dict[str, np.ndarray]:
    """Compute one image per time gate.

    A gate includes bins whose centers satisfy ``start_ns <= t < stop_ns``.
    Empty gates produce a zero image with the same spatial shape as the cube.
    """

    cube, axis = _validate_cube_and_axis(cube_counts, t_axis_ns)
    gates = validate_gates(gates)
    if not gates:
        return {}

    spatial_shape = cube.shape[:-1]
    out: dict[str, np.ndarray] = {}
    for gate in gates:
        mask = (axis >= gate.start_ns) & (axis < gate.stop_ns)
        if not np.any(mask):
            out[gate.name] = np.zeros(spatial_shape, dtype=cube.dtype)
        else:
            out[gate.name] = cube[..., mask].sum(axis=-1)
    return out
