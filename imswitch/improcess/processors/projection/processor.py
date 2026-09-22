"""Generic projection processor."""

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.analysis.projections import axis_index_from_label, project_array
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import ProjectionResult


class ProjectionProcessor(Processor):
    """Project an ImProcess result along one axis."""

    name = "Projection"
    id = "projection"
    category = "Dimensions and channels"
    kinds = ("image", "composite")

    @classmethod
    def default_params(cls) -> dict:
        return {'axis': 'Auto', 'mode': 'max', 'start': None, 'stop': None}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        # A stack, as ImageJ's Z Project requires. Collapsing one of a 2D
        # image's two axes leaves a 1D profile that is no image at all: the
        # viewer cannot show it and the TIFF writer cannot store it. The
        # profile tools exist for that question. Gating here means a batch
        # over mixed results refuses the flat ones with a reason, instead of
        # producing a result nothing downstream can take.
        return lambda result: result.data.ndim >= 3

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", "T", "Z", "C", "D0", "D1", "D2"])
        layout.addRow("Axis:", axis_combo)

        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItems(["max", "mean", "sum", "median", "std"])
        layout.addRow("Mode:", mode_combo)

        # ImageJ's Z Project projects a slice range, not always the whole
        # axis. 1-based and inclusive here, as in the Crop/Substack dialog;
        # 0 means "from the start" / "to the end".
        first_spin = QtWidgets.QSpinBox()
        first_spin.setRange(0, 999999)
        first_spin.setSpecialValueText("first")
        first_spin.setToolTip("First slice to project (1-based); 'first' = 1")
        layout.addRow("First slice:", first_spin)

        last_spin = QtWidgets.QSpinBox()
        last_spin.setRange(0, 999999)
        last_spin.setSpecialValueText("last")
        last_spin.setToolTip("Last slice to project, inclusive; 'last' = end of axis")
        layout.addRow("Last slice:", last_spin)

        def get_values():
            first = first_spin.value()
            last = last_spin.value()
            return {
                "axis": axis_combo.currentText(),
                "mode": mode_combo.currentText(),
                "start": (first - 1) if first > 0 else None,
                "stop": last if last > 0 else None,
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        labels = list(result.axis_labels)
        requested_axis = params.get("axis", "Auto")
        if requested_axis == "Auto":
            axis = self._default_axis(result)
        else:
            axis = axis_index_from_label(str(requested_axis), labels, result.data.ndim)

        data, start, stop = self._slice_range(result.data, axis, params)
        analysis = project_array(
            data,
            axis=axis,
            mode=params.get("mode", "max"),
            axis_labels=labels,
            axis_scales=result.axis_scales,
        )
        name = f"{result.name} ({analysis.mode} {analysis.axis_label}-projection"
        if (start, stop) != (0, result.data.shape[axis]):
            # ImageJ's Z Project reports its slice range in the window title,
            # and so should this: two projections of one stack over different
            # ranges are otherwise named identically.
            name += f" {start + 1}-{stop}"
        name += ")"
        # Collapsing a non-spatial axis (Z, T) leaves every pixel where it was,
        # so the output shares the input's grid. Collapsing one of the two
        # *displayed* axes (an explicit "X" or "Y") does not: the result is
        # laid out on another grid, and an ROI from the source means nothing
        # on it.
        same_grid = axis < result.data.ndim - 2
        return ProjectionResult(
            name=name,
            analysis=analysis,
            scale_unit=result.scale_unit,
            params={**params, "start": start, "stop": stop},
        ).adopt_identity_from(result, same_grid=same_grid)

    @staticmethod
    def _slice_range(data, axis: int, params: dict):
        """Restrict the projected axis to ``[start, stop)`` before projecting.

        Zero-based, ``stop`` exclusive, matching ``stack-subset``. Omitted or
        out-of-range bounds clamp to the whole axis rather than raising: the
        range is a convenience, and a stale bound left over from a longer
        stack should not turn into an error.
        """
        size = int(data.shape[axis])
        start = params.get("start")
        stop = params.get("stop")
        start = 0 if start is None else max(0, min(int(start), size - 1))
        stop = size if stop is None else max(start + 1, min(int(stop), size))
        if (start, stop) == (0, size):
            return data, start, stop
        index = [slice(None)] * data.ndim
        index[axis] = slice(start, stop)
        return data[tuple(index)], start, stop

    @staticmethod
    def _default_axis(result: ProcessingResult) -> int:
        for label in ("Z", "T", "C"):
            if label in result.axis_labels and result.data.shape[result.axis_labels.index(label)] > 1:
                return result.axis_labels.index(label)
        if result.data.ndim > 2:
            return 0
        return result.data.ndim - 1
