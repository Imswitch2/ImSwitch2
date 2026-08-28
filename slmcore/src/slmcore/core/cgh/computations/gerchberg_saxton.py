"""Gerchberg-Saxton CGH computation and weighted variants."""

from __future__ import annotations

from typing import Any,Mapping

import numpy as np

from ...engine.parameters import ParamSpec, ParamDisplayLevel
from ...engine.registry import register_cgh_algorithm
from ..shape import fit_array_centered
from ..targets.resolution import TargetResolution
from .initialization import resolve_initial_phase,validate_initial_field
from .metrics import evaluate_intensity_metrics,normalize_relative_intensity
from .types import CGHAlgorithmOutput


GERCHBERG_SAXTON_PARAMS = {
    "n_iterations": ParamSpec(50,int,min_value=1,max_value=300),
    "weighted_gs": ParamSpec(True,bool),
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
        metrics=internal_output.metrics,
        warnings=tuple(warnings),
        diagnostics=diagnostics,
    )


def _run_gerchberg_saxton(
    target_intensity: np.ndarray,
    compute_params: Mapping[str,Any],
    initial_field: np.ndarray | None,
) -> CGHAlgorithmOutput:
    """Run weighted or unweighted Gerchberg-Saxton on one intensity raster."""
    weighted_gs = bool(compute_params.get("weighted_gs",True))
    n_iterations = int(compute_params.get("n_iterations",50))
    phase_fixing = bool(compute_params.get("phase_fixing",True))
    phase_fixing_value = int(compute_params.get("phase_fixing_value",30))
    quad_phase = bool(compute_params.get("quad_phase",False))
    quad_phase_coeff = compute_params.get("quad_phase_coeff",0.004)
    seed = int(compute_params.get("seed",1))

    if n_iterations <= 0:
        raise ValueError("n_iterations must be >= 1")
    if phase_fixing_value <= 0:
        raise ValueError("phase_fixing_value must be >= 1")

    target_intensity = normalize_relative_intensity(target_intensity)
    target_shape = target_intensity.shape
    target_support = target_intensity > 0
    target_amplitude = np.sqrt(target_intensity)

    phase_slm,initialization_warnings = resolve_initial_phase(
        shape=target_shape,
        initial_field=initial_field,
        quad_phase=quad_phase,
        quad_phase_coeff=quad_phase_coeff,
        seed=seed,
    )

    source_amplitude = np.ones(target_shape,dtype=np.float64)
    weights = np.ones(target_shape,dtype=np.float64)
    metrics = []
    target_phase = None
    epsilon = 1e-12

    for iteration in range(n_iterations):
        field_slm = source_amplitude * np.exp(1j * phase_slm)
        field_target = np.fft.fftshift(np.fft.fft2(field_slm))
        measured_intensity = np.abs(field_target)**2

        if not phase_fixing or iteration < phase_fixing_value:
            target_phase = np.angle(field_target)
        if target_phase is None:
            raise RuntimeError("Target phase was not initialized")

        total_power = float(np.sum(measured_intensity))
        if total_power <= 0.0:
            raise RuntimeError("Gerchberg-Saxton propagated no power")
        captured_power = float(np.sum(measured_intensity[target_support]))
        efficiency = captured_power / total_power

        metrics.append(evaluate_intensity_metrics(
            iteration=iteration + 1,
            measured_intensity=measured_intensity[target_support],
            desired_intensity=target_intensity[target_support],
            efficiency=efficiency,
        ))

        if weighted_gs:
            measured_support = measured_intensity[target_support]
            measured_mean = float(np.mean(measured_support))
            desired_support = target_intensity[target_support]
            desired_mean = float(np.mean(desired_support))
            if measured_mean <= 0.0 or desired_mean <= 0.0:
                raise RuntimeError("Weighted GS requires positive target power")

            measured_relative = measured_support / measured_mean
            desired_relative = desired_support / desired_mean
            weights[target_support] *= np.sqrt(
                desired_relative / (measured_relative + epsilon)
            )
            weights[target_support] /= (
                np.mean(weights[target_support]) + epsilon
            )

        enforced_amplitude = target_amplitude
        if weighted_gs:
            enforced_amplitude = weights * target_amplitude

        field_target = enforced_amplitude * np.exp(1j * target_phase)
        field_slm = np.fft.ifft2(np.fft.ifftshift(field_target))
        phase_slm = np.angle(field_slm)

    return CGHAlgorithmOutput(
        pattern=np.exp(1j * phase_slm),
        metrics=tuple(metrics),
        warnings=initialization_warnings,
        diagnostics={
            "weighted_gs": weighted_gs,
            "iterations": n_iterations,
            "phase_fixing": phase_fixing,
            "phase_fixing_value": phase_fixing_value,
            "target_pixel_count": int(np.count_nonzero(target_support)),
            "initialization": (
                "previous_field" if initial_field is not None
                else "quadratic_phase" if quad_phase and not initialization_warnings
                else "deterministic_random"
            ),
        },
    )
