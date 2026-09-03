"""Gerchberg-Saxton CGH computation and weighted variants."""

from __future__ import annotations

from typing import Any,Mapping

import numpy as np

from ...engine.parameters import ParamDisplayLevel,ParamSpec
from ...engine.registry import register_cgh_algorithm
from ..shape import fit_array_centered
from ..targets.resolution import TargetResolution
from .array_backend import (
    available_backends,default_backend,get_array_module,to_float,to_numpy,
)
from .initialization import resolve_initial_phase,validate_initial_field
from .metrics import (
    evaluate_intensity_metrics,evaluate_intensity_metrics_backend,
    normalize_relative_intensity,
)
from .types import CGHAlgorithmOutput


GERCHBERG_SAXTON_PARAMS = {
    "n_iterations": ParamSpec(50,int,min_value=1,max_value=300),
    "weighted_gs": ParamSpec(True,bool),
    "backend": ParamSpec(
        default_backend(),str,choices=available_backends(),
        tooltip="Computation backend. GPU is offered only when CUDA/CuPy passed the startup probe.",
    ),
    "phase_fixing": ParamSpec(
        True,bool,display_level=ParamDisplayLevel.ADVANCED),
    "phase_fixing_value": ParamSpec(
        30,int,display_level=ParamDisplayLevel.ADVANCED),
    "quad_phase": ParamSpec(
        False,bool,display_level=ParamDisplayLevel.ADVANCED),
    "quad_phase_coeff": ParamSpec(
        0.004,float,display_level=ParamDisplayLevel.ADVANCED),
}


@register_cgh_algorithm(
    "gerchberg_saxton",
    params=GERCHBERG_SAXTON_PARAMS,
)
def compute(
    resolution: TargetResolution,
    compute_params: Mapping[str,Any],
    initial_field: np.ndarray | None,
    *,
    fixed_target_phase: np.ndarray | None=None,
) -> CGHAlgorithmOutput:
    """Compute a raster CGH and fit its complex field to the SLM section."""
    if not isinstance(resolution,TargetResolution):
        raise TypeError(
            "resolution must be TargetResolution, got "
            f"{type(resolution).__name__}"
        )
    if resolution.target_array is None:
        raise ValueError("Gerchberg-Saxton requires resolution.target_array")

    params = dict(compute_params or {})
    target_intensity = normalize_relative_intensity(resolution.target_array)
    target_shape = target_intensity.shape
    initial_internal = None
    initial_field_cropped = False

    if initial_field is not None:
        initial_section = validate_initial_field(
            initial_field,resolution.section_shape
        )
        initial_internal,initial_field_cropped = fit_array_centered(
            initial_section,target_shape,pad_mode="wrap"
        )

    internal_output = _run_gerchberg_saxton(
        target_intensity=target_intensity,
        compute_params=params,
        initial_field=initial_internal,
        fixed_target_phase=fixed_target_phase,
    )
    pattern,cropped = fit_array_centered(
        internal_output.pattern,resolution.section_shape,pad_mode="wrap"
    )

    warnings = list(internal_output.warnings)
    if cropped:
        warnings.append(
            "CGH pattern was cropped to fit the section size; this may "
            "introduce phase artefacts."
        )

    diagnostics = dict(internal_output.diagnostics)
    diagnostics.update({
        "internal_shape": tuple(int(value) for value in target_shape),
        "section_shape": tuple(int(value) for value in resolution.section_shape),
        "output_cropped": bool(cropped),
        "initial_field_cropped": bool(initial_field_cropped),
    })

    return CGHAlgorithmOutput(
        pattern=pattern,
        target_phase=internal_output.target_phase,
        metrics=internal_output.metrics,
        warnings=tuple(warnings),
        diagnostics=diagnostics,
    )


def _run_gerchberg_saxton(
    target_intensity: np.ndarray,
    compute_params: Mapping[str,Any],
    initial_field: np.ndarray | None,
    fixed_target_phase: np.ndarray | None=None,
) -> CGHAlgorithmOutput:
    """Run weighted or unweighted Gerchberg-Saxton on one intensity raster."""
    weighted_gs = bool(compute_params.get("weighted_gs",True))
    n_iterations = int(compute_params.get("n_iterations",50))
    phase_fixing = bool(compute_params.get("phase_fixing",True))
    phase_fixing_value = int(compute_params.get("phase_fixing_value",30))
    quad_phase = bool(compute_params.get("quad_phase",False))
    quad_phase_coeff = compute_params.get("quad_phase_coeff",0.004)
    seed = int(compute_params.get("seed",1))
    backend = compute_params.get("backend",default_backend())

    if n_iterations <= 0:
        raise ValueError("n_iterations must be >= 1")
    if phase_fixing_value <= 0:
        raise ValueError("phase_fixing_value must be >= 1")

    target_intensity_np = normalize_relative_intensity(target_intensity)
    target_shape = target_intensity_np.shape
    target_support_np = target_intensity_np > 0
    target_pixel_count = int(np.count_nonzero(target_support_np))

    phase_np,initialization_warnings = resolve_initial_phase(
        shape=target_shape,
        initial_field=initial_field,
        quad_phase=quad_phase,
        quad_phase_coeff=quad_phase_coeff,
        seed=seed,
    )

    fixed_target_phase_np = None
    if fixed_target_phase is not None:
        fixed_target_phase_np = np.asarray(fixed_target_phase,dtype=np.float64)
        if (
            fixed_target_phase_np.shape != target_shape
            or not np.all(np.isfinite(fixed_target_phase_np))
        ):
            raise ValueError(
                "fixed_target_phase must be a finite array matching the target shape"
            )
        fixed_target_phase_np = np.array(fixed_target_phase_np,copy=True)

    xp,using_gpu,backend_name = get_array_module(backend)
    dtype_float = xp.float32 if using_gpu else np.float64
    dtype_complex = xp.complex64 if using_gpu else np.complex128
    epsilon = dtype_float(1e-7 if using_gpu else 1e-12)

    target_intensity_xp = xp.asarray(target_intensity_np,dtype=dtype_float)
    target_support = xp.asarray(target_support_np)
    target_amplitude = xp.sqrt(target_intensity_xp)
    phase_slm = xp.asarray(phase_np,dtype=dtype_float)
    source_amplitude = xp.ones(target_shape,dtype=dtype_float)
    weights = xp.ones(target_shape,dtype=dtype_float)
    metrics = []
    target_phase = (
        None if fixed_target_phase_np is None
        else xp.asarray(fixed_target_phase_np,dtype=dtype_float)
    )

    for iteration in range(n_iterations):
        field_slm = (
            source_amplitude * xp.exp(1j * phase_slm)
        ).astype(dtype_complex,copy=False)
        field_target = xp.fft.fftshift(xp.fft.fft2(field_slm))
        measured_intensity = xp.abs(field_target)**2

        if fixed_target_phase_np is None and (
            not phase_fixing or iteration < phase_fixing_value
        ):
            target_phase = xp.angle(field_target).astype(dtype_float,copy=False)
        if target_phase is None:
            raise RuntimeError("Target phase was not initialized")

        total_power = to_float(xp.sum(measured_intensity),using_gpu)
        if total_power <= 0.0:
            raise RuntimeError("Gerchberg-Saxton propagated no power")
        measured_support = measured_intensity[target_support]
        desired_support = target_intensity_xp[target_support]
        captured_power = to_float(xp.sum(measured_support),using_gpu)
        efficiency = captured_power / total_power

        if using_gpu:
            metrics.append(evaluate_intensity_metrics_backend(
                iteration=iteration + 1,
                measured_intensity=measured_support,
                desired_intensity=desired_support,
                efficiency=efficiency,
                xp=xp,
                using_gpu=True,
            ))
        else:
            metrics.append(evaluate_intensity_metrics(
                iteration=iteration + 1,
                measured_intensity=measured_support,
                desired_intensity=desired_support,
                efficiency=efficiency,
            ))

        if weighted_gs:
            measured_mean = to_float(xp.mean(measured_support),using_gpu)
            desired_mean = to_float(xp.mean(desired_support),using_gpu)
            if measured_mean <= 0.0 or desired_mean <= 0.0:
                raise RuntimeError("Weighted GS requires positive target power")

            measured_relative = measured_support / dtype_float(measured_mean)
            desired_relative = desired_support / dtype_float(desired_mean)
            weights_support = weights[target_support]
            weights_support *= xp.sqrt(
                desired_relative / (measured_relative + epsilon)
            )
            weights_support /= xp.mean(weights_support) + epsilon
            weights[target_support] = weights_support

        enforced_amplitude = target_amplitude
        if weighted_gs:
            enforced_amplitude = weights * target_amplitude

        field_target = (
            enforced_amplitude * xp.exp(1j * target_phase)
        ).astype(dtype_complex,copy=False)
        field_slm = xp.fft.ifft2(xp.fft.ifftshift(field_target))
        phase_slm = xp.angle(field_slm).astype(dtype_float,copy=False)

    pattern = xp.exp(1j * phase_slm).astype(dtype_complex,copy=False)
    pattern_np = to_numpy(pattern,using_gpu).astype(np.complex128,copy=False)
    target_phase_np = to_numpy(target_phase,using_gpu).astype(
        np.float64,copy=False
    )

    return CGHAlgorithmOutput(
        pattern=pattern_np,
        target_phase=target_phase_np,
        metrics=tuple(metrics),
        warnings=initialization_warnings,
        diagnostics={
            "backend": backend_name,
            "weighted_gs": weighted_gs,
            "iterations": n_iterations,
            "phase_fixing": phase_fixing,
            "phase_fixing_value": phase_fixing_value,
            "fixed_target_phase": fixed_target_phase_np is not None,
            "target_pixel_count": target_pixel_count,
            "initialization": (
                "previous_field" if initial_field is not None
                else "quadratic_phase" if quad_phase and not initialization_warnings
                else "deterministic_random"
            ),
        },
    )
