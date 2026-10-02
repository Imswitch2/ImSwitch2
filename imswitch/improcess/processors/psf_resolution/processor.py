"""PSF / bead resolution processor.

Finds the beads in a 2-D image or 3-D stack (or takes one per ROI Manager
ROI), fits each with a Gaussian, selects the consistent ones and reports
their FWHM statistics. Optionally averages the selected beads into a mean
PSF and, for calibrated 3-D stacks, estimates Zernike aberrations from it.

The analysis lives in :mod:`imswitch.improcess.analysis.bead_psf` and
:mod:`imswitch.improcess.analysis.psf_aberrations`; this class only resolves
the input (axes, pixel size) and wraps the outputs. :func:`run_bead_analysis`
is the shared first half: the panel's preview runs exactly what a fit runs,
without publishing anything.

Output ports: ``beads`` (the selected beads), ``summary`` (their statistics,
the rejection counts and, when fitted, the aberration headline),
``average_psf`` (the mean bead image, when requested and at least one bead
qualifies), and with aberrations requested on a calibrated stack
``aberrations`` (Zernike table), ``aberration_fit`` (data | model stack) and
``wavefront`` (pupil phase map).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.bead_psf import (
    BEAD_LABELINGS,
    FIT_MODES_3D,
    BeadAnalysis,
    BeadPSFParams,
    Selection,
    analyze_beads,
    analyze_whole_image,
    average_psf,
    field_trend,
    focal_surface,
    rejection_reasons,
    roi_candidates,
    select_beads,
    summarize,
)
from imswitch.improcess.analysis.psf_aberrations import ILLUMINATION_CHOICES
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._extraction import extract_2d_plane, validate_axes
from imswitch.improcess.processors.base import OutputSpec, Processor, ProcessorOutput

from ._params import CALIBRATION_GROUP, SELECTION_FIELDS, build_form, selection_from_params
from .result import (
    AberrationsResult,
    BeadTableResult,
    PSFSummaryResult,
    aberration_fit_image,
    wavefront_image,
)

SOURCES = ("auto", "rois", "full_image")

#: Length units a result's ``scale_unit`` may carry, in nanometres.
UNIT_TO_NM = {"nm": 1.0, "um": 1000.0, "µm": 1000.0, "μm": 1000.0, "micron": 1000.0, "mm": 1e6}


@dataclass
class InputLayout:
    """What the analysis will see of a result, known without reading pixels."""

    shape: tuple[int, ...]
    labels: tuple[str, ...]
    is3d: bool
    pixel_size: tuple[float, ...] | None  # (z,) y, x in nm; None = uncalibrated
    from_metadata: bool
    note: str = ""
    #: A non-z stack axis searched on its maximum projection (``shape`` is
    #: then ``(planes, Y, X)``), or ``None``.
    stack_axis: int | None = None

    def describe(self) -> str:
        dims = " × ".join(str(n) for n in self.shape)
        if self.is3d:
            kind = f"3-D stack {dims} (Z, Y, X)"
        elif self.stack_axis is not None:
            kind = (f"stack {dims} ({self.labels[self.stack_axis]}, Y, X), beads found on its "
                    "maximum projection and fitted in their brightest plane")
        else:
            kind = f"2-D image {dims} (Y, X)"
        if self.pixel_size is None:
            calibration = "no calibration: widths in px"
        else:
            size = " × ".join(f"{v:.4g}" for v in self.pixel_size)
            origin = "from metadata" if self.from_metadata else "override"
            calibration = f"voxel {size} nm ({origin})" if self.is3d else f"pixel {size} nm ({origin})"
        text = f"{kind}; {calibration}."
        return f"{text} {self.note}" if self.note else text


@dataclass
class BeadRun:
    """One bead analysis on one result: the shared half of fit and preview."""

    data: np.ndarray
    analysis: BeadAnalysis
    selection: Selection
    mask: np.ndarray
    reasons: list[str]
    pixel_size: tuple[float, ...]
    unit: str
    layout: InputLayout

    def summary(self) -> dict:
        return summarize(self.analysis, self.mask, self.selection)


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
                            "image as a single PSF.", group="Beads"),
            ParamField("na", "float", 0.0, label="NA",
                       help="Numerical aperture of the detection; 0 = unknown (no diffraction-limit "
                            "comparison, no aberrations).", min=0.0, max=2.0, decimals=3, group="Optics"),
            ParamField("wavelength_nm", "float", 0.0, label="Emission wavelength",
                       help="0 = unknown.", min=0.0, max=2000.0, decimals=1, suffix="nm", group="Optics"),
            ParamField("refractive_index", "float", 1.515, label="Immersion index",
                       help="Refractive index of the immersion medium (oil 1.515, water 1.33, "
                            "silicone 1.40).", min=1.0, max=2.0, decimals=3, group="Optics"),
            ParamField("bead_diameter_nm", "float", 0.0, label="Bead diameter",
                       help="0 = no bead-size correction.", min=0.0, max=10000.0, decimals=1,
                       suffix="nm", group="Optics"),
            ParamField("average_psf", "bool", True, label="Average selected beads",
                       help="Also output the aligned mean bead and its widths.", group="Outputs"),
            ParamField("fit_aberrations", "bool", False, label="Estimate aberrations (3-D)",
                       help="Fit Zernike modes 5-11 to the averaged bead. Needs a calibrated "
                            "through-focus stack, NA and wavelength. Takes a few seconds.", group="Outputs"),
            ParamField("illumination", "select", "auto", label="Illumination", options=ILLUMINATION_CHOICES,
                       help="For the aberration model. 'Light sheet' fits the excitation sheet "
                            "(thickness and tilt, e.g. an oblique plane microscope) along with the "
                            "aberrations; a widefield model cannot describe a light-sheet PSF. 'Auto' "
                            "tries widefield and, if that fits poorly, the light sheet too.",
                       group="Outputs"),
            *SELECTION_FIELDS,
            ParamField("pixel_size_nm", "float", 0.0, label="Pixel size",
                       help="Overrides the lateral pixel size of the data; 0 = from the metadata.",
                       min=0.0, max=1e6, decimals=3, suffix="nm", group=CALIBRATION_GROUP),
            ParamField("z_step_nm", "float", 0.0, label="Z step",
                       help="Overrides the z step of a 3-D stack; 0 = from the metadata.",
                       min=0.0, max=1e6, decimals=3, suffix="nm", group=CALIBRATION_GROUP),
            ParamField("bead_labeling", "select", "volume", label="Bead labelling", options=BEAD_LABELINGS,
                       help="Volume-labelled beads: sigma_bead^2 = d^2/20; shell-labelled: d^2/12.",
                       advanced=True),
            ParamField("detection_threshold", "float", 5.0, label="Detection threshold",
                       help="Smoothed bead peak above the background, in units of the background "
                            "noise.", min=1.0, max=100.0, decimals=1, advanced=True),
            ParamField("isolation_factor", "float", 3.0, label="Isolation (x FWHM)",
                       help="Beads closer than this many expected FWHMs are flagged crowded.",
                       min=1.0, max=20.0, decimals=1, advanced=True),
            ParamField("expected_fwhm_nm", "float", 0.0, label="Expected lateral FWHM",
                       help="Detection scale; 0 = estimated from the beads.",
                       min=0.0, max=1e5, decimals=1, suffix="nm", advanced=True),
            ParamField("fit_mode_3d", "select", "separable", label="3-D fit", options=FIT_MODES_3D,
                       help="Separable fits the focal plane and the axial profile; full fits a "
                            "3-D Gaussian.", advanced=True),
            ParamField("zero_is_invalid", "bool", True, label="Zero = no data",
                       help="Treat solid regions of exact zeros (the padding of deskewed or "
                            "registered volumes) as having no data.", advanced=True),
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
            ports += ["aberrations", "aberration_fit", "wavefront"]
        return OutputSpec(ports=tuple(ports))

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Any image with at least a (Y, X) plane; a Z axis makes it 3-D."""
        return lambda result: np.ndim(result.data) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return build_form(parent, self.param_spec(), collapsed=("Selection", CALIBRATION_GROUP, "Advanced"))

    # ------------------------------------------------------------------ #
    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        p = {**self.default_params(), **(params or {})}
        run = run_bead_analysis(result, p)
        analysis, mask, selection = run.analysis, run.mask, run.selection
        averaged = average_psf(run.data, analysis, mask) if p["average_psf"] else None
        summary = run.summary()

        aberration_fit = None
        if p["fit_aberrations"]:
            aberration_fit = self._aberrations(run, p, summary)

        recorded = {k: v for k, v in p.items() if k != "rois"}
        results: list[ProcessingResult] = [
            BeadTableResult(f"{result.name} (PSF beads)", analysis, mask, selection, recorded),
            PSFSummaryResult(
                f"{result.name} (PSF summary)", summary, averaged,
                focal_surface(analysis, mask, notes=summary["notes"]), field_trend(analysis, mask), recorded,
                aberrations=aberration_fit, detection_fwhm=analysis.expected_fwhm(),
            ),
        ]
        keys = ["beads", "summary"]
        if averaged is not None:
            results.append(_averaged_result(result.name, averaged, run.pixel_size, run.unit))
            keys.append("average_psf")
        if aberration_fit is not None:
            results += [
                AberrationsResult(f"{result.name} (aberrations)", aberration_fit, recorded),
                aberration_fit_image(result.name, aberration_fit),
                wavefront_image(result.name, aberration_fit),
            ]
            keys += ["aberrations", "aberration_fit", "wavefront"]
        return ProcessorOutput(results, keys=keys)

    @staticmethod
    def _aberrations(run: BeadRun, p: dict, summary: dict):
        """The aberration fit, or ``None`` with ``summary["aberrations_skipped"]``
        saying why it was not estimated."""
        from imswitch.improcess.analysis.psf_aberrations import fit_aberrations_from_analysis

        missing = aberration_requirements(run.layout, p)
        if missing:
            summary["aberrations_skipped"] = missing
            return None
        try:
            return fit_aberrations_from_analysis(
                run.data, run.analysis, run.mask, lateral_half_nm=float(p["aberration_crop_nm"]),
                z_flip=bool(p["z_flip"]), illumination=str(p.get("illumination", "auto")),
            )
        except ValueError as exc:
            summary["aberrations_skipped"] = str(exc)
            return None


def aberration_requirements(layout: InputLayout, params: dict) -> str:
    """What an aberration fit on this input still needs (``""``: nothing).

    Known from the input's layout and the parameters alone, so the panel can
    say it before a fit runs.
    """
    if not layout.is3d:
        return "it needs a through-focus z-stack (an axis labelled Z with several planes)"
    if layout.pixel_size is None:
        return "it needs the pixel size and z step (from the metadata or the Calibration override)"
    missing = [name for key, name in (("na", "the NA"), ("wavelength_nm", "the emission wavelength"))
               if not float(params.get(key, 0.0) or 0.0) > 0]
    if missing:
        return f"set {' and '.join(missing)} under Optics"
    return ""


def bead_params(p: dict, pixel_size: tuple[float, ...], unit: str) -> BeadPSFParams:
    """The analysis settings of a parameter dict."""
    expected = float(p.get("expected_fwhm_nm", 0.0) or 0.0)
    return BeadPSFParams(
        pixel_size=pixel_size,
        unit=unit,
        bead_diameter_nm=float(p.get("bead_diameter_nm", 0.0) or 0.0) or None,
        bead_labeling=str(p.get("bead_labeling", "volume")),
        expected_fwhm_nm=(expected,) if expected > 0 else None,
        na=float(p.get("na", 0.0) or 0.0) or None,
        wavelength_nm=float(p.get("wavelength_nm", 0.0) or 0.0) or None,
        refractive_index=float(p.get("refractive_index", 1.515)),
        detection_threshold=float(p.get("detection_threshold", 5.0)),
        isolation_factor=float(p.get("isolation_factor", 3.0)),
        fit_mode_3d=str(p.get("fit_mode_3d", "separable")),
        zero_is_invalid=bool(p.get("zero_is_invalid", True)),
    )


def run_bead_analysis(result: ProcessingResult, params: dict) -> BeadRun:
    """Resolve the input, detect / fit the beads and select them.

    Shared by :meth:`PSFResolutionProcessor.apply` and the panel's preview,
    so what the preview shows is what a fit uses.
    """
    p = {**PSFResolutionProcessor.default_params(), **(params or {})}
    source = str(p["source"])
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; expected one of {SOURCES}")
    layout = input_layout(result, p)
    data, pixel_size, unit = resolve_input(result, p)
    settings = bead_params(p, pixel_size, unit)
    stack_2d = layout.stack_axis is not None
    if source == "full_image":
        if stack_2d:  # the brightest plane holds the PSF in focus
            plane = int(np.argmax(data.reshape(data.shape[0], -1).max(axis=1)))
            analysis = analyze_whole_image(data[plane], settings)
            for bead in analysis.beads:
                bead["plane"] = plane
        else:
            analysis = analyze_whole_image(data, settings)
    elif source == "rois":
        rois = p.get("rois") or []
        if not rois:
            raise ValueError("Source is 'ROI Manager' but no ROIs were given.")
        image = data.max(axis=0) if data.ndim == 3 else data
        analysis = analyze_beads(data, settings, candidates_yx=roi_candidates(image, rois), stack_2d=stack_2d)
    else:
        analysis = analyze_beads(data, settings, stack_2d=stack_2d)
    selection = selection_from_params(p)
    reasons = rejection_reasons(analysis, selection)
    mask = np.array([r == "" for r in reasons], dtype=bool)
    if layout.note:
        analysis.warnings.append(layout.note)
    return BeadRun(data, analysis, selection, mask, reasons, pixel_size, unit, layout)


def input_layout(result: ProcessingResult, params: dict) -> InputLayout:
    """Which part of ``result`` the analysis takes, and its calibration.

    A result with a ``Z`` axis of more than one plane (other than the last two
    axes) gives a ``(Z, Y, X)`` stack, every other non-spatial axis taken at
    index 0. Without one, a stack over any other axis (frames, time, an
    unlabelled axis; the one nearest the image plane if several) gives a
    ``(planes, Y, X)`` stack analysed in 2-D on its maximum projection, never
    only its first plane; a single plane is a 2-D image. The pixel size comes
    from ``axis_scales`` and ``scale_unit`` (converted to nm), unless the
    ``pixel_size_nm`` / ``z_step_nm`` overrides are set.
    """
    labels = [str(label) for label in result.axis_labels]
    shape = tuple(int(n) for n in np.shape(result.data))
    ndim = len(shape)
    scales = list(getattr(result, "axis_scales", None) or [1.0] * ndim)
    factor = UNIT_TO_NM.get(str(getattr(result, "scale_unit", "px") or "px").strip().lower())
    z_index = labels.index("Z") if "Z" in labels else None
    is3d = z_index is not None and z_index < ndim - 2 and shape[z_index] > 1
    plane = shape[-2:]
    stacked = [k for k in range(ndim - 2) if shape[k] > 1]
    stack_axis = stacked[-1] if stacked and not is3d else None
    lead = z_index if is3d else stack_axis
    out_shape = ((shape[lead],) if lead is not None else ()) + tuple(plane)

    notes = []
    others = [labels[k] for k in stacked if k != lead]
    if others:
        notes.append(f"Only index 0 of {', '.join(others)} is analysed.")
    if stack_axis is not None:
        notes.append(f"If {labels[stack_axis]} is a z-stack, label it Z (and set its scale) for a 3-D "
                     "analysis: axial widths and aberrations.")
    note = " ".join(notes)

    lateral_override = float(params.get("pixel_size_nm", 0.0) or 0.0)
    z_override = float(params.get("z_step_nm", 0.0) or 0.0)
    from_metadata = True
    if lateral_override > 0:
        lateral = (lateral_override, lateral_override)
        from_metadata = False
    elif factor is not None:
        lateral = (float(scales[-2]) * factor, float(scales[-1]) * factor)
    else:
        lateral = None
    axial = None
    if is3d:
        if z_override > 0:
            axial = z_override
            from_metadata = False
        elif factor is not None:
            axial = float(scales[z_index]) * factor
    if lateral is not None and (not is3d or axial is not None):
        pixel_size = ((axial,) if is3d else ()) + lateral
    else:
        pixel_size = None
    return InputLayout(out_shape, tuple(labels), is3d, pixel_size, from_metadata, note, stack_axis)


def resolve_input(result: ProcessingResult, params: dict) -> tuple[np.ndarray, tuple[float, ...], str]:
    """``(data, pixel_size, unit)`` for the analysis (see :func:`input_layout`);
    uncalibrated data without overrides are analysed in px."""
    validate_axes(result)
    layout = input_layout(result, params)
    labels = list(result.axis_labels)
    raw = result.data
    ndim = np.ndim(raw)
    lead = labels.index("Z") if layout.is3d else layout.stack_axis
    if lead is not None:
        index = tuple(slice(None) if k in (lead, ndim - 2, ndim - 1) else 0 for k in range(ndim))
        data = np.asarray(raw[index])
    else:
        data = np.asarray(extract_2d_plane(result))

    if layout.pixel_size is not None:
        return data, tuple(float(v) for v in layout.pixel_size), "nm"
    lateral_override = float(params.get("pixel_size_nm", 0.0) or 0.0)
    z_override = float(params.get("z_step_nm", 0.0) or 0.0)
    if layout.is3d and lateral_override > 0:
        raise ValueError("The lateral pixel size is set but the z step is unknown: set 'Z step'.")
    if layout.is3d and z_override > 0:
        raise ValueError("The z step is set but the lateral pixel size is unknown: set 'Pixel size'.")
    analysed = 2 if layout.stack_axis is not None else data.ndim
    return data, (1.0,) * analysed, "px"


def _averaged_result(name: str, averaged, pixel_size, unit: str) -> ArrayProcessingResult:
    image = np.asarray(averaged.image, dtype=np.float32)
    labels = ["Z", "Y", "X"] if image.ndim == 3 else ["Y", "X"]
    scale = {"nm": 1e-3, "px": 1.0}[unit]  # published in um when calibrated, like the inputs
    from imswitch.improcess.model.result import ViewMode

    view_modes = (
        [ViewMode("XY", (0, 1, 2)), ViewMode("XZ", (1, 0, 2)), ViewMode("YZ", (2, 0, 1))]
        if image.ndim == 3 else None
    )
    return ArrayProcessingResult(
        name=f"{name} (averaged PSF, n={averaged.n})",
        data=image,
        axis_labels=labels,
        view_modes=view_modes,
        axis_scales=[float(v) * scale for v in pixel_size],
        scale_unit="um" if unit == "nm" else "px",
        display_levels=(float(np.nanmin(image)), float(np.nanmax(image))),
    )
