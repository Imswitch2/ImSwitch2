"""Reusable presentation widgets for CGH session inspection."""

from __future__ import annotations

from typing import Any,Mapping

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore,QtWidgets

from ...engine.parameters.converters import METRIC_UNIT,SLM_UNIT
from ...engine.registry import TargetPresentation,TargetPresentationFieldKind


_MUTED_COLOR = "#888"
_WARNING_COLOR = "#a66a00"
_UNIFORMITY_COLOR = "#4C9AFF"
_STD_COLOR = "#FFAB00"
_EFFICIENCY_COLOR = "#36B37E"


def _metric_legend(items) -> QtWidgets.QWidget:
    """Return a compact external legend for metric plots."""
    widget = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(widget)
    layout.setContentsMargins(0,4,8,0)
    layout.setSpacing(6)
    for label,color,glyph in items:
        item = QtWidgets.QLabel(
            "<span style='color:%s; font-weight:700;'>%s</span>&nbsp;&nbsp;%s"
            % (color,glyph,label)
        )
        item.setTextFormat(QtCore.Qt.RichText)
        layout.addWidget(item)
    layout.addStretch(1)
    widget.setMinimumWidth(112)
    widget.setMaximumWidth(145)
    return widget


def format_target_summary(
    presentation: TargetPresentation | None,
    params: Mapping[str,Any],
    specs: Mapping[str,Any],
    *,
    unit_mode: str=SLM_UNIT,
    conversion_context: Any=None,
) -> str:
    """Format one target summary from registry-owned semantic metadata.

    The formatter deliberately knows nothing about concrete target classes or
    registry keys. Targets declare their title and summary fields through
    :class:`TargetPresentation`; Qt only renders those semantics and applies
    generic :class:`ParamSpec` conversion metadata when available.
    """
    if presentation is None:
        return "Target"

    parts = [presentation.title]
    for field in presentation.summary_fields:
        values = []
        value_specs = []
        for key in field.parameter_keys:
            if key not in params or key not in specs:
                values = []
                break
            spec = specs[key]
            value = params[key]
            display_unit = unit_mode
            if getattr(spec,"conversion_available",False):
                try:
                    value = spec.to_unit(value,display_unit,conversion_context)
                except Exception:
                    display_unit = SLM_UNIT
                    value = params[key]
            values.append(value)
            value_specs.append((spec,display_unit))
        if not values:
            continue

        if field.kind is TargetPresentationFieldKind.DIMENSIONS:
            parts.append("×".join(_format_number(value) for value in values))
            continue

        rendered = []
        for value,(spec,display_unit) in zip(values,value_specs):
            rendered.append(
                _format_param_value(value,spec,display_unit)
            )
        label = field.compact_label or field.label or field.key
        parts.append("%s %s" % (label,"×".join(rendered)))

    return " · ".join(parts)


def _format_param_value(value: Any,spec: Any,unit_mode: str) -> str:
    text = _format_number(value,_decimals_for(spec,unit_mode))
    if getattr(spec,"conversion_available",False):
        suffix = "µm" if unit_mode == METRIC_UNIT else "px"
        return "%s %s" % (text,suffix)
    unit = getattr(spec,"unit",None)
    return text if not unit else "%s %s" % (text,str(unit))


def _decimals_for(spec: Any,unit_mode: str) -> int | None:
    resolver = getattr(spec,"decimals_for_unit",None)
    if callable(resolver):
        return int(resolver(unit_mode))
    value = getattr(spec,"decimals",None)
    return None if value is None else int(value)


def _format_number(value: Any,decimals: int | None=None) -> str:
    if isinstance(value,(np.integer,int)) and not isinstance(value,bool):
        return str(int(value))
    if isinstance(value,(np.floating,float)):
        number = float(value)
        if decimals is not None:
            text = ("%%.%df" % decimals) % number
            return text.rstrip("0").rstrip(".") if "." in text else text
        if number.is_integer():
            return str(int(number))
        return "%.4g" % number
    return str(value)


class FeedbackTargetView(QtWidgets.QWidget):
    """Pan/zoom view of the effective or pending adapted intensity target."""

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self._positions_kxy: np.ndarray | None = None
        self._intensities: np.ndarray | None = None
        self._colormap = pg.colormap.get("viridis")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(4)

        header = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        header.addWidget(self.status_label)
        header.addStretch(1)
        self.reset_button = QtWidgets.QPushButton("Reset View")
        self.reset_button.setFixedHeight(22)
        self.reset_button.clicked.connect(self.reset_view)
        header.addWidget(self.reset_button)
        layout.addLayout(header)

        self.graphics = pg.GraphicsLayoutWidget(self)
        self.graphics.setMinimumHeight(300)
        self.view_box = self.graphics.addViewBox(row=0,col=0)
        self.view_box.setAspectLocked(True)
        self.view_box.setMouseMode(pg.ViewBox.PanMode)
        self.scatter_item = pg.ScatterPlotItem(
            size=7,
            pen=None,
        )
        self.view_box.addItem(self.scatter_item)
        layout.addWidget(self.graphics,1)
        self.clear()

    def set_target(
        self,
        positions_kxy: Any,
        intensities: Any,
        *,
        pending: bool=False,
    ) -> None:
        if positions_kxy is None or intensities is None:
            self.clear()
            return
        positions = np.asarray(positions_kxy,dtype=np.float64)
        values = np.asarray(intensities,dtype=np.float64)
        if positions.ndim != 2 or positions.shape[0] != 2:
            raise ValueError(
                "Target positions must have shape (2, N), got "
                f"{positions.shape}"
            )
        if values.ndim != 1:
            raise ValueError(
                "Target intensities must be one-dimensional, got "
                f"{values.shape}"
            )
        if values.shape != (positions.shape[1],):
            raise ValueError(
                "Target intensities must align with positions, got "
                f"{values.shape} for {positions.shape[1]} spots"
            )
        if (
            not np.all(np.isfinite(positions))
            or not np.all(np.isfinite(values))
        ):
            raise ValueError("Target scatter data contains non-finite values")
        if np.any(values < 0):
            raise ValueError("Target intensities cannot contain negative values")
        changed = (
            self._positions_kxy is None
            or self._intensities is None
            or self._positions_kxy.shape != positions.shape
            or self._intensities.shape != values.shape
            or not np.array_equal(self._positions_kxy,positions)
            or not np.array_equal(self._intensities,values)
        )
        if changed:
            self._positions_kxy = np.array(positions,dtype=np.float64,copy=True)
            self._intensities = np.array(values,dtype=np.float64,copy=True)
            self._set_scatter_data()
        self.status_label.setText(
            "Pending adaptation" if pending else "Current round target"
        )
        self.status_label.setStyleSheet(
            "color: %s;" % (_WARNING_COLOR if pending else _MUTED_COLOR)
        )
        self.reset_button.setEnabled(True)
        if changed:
            self.reset_view()

    def _set_scatter_data(self) -> None:
        positions = self._positions_kxy
        values = self._intensities
        if positions is None or values is None or values.size == 0:
            self.scatter_item.setData([],[])
            return
        maximum = float(np.max(values))
        normalized = (
            values / maximum
            if maximum > 0
            else np.zeros_like(values)
        )
        brushes = [
            pg.mkBrush(color)
            for color in self._colormap.map(normalized,mode="qcolor")
        ]
        self.scatter_item.setData(
            x=positions[0],
            y=positions[1],
            size=7,
            brush=brushes,
            pen=None,
        )

    def clear(self) -> None:
        self._positions_kxy = None
        self._intensities = None
        self.scatter_item.setData([],[])
        self.status_label.setText("Target data unavailable")
        self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.reset_button.setEnabled(False)

    def reset_view(self,*_args: Any) -> None:
        self.view_box.setRange(
            xRange=(-0.5,0.5),
            yRange=(-0.5,0.5),
            padding=0.0,
        )


class FeedbackMetricsHistoryView(QtWidgets.QWidget):
    """Compact measured uniformity/efficiency history over complete rounds."""

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(4)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        layout.addWidget(self.status_label)

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0,0,0,0)
        body.setSpacing(4)
        self.legend = _metric_legend((
            ("Efficiency",_EFFICIENCY_COLOR,"━"),
            ("Uniformity",_UNIFORMITY_COLOR,"━"),
        ))
        body.addWidget(self.legend,0,QtCore.Qt.AlignTop)

        self.plot = pg.PlotWidget(self)
        self.plot.setBackground(None)
        self.plot.showGrid(x=True,y=True,alpha=0.2)
        self.plot.setLabel("bottom","Round")
        self.plot.setLabel("left","Measured metric")
        self.plot.setYRange(0.0,1.0,padding=0.05)
        self.plot.setMinimumHeight(85)
        body.addWidget(self.plot,1)
        layout.addLayout(body,1)

    def set_rounds(self,rounds: Any) -> None:
        self.plot.clear()
        indices = []
        uniformity = []
        efficiency = []
        for round_record in tuple(rounds or ()):
            evaluation = getattr(round_record,"evaluation",None)
            analysis = (
                None if evaluation is None
                else getattr(evaluation,"intensity_analysis",None)
            )
            if analysis is None:
                continue
            indices.append(int(round_record.index))
            uniformity.append(float(analysis.uniformity))
            efficiency.append(float(analysis.efficiency))

        axis = self.plot.getAxis("bottom")
        if not indices:
            self.status_label.setText("No measured round metrics yet.")
            self.plot.setEnabled(False)
            axis.setTicks([[]])
            return
        self.status_label.setText("")
        self.plot.setEnabled(True)
        x = np.asarray(indices,dtype=float)
        uniformity_pen = pg.mkPen(_UNIFORMITY_COLOR,width=2)
        efficiency_pen = pg.mkPen(_EFFICIENCY_COLOR,width=2)
        self.plot.plot(
            x,np.asarray(uniformity),pen=uniformity_pen,
            symbol="o",symbolPen=uniformity_pen,symbolBrush=None,
        )
        self.plot.plot(
            x,np.asarray(efficiency),pen=efficiency_pen,
            symbol="t",symbolPen=efficiency_pen,symbolBrush=None,
        )

        ticks = [(float(index),str(index)) for index in indices]
        axis.setTicks([ticks])
        minimum = float(min(indices))
        maximum = float(max(indices))
        margin = 0.5 if minimum == maximum else max(0.35,0.08*(maximum-minimum))
        x_min = minimum - margin
        x_max = maximum + margin
        self.plot.setLimits(xMin=x_min,xMax=x_max)
        self.plot.setXRange(x_min,x_max,padding=0.0)


class CghPerformanceView(QtWidgets.QWidget):
    """Embedded plot of the CGH iteration metrics for one computed round."""

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(5)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0,0,0,0)
        body.setSpacing(4)
        self.legend = _metric_legend((
            ("Uniformity",_UNIFORMITY_COLOR,"━"),
            ("Normalized STD",_STD_COLOR,"┄"),
            ("Efficiency",_EFFICIENCY_COLOR,"━"),
        ))
        body.addWidget(self.legend,0,QtCore.Qt.AlignTop)

        self.plot = pg.PlotWidget(self)
        self.plot.setBackground(None)
        self.plot.showGrid(x=True,y=True,alpha=0.2)
        self.plot.setLabel("bottom","Iteration")
        self.plot.setLabel("left","Value")
        self.plot.setMinimumHeight(230)
        self.plot.setMaximumHeight(280)
        body.addWidget(self.plot,1)
        layout.addLayout(body,0)
        layout.addStretch(1)

    def set_result(self,result: Any) -> None:
        self.plot.clear()
        metrics = tuple(getattr(result,"metrics",()) or ()) if result is not None else ()
        if not metrics:
            self.status_label.setText(
                "No CGH iteration metrics are available for this round."
                if result is not None else "CGH has not been computed for this round."
            )
            self.plot.setEnabled(False)
            return

        self.status_label.setText("")
        self.plot.setEnabled(True)
        iterations = np.asarray([item.iteration for item in metrics],dtype=float)
        uniformity = np.asarray([item.uniformity for item in metrics],dtype=float)
        normalized_std = np.asarray(
            [item.normalized_std for item in metrics],dtype=float,
        )
        uniformity_pen = pg.mkPen(_UNIFORMITY_COLOR,width=2)
        std_pen = pg.mkPen(_STD_COLOR,width=2,style=QtCore.Qt.DashLine)
        efficiency_pen = pg.mkPen(_EFFICIENCY_COLOR,width=2)
        self.plot.plot(
            iterations,uniformity,pen=uniformity_pen,
            symbol="o",symbolPen=uniformity_pen,symbolBrush=None,
        )
        self.plot.plot(
            iterations,normalized_std,pen=std_pen,
            symbol="s",symbolPen=std_pen,symbolBrush=None,
        )
        efficiencies = [item.efficiency for item in metrics]
        if any(value is not None for value in efficiencies):
            values = np.asarray([
                np.nan if value is None else float(value)
                for value in efficiencies
            ])
            self.plot.plot(
                iterations,values,pen=efficiency_pen,
                symbol="t",symbolPen=efficiency_pen,symbolBrush=None,
            )
        self.plot.enableAutoRange()


class PropagationView(QtWidgets.QWidget):
    """Propagation image with compact controls in a right-hand sidebar."""

    sigSimulateRequested = QtCore.Signal(int)
    sigPadSizeChanged = QtCore.Signal(int)

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self._image: np.ndarray | None = None
        self._round_available = False

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(8)

        self.graphics = pg.GraphicsLayoutWidget(self)
        self.graphics.setMinimumHeight(440)
        self.view_box = self.graphics.addViewBox(row=0,col=0)
        self.view_box.setAspectLocked(True)
        self.view_box.setMouseMode(pg.ViewBox.PanMode)
        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.view_box.addItem(self.image_item)
        layout.addWidget(self.graphics,1)

        side = QtWidgets.QWidget(self)
        side.setFixedWidth(165)
        side_layout = QtWidgets.QVBoxLayout(side)
        side_layout.setContentsMargins(0,0,0,0)
        side_layout.setSpacing(6)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.status_label.setWordWrap(True)
        side_layout.addWidget(self.status_label)

        side_layout.addWidget(QtWidgets.QLabel("Pad size"))
        self.pad_size = QtWidgets.QSpinBox()
        self.pad_size.setRange(1,16384)
        self.pad_size.setValue(1024)
        self.pad_size.setSingleStep(256)
        self.pad_size.valueChanged.connect(
            lambda value:self.sigPadSizeChanged.emit(int(value))
        )
        side_layout.addWidget(self.pad_size)

        self.simulate_button = QtWidgets.QPushButton("Simulate")
        self.simulate_button.clicked.connect(
            lambda _checked=False:self.sigSimulateRequested.emit(
                int(self.pad_size.value())
            )
        )
        side_layout.addWidget(self.simulate_button)

        self.reset_button = QtWidgets.QPushButton("Reset View")
        self.reset_button.clicked.connect(self.reset_view)
        side_layout.addWidget(self.reset_button)
        side_layout.addStretch(1)
        layout.addWidget(side,0)

        self.set_round_available(False)

    def set_round_available(self,available: bool,reason: str="") -> None:
        self._round_available = bool(available)
        self.simulate_button.setEnabled(self._round_available)
        self.pad_size.setEnabled(self._round_available)
        if not self._round_available:
            self.clear_image()
            self.status_label.setText(
                str(reason or "CGH has not been computed for this round.")
            )
        elif self._image is None:
            self.status_label.setText("Not simulated for this round.")
            self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)

    def set_not_simulated(self) -> None:
        self.clear_image()
        self.status_label.setText("Not simulated for this round.")
        self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)

    def set_image(self,image: Any) -> None:
        array = np.asarray(image)
        if array.ndim != 2:
            raise ValueError(
                "Propagation image must be two-dimensional, got %s" % (array.shape,)
            )
        self._image = np.array(array,copy=True)
        self.image_item.setImage(self._image,autoLevels=True)
        self.status_label.setText("")
        self.status_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.reset_view()

    def clear_image(self) -> None:
        self._image = None
        self.image_item.clear()
        self.reset_button.setEnabled(False)

    def set_busy(self,busy: bool) -> None:
        busy = bool(busy)
        self.simulate_button.setEnabled(self._round_available and not busy)
        self.pad_size.setEnabled(self._round_available and not busy)
        if busy:
            self.status_label.setText("Simulating propagation...")

    def set_error(self,error: Any) -> None:
        self.set_busy(False)
        self.status_label.setText(str(error))
        self.status_label.setStyleSheet("color: %s;" % _WARNING_COLOR)

    def reset_view(self,*_args: Any) -> None:
        self.reset_button.setEnabled(self._image is not None)
        if self._image is not None:
            self.view_box.autoRange()


class InspectView(QtWidgets.QWidget):
    """First-pass CGH inspection view: performance beside propagation."""

    sigPropagationRequested = QtCore.Signal(int)

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal,self)
        splitter.setChildrenCollapsible(False)

        performance_box = QtWidgets.QGroupBox("CGH Performance")
        performance_layout = QtWidgets.QVBoxLayout(performance_box)
        self.performance_view = CghPerformanceView(performance_box)
        performance_layout.addWidget(self.performance_view,0)
        performance_layout.addStretch(1)
        splitter.addWidget(performance_box)

        propagation_box = QtWidgets.QGroupBox("Propagation")
        propagation_layout = QtWidgets.QVBoxLayout(propagation_box)
        self.propagation_view = PropagationView(propagation_box)
        self.propagation_view.sigSimulateRequested.connect(
            self.sigPropagationRequested.emit
        )
        propagation_layout.addWidget(self.propagation_view)
        splitter.addWidget(propagation_box)

        splitter.setStretchFactor(0,45)
        splitter.setStretchFactor(1,55)
        splitter.setSizes([620,760])
        layout.addWidget(splitter,1)

    def set_round(self,round_inspection: Any) -> None:
        result = None if round_inspection is None else round_inspection.result
        self.performance_view.set_result(result)
        reason = (
            "CGH has not been computed for this round."
            if round_inspection is None
            else str(
                getattr(round_inspection,"unavailable_reason",None)
                or "CGH has not been computed for this round."
            )
        )
        self.propagation_view.set_round_available(
            result is not None,reason=reason,
        )


class PositionCorrectionView(QtWidgets.QWidget):
    """Pan/zoom vector-field view of one sampled position correction.

    Spot colors encode the actual displacement magnitude in normalized kxy
    coordinates. Vector lengths may be magnified for visibility; magnification
    is presentation-only and never changes the represented correction values.
    """

    DEFAULT_VECTOR_SCALE = 10.0

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self._ideal_positions_kxy: np.ndarray | None = None
        self._displacement_kxy: np.ndarray | None = None
        self._colormap = pg.colormap.get("viridis")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(4)

        self.graphics = pg.GraphicsLayoutWidget(self)
        self.graphics.setMinimumHeight(300)

        self.view_box = self.graphics.addViewBox(row=0,col=0)
        self.view_box.setAspectLocked(True)
        self.view_box.setMouseMode(pg.ViewBox.PanMode)

        # Render all vectors in one graphics item. This stays responsive for
        # multi-thousand-focus targets, unlike one ArrowItem per focus.
        self.vector_item = pg.PlotCurveItem(
            pen=pg.mkPen(_MUTED_COLOR,width=1),
        )
        self.ideal_item = pg.ScatterPlotItem(size=7,pen=None)
        self.endpoint_item = pg.ScatterPlotItem(
            size=4,
            pen=None,
            brush=pg.mkBrush("#dddddd"),
        )

        self.view_box.addItem(self.vector_item)
        self.view_box.addItem(self.ideal_item)
        self.view_box.addItem(self.endpoint_item)
        layout.addWidget(self.graphics,1)

        # Bottom legend / presentation controls.
        legend = QtWidgets.QGridLayout()
        legend.setContentsMargins(0,2,0,0)
        legend.setHorizontalSpacing(6)
        legend.setVerticalSpacing(3)

        magnitude_title = QtWidgets.QLabel("Color = |Δk|")
        magnitude_title.setToolTip(
            "Color encodes the actual position-correction magnitude in "
            "normalized kxy coordinates."
        )
        legend.addWidget(magnitude_title,0,0)

        gradient = QtWidgets.QFrame()
        gradient.setFixedSize(78,10)
        gradient.setStyleSheet(
            "QFrame {"
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #440154, stop:0.5 #21918c, stop:1 #fde725);"
            "border: 1px solid rgba(160,160,160,90);"
            "}"
        )
        legend.addWidget(gradient,0,1)

        self.magnitude_range_label = QtWidgets.QLabel("0 → —")
        self.magnitude_range_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        legend.addWidget(self.magnitude_range_label,0,2)

        vector_legend = QtWidgets.QLabel(
            "Vector: ideal ●  →  ○ displayed endpoint"
        )
        vector_legend.setToolTip(
            "Vectors start at ideal target positions. The light endpoint is "
            "the corrected position after applying the display-only vector scale."
        )
        legend.addWidget(vector_legend,0,3)

        legend.addWidget(QtWidgets.QLabel("Vector scale:"),0,4)
        self.vector_scale = QtWidgets.QDoubleSpinBox()
        self.vector_scale.setRange(1.0,1000.0)
        self.vector_scale.setDecimals(1)
        self.vector_scale.setSingleStep(1.0)
        self.vector_scale.setValue(self.DEFAULT_VECTOR_SCALE)
        self.vector_scale.setSuffix("×")
        self.vector_scale.setToolTip(
            "Display-only magnification of correction vectors. "
            "Color and reported displacement magnitudes remain unscaled."
        )
        self.vector_scale.setFixedWidth(82)
        self.vector_scale.valueChanged.connect(self._update_plot_data)
        legend.addWidget(self.vector_scale,0,5)

        self.reset_button = QtWidgets.QPushButton("Reset View")
        self.reset_button.setFixedHeight(22)
        self.reset_button.clicked.connect(self.reset_view)
        legend.addWidget(self.reset_button,0,6)

        self.magnitude_label = QtWidgets.QLabel("")
        self.magnitude_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        legend.addWidget(self.magnitude_label,1,0,1,7)
        legend.setColumnStretch(3,1)
        layout.addLayout(legend)

        self.clear()

    def set_correction(
        self,
        correction: Any,
        *,
        active: bool | None=None,
    ) -> None:
        """Display one ``PositionCorrection``-like object.

        ``active`` is accepted for compatibility but workflow state is
        intentionally presented by the surrounding controls, not this view.
        """
        del active
        if correction is None:
            self.clear()
            return
        self.set_data(
            ideal_positions_kxy=getattr(
                correction,"ideal_positions_kxy",None,
            ),
            displacement_kxy=getattr(
                correction,"displacement_kxy",None,
            ),
        )

    def set_data(
        self,
        ideal_positions_kxy: Any,
        displacement_kxy: Any,
        *,
        active: bool | None=None,
    ) -> None:
        """Display sampled position-correction vectors in normalized kxy."""
        del active
        if ideal_positions_kxy is None or displacement_kxy is None:
            self.clear()
            return

        ideal = np.asarray(ideal_positions_kxy,dtype=np.float64)
        displacement = np.asarray(displacement_kxy,dtype=np.float64)

        if ideal.ndim != 2 or ideal.shape[0] != 2:
            raise ValueError(
                "Ideal positions must have shape (2, N), got %s"
                % (ideal.shape,)
            )
        if displacement.shape != ideal.shape:
            raise ValueError(
                "Position displacements must match ideal positions, got %s "
                "for %s" % (displacement.shape,ideal.shape)
            )
        if ideal.shape[1] == 0:
            raise ValueError(
                "Position correction must contain at least one spot"
            )
        if (
            not np.all(np.isfinite(ideal))
            or not np.all(np.isfinite(displacement))
        ):
            raise ValueError(
                "Position correction contains non-finite values"
            )

        changed = (
            self._ideal_positions_kxy is None
            or self._displacement_kxy is None
            or self._ideal_positions_kxy.shape != ideal.shape
            or self._displacement_kxy.shape != displacement.shape
            or not np.array_equal(self._ideal_positions_kxy,ideal)
            or not np.array_equal(self._displacement_kxy,displacement)
        )
        if changed:
            self._ideal_positions_kxy = np.array(
                ideal,dtype=np.float64,copy=True,
            )
            self._displacement_kxy = np.array(
                displacement,dtype=np.float64,copy=True,
            )
            self._update_plot_data()

        self.vector_scale.setEnabled(True)
        self.reset_button.setEnabled(True)
        if changed:
            self.reset_view()

    def clear(self) -> None:
        self._ideal_positions_kxy = None
        self._displacement_kxy = None
        self.vector_item.setData([],[])
        self.ideal_item.setData([],[])
        self.endpoint_item.setData([],[])
        self.magnitude_range_label.setText("0 → —")
        self.magnitude_label.setText("Correction data unavailable")
        self.vector_scale.setEnabled(False)
        self.reset_button.setEnabled(False)

    def reset_view(self,*_args: Any) -> None:
        """Restore the canonical normalized Fourier-coordinate FOV."""
        self.view_box.setRange(
            xRange=(-0.5,0.5),
            yRange=(-0.5,0.5),
            padding=0.0,
        )

    def _update_plot_data(self,*_args: Any) -> None:
        ideal = self._ideal_positions_kxy
        displacement = self._displacement_kxy
        if ideal is None or displacement is None:
            self.vector_item.setData([],[])
            self.ideal_item.setData([],[])
            self.endpoint_item.setData([],[])
            return

        magnitudes = np.linalg.norm(displacement,axis=0)
        maximum = float(np.max(magnitudes)) if magnitudes.size else 0.0
        normalized = (
            magnitudes / maximum
            if maximum > 0.0
            else np.zeros_like(magnitudes)
        )
        brushes = [
            pg.mkBrush(color)
            for color in self._colormap.map(normalized,mode="qcolor")
        ]

        scale = float(self.vector_scale.value())
        endpoints = ideal + scale * displacement
        count = ideal.shape[1]
        x = np.empty(2 * count,dtype=np.float64)
        y = np.empty(2 * count,dtype=np.float64)
        x[0::2],x[1::2] = ideal[0],endpoints[0]
        y[0::2],y[1::2] = ideal[1],endpoints[1]

        self.vector_item.setData(x=x,y=y,connect="pairs")
        self.ideal_item.setData(
            x=ideal[0],y=ideal[1],size=7,brush=brushes,pen=None,
        )
        self.endpoint_item.setData(
            x=endpoints[0],
            y=endpoints[1],
            size=4,
            brush=pg.mkBrush("#dddddd"),
            pen=None,
        )

        self.magnitude_range_label.setText(
            "0 → %s" % _format_number(maximum)
        )
        self.magnitude_label.setText(
            "%d spots · actual |Δk| median %s · max %s · vectors shown at %s×"
            % (
                magnitudes.size,
                _format_number(float(np.median(magnitudes))),
                _format_number(maximum),
                _format_number(scale),
            )
        )

__all__ = [
    "CghPerformanceView",
    "FeedbackMetricsHistoryView",
    "FeedbackTargetView",
    "InspectView",
    "PositionCorrectionView",
    "PropagationView",
    "format_target_summary",
]
