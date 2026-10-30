"""Fourier ring correlation processor."""

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.analysis.frc import frc_two_image, single_image_frc
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors._extraction import extract_2d_plane, resolve_axis
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.processors.base import Processor

from .result import FRCResult

#: Length units a result's ``scale_unit`` may carry, in nanometres.
_NM_PER_UNIT = {
    "nm": 1.0, "nanometer": 1.0, "nanometers": 1.0,
    "um": 1e3, "micrometer": 1e3, "micrometers": 1e3, "micron": 1e3, "microns": 1e3,
    "mm": 1e6, "millimeter": 1e6, "millimeters": 1e6,
}


class FRCProcessor(Processor):
    """Compute two-image or single-image Fourier ring correlation."""

    name = "FRC Resolution"
    id = "frc"
    output_kind = "curve"
    category = "Measurement"
    #: Two-image FRC compares either two planes of one result or one plane
    #: from each of two results — comparing two separate reconstructions is
    #: the common case, so the second input is optional rather than absent.
    min_inputs = 1
    max_inputs = 2

    @classmethod
    def default_params(cls) -> dict:
        return {   'mode': 'single-image',
        'compare_axis': 'Auto',
        'index_a': 0,
        'index_b': 1,
        'single_image_split': 'checkerboard',
        'window': 'hann',
        'pixel_size': 0.0,
        'resolution_unit': 'nm'}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                'mode', 'select', 'single-image', label='Mode',
                help='Two-image FRC compares two planes of one result, or one plane from each of two checked results',
                options=('single-image', 'two-image'),
            ),
            ParamField(
                'compare_axis', 'select', 'Auto', label='Compare axis',
                help='Axis the plane indices count along; ignored for an input that is already a single plane',
                options=('Auto', 'T', 'C', 'Z', 'D0', 'D1', 'D2'),
            ),
            ParamField('index_a', 'int', 0, label='Index A', min=0, max=999999),
            ParamField('index_b', 'int', 1, label='Index B', min=0, max=999999),
            ParamField(
                'single_image_split', 'select', 'checkerboard', label='Single-image split',
                options=('checkerboard', 'odd-even'),
            ),
            ParamField('window', 'select', 'hann', label='Window', options=('hann', 'none')),
            ParamField(
                'pixel_size', 'float', 0.0, label='Pixel size', min=0, decimals=6,
                help='In the resolution unit; 0 = from the metadata (pixels when the data has none)',
            ),
            ParamField(
                'resolution_unit', 'select', 'nm', label='Resolution unit',
                options=('nm', 'um', 'px'),
            ),
        )

    def __init__(self):
        self._logger = initLogger(self, tryInheritParent=False)

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Require at least a 2-D image. Single-image FRC works on one plane;
        two-image FRC needs a stack, which the ``apply`` path validates when it
        resolves a compare axis."""
        return lambda result: result.data.ndim >= 2

    def check_inputs(self, results) -> tuple[bool, str]:
        ok, reason = super().check_inputs(results)
        if not ok:
            return ok, reason
        results = list(results or [])
        if len(results) < 2:
            return True, ""
        first, second = _plane_shape(results[0]), _plane_shape(results[1])
        if first != second:
            return False, (
                f"FRC compares equal-sized images: '{_name_of(results[0])}' is "
                f"{first[0]}x{first[1]} but '{_name_of(results[1])}' is "
                f"{second[0]}x{second[1]}"
            )
        return True, ""

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItems(["single-image", "two-image"])
        mode_combo.setCurrentText("single-image")
        mode_combo.setToolTip(
            "Two-image FRC compares two planes of one result, or one plane "
            "from each of two checked results"
        )
        layout.addRow("Mode:", mode_combo)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", "T", "C", "Z", "D0", "D1", "D2"])
        axis_combo.setCurrentText("Auto")
        axis_combo.setToolTip(
            "Axis the plane indices count along; ignored for an input that "
            "is already a single plane"
        )
        layout.addRow("Compare axis:", axis_combo)

        index_a_spin = QtWidgets.QSpinBox()
        index_a_spin.setRange(0, 999999)
        index_a_spin.setValue(0)
        layout.addRow("Index A:", index_a_spin)

        index_b_spin = QtWidgets.QSpinBox()
        index_b_spin.setRange(0, 999999)
        index_b_spin.setValue(1)
        layout.addRow("Index B:", index_b_spin)

        split_combo = QtWidgets.QComboBox()
        split_combo.addItems(["checkerboard", "odd-even"])
        split_combo.setCurrentText("checkerboard")
        layout.addRow("Single-image split:", split_combo)

        window_combo = QtWidgets.QComboBox()
        window_combo.addItems(["hann", "none"])
        window_combo.setCurrentText("hann")
        layout.addRow("Window:", window_combo)

        pixel_size_spin = QtWidgets.QDoubleSpinBox()
        pixel_size_spin.setDecimals(6)
        pixel_size_spin.setRange(0.0, 1e12)
        pixel_size_spin.setSpecialValueText("from metadata")
        pixel_size_spin.setValue(0.0)
        pixel_size_spin.setToolTip(
            "In the resolution unit; 0 = from the metadata (pixels when the data has none)"
        )
        layout.addRow("Pixel size:", pixel_size_spin)

        unit_combo = QtWidgets.QComboBox()
        unit_combo.addItems(["nm", "um", "px"])
        unit_combo.setCurrentText("nm")
        layout.addRow("Resolution unit:", unit_combo)

        def get_values():
            return {
                "mode": mode_combo.currentText(),
                "compare_axis": axis_combo.currentText(),
                "index_a": index_a_spin.value(),
                "index_b": index_b_spin.value(),
                "single_image_split": split_combo.currentText(),
                "window": window_combo.currentText(),
                "pixel_size": pixel_size_spin.value(),
                "resolution_unit": unit_combo.currentText(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        mode = params.get("mode", "single-image")
        window = params.get("window", "hann")
        other = self._second_input(result, params) if mode == "two-image" else None
        pixel_size, resolution_unit, pixel_size_from = _pixel_size(
            [result] if other is None else [result, other],
            unit=str(params.get("resolution_unit", "nm") or "nm"),
            override=float(params.get("pixel_size", 0.0) or 0.0),
        )
        if pixel_size_from == "none" and params.get("resolution_unit", "nm") != "px":
            self._logger.warning(
                "'%s' has no physical pixel size; FRC resolution is in pixels "
                "(set Pixel size to calibrate it)", _name_of(result),
            )
        frequency_unit = f"cycles/{resolution_unit}"

        if mode == "two-image":
            if other is not None:
                # Two separate reconstructions: each contributes one plane, so
                # there is no shared axis to compare along. Each input is
                # reduced on its own — either may be a plain 2-D image.
                ok, reason = self.check_inputs([result, other])
                if not ok:
                    raise ValueError(reason)
                requested = params.get("compare_axis", "Auto")
                image_a = _plane_of(result, requested, int(params.get("index_a", 0)))
                image_b = _plane_of(other, requested, int(params.get("index_b", 0)))
                name_suffix = f"FRC vs {other.name}"
            else:
                axis_label = resolve_axis(
                    result,
                    ("T", "C", "Z"),
                    requested=params.get("compare_axis", "Auto"),
                    error_message="Two-image FRC needs an axis with at least two planes",
                )
                image_a = extract_2d_plane(
                    result, compare_axis=axis_label, index=int(params.get("index_a", 0))
                )
                image_b = extract_2d_plane(
                    result, compare_axis=axis_label, index=int(params.get("index_b", 1))
                )
                name_suffix = (
                    f"FRC {axis_label}{params.get('index_a', 0)}-"
                    f"{params.get('index_b', 1)}"
                )
            analysis = frc_two_image(
                image_a,
                image_b,
                pixel_size=pixel_size,
                frequency_unit=frequency_unit,
                resolution_unit=resolution_unit,
                window=window,
            )
            name = f"{result.name} ({name_suffix})"
        elif mode == "single-image":
            image = extract_2d_plane(result, index=int(params.get("index_a", 0)))
            analysis = single_image_frc(
                image,
                split=params.get("single_image_split", "checkerboard"),
                pixel_size=pixel_size,
                frequency_unit=frequency_unit,
                resolution_unit=resolution_unit,
                window=window,
            )
            name = f"{result.name} (single-image FRC)"
        else:
            raise ValueError(f"Unsupported FRC mode: {mode!r}")

        self._logger.info(
            "FRC result: cutoff=%s %s, resolution=%s %s",
            analysis.cutoff_frequency,
            analysis.frequency_unit,
            analysis.resolution,
            analysis.resolution_unit,
        )
        analysis.metadata["pixel_size_from"] = pixel_size_from
        return FRCResult(name=name, analysis=analysis, params=dict(params))

    @staticmethod
    def _second_input(result: ProcessingResult, params: dict):
        """Return the second FRC input, or ``None`` for the one-result form.

        ``params["results"]`` is the multi-input contract from
        :class:`~imswitch.improcess.processors.base.Processor`; a run that
        passes only the first result (or the same result twice) is the
        compare-along-an-axis case.
        """
        results = list(params.get("results", []) or [])
        if len(results) < 2:
            return None
        other = results[1]
        return None if other is result else other


def _plane_of(result: ProcessingResult, requested_axis, index: int):
    """One (Y, X) plane of ``result``, indexed along its stack axis if it has
    one. A plain 2-D reconstruction has a single plane and no axis to index,
    which must not be an error when the *other* input supplied the stack."""
    try:
        axis_label = resolve_axis(result, ("T", "C", "Z"), requested=requested_axis)
    except ValueError:
        return extract_2d_plane(result)
    return extract_2d_plane(result, compare_axis=axis_label, index=index)


def _pixel_size(results, *, unit: str, override: float) -> tuple[float, str, str]:
    """``(pixel size, unit, where it came from)`` for the FRC frequency axis.

    A set ``Pixel size`` wins, in the chosen unit. Otherwise the inputs'
    calibration is used, converted to that unit; data without one is
    analysed in pixels, and the unit says so rather than mislabelling them.
    Two calibrated inputs must agree: FRC rings assume one pixel grid.
    """
    if unit == "px":
        return (override or 1.0), "px", "parameter" if override else "none"
    if override > 0:
        return override, unit, "parameter"
    if unit not in _NM_PER_UNIT:
        raise ValueError(f"Unsupported FRC resolution unit: {unit!r}")
    calibrated = [(r, size) for r in results for size in [_calibrated_nm(r)] if size is not None]
    if not calibrated:
        return 1.0, "px", "none"
    first_result, first = calibrated[0]
    for other_result, size in calibrated[1:]:
        if not np.isclose(size, first, rtol=1e-6):
            raise ValueError(
                f"FRC compares images on one pixel grid: '{_name_of(first_result)}' has "
                f"{first:.6g} nm pixels but '{_name_of(other_result)}' has {size:.6g} nm"
            )
    return first / _NM_PER_UNIT[unit], unit, "metadata"


def _calibrated_nm(result: ProcessingResult) -> float | None:
    """The input's square Y/X pixel size in nm, or ``None`` when it has none."""
    unit = str(getattr(result, "scale_unit", "px") or "px").strip().lower()
    nm_per_unit = _NM_PER_UNIT.get(unit.replace("µ", "u").replace("μ", "u"))
    if nm_per_unit is None:
        return None
    labels = axis_labels_for_result(result)
    scales = axis_scales_for_result(result)
    if "Y" in labels and "X" in labels:
        dy, dx = scales[labels.index("Y")], scales[labels.index("X")]
    else:
        dy, dx = scales[-2], scales[-1]
    dy, dx = float(dy) * nm_per_unit, float(dx) * nm_per_unit
    if not (dy > 0 and dx > 0):
        return None
    if not np.isclose(dy, dx, rtol=1e-6):
        raise ValueError(
            f"'{_name_of(result)}' has non-square pixels ({dy:.6g} x {dx:.6g} nm) and "
            f"FRC needs square ones; set Pixel size to analyse it anyway"
        )
    return dx


def _name_of(result: ProcessingResult) -> str:
    return str(getattr(result, "name", "input"))


def _plane_shape(result: ProcessingResult) -> tuple[int, int]:
    """(Y, X) size of the plane FRC would analyse in ``result``.

    Reads the shape off the array without materializing it, so checking a
    lazily-backed result never pulls it from disk.
    """
    shape = shape_for_result(result)
    if len(shape) < 2:
        raise ValueError(f"'{_name_of(result)}' has no 2-D plane to compare")
    return int(shape[-2]), int(shape[-1])
