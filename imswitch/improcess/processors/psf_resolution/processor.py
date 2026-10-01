"""PSF / bead resolution processor.

Finds the beads in a 2-D image or 3-D stack (or takes one per ROI Manager
ROI), fits each with a Gaussian, selects the consistent ones and reports
their FWHM statistics. Optionally averages the selected beads into a mean
PSF and, for calibrated 3-D stacks, estimates Zernike aberrations from it.

The analysis lives in :mod:`imswitch.improcess.analysis.bead_psf` and
:mod:`imswitch.improcess.analysis.psf_aberrations`; this class only resolves
the input (axes, pixel size) and wraps the outputs.

Output ports: ``beads`` (every candidate, with a ``selected`` flag),
``summary`` (statistics of the selected beads), ``average_psf`` (the mean
bead image, when requested and at least one bead qualifies) and
``aberrations`` (3-D, calibrated, when requested).
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.bead_psf import (
    BEAD_LABELINGS,
    FIT_MODES_3D,
    BeadPSFParams,
    analyze_beads,
    analyze_whole_image,
    average_psf,
    field_trend,
    focal_surface,
    roi_candidates,
    select_beads,
    summarize,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._extraction import extract_2d_plane, validate_axes
from imswitch.improcess.processors.base import OutputSpec, Processor, ProcessorOutput

from ._params import SELECTION_FIELDS, build_form, selection_from_params
from .result import AberrationsResult, BeadTableResult, PSFSummaryResult

SOURCES = ("auto", "rois", "full_image")

#: Length units a result's ``scale_unit`` may carry, in nanometres.
UNIT_TO_NM = {"nm": 1.0, "um": 1000.0, "µm": 1000.0, "μm": 1000.0, "micron": 1000.0, "mm": 1e6}


class PSFResolutionProcessor(Processor):
    """Bead-based PSF / resolution measurement for 2-D images and 3-D stacks."""

    name = "PSF / Bead Resolution"
    id = "psf-resolution"
    category = "Measurement"
    params_version = 2
    #: The ROI Manager panel passes its ROIs under this key.
    extra_param_keys = ("rois",)

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField("source", "select", "auto", label="Beads from", options=SOURCES,
                       help="Auto-detect beads, use one bead per ROI Manager ROI, or fit the whole "
                            "image as a single PSF.", group="Input"),
            ParamField("pixel_size_nm", "float", 0.0, label="Pixel size",
                       help="Lateral pixel size; 0 = from the data's scale.",
                       min=0.0, max=1e6, decimals=3, suffix="nm", group="Input"),
            ParamField("z_step_nm", "float", 0.0, label="Z step",
                       help="Axial step of a 3-D stack; 0 = from the data's scale.",
                       min=0.0, max=1e6, decimals=3, suffix="nm", group="Input"),
            ParamField("na", "float", 0.0, label="NA",
                       help="Numerical aperture; 0 = unknown (no diffraction-limit comparison, "
                            "no aberrations).", min=0.0, max=2.0, decimals=3, group="Optics"),
            ParamField("wavelength_nm", "float", 0.0, label="Emission wavelength",
                       help="0 = unknown.", min=0.0, max=2000.0, decimals=1, suffix="nm", group="Optics"),
            ParamField("refractive_index", "float", 1.515, label="Immersion index",
                       min=1.0, max=2.0, decimals=3, group="Optics"),
            ParamField("bead_diameter_nm", "float", 0.0, label="Bead diameter",
                       help="0 = no bead-size correction.", min=0.0, max=10000.0, decimals=1,
                       suffix="nm", group="Optics"),
            ParamField("bead_labeling", "select", "volume", label="Bead labelling", options=BEAD_LABELINGS,
                       help="Volume-labelled beads: sigma_bead^2 = d^2/20; shell-labelled: d^2/12.",
                       group="Optics"),
            *SELECTION_FIELDS,
            ParamField("average_psf", "bool", True, label="Average selected beads",
                       help="Also output the aligned mean bead and its fitted FWHM.", group="Outputs"),
            ParamField("fit_aberrations", "bool", False, label="Estimate aberrations (3-D)",
                       help="Fit Zernike modes 5-11 to the averaged bead. Needs a calibrated "
                            "through-focus stack, NA and wavelength. Slow (seconds).", group="Outputs"),
            ParamField("detection_threshold", "float", 5.0, label="Detection threshold",
                       help="LoG response above median + k MAD.", min=1.0, max=100.0, decimals=1,
                       advanced=True),
            ParamField("isolation_factor", "float", 3.0, label="Isolation (x FWHM)",
                       help="Beads closer than this many expected FWHMs are flagged crowded.",
                       min=1.0, max=20.0, decimals=1, advanced=True),
            ParamField("expected_fwhm_nm", "float", 0.0, label="Expected lateral FWHM",
                       help="Detection scale; 0 = from NA and wavelength, else 2 px sigma.",
                       min=0.0, max=1e5, decimals=1, suffix="nm", advanced=True),
            ParamField("fit_mode_3d", "select", "separable", label="3-D fit", options=FIT_MODES_3D,
                       help="Separable fits the axial profile and the focal plane; full fits a "
                            "3-D Gaussian.", advanced=True),
            ParamField("aberration_crop_nm", "float", 1500.0, label="Aberration crop half-width",
                       help="Lateral half-width of the averaged bead used for phase retrieval.",
                       min=300.0, max=20000.0, decimals=0, suffix="nm", advanced=True),
            ParamField("z_flip", "bool", False, label="Flip z for aberrations",
                       help="Reverse the z direction convention (changes the sign of even modes "
                            "such as spherical).", advanced=True),
        )

    @classmethod
    def default_params(cls) -> dict:
        return {f.key: f.default for f in cls.param_spec()}

    def migrate_params(self, encoded: dict | None, from_version: int) -> dict:
        """v1 (``pixel_size`` + ``unit``, whole image or ROIs) to v2."""
        params = dict(encoded or {})
        if from_version >= 2:
            return params
        pixel_size = float(params.pop("pixel_size", 1.0) or 1.0)
        unit = str(params.pop("unit", "px"))
        migrated = dict(self.default_params())
        migrated["source"] = "rois" if params.get("rois") else "full_image"
        migrated["pixel_size_nm"] = pixel_size * UNIT_TO_NM[unit] if unit in UNIT_TO_NM else 0.0
        migrated["average_psf"] = False
        if "rois" in params:
            migrated["rois"] = params["rois"]
        return migrated

    def output_spec(self, params: dict | None = None, input_specs=None) -> OutputSpec:
        merged = {**self.default_params(), **(params or {})}
        ports = ["beads", "summary"]
        if merged.get("average_psf"):
            ports.append("average_psf")
        if merged.get("fit_aberrations"):
            ports.append("aberrations")
        return OutputSpec(ports=tuple(ports))

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Any image with at least a (Y, X) plane; a Z axis makes it 3-D."""
        return lambda result: np.ndim(result.data) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return build_form(parent, self.param_spec())

    # ------------------------------------------------------------------ #
    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        p = {**self.default_params(), **(params or {})}
        source = str(p["source"])
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}; expected one of {SOURCES}")
        data, pixel_size, unit = resolve_input(result, p)
        bead_params = BeadPSFParams(
            pixel_size=pixel_size,
            unit=unit,
            bead_diameter_nm=float(p["bead_diameter_nm"]) or None,
            bead_labeling=str(p["bead_labeling"]),
            expected_fwhm_nm=(float(p["expected_fwhm_nm"]),) if float(p["expected_fwhm_nm"]) > 0 else None,
            na=float(p["na"]) or None,
            wavelength_nm=float(p["wavelength_nm"]) or None,
            refractive_index=float(p["refractive_index"]),
            detection_threshold=float(p["detection_threshold"]),
            isolation_factor=float(p["isolation_factor"]),
            fit_mode_3d=str(p["fit_mode_3d"]),
        )
        if source == "full_image":
            analysis = analyze_whole_image(data, bead_params)
        elif source == "rois":
            rois = p.get("rois") or []
            if not rois:
                raise ValueError("Source is 'ROI Manager' but no ROIs were given.")
            image = data.max(axis=0) if data.ndim == 3 else data
            analysis = analyze_beads(data, bead_params, candidates_yx=roi_candidates(image, rois))
        else:
            analysis = analyze_beads(data, bead_params)

        selection = selection_from_params(p)
        mask = select_beads(analysis, selection)
        averaged = average_psf(data, analysis, mask) if p["average_psf"] else None
        summary = summarize(analysis, mask, selection)

        aberration_fit = None
        if p["fit_aberrations"]:
            aberration_fit = self._aberrations(data, analysis, mask, p, summary["warnings"])

        recorded = {k: v for k, v in p.items() if k != "rois"}
        results: list[ProcessingResult] = [
            BeadTableResult(f"{result.name} (PSF beads)", analysis, mask, selection, recorded),
            PSFSummaryResult(
                f"{result.name} (PSF summary)", summary, averaged,
                focal_surface(analysis, mask), field_trend(analysis, mask), recorded,
            ),
        ]
        keys = ["beads", "summary"]
        if averaged is not None:
            results.append(_averaged_result(result.name, averaged, pixel_size, unit))
            keys.append("average_psf")
        if aberration_fit is not None:
            results.append(AberrationsResult(f"{result.name} (aberrations)", aberration_fit, recorded))
            keys.append("aberrations")
        return ProcessorOutput(results, keys=keys)

    @staticmethod
    def _aberrations(data, analysis, mask, p: dict, warnings: list[str]):
        from imswitch.improcess.analysis.psf_aberrations import fit_aberrations_from_analysis

        if analysis.ndim != 3:
            warnings.append("Aberrations need a 3-D stack; skipped.")
            return None
        if not analysis.params.physical:
            warnings.append("Aberrations need a calibrated pixel size and z step; skipped.")
            return None
        if not (analysis.params.na and analysis.params.wavelength_nm):
            warnings.append("Aberrations need NA and emission wavelength; skipped.")
            return None
        try:
            fit = fit_aberrations_from_analysis(
                data, analysis, mask, lateral_half_nm=float(p["aberration_crop_nm"]),
                z_flip=bool(p["z_flip"]),
            )
        except ValueError as exc:
            warnings.append(f"Aberrations skipped: {exc}")
            return None
        warnings.extend(fit.warnings)
        return fit


def resolve_input(result: ProcessingResult, params: dict) -> tuple[np.ndarray, tuple[float, ...], str]:
    """``(data, pixel_size, unit)`` for the analysis.

    A result with a ``Z`` axis of more than one plane (other than the last two
    axes) gives a ``(Z, Y, X)`` stack, every other non-spatial axis taken at
    index 0; anything else gives the first ``(Y, X)`` plane. The pixel size
    comes from ``axis_scales`` and ``scale_unit`` (converted to nm), unless
    the ``pixel_size_nm`` / ``z_step_nm`` overrides are set; uncalibrated
    data without overrides are analysed in px.
    """
    validate_axes(result)
    labels = list(result.axis_labels)
    raw = result.data
    ndim = np.ndim(raw)
    scales = list(getattr(result, "axis_scales", None) or [1.0] * ndim)
    factor = UNIT_TO_NM.get(str(getattr(result, "scale_unit", "px") or "px").strip().lower())

    z_index = labels.index("Z") if "Z" in labels else None
    use_3d = z_index is not None and z_index < ndim - 2 and np.shape(raw)[z_index] > 1
    if use_3d:
        index = tuple(slice(None) if k in (z_index, ndim - 2, ndim - 1) else 0 for k in range(ndim))
        data = np.asarray(raw[index])
    else:
        data = np.asarray(extract_2d_plane(result))

    lateral_override = float(params.get("pixel_size_nm", 0.0) or 0.0)
    z_override = float(params.get("z_step_nm", 0.0) or 0.0)
    if lateral_override > 0:
        lateral = (lateral_override, lateral_override)
    elif factor is not None:
        lateral = (float(scales[-2]) * factor, float(scales[-1]) * factor)
    else:
        lateral = None
    axial = None
    if use_3d:
        if z_override > 0:
            axial = z_override
        elif factor is not None:
            axial = float(scales[z_index]) * factor

    if lateral is not None and (not use_3d or axial is not None):
        pixel_size = ((axial,) if use_3d else ()) + lateral
        return data, tuple(float(v) for v in pixel_size), "nm"
    if lateral is not None and use_3d:
        raise ValueError("The lateral pixel size is set but the z step is unknown: set 'Z step'.")
    if use_3d and z_override > 0:
        raise ValueError("The z step is set but the lateral pixel size is unknown: set 'Pixel size'.")
    return data, (1.0,) * data.ndim, "px"


def _averaged_result(name: str, averaged, pixel_size, unit: str) -> ArrayProcessingResult:
    image = np.asarray(averaged.image, dtype=np.float32)
    labels = ["Z", "Y", "X"] if image.ndim == 3 else ["Y", "X"]
    scale = {"nm": 1e-3, "px": 1.0}[unit]  # published in um when calibrated, like the inputs
    return ArrayProcessingResult(
        name=f"{name} (averaged PSF, n={averaged.n})",
        data=image,
        axis_labels=labels,
        axis_scales=[float(v) * scale for v in pixel_size],
        scale_unit="um" if unit == "nm" else "px",
        display_levels=(float(np.nanmin(image)), float(np.nanmax(image))),
    )
