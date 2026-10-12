"""Reusable presentation widgets for CGH session inspection."""

from __future__ import annotations

from typing import Any,Mapping

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore,QtWidgets

from ...core.engine.parameters.converters import METRIC_UNIT,SLM_UNIT
from ...core.engine.registry import TargetPresentation,TargetPresentationFieldKind
from ...core.transform2d import (
    FittedTransform2D,TRANSFORM_AFFINE,TRANSFORM_POLYNOMIAL,fit_transform2d,
)


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


class PositionReferenceView(QtWidgets.QWidget):
    """Detector-space Current / Reference / Predicted alignment workbench."""

    sigCenterOffsetRequested = QtCore.Signal(float,float)
    # Compatibility signal retained for hosts that still listen for affine matrices.
    sigAffineTransformRequested = QtCore.Signal(object)
    sigManualTransformRequested = QtCore.Signal(object)

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self._preview = None
        self._reference_positions = None
        self._raw_reference_positions = None
        self._measured_positions = None
        self._global_positions = None
        self._fit_center = None
        self._reference_center = None
        self._updating_center = False
        self._interactive_editable = False
        self._interactive_model: str | None = None
        self._fitted_transform: FittedTransform2D | None = None

        self._affine_updating = False
        self._affine_source_anchors = None
        self._affine_source_corners = None
        self._affine_last_origin = None

        self._polynomial_updating = False
        self._polynomial_source_handles = None
        self._polynomial_grid_shape = None
        self._polynomial_degree = 2
        self._polynomial_handles: list[pg.TargetItem] = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(4)

        self.graphics = pg.GraphicsLayoutWidget(self)
        self.graphics.setMinimumHeight(300)
        self.view_box = self.graphics.addViewBox(row=0,col=0)
        self.view_box.setAspectLocked(True)
        self.view_box.invertY(True)
        self.view_box.setMouseMode(pg.ViewBox.PanMode)

        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.global_item = pg.ScatterPlotItem(
            symbol="+",size=10,pen=pg.mkPen("#aaaaaa",width=1.0),
        )
        self.measured_item = pg.ScatterPlotItem(
            symbol="x",size=9,pen=pg.mkPen("#55aa55",width=1.6),
        )
        # Historical names are retained internally: reference_item is Predicted;
        # raw_reference_item is the actual selected Reference geometry.
        self.reference_item = pg.ScatterPlotItem(
            symbol="+",size=12,pen=pg.mkPen("#55c7d9",width=1.8),
        )
        self.raw_reference_item = pg.ScatterPlotItem(
            symbol="o",size=10,pen=pg.mkPen("#e06cdb",width=1.3),brush=None,
        )
        self.vector_item = pg.PlotCurveItem(
            pen=pg.mkPen("#ff9966",width=1.1),
        )
        self.residual_item = pg.PlotCurveItem(
            pen=pg.mkPen("#cc6666",width=1.1),
        )
        self.center_item = pg.TargetItem(
            movable=True,size=16,pen=pg.mkPen("#e06cdb",width=1.4),
        )

        # Affine editor: a cage plus center/x-axis/y-axis handles. The center
        # translates the complete transform; the axis handles control the two
        # affine basis vectors without exposing matrix coefficients.
        self.affine_frame_item = pg.PlotCurveItem(
            pen=pg.mkPen("#ffcc66",width=1.5),
        )
        self.affine_origin_item = pg.TargetItem(
            movable=True,size=14,pen=pg.mkPen("#ffcc66",width=1.5),
        )
        self.affine_x_item = pg.TargetItem(
            movable=True,size=11,pen=pg.mkPen("#ffcc66",width=1.4),
        )
        self.affine_y_item = pg.TargetItem(
            movable=True,size=11,pen=pg.mkPen("#ffcc66",width=1.4),
        )
        self.polynomial_mesh_item = pg.PlotCurveItem(
            pen=pg.mkPen("#ffcc66",width=1.0),
        )

        for item in (
            self.image_item,self.global_item,self.vector_item,self.residual_item,
            self.measured_item,self.raw_reference_item,self.reference_item,
            self.affine_frame_item,self.polynomial_mesh_item,self.center_item,
            self.affine_origin_item,self.affine_x_item,self.affine_y_item,
        ):
            self.view_box.addItem(item)
        self.center_item.setVisible(False)
        for item in (
            self.affine_frame_item,self.affine_origin_item,self.affine_x_item,
            self.affine_y_item,self.polynomial_mesh_item,
        ):
            item.setVisible(False)

        self.center_item.sigPositionChanged.connect(self._on_center_position_changed)
        self.center_item.sigPositionChangeFinished.connect(
            self._on_center_position_finished
        )
        self.affine_origin_item.sigPositionChanged.connect(
            self._on_affine_origin_changed
        )
        self.affine_x_item.sigPositionChanged.connect(self._on_affine_axis_changed)
        self.affine_y_item.sigPositionChanged.connect(self._on_affine_axis_changed)
        for item in (
            self.affine_origin_item,self.affine_x_item,self.affine_y_item,
        ):
            item.sigPositionChangeFinished.connect(self._on_affine_edit_finished)
        layout.addWidget(self.graphics,1)

        overlay_row = QtWidgets.QHBoxLayout()
        overlay_row.setContentsMargins(0,2,0,0)
        overlay_row.setSpacing(8)
        self.current_checkbox = QtWidgets.QCheckBox("Current")
        self.reference_checkbox = QtWidgets.QCheckBox("Reference")
        self.predicted_checkbox = QtWidgets.QCheckBox("Predicted")
        self.correction_vectors_checkbox = QtWidgets.QCheckBox("Correction vectors")
        self.residuals_checkbox = QtWidgets.QCheckBox("Residuals")
        self.global_fit_checkbox = QtWidgets.QCheckBox("Global fit")
        for checkbox in (
            self.current_checkbox,self.reference_checkbox,self.predicted_checkbox,
        ):
            checkbox.setChecked(True)
        for checkbox in (
            self.correction_vectors_checkbox,self.residuals_checkbox,
            self.global_fit_checkbox,
        ):
            checkbox.setChecked(False)
        self.current_checkbox.setToolTip("Localized positions of the current CGH.")
        self.reference_checkbox.setToolTip(
            "Desired detector positions from the selected reference source."
        )
        self.predicted_checkbox.setToolTip(
            "Where Current is predicted to land after the selected correction."
        )
        self.correction_vectors_checkbox.setToolTip(
            "Current → Predicted correction vectors."
        )
        self.residuals_checkbox.setToolTip(
            "Predicted → Reference residual vectors."
        )
        self.global_fit_checkbox.setToolTip(
            "Original global lattice fit retained as a diagnostic overlay."
        )
        for checkbox in (
            self.current_checkbox,self.reference_checkbox,self.predicted_checkbox,
            self.correction_vectors_checkbox,self.residuals_checkbox,
            self.global_fit_checkbox,
        ):
            checkbox.toggled.connect(self._apply_overlay_visibility)
            overlay_row.addWidget(checkbox)
        overlay_row.addStretch(1)
        layout.addLayout(overlay_row)

        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(0,0,0,0)
        footer.setSpacing(8)
        self.legend_label = QtWidgets.QLabel(
            "Current ×   Reference ○   Predicted +"
        )
        self.legend_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        footer.addWidget(self.legend_label)
        footer.addStretch(1)
        self.summary_label = QtWidgets.QLabel("")
        self.summary_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        footer.addWidget(self.summary_label)
        self.reset_button = QtWidgets.QPushButton("Reset View")
        self.reset_button.setFixedHeight(22)
        self.reset_button.clicked.connect(self.view_box.autoRange)
        footer.addWidget(self.reset_button)
        layout.addLayout(footer)
        self.clear()

    def set_preview(
        self,preview: Mapping[str,Any] | None,*,center_editable: bool=False,
    ) -> None:
        if not preview:
            self.clear()
            return
        image = np.asarray(preview.get("cropped_image"),dtype=np.float64)
        measured = np.asarray(preview.get("measured_positions_px"),dtype=np.float64)
        global_positions = np.asarray(
            preview.get("global_positions_px"),dtype=np.float64,
        )
        predicted = np.asarray(
            preview.get("reference_positions_px"),dtype=np.float64,
        )
        reference = np.asarray(
            preview.get("raw_reference_positions_px",predicted),dtype=np.float64,
        )
        if image.ndim != 2:
            raise ValueError("Position-reference preview image must be two-dimensional")
        for name,array in (
            ("measured",measured),("global",global_positions),
            ("predicted",predicted),("reference",reference),
        ):
            if array.ndim != 2 or array.shape[0] != 2:
                raise ValueError("%s positions must have shape (2, N)" % name)
        if (
            measured.shape != predicted.shape
            or global_positions.shape != predicted.shape
            or reference.shape != predicted.shape
        ):
            raise ValueError("Position-reference preview point arrays must share shape")

        reset_view = (
            self._preview is None
            or self._measured_positions is None
            or self._measured_positions.shape != measured.shape
            or not np.array_equal(self._measured_positions,measured)
        )

        self._preview = dict(preview)
        self._measured_positions = np.array(measured,copy=True)
        self._global_positions = np.array(global_positions,copy=True)
        self._reference_positions = np.array(predicted,copy=True)
        self._raw_reference_positions = np.array(reference,copy=True)
        self._fit_center = np.asarray(preview.get("fit_center_px"),dtype=np.float64).reshape(2)
        self._reference_center = np.asarray(
            preview.get("reference_center_px"),dtype=np.float64,
        ).reshape(2)

        self.image_item.setImage(image,autoLevels=True)
        self.global_item.setData(x=global_positions[0],y=global_positions[1])
        self.measured_item.setData(x=measured[0],y=measured[1])
        self._set_reference_overlay(predicted)
        self.raw_reference_item.setData(x=reference[0],y=reference[1])
        self._set_residual_overlay(predicted,reference)

        self._fitted_transform = self._preview_transform(preview)
        self._prepare_interactive_editor()

        self._updating_center = True
        try:
            self.center_item.setPos(
                float(self._reference_center[0]),float(self._reference_center[1]),
            )
        finally:
            self._updating_center = False
        self.center_item.setVisible(bool(center_editable))
        self.center_item.movable = bool(center_editable)
        self._update_summary(predicted)
        self._apply_overlay_visibility()
        self._refresh_interactive_visibility()
        self.reset_button.setEnabled(True)
        if reset_view:
            self.view_box.autoRange()

    def _preview_transform(self,preview: Mapping[str,Any]) -> FittedTransform2D | None:
        payload=preview.get("fitted_transform")
        if payload:
            try:
                return FittedTransform2D.from_dict(payload)
            except (KeyError,TypeError,ValueError):
                pass
        affine=preview.get("affine_transform")
        if affine is None:
            return None
        try:
            return FittedTransform2D(model=TRANSFORM_AFFINE,matrix=affine)
        except (TypeError,ValueError):
            return None

    def set_center_editable(self,enabled: bool) -> None:
        visible = bool(enabled and self._preview is not None)
        self.center_item.setVisible(visible)
        self.center_item.movable = visible

    def set_interactive_editing(self,enabled: bool,model: str | None=None) -> None:
        self._interactive_editable=bool(enabled)
        self._interactive_model=None if model is None else str(model)
        self._refresh_interactive_visibility()

    def set_affine_editable(self,enabled: bool) -> None:
        """Compatibility alias for the Pass-1 affine-only API."""
        self.set_interactive_editing(enabled,TRANSFORM_AFFINE)

    def clear(self) -> None:
        self._preview = None
        self._reference_positions = None
        self._raw_reference_positions = None
        self._measured_positions = None
        self._global_positions = None
        self._fit_center = None
        self._reference_center = None
        self._fitted_transform = None
        self.image_item.clear()
        self.global_item.setData(x=[],y=[])
        self.measured_item.setData(x=[],y=[])
        self.reference_item.setData(x=[],y=[])
        self.raw_reference_item.setData(x=[],y=[])
        self.vector_item.setData([],[])
        self.residual_item.setData([],[])
        self.center_item.setVisible(False)
        self._hide_affine_overlay()
        self._clear_polynomial_overlay()
        self.summary_label.setText("Reference preview unavailable")
        self.reset_button.setEnabled(False)

    def _set_reference_overlay(self,predicted: np.ndarray) -> None:
        measured = self._measured_positions
        if measured is None:
            return
        self.reference_item.setData(x=predicted[0],y=predicted[1])
        count = predicted.shape[1]
        x = np.empty(2*count,dtype=np.float64)
        y = np.empty(2*count,dtype=np.float64)
        x[0::2],x[1::2] = measured[0],predicted[0]
        y[0::2],y[1::2] = measured[1],predicted[1]
        self.vector_item.setData(x=x,y=y,connect="pairs")

    def _set_residual_overlay(
        self,predicted: np.ndarray,reference: np.ndarray,
    ) -> None:
        count = predicted.shape[1]
        x = np.empty(2*count,dtype=np.float64)
        y = np.empty(2*count,dtype=np.float64)
        x[0::2],x[1::2] = predicted[0],reference[0]
        y[0::2],y[1::2] = predicted[1],reference[1]
        self.residual_item.setData(x=x,y=y,connect="pairs")

    def _update_summary(self,predicted: np.ndarray) -> None:
        measured=self._measured_positions
        reference=self._raw_reference_positions
        if measured is None or reference is None:
            return
        correction_magnitude=np.linalg.norm(predicted-measured,axis=0)
        residual_magnitude=np.linalg.norm(reference-predicted,axis=0)
        residual_rms=(
            float(np.sqrt(np.mean(residual_magnitude*residual_magnitude)))
            if residual_magnitude.size else 0.0
        )
        self.summary_label.setText(
            "%d spots · correction median %.3g px · residual RMS %.3g px" % (
                predicted.shape[1],
                float(np.median(correction_magnitude))
                if correction_magnitude.size else 0.0,
                residual_rms,
            )
        )

    def _apply_overlay_visibility(self,*_args: Any) -> None:
        self.measured_item.setVisible(self.current_checkbox.isChecked())
        self.raw_reference_item.setVisible(self.reference_checkbox.isChecked())
        self.reference_item.setVisible(self.predicted_checkbox.isChecked())
        self.vector_item.setVisible(self.correction_vectors_checkbox.isChecked())
        self.residual_item.setVisible(self.residuals_checkbox.isChecked())
        self.global_item.setVisible(self.global_fit_checkbox.isChecked())

    def _prepare_interactive_editor(self) -> None:
        transform=self._fitted_transform
        global_positions=self._global_positions
        if transform is None or global_positions is None:
            self._hide_affine_overlay()
            self._clear_polynomial_overlay()
            return
        if transform.model==TRANSFORM_AFFINE:
            self._clear_polynomial_overlay()
            self._set_affine_transform(global_positions,transform)
        elif transform.model==TRANSFORM_POLYNOMIAL:
            self._hide_affine_overlay()
            self._set_polynomial_transform(global_positions,transform)
        else:
            self._hide_affine_overlay()
            self._clear_polynomial_overlay()

    def _refresh_interactive_visibility(self) -> None:
        transform=self._fitted_transform
        model=None if transform is None else transform.model
        requested=self._interactive_model or model
        affine_visible=bool(
            self._interactive_editable and self._preview is not None
            and model==TRANSFORM_AFFINE and requested==TRANSFORM_AFFINE
        )
        self.affine_frame_item.setVisible(affine_visible)
        for item in (
            self.affine_origin_item,self.affine_x_item,self.affine_y_item,
        ):
            item.setVisible(affine_visible)
            item.movable=affine_visible
        polynomial_visible=bool(
            self._interactive_editable and self._preview is not None
            and model==TRANSFORM_POLYNOMIAL and requested==TRANSFORM_POLYNOMIAL
        )
        self.polynomial_mesh_item.setVisible(polynomial_visible)
        for item in self._polynomial_handles:
            item.setVisible(polynomial_visible)
            item.movable=polynomial_visible

    @staticmethod
    def _target_position(item: pg.TargetItem) -> np.ndarray:
        pos=item.pos()
        return np.asarray([float(pos.x()),float(pos.y())],dtype=np.float64)

    def _set_affine_transform(
        self,global_positions: np.ndarray,transform: FittedTransform2D,
    ) -> None:
        points=np.asarray(global_positions,dtype=np.float64)
        if points.shape[0]!=2 or transform.model!=TRANSFORM_AFFINE:
            self._hide_affine_overlay()
            return
        xmin,xmax=np.min(points[0]),np.max(points[0])
        ymin,ymax=np.min(points[1]),np.max(points[1])
        if abs(float(xmax-xmin))<1e-9 or abs(float(ymax-ymin))<1e-9:
            self._hide_affine_overlay()
            return
        center=np.asarray([(xmin+xmax)/2.0,(ymin+ymax)/2.0])
        source_anchors=np.column_stack([
            center,
            [xmax,center[1]],
            [center[0],ymax],
        ]).astype(np.float64)
        source_corners=np.asarray([
            [xmin,xmax,xmax,xmin],
            [ymin,ymin,ymax,ymax],
        ],dtype=np.float64)
        target_anchors=transform.apply(source_anchors)
        self._affine_source_anchors=source_anchors
        self._affine_source_corners=source_corners
        self._affine_updating=True
        try:
            self.affine_origin_item.setPos(*target_anchors[:,0])
            self.affine_x_item.setPos(*target_anchors[:,1])
            self.affine_y_item.setPos(*target_anchors[:,2])
            self._affine_last_origin=np.array(target_anchors[:,0],copy=True)
        finally:
            self._affine_updating=False
        self._update_affine_frame()

    def _hide_affine_overlay(self) -> None:
        self._affine_source_anchors=None
        self._affine_source_corners=None
        self._affine_last_origin=None
        self.affine_frame_item.setData([],[])
        for item in (
            self.affine_frame_item,self.affine_origin_item,self.affine_x_item,
            self.affine_y_item,
        ):
            item.setVisible(False)

    def _current_affine_transform(self) -> FittedTransform2D | None:
        source=self._affine_source_anchors
        if source is None:
            return None
        target=np.column_stack([
            self._target_position(self.affine_origin_item),
            self._target_position(self.affine_x_item),
            self._target_position(self.affine_y_item),
        ])
        design=np.column_stack([source.T,np.ones(3,dtype=np.float64)])
        try:
            coefficients=np.linalg.solve(design,target.T)
            return FittedTransform2D(
                model=TRANSFORM_AFFINE,matrix=coefficients.T,
            )
        except (np.linalg.LinAlgError,ValueError):
            return None

    def _update_affine_frame(self) -> None:
        transform=self._current_affine_transform()
        corners=self._affine_source_corners
        if transform is None or corners is None:
            self.affine_frame_item.setData([],[])
            return
        mapped=transform.apply(corners)
        closed=np.column_stack([mapped,mapped[:,0]])
        self.affine_frame_item.setData(x=closed[0],y=closed[1])

    def _on_affine_origin_changed(self,*_args: Any) -> None:
        if self._affine_updating:
            return
        origin=self._target_position(self.affine_origin_item)
        previous=self._affine_last_origin
        self._affine_last_origin=np.array(origin,copy=True)
        if previous is not None:
            delta=origin-previous
            self._affine_updating=True
            try:
                for item in (self.affine_x_item,self.affine_y_item):
                    current=self._target_position(item)+delta
                    item.setPos(float(current[0]),float(current[1]))
            finally:
                self._affine_updating=False
        self._update_affine_live_preview()

    def _on_affine_axis_changed(self,*_args: Any) -> None:
        if not self._affine_updating:
            self._update_affine_live_preview()

    def _update_affine_live_preview(self) -> None:
        self._update_affine_frame()
        transform=self._current_affine_transform()
        if transform is not None:
            self._update_transform_live_preview(transform)

    def _on_affine_edit_finished(self,*_args: Any) -> None:
        if self._affine_updating or not self._interactive_editable:
            return
        transform=self._current_affine_transform()
        if transform is None:
            return
        self.sigAffineTransformRequested.emit(np.asarray(transform.matrix).copy())
        self.sigManualTransformRequested.emit(transform.to_dict())

    def _set_polynomial_transform(
        self,global_positions: np.ndarray,transform: FittedTransform2D,
    ) -> None:
        points=np.asarray(global_positions,dtype=np.float64)
        if points.shape[0]!=2 or transform.model!=TRANSFORM_POLYNOMIAL:
            self._clear_polynomial_overlay()
            return
        xmin,xmax=np.min(points[0]),np.max(points[0])
        ymin,ymax=np.min(points[1]),np.max(points[1])
        if abs(float(xmax-xmin))<1e-9 or abs(float(ymax-ymin))<1e-9:
            self._clear_polynomial_overlay()
            return
        degree=int(transform.polynomial_degree or 2)
        grid_size=max(3,degree+1)
        xs=np.linspace(xmin,xmax,grid_size)
        ys=np.linspace(ymin,ymax,grid_size)
        xx,yy=np.meshgrid(xs,ys,indexing="xy")
        source=np.vstack([xx.ravel(),yy.ravel()])
        target=transform.apply(source)
        self._clear_polynomial_handles()
        self._polynomial_source_handles=source
        self._polynomial_grid_shape=(grid_size,grid_size)
        self._polynomial_degree=degree
        self._polynomial_updating=True
        try:
            for index in range(source.shape[1]):
                item=pg.TargetItem(
                    movable=True,size=9,pen=pg.mkPen("#ffcc66",width=1.2),
                )
                self.view_box.addItem(item)
                item.setPos(float(target[0,index]),float(target[1,index]))
                item.sigPositionChanged.connect(self._on_polynomial_handle_changed)
                item.sigPositionChangeFinished.connect(
                    self._on_polynomial_edit_finished
                )
                self._polynomial_handles.append(item)
        finally:
            self._polynomial_updating=False
        self._update_polynomial_mesh()

    def _clear_polynomial_handles(self) -> None:
        for item in self._polynomial_handles:
            self.view_box.removeItem(item)
        self._polynomial_handles=[]

    def _clear_polynomial_overlay(self) -> None:
        self._clear_polynomial_handles()
        self._polynomial_source_handles=None
        self._polynomial_grid_shape=None
        self.polynomial_mesh_item.setData([],[])
        self.polynomial_mesh_item.setVisible(False)

    def _polynomial_target_handles(self) -> np.ndarray | None:
        if not self._polynomial_handles:
            return None
        return np.column_stack([
            self._target_position(item) for item in self._polynomial_handles
        ])

    def _current_polynomial_transform(self) -> FittedTransform2D | None:
        source=self._polynomial_source_handles
        target=self._polynomial_target_handles()
        if source is None or target is None:
            return None
        try:
            return fit_transform2d(
                source,target-source,model=TRANSFORM_POLYNOMIAL,
                polynomial_degree=self._polynomial_degree,
            )
        except (TypeError,ValueError,np.linalg.LinAlgError):
            return None

    def _update_polynomial_mesh(self) -> None:
        target=self._polynomial_target_handles()
        shape=self._polynomial_grid_shape
        if target is None or shape is None:
            self.polynomial_mesh_item.setData([],[])
            return
        rows,cols=shape
        grid=target.reshape(2,rows,cols)
        segments=[]
        for row in range(rows):
            segments.append(grid[:,row,:])
        for col in range(cols):
            segments.append(grid[:,:,col])
        x=[]; y=[]
        for segment in segments:
            x.extend(segment[0].tolist()); x.append(np.nan)
            y.extend(segment[1].tolist()); y.append(np.nan)
        self.polynomial_mesh_item.setData(x=np.asarray(x),y=np.asarray(y))

    def _on_polynomial_handle_changed(self,*_args: Any) -> None:
        if self._polynomial_updating:
            return
        self._update_polynomial_mesh()
        transform=self._current_polynomial_transform()
        if transform is not None:
            self._update_transform_live_preview(transform)

    def _on_polynomial_edit_finished(self,*_args: Any) -> None:
        if self._polynomial_updating or not self._interactive_editable:
            return
        transform=self._current_polynomial_transform()
        if transform is not None:
            self.sigManualTransformRequested.emit(transform.to_dict())

    def _update_transform_live_preview(self,transform: FittedTransform2D) -> None:
        global_positions=self._global_positions
        measured=self._measured_positions
        if global_positions is None or measured is None:
            return
        predicted=measured+transform.displacement(global_positions)
        self._set_reference_overlay(predicted)
        reference=self._raw_reference_positions
        if reference is not None:
            self._set_residual_overlay(predicted,reference)
        self._update_summary(predicted)

    def _on_center_position_changed(self,*_args: Any) -> None:
        if self._updating_center or self._reference_positions is None:
            return
        pos=self.center_item.pos()
        center=np.asarray([float(pos.x()),float(pos.y())],dtype=np.float64)
        base_center=self._reference_center
        if base_center is None:
            return
        shifted=self._reference_positions+(center-base_center)[:,None]
        self._set_reference_overlay(shifted)

    def _on_center_position_finished(self,*_args: Any) -> None:
        if self._updating_center or self._fit_center is None:
            return
        pos=self.center_item.pos()
        self.sigCenterOffsetRequested.emit(
            float(pos.x())-float(self._fit_center[0]),
            float(pos.y())-float(self._fit_center[1]),
        )


class PositionCorrectionView(QtWidgets.QWidget):
    """Pan/zoom vector-field view of one sampled position correction.

    Spot colors encode the actual displacement magnitude in normalized kxy
    coordinates. Vector lengths may be magnified for visibility; magnification
    is presentation-only and never changes the represented correction values.
    """

    DEFAULT_VECTOR_SCALE = 2.0

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
            "Vector: baseline ●  →  ○ displayed endpoint"
        )
        vector_legend.setToolTip(
            "Vectors start at the current correction baseline. The light endpoint is "
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
                correction,"baseline_positions_kxy",
                getattr(correction,"ideal_positions_kxy",None),
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
    "PositionReferenceView",
    "PropagationView",
    "format_target_summary",
]
