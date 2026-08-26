"""Direct-summation CGH backend for continuous spot resolutions.

The backend consumes normalized Fourier positions in cycles per SLM pixel and
relative spot intensities, and returns a section-sized complex phase field. It
does not depend on a concrete target subclass.
"""

from __future__ import annotations

import logging

from typing import Any,Mapping

import numpy as np

from ...engine.parameters import ParamSpec, ParamDisplayLevel
from ...engine.registry import register_cgh_algorithm
from ..targets.resolution import TargetResolution
from .initialization import resolve_initial_phase,validate_initial_field
from .metrics import evaluate_intensity_metrics,normalize_relative_intensity
from .types import CGHAlgorithmOutput

_logger = logging.getLogger(__name__)


DIRECT_SUMMATION_PARAMS = {
    "n_iterations": ParamSpec(50,int,min_value=1,max_value=300),
    "weighted_gs": ParamSpec(True,bool),
}


@register_cgh_algorithm(
    "direct_summation",
    params=DIRECT_SUMMATION_PARAMS,
)
def compute(
    resolution: TargetResolution,
    compute_params: Mapping[str,Any],
    initial_field: np.ndarray | None,
) -> CGHAlgorithmOutput:
    """Compute a phase-only hologram from effective continuous spot positions."""
    if not isinstance(resolution,TargetResolution):
        raise TypeError(
            "resolution must be TargetResolution, got "
            f"{type(resolution).__name__}"
        )

    return _direct_spot_wgs(
        spot_positions_kxy=resolution.spot_positions_kxy,
        spot_intensities=resolution.spot_intensities,
        shape=resolution.section_shape,
        compute_params=dict(compute_params or {}),
        initial_field=initial_field,
    )


def _direct_spot_wgs(
    spot_positions_kxy: np.ndarray,
    spot_intensities: np.ndarray,
    shape,
    compute_params: Mapping[str,Any],
    initial_field: np.ndarray | None,
) -> CGHAlgorithmOutput:
    """Run direct nonuniform Fourier projection with optional weighted GS."""
    height,width = _validate_shape(shape)
    positions = np.asarray(spot_positions_kxy,dtype=np.float32)
    if positions.ndim != 2 or positions.shape[0] != 2:
        raise ValueError(
            "spot_positions_kxy must have shape (2, N), got "
            f"{positions.shape}"
        )
    if not np.all(np.isfinite(positions)):
        raise ValueError("spot_positions_kxy contains non-finite values")

    n_spots = int(positions.shape[1])
    if n_spots <= 0:
        raise ValueError("spot_positions_kxy contains no spots")

    target_intensity_np = normalize_relative_intensity(spot_intensities)
    if target_intensity_np.shape != (n_spots,):
        raise ValueError(
            f"spot_intensities must have shape ({n_spots},), got "
            f"{target_intensity_np.shape}"
        )

    weighted_gs = bool(compute_params.get("weighted_gs",True))
    n_iterations = int(compute_params.get("n_iterations",50))
    if n_iterations <= 0:
        raise ValueError("n_iterations must be >= 1")

    cuda_requested = bool(
        compute_params.get(
            "direct_cuda",compute_params.get("cuda",True)
        )
    )
    xp,using_cuda,backend_name,backend_warning = _get_array_module(
        cuda_requested
    )
    warnings = [backend_warning] if backend_warning else []

    dtype_complex = xp.complex64
    dtype_float = xp.float32
    kx = xp.asarray(positions[0],dtype=dtype_float)
    ky = xp.asarray(positions[1],dtype=dtype_float)
    target_intensity = xp.asarray(target_intensity_np,dtype=dtype_float)
    target_amplitude = xp.sqrt(target_intensity)

    scale_x_value,scale_y_value = _resolve_kxy_scale(
        compute_params.get("direct_kxy_scale")
    )
    sign_value = float(compute_params.get("direct_sign",-1))
    feedback_exponent_value = float(
        compute_params.get("direct_feedback_exponent",0.8)
    )
    seed = int(compute_params.get("direct_seed",1))
    verbose = bool(compute_params.get("direct_verbose",False))
    minimum_update_value = float(
        compute_params.get("direct_min_weight_update",0.2)
    )
    maximum_update_value = float(
        compute_params.get("direct_max_weight_update",5.0)
    )
    free_gpu_memory = bool(
        compute_params.get("direct_free_gpu_memory",False)
    )
    quad_phase = bool(compute_params.get("quad_phase",False))
    quad_phase_coeff = compute_params.get("quad_phase_coeff")

    if feedback_exponent_value <= 0:
        raise ValueError("direct_feedback_exponent must be > 0")
    if minimum_update_value <= 0:
        raise ValueError("direct_min_weight_update must be > 0")
    if maximum_update_value < minimum_update_value:
        raise ValueError(
            "direct_max_weight_update must be >= direct_min_weight_update"
        )

    scale_x = dtype_float(scale_x_value)
    scale_y = dtype_float(scale_y_value)
    sign = dtype_float(sign_value)
    feedback_exponent = dtype_float(feedback_exponent_value)
    minimum_update = dtype_float(minimum_update_value)
    maximum_update = dtype_float(maximum_update_value)
    epsilon = dtype_float(1e-9)
    twopi = dtype_float(2.0 * np.pi)

    x = xp.asarray(
        np.arange(width,dtype=np.float32) - (width - 1) / 2.0,
        dtype=dtype_float,
    )
    y = xp.asarray(
        np.arange(height,dtype=np.float32) - (height - 1) / 2.0,
        dtype=dtype_float,
    )

    ex_forward = xp.exp(
        1j * sign * twopi * (kx[:,None] * scale_x) * x[None,:]
    ).astype(dtype_complex)
    ey_forward = xp.exp(
        1j * sign * twopi * (ky[:,None] * scale_y) * y[None,:]
    ).astype(dtype_complex)
    ex_backward = xp.conj(ex_forward)
    ey_backward = xp.conj(ey_forward)

    initial_validated = None
    if initial_field is not None:
        initial_validated = validate_initial_field(
            initial_field,(height,width)
        )
    phase_np,initialization_warnings = resolve_initial_phase(
        shape=(height,width),
        initial_field=initial_validated,
        quad_phase=quad_phase,
        quad_phase_coeff=quad_phase_coeff,
        seed=seed,
    )
    warnings.extend(initialization_warnings)
    phase_slm = xp.asarray(phase_np,dtype=dtype_float)

    source_amplitude = xp.ones((height,width),dtype=dtype_float)
    weights = xp.ones(n_spots,dtype=dtype_float)
    metrics = []
    final_concentration_proxy = None

    for iteration in range(n_iterations):
        field_slm = source_amplitude * xp.exp(1j * phase_slm).astype(
            dtype_complex
        )

        intermediate = field_slm @ ex_forward.T
        spot_field = xp.sum(intermediate.T * ey_forward,axis=1)
        measured_amplitude = xp.abs(spot_field).astype(dtype_float)
        measured_intensity = measured_amplitude**2
        measured_phase = xp.angle(spot_field).astype(dtype_float)

        measured_intensity_np = _to_numpy_array(
            measured_intensity,using_cuda
        ).astype(np.float64,copy=False)
        metrics.append(evaluate_intensity_metrics(
            iteration=iteration + 1,
            measured_intensity=measured_intensity_np,
            desired_intensity=target_intensity_np,
            efficiency=None,
        ))

        maximum_intensity = float(np.max(measured_intensity_np))
        final_concentration_proxy = float(
            np.mean(
                measured_intensity_np / (maximum_intensity + 1e-12)
            )
        )

        if verbose:
            current = metrics[-1]
            _logger.info(
                "Direct WGS iter %d/%d: uniformity=%.4f, std=%.4f",
                iteration + 1,n_iterations,current.uniformity,current.normalized_std,
            )

        if weighted_gs:
            measured_relative = measured_amplitude / (
                xp.mean(measured_amplitude) + epsilon
            )
            desired_relative = target_amplitude / (
                xp.mean(target_amplitude) + epsilon
            )
            update = (
                desired_relative / (measured_relative + epsilon)
            )**feedback_exponent
            weights *= xp.clip(
                update,minimum_update,maximum_update
            )
            weights /= xp.mean(weights) + epsilon

        enforced_spots = (
            weights
            * target_amplitude
            * xp.exp(1j * measured_phase).astype(dtype_complex)
        )
        backward_y = ey_backward.T * enforced_spots[None,:]
        field_back = backward_y @ ex_backward
        phase_slm = xp.angle(field_back).astype(dtype_float)

    field = xp.exp(1j * phase_slm).astype(dtype_complex)
    field_np = _to_numpy_array(field,using_cuda).astype(
        np.complex128,copy=False
    )

    if using_cuda and free_gpu_memory:
        try:
            import cupy as cp
            cp.get_default_memory_pool().free_all_blocks()
        except Exception:
            _logger.debug("Could not release CuPy memory pool",exc_info=True)

    return CGHAlgorithmOutput(
        pattern=field_np,
        metrics=tuple(metrics),
        warnings=tuple(warnings),
        diagnostics={
            "backend": backend_name,
            "weighted_gs": weighted_gs,
            "iterations": n_iterations,
            "spot_count": n_spots,
            "section_shape": (height,width),
            "kxy_scale": (scale_x_value,scale_y_value),
            "forward_sign": sign_value,
            "efficiency_available": False,
            "final_peak_concentration_proxy": final_concentration_proxy,
            "initialization": (
                "previous_field" if initial_validated is not None
                else "quadratic_phase" if quad_phase and not initialization_warnings
                else "deterministic_random"
            ),
        },
    )


def _validate_shape(shape):
    if shape is None or len(shape) != 2:
        raise ValueError(f"shape must be (height, width), got {shape}")
    height,width = int(shape[0]),int(shape[1])
    if height <= 0 or width <= 0:
        raise ValueError(f"Invalid shape: {shape}")
    return height,width


def _get_array_module(use_cuda):
    """Return NumPy or CuPy and a recoverable fallback warning."""
    if not use_cuda:
        return np,False,"numpy",None

    try:
        import cupy as cp
        _ = cp.zeros((1,),dtype=cp.float32)
        return cp,True,"cupy",None
    except Exception as error:
        warning = (
            "CUDA was requested but CuPy was unavailable or failed; "
            f"NumPy CPU was used instead: {error}"
        )
        return np,False,"numpy",warning


def _resolve_kxy_scale(direct_kxy_scale=None):
    """Return diagnostic X/Y scale under cycles-per-SLM-pixel semantics."""
    if direct_kxy_scale is not None:
        if isinstance(direct_kxy_scale,(tuple,list,np.ndarray)):
            if len(direct_kxy_scale) >= 2:
                return float(direct_kxy_scale[0]),float(direct_kxy_scale[1])
            if len(direct_kxy_scale) == 1:
                scale = float(direct_kxy_scale[0])
                return scale,scale
        scale = float(direct_kxy_scale)
        return scale,scale
    return 1.0,1.0


def _to_numpy_array(value,using_cuda):
    if using_cuda:
        return value.get()
    return np.asarray(value)
