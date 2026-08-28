"""Shared initialization rules for CGH computation algorithms."""

from __future__ import annotations



import numpy as np


def validate_initial_field(
    initial_field: np.ndarray,
    expected_shape: tuple[int, int],
) -> np.ndarray:
    """Return a detached validated complex unit field."""
    expected_shape = _validate_shape(expected_shape)
    field = np.asarray(initial_field)

    if field.shape != expected_shape:
        raise ValueError(
            f"Initial field shape {field.shape}; expected {expected_shape}"
        )
    if not np.iscomplexobj(field):
        raise ValueError("Initial field must be complex")
    if not np.all(np.isfinite(field)):
        raise ValueError("Initial field contains non-finite values")
    if not np.allclose(np.abs(field),1.0,rtol=0.0,atol=1e-6):
        raise ValueError("Initial field must have unit amplitude")

    field = np.array(field,dtype=np.complex128,copy=True)
    field.setflags(write=False)
    return field


def resolve_initial_phase(
    shape: tuple[int, int],
    initial_field: np.ndarray | None,
    quad_phase: bool,
    quad_phase_coeff: float | None,
    seed: int = 1,
):
    """Resolve initialization using field, quadratic phase, then random phase."""
    shape = _validate_shape(shape)

    if initial_field is not None:
        field = validate_initial_field(initial_field,shape)
        return np.angle(field),()

    if quad_phase:
        if quad_phase_coeff is None:
            warning = (
                "Quadratic phase was requested without a coefficient; "
                "deterministic random initialization was used."
            )
            return deterministic_random_phase(shape,seed),(warning,)

        coefficient = float(quad_phase_coeff)
        if not np.isfinite(coefficient):
            raise ValueError("Quadratic phase coefficient must be finite")
        return quadratic_phase(shape,coefficient),()

    return deterministic_random_phase(shape,seed),()


def deterministic_random_phase(
    shape: tuple[int, int],seed: int = 1
) -> np.ndarray:
    """Return deterministic random phase in ``[-pi, pi]`` without global RNG state."""
    shape = _validate_shape(shape)
    rng = np.random.RandomState(int(seed))
    return rng.rand(*shape) * 2.0 * np.pi - np.pi


def quadratic_phase(shape: tuple[int, int],coefficient: float) -> np.ndarray:
    """Return centered quadratic phase wrapped to ``[-pi, pi]``."""
    height,width = _validate_shape(shape)
    x = np.arange(width,dtype=np.float64) - (width - 1) / 2.0
    y = np.arange(height,dtype=np.float64) - (height - 1) / 2.0
    grid_x,grid_y = np.meshgrid(x,y)
    phase = float(coefficient) * (grid_x**2 + grid_y**2)
    return (phase + np.pi) % (2.0 * np.pi) - np.pi


def _validate_shape(shape: tuple[int, int]) -> tuple[int, int]:
    if shape is None or len(shape) != 2:
        raise ValueError(f"shape must be (height, width), got {shape}")
    height,width = int(shape[0]),int(shape[1])
    if height <= 0 or width <= 0:
        raise ValueError(f"shape must be positive, got {shape}")
    return height,width
