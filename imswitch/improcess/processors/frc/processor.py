"""Fourier ring correlation processor."""

from typing import Callable

from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.analysis.frc import frc_two_image, single_image_frc
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import shape_for_result
from imswitch.improcess.processors._extraction import extract_2d_plane, resolve_axis
from imswitch.improcess.processors.base import Processor

from .result import FRCResult


class FRCProcessor(Processor):
    """Compute two-image or single-image Fourier ring correlation."""

    name = "FRC Resolution"
    id = "frc"
    category = "Measurement"
    #: Two-image FRC compares either two planes of one result or one plane
    #: from each of two results — comparing two separate reconstructions is
    #: the common case, so the second input is optional rather than absent.
    min_inputs = 1
    max_inputs = 2

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
        pixel_size_spin.setRange(1e-9, 1e12)
        pixel_size_spin.setValue(1.0)
        layout.addRow("Pixel size:", pixel_size_spin)

        unit_combo = QtWidgets.QComboBox()
        unit_combo.addItems(["px", "nm", "um"])
        unit_combo.setCurrentText("px")
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
        pixel_size = float(params.get("pixel_size", 1.0))
        resolution_unit = params.get("resolution_unit", "px")
        frequency_unit = f"cycles/{resolution_unit}"
        window = params.get("window", "hann")

        if mode == "two-image":
            other = self._second_input(result, params)
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
