"""Fourier ring correlation processor."""

import math
from typing import Callable

from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.analysis.frc import frc_two_image, single_image_frc
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import axis_scales_for_result, shape_for_result
from imswitch.improcess.processors._extraction import extract_2d_plane, resolve_axis
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.processors.base import Processor

from .result import FRCResult

#: ``resolution_unit`` that takes the input's own unit (its ``scale_unit``).
AUTO_UNIT = "auto"
RESOLUTION_UNITS = (AUTO_UNIT, "px", "nm", "um")

_NM_PER_UNIT = {"nm": 1.0, "um": 1e3, "mm": 1e6, "m": 1e9}

_UNIT_ALIASES = {
    "": "px",
    "pixel": "px",
    "pixels": "px",
    "nanometer": "nm",
    "nanometers": "nm",
    "micrometer": "um",
    "micrometers": "um",
    "micron": "um",
    "microns": "um",
    "millimeter": "mm",
    "millimeters": "mm",
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
    #: 2: ``pixel_size`` 0 and ``resolution_unit`` ``"auto"`` (the new
    #: defaults) take the input's calibration; version 1 meant 1 px unless set.
    params_version = 2

    @classmethod
    def default_params(cls) -> dict:
        return {   'mode': 'single-image',
        'compare_axis': 'Auto',
        'index_a': 0,
        'index_b': 1,
        'single_image_split': 'checkerboard',
        'window': 'hann',
        'pixel_size': 0.0,
        'resolution_unit': AUTO_UNIT}

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
                help="0 uses the image's calibrated Y/X pixel size",
            ),
            ParamField(
                'resolution_unit', 'select', AUTO_UNIT, label='Resolution unit',
                help="auto reports in the image's own unit (px when it is uncalibrated)",
                options=RESOLUTION_UNITS,
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
        pixel_size_spin.setSpecialValueText("From image")
        pixel_size_spin.setValue(0.0)
        pixel_size_spin.setToolTip("0 uses the image's calibrated Y/X pixel size")
        layout.addRow("Pixel size:", pixel_size_spin)

        unit_combo = QtWidgets.QComboBox()
        unit_combo.addItems(list(RESOLUTION_UNITS))
        unit_combo.setCurrentText(AUTO_UNIT)
        unit_combo.setToolTip(
            "auto reports in the image's own unit (px when it is uncalibrated)"
        )
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

    def migrate_params(self, encoded: dict | None, from_version: int) -> dict:
        encoded = dict(encoded or {})
        if from_version < 2:
            # Version 1 defaulted to 1 px; a recording that left either key
            # out meant exactly that, not the image's calibration.
            encoded.setdefault("pixel_size", 1.0)
            encoded.setdefault("resolution_unit", "px")
        return encoded

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        mode = params.get("mode", "single-image")
        window = params.get("window", "hann")
        other = self._second_input(result, params) if mode == "two-image" else None
        inputs = [result] if other is None else [result, other]
        pixel_size, resolution_unit = _pixel_size_and_unit(inputs, params)
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


def _pixel_size_and_unit(results, params: dict) -> tuple[float, str]:
    """``(pixel_size, unit)`` the FRC of ``results`` is reported in.

    An explicit ``pixel_size`` and ``resolution_unit`` are used as given.
    ``pixel_size`` 0 takes the Y/X pixel size from the inputs' calibration,
    converted to ``resolution_unit``; ``resolution_unit`` ``"auto"`` takes the
    inputs' own unit. An uncalibrated input is measured in px, unless a
    physical unit was asked for, which then needs a pixel size.
    """
    pixel_size = float(params.get("pixel_size", 0.0) or 0.0)
    unit = _normalized_unit(params.get("resolution_unit", AUTO_UNIT))
    if pixel_size < 0 or not math.isfinite(pixel_size):
        raise ValueError(f"Pixel size must be 0 (from the image) or positive, got {pixel_size}")
    if pixel_size > 0:
        # A pixel size given with unit auto is in the image's own unit.
        return pixel_size, (_unit_of(results[0]) if unit == AUTO_UNIT else unit)
    if unit == "px":
        return 1.0, "px"

    image_size, image_unit = _shared_calibration(results)
    if unit == AUTO_UNIT:
        unit = image_unit
    if unit == image_unit:
        return image_size, unit
    name = _name_of(results[0])
    if image_unit == "px":
        raise ValueError(
            f"'{name}' has no physical pixel size (it is in px); set Pixel size "
            f"to report FRC in {unit}"
        )
    if image_unit not in _NM_PER_UNIT or unit not in _NM_PER_UNIT:
        raise ValueError(
            f"Cannot convert the pixel size of '{name}' from {image_unit!r} to "
            f"{unit!r}; set Pixel size or choose Resolution unit auto"
        )
    return image_size * _NM_PER_UNIT[image_unit] / _NM_PER_UNIT[unit], unit


def _shared_calibration(results) -> tuple[float, str]:
    """The one ``(pixel_size, unit)`` every input shares; FRC compares planes
    on one grid, so inputs calibrated differently are an error, not a guess."""
    first = _calibration(results[0])
    for other in results[1:]:
        size, unit = _calibration(other)
        if unit != first[1] or not math.isclose(size, first[0], rel_tol=1e-3):
            raise ValueError(
                f"FRC compares images on one grid: '{_name_of(results[0])}' has "
                f"{first[0]:g} {first[1]} pixels but '{_name_of(other)}' has "
                f"{size:g} {unit}; set Pixel size to compare them anyway"
            )
    return first


def _calibration(result: ProcessingResult) -> tuple[float, str]:
    """``(pixel_size, unit)`` of the Y/X plane FRC analyses: the last two
    axes, as :func:`extract_2d_plane` takes them. ``(1.0, "px")`` when the
    result is uncalibrated."""
    unit = _unit_of(result)
    if unit == "px":
        return 1.0, "px"
    scales = axis_scales_for_result(result)
    dy, dx = float(scales[-2]), float(scales[-1])
    if not (dy > 0 and dx > 0 and math.isfinite(dy) and math.isfinite(dx)):
        raise ValueError(
            f"'{_name_of(result)}' has a non-positive Y/X pixel size "
            f"({dy:g}, {dx:g} {unit}); set Pixel size to measure it"
        )
    if not math.isclose(dy, dx, rel_tol=1e-3):
        raise ValueError(
            f"FRC needs square pixels: '{_name_of(result)}' has {dy:g} x {dx:g} "
            f"{unit} pixels (Y x X); set Pixel size to measure it anyway"
        )
    return dx, unit


def _unit_of(result: ProcessingResult) -> str:
    """The result's spatial unit; px for an uncalibrated or 1-D result."""
    if len(axis_scales_for_result(result)) < 2:
        return "px"
    return _normalized_unit(getattr(result, "scale_unit", "px"))


def _normalized_unit(unit) -> str:
    text = str(unit if unit is not None else "px").strip().lower()
    text = text.replace("\u00b5", "u").replace("\u03bc", "u")
    return _UNIT_ALIASES.get(text, text)


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
