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


def resolve_gates(
    gates: tuple[GateSpec, ...] | list[GateSpec],
    peak_time_ns: float | None,
) -> tuple[GateSpec, ...]:
    """Turn peak-relative gates into absolute ones on the histogram axis.

    Absolute gates pass through unchanged. A peak-relative gate needs
    ``peak_time_ns``; without it a ``ValueError`` says which gate.
    """

    resolved = []
    for gate in validate_gates(gates):
        if gate.reference == "absolute":
            resolved.append(gate)
            continue
        if peak_time_ns is None:
            raise ValueError(
                f"Gate {gate.name!r} is relative to the IRF peak, but no peak "
                "time is known yet"
            )
        resolved.append(
            GateSpec(
                gate.name,
                gate.start_ns + float(peak_time_ns),
                gate.stop_ns + float(peak_time_ns),
                reference="absolute",
            )
        )
    return tuple(resolved)


def compute_gate_images(
    cube_counts: np.ndarray,
    t_axis_ns: np.ndarray,
    gates: tuple[GateSpec, ...] | list[GateSpec],
    peak_time_ns: float | None = None,
) -> dict[str, np.ndarray]:
    """Compute one image per time gate.

    A gate includes bins whose centers satisfy ``start_ns <= t < stop_ns``
    on the histogram axis; peak-relative gates are resolved against
    ``peak_time_ns`` first (see :func:`resolve_gates`). Empty gates produce
    a zero image with the same spatial shape as the cube.
    """

    cube, axis = _validate_cube_and_axis(cube_counts, t_axis_ns)
    gates = resolve_gates(gates, peak_time_ns)
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


# --------------------------------------------------------------------------- #
# TCSPC direction, background, pile-up                                         #
# --------------------------------------------------------------------------- #

TCSPC_DIRECTIONS = ("forward", "reverse")


def orient_cube(
    cube_counts: np.ndarray,
    t_axis_ns: np.ndarray,
    direction: str,
    period_ns: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(cube, t_axis_ns)`` in forward time.

    ``"forward"`` (start = laser sync, click = photon) returns the inputs
    unchanged. ``"reverse"`` (start = photon, click = sync: what the card's
    conditional filter forces) measured ``t' = T_rep - t``, so each bin is
    mapped to ``t = T_rep - t'`` and the bin order is reversed to keep the
    axis ascending. The mapped axis is carried as values, not re-gridded:
    with 391 bins of 32 ps on a 12.5 ns period the mirrored centres sit 20 ps
    off the forward grid, and every consumer reads the axis rather than
    assuming ``k * binwidth``.
    """

    cube, axis = _validate_cube_and_axis(cube_counts, t_axis_ns)
    if axis is None:
        raise ValueError("orient_cube needs t_axis_ns")
    direction = str(direction).strip().lower()
    if direction not in TCSPC_DIRECTIONS:
        raise ValueError(
            f"direction must be one of {TCSPC_DIRECTIONS}, not {direction!r}"
        )
    if direction == "forward":
        return cube, axis
    if period_ns is None or not np.isfinite(period_ns) or period_ns <= 0:
        raise ValueError("reverse orientation needs the laser period in ns")
    mirrored = (float(period_ns) - axis)[::-1]
    return cube[..., ::-1], np.ascontiguousarray(mirrored)


def roll_to_peak(
    cube_counts: np.ndarray,
    peak_bin: int,
    target_bin: int,
) -> np.ndarray:
    """Circularly shift the time axis so ``peak_bin`` lands on ``target_bin``.

    Exact for a histogram that spans whole laser periods (reverse mode
    guarantees that); the software counterpart of ``setInputDelay`` on the
    photon channel, which must not be used in reverse mode because it drops
    the earliest photons instead of moving them.
    """

    cube, _ = _validate_cube_and_axis(cube_counts)
    n_bins = cube.shape[-1]
    shift = (int(target_bin) - int(peak_bin)) % n_bins
    if shift == 0:
        return cube
    return np.roll(cube, shift, axis=-1)


def background_per_bin(
    rate_hz: float,
    dwell_s: float,
    binwidth_ps: float,
    period_ps: float,
) -> float:
    """Expected dark + afterpulsing counts per histogram bin per pixel.

    A flat rate contributes ``rate * dwell`` counts per pixel, spread
    uniformly over the laser period ``period_ps``, so each bin of
    ``binwidth_ps`` collects the fraction ``binwidth / period`` of them,
    independent of the number of bins. (Without the division by the period
    the number is wrong by a factor of the repetition rate: 80 million at
    80 MHz.)
    """

    rate = max(0.0, float(rate_hz))
    period = float(period_ps)
    if period <= 0:
        return 0.0
    counts = rate * max(0.0, float(dwell_s))
    return counts * max(0.0, float(binwidth_ps)) / period


def subtract_background(
    cube_counts: np.ndarray,
    per_bin: float,
) -> np.ndarray:
    """Subtract a flat ``per_bin`` from every histogram, clipped at zero.

    Returns a float copy; the input is never modified. ``per_bin <= 0``
    returns the input as is.
    """

    cube, _ = _validate_cube_and_axis(cube_counts)
    if per_bin <= 0:
        return cube
    out = cube.astype(np.float64, copy=True) - float(per_bin)
    np.maximum(out, 0.0, out=out)
    return out.astype(np.float32, copy=False)


def estimate_prepulse_background(
    decay_counts: np.ndarray,
    t_axis_ns: np.ndarray,
    peak_time_ns: float,
    window_ns: float,
    n_pixels: int = 1,
) -> float:
    """Flat background per bin per pixel from the bins before the IRF peak.

    A diagnostic, not a default: at 80 MHz the previous period's tail is
    still falling where the window sits, so the estimate is biased high for
    long lifetimes. The window ends ``window_ns`` before the peak and starts
    at the axis origin; with no bin in it the estimate is ``0``.
    """

    decay = np.asarray(decay_counts, dtype=np.float64)
    axis = np.asarray(t_axis_ns, dtype=np.float64)
    if decay.ndim != 1 or axis.shape != decay.shape:
        raise ValueError("decay_counts and t_axis_ns must be matching 1D arrays")
    stop = float(peak_time_ns) - max(0.0, float(window_ns))
    mask = axis < stop
    if not np.any(mask):
        return 0.0
    return float(decay[mask].mean()) / max(1, int(n_pixels))


def pileup_fraction(
    intensity: np.ndarray,
    laser_rep_rate_hz: float,
    dwell_s: float,
) -> np.ndarray:
    """Detected photons per excitation pulse, per pixel.

    Classical TCSPC pile-up: above a few percent the histogram is biased
    towards the earlier photon of a pair (forward mode) or the later one
    (reverse mode). Uses the *configured* rep rate, never a measured sync
    rate -- with the conditional filter on, the measured sync rate is the
    photon rate.
    """

    pulses = max(1e-30, float(laser_rep_rate_hz)) * max(0.0, float(dwell_s))
    if pulses <= 0:
        return np.zeros(np.shape(intensity), dtype=np.float32)
    return (np.asarray(intensity, dtype=np.float64) / pulses).astype(np.float32)


#: Pile-up fractions at which the preflight and the widget warn, then go red.
PILEUP_WARN = 0.05
PILEUP_RED = 0.10
