from __future__ import annotations

from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtGui,QtWidgets

from ..measurement.localization_view import MeasurementLocalizationView


_ORIENTATION_ITEMS=(
    ("Identity","identity"),
    ("Flip horizontal","flip_horizontal"),
    ("Flip vertical","flip_vertical"),
    ("Rotate 180°","rotate_180"),
    ("Rotate 90° CW","rotate_90_cw"),
    ("Rotate 90° CCW","rotate_90_ccw"),
    ("Transpose","transpose"),
    ("Anti-transpose","anti_transpose"),
)


class GeometryOrientationPreview(QtWidgets.QWidget):
    """Simple asymmetric-grid preview for one discrete camera orientation."""

    def __init__(self,parent=None) -> None:
        super().__init__(parent)
        self._orientation="identity"
        self.setMinimumSize(420,300)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Expanding)

    def set_orientation(self,value: str | None) -> None:
        value=str(value or "identity")
        if value!=self._orientation:
            self._orientation=value; self.update()

    @staticmethod
    def _transform(x: float,y: float,orientation: str) -> tuple[float,float]:
        if orientation=="flip_horizontal": return -x,y
        if orientation=="flip_vertical": return x,-y
        if orientation=="rotate_180": return -x,-y
        if orientation=="rotate_90_cw": return y,-x
        if orientation=="rotate_90_ccw": return -y,x
        if orientation=="transpose": return y,x
        if orientation=="anti_transpose": return -y,-x
        return x,y

    def paintEvent(self,event) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        painter=QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing,True)
        rect=self.rect().adjusted(10,10,-10,-10)
        painter.fillRect(rect,self.palette().base())
        painter.setPen(QtGui.QPen(self.palette().mid().color(),1))
        painter.drawRect(rect)

        title_font=painter.font(); title_font.setBold(True); painter.setFont(title_font)
        painter.setPen(self.palette().text().color())
        painter.drawText(rect.adjusted(8,6,-8,-6),QtCore.Qt.AlignTop|QtCore.Qt.AlignHCenter,"Camera coordinates  →  Logical coordinates")
        painter.setFont(QtGui.QFont())

        top=rect.top()+42; h=max(120,rect.height()-58); gap=24
        w=max(120,(rect.width()-gap-24)//2)
        left=QtCore.QRectF(rect.left()+8,top,w,h)
        right=QtCore.QRectF(left.right()+gap,top,w,h)
        self._draw_grid(painter,left,"identity","Camera")
        self._draw_grid(painter,right,self._orientation,"Logical")
        painter.end()

    def _draw_grid(self,painter: QtGui.QPainter,rect: QtCore.QRectF,orientation: str,label: str) -> None:
        painter.setPen(QtGui.QPen(self.palette().mid().color(),1))
        painter.drawRoundedRect(rect,4,4)
        painter.setPen(self.palette().text().color())
        font=painter.font(); font.setBold(True); painter.setFont(font)
        painter.drawText(rect.adjusted(6,4,-6,-4),QtCore.Qt.AlignTop|QtCore.Qt.AlignLeft,label)
        font.setBold(False); painter.setFont(font)

        cx=rect.center().x(); cy=rect.center().y()+8
        scale=min(rect.width()*0.28,rect.height()*0.28)
        axis_pen=QtGui.QPen(self.palette().text().color(),1.4)
        painter.setPen(axis_pen)
        painter.drawLine(QtCore.QPointF(cx-scale*1.25,cy),QtCore.QPointF(cx+scale*1.25,cy))
        painter.drawLine(QtCore.QPointF(cx,cy+scale),QtCore.QPointF(cx,cy-scale))
        painter.drawText(QtCore.QPointF(cx+scale*1.08,cy-5),"X")
        painter.drawText(QtCore.QPointF(cx+5,cy-scale*0.92),"Y")

        # Asymmetric 3×2 lattice plus an off-centre marker makes every discrete
        # orientation visually distinguishable.
        points=[(-1.0,-0.55),(0.0,-0.55),(1.0,-0.55),(-1.0,0.55),(0.0,0.55),(1.0,0.55)]
        for index,(x,y) in enumerate(points):
            tx,ty=self._transform(x,y,orientation)
            px=cx+tx*scale; py=cy-ty*scale
            radius=6 if index==5 else 4
            painter.setBrush(self.palette().highlight() if index==5 else self.palette().text())
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawEllipse(QtCore.QPointF(px,py),radius,radius)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(self.palette().text().color())
        painter.drawText(rect.adjusted(6,4,-6,-4),QtCore.Qt.AlignBottom|QtCore.Qt.AlignHCenter,orientation.replace("_"," "))


class GeometryCalibrationDialog(QtWidgets.QDialog):
    """Reusable section × observation-plane geometry calibration UI."""

    sigObservationPlaneChanged=QtCore.Signal(object)  # compatibility; selection is host-owned now
    sigComputeRequested=QtCore.Signal(object)
    sigAcquireRequested=QtCore.Signal(str)
    sigLoadRequested=QtCore.Signal()
    sigLocalizationRequested=QtCore.Signal(object)
    sigAnalyzeRequested=QtCore.Signal()
    sigSaveRequested=QtCore.Signal()
    sigManualSaveRequested=QtCore.Signal(str)
    sigFinishRequested=QtCore.Signal()

    def __init__(
        self,*,section_key: str,observation_planes: Sequence[str],
        current_observation_plane: str | None,detectors: Sequence[str],
        current_detector: str | None,localization_parameters: Mapping[str,Any] | None=None,
        section_title: str | None=None,
        parent=None,
        embedded: bool=False,
    ) -> None:
        super().__init__(parent)
        self._embedded=bool(embedded)
        self._observation_plane_name=str(current_observation_plane or "").strip() or None
        if self._embedded:self.setWindowFlags(QtCore.Qt.Widget)
        self.section_key=str(section_key)
        self.section_title=str(section_title or self.section_key)
        self.setWindowTitle("Geometry calibration — %s" % self.section_title)
        self.resize(1180,780)
        root=QtWidgets.QVBoxLayout(self); root.setContentsMargins(8,8,8,8); root.setSpacing(6)

        method_row=QtWidgets.QHBoxLayout(); method_row.setSpacing(8)
        method_row.addWidget(QtWidgets.QLabel("Method:"))
        self.mode_combo=QtWidgets.QComboBox()
        self.mode_combo.addItem("Manual","manual")
        self.mode_combo.addItem("CGH target","cgh_target")
        method_row.addWidget(self.mode_combo); method_row.addStretch(1)
        root.addLayout(method_row)

        self.error_label=QtWidgets.QLabel(""); self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b00020;"); self.error_label.hide(); root.addWidget(self.error_label)

        self.mode_stack=QtWidgets.QStackedWidget(); root.addWidget(self.mode_stack,1)

        # Manual mode: large orientation visualization left, compact controls right.
        manual_page=QtWidgets.QWidget(); manual_root=QtWidgets.QVBoxLayout(manual_page); manual_root.setContentsMargins(0,0,0,0)
        self.manual_splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal); self.manual_splitter.setChildrenCollapsible(False)
        manual_root.addWidget(self.manual_splitter,1)
        self.manual_preview=GeometryOrientationPreview(); self.manual_splitter.addWidget(self.manual_preview)
        manual_controls=QtWidgets.QWidget(); manual_controls_layout=QtWidgets.QVBoxLayout(manual_controls)
        manual_controls_layout.setContentsMargins(8,4,8,4); manual_controls_layout.setSpacing(8)
        heading=QtWidgets.QLabel("Manual geometry"); font=heading.font(); font.setBold(True); heading.setFont(font)
        manual_controls_layout.addWidget(heading)
        self.manual_orientation_combo=QtWidgets.QComboBox()
        for label,value in _ORIENTATION_ITEMS:self.manual_orientation_combo.addItem(label,value)
        manual_controls_layout.addWidget(QtWidgets.QLabel("Orientation")); manual_controls_layout.addWidget(self.manual_orientation_combo)
        self.manual_save_button=QtWidgets.QPushButton("Save geometry")
        self.manual_save_button.clicked.connect(
            lambda:self.sigManualSaveRequested.emit(str(self.manual_orientation_combo.currentData() or "identity"))
        )
        manual_controls_layout.addWidget(self.manual_save_button); manual_controls_layout.addStretch(1)
        self.manual_splitter.addWidget(manual_controls)
        self.manual_splitter.setStretchFactor(0,7); self.manual_splitter.setStretchFactor(1,3); self.manual_splitter.setSizes([700,300])
        self.mode_stack.addWidget(manual_page)

        # CGH-target mode: localization workbench left, compact target/analysis right.
        cgh_page=QtWidgets.QWidget(); cgh_root=QtWidgets.QVBoxLayout(cgh_page); cgh_root.setContentsMargins(0,0,0,0)
        self.cgh_splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal); self.cgh_splitter.setChildrenCollapsible(False)
        cgh_root.addWidget(self.cgh_splitter,1)
        self.measurement_view=MeasurementLocalizationView(
            parameters=dict(localization_parameters or {}),detectors=detectors,
            current_detector=current_detector,show_metrics=False,
            show_infer_missing=False,show_accept=False,parent=cgh_page,
        )
        self.measurement_view.sigAcquireRequested.connect(self.sigAcquireRequested.emit)
        self.measurement_view.sigLoadRequested.connect(self.sigLoadRequested.emit)
        self.measurement_view.sigRunRequested.connect(self.sigLocalizationRequested.emit)
        self.cgh_splitter.addWidget(self.measurement_view)

        right_scroll=QtWidgets.QScrollArea(); right_scroll.setWidgetResizable(True); right_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        right=QtWidgets.QWidget(); side=QtWidgets.QVBoxLayout(right); side.setContentsMargins(8,4,8,4); side.setSpacing(8)
        target_heading=QtWidgets.QLabel("CGH target"); font=target_heading.font(); font.setBold(True); target_heading.setFont(font); side.addWidget(target_heading)
        form=QtWidgets.QGridLayout(); form.setHorizontalSpacing(6); form.setVerticalSpacing(4)
        form.addWidget(QtWidgets.QLabel("Grid"),0,0)
        self.grid_x=QtWidgets.QSpinBox(); self.grid_x.setRange(3,31); self.grid_x.setValue(5); form.addWidget(self.grid_x,0,1)
        form.addWidget(QtWidgets.QLabel("×"),0,2)
        self.grid_y=QtWidgets.QSpinBox(); self.grid_y.setRange(3,31); self.grid_y.setValue(4); form.addWidget(self.grid_y,0,3)
        self.period_x_label=QtWidgets.QLabel("Period X (µm)"); form.addWidget(self.period_x_label,1,0)
        self.period_x=QtWidgets.QDoubleSpinBox(); self.period_x.setRange(0.0001,1e6); self.period_x.setDecimals(4); form.addWidget(self.period_x,1,1)
        self.period_y_label=QtWidgets.QLabel("Y"); form.addWidget(self.period_y_label,1,2)
        self.period_y=QtWidgets.QDoubleSpinBox(); self.period_y.setRange(0.0001,1e6); self.period_y.setDecimals(4); form.addWidget(self.period_y,1,3)
        side.addLayout(form)
        target_buttons=QtWidgets.QHBoxLayout()
        self.compute_button=QtWidgets.QPushButton("Compute target"); self.compute_button.clicked.connect(self._compute); target_buttons.addWidget(self.compute_button)
        self.finish_button=QtWidgets.QPushButton("Restore CGH"); self.finish_button.clicked.connect(self.sigFinishRequested.emit); target_buttons.addWidget(self.finish_button)
        side.addLayout(target_buttons)
        self.target_label=QtWidgets.QLabel("Compute a calibration target to begin."); self.target_label.setWordWrap(True); side.addWidget(self.target_label)

        separator=QtWidgets.QFrame(); separator.setFrameShape(QtWidgets.QFrame.HLine); separator.setFrameShadow(QtWidgets.QFrame.Sunken); side.addWidget(separator)
        analysis_heading=QtWidgets.QLabel("Analysis"); font=analysis_heading.font(); font.setBold(True); analysis_heading.setFont(font); side.addWidget(analysis_heading)
        self.analysis_label=QtWidgets.QLabel("Acquire/load and localize the calibration target first."); self.analysis_label.setWordWrap(True); side.addWidget(self.analysis_label)
        self.analysis_preview=GeometryOrientationPreview(); self.analysis_preview.setMinimumSize(260,220); side.addWidget(self.analysis_preview,1)
        analysis_buttons=QtWidgets.QHBoxLayout()
        self.analyze_button=QtWidgets.QPushButton("Analyze"); self.analyze_button.clicked.connect(self.sigAnalyzeRequested.emit); analysis_buttons.addWidget(self.analyze_button)
        self.save_button=QtWidgets.QPushButton("Validate && Save"); self.save_button.clicked.connect(self.sigSaveRequested.emit); analysis_buttons.addWidget(self.save_button)
        side.addLayout(analysis_buttons); side.addStretch(1)
        right_scroll.setWidget(right); self.cgh_splitter.addWidget(right_scroll)
        self.cgh_splitter.setStretchFactor(0,7); self.cgh_splitter.setStretchFactor(1,3); self.cgh_splitter.setSizes([700,300])
        self.mode_stack.addWidget(cgh_page)

        if not self._embedded:
            close=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close); close.rejected.connect(self.reject); root.addWidget(close)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.manual_orientation_combo.currentIndexChanged.connect(self._manual_orientation_changed)
        self.configure_observation_planes(observation_planes,current_observation_plane)
        self._mode_changed(); self._manual_orientation_changed()

    def configure_observation_planes(self,names: Sequence[str],current: str | None) -> None:
        # Observation selection belongs to the Calibration workspace's left side.
        # Keep only the authoritative context here so existing coordinators can
        # refresh the workflow without duplicating the selector in this dialog.
        current=str(current or "").strip() or None
        if current is not None and current not in tuple(str(name) for name in names):current=None
        self._observation_plane_name=current

    def configure_detectors(self,names: Sequence[str],current: str | None) -> None:
        self.measurement_view.configure_detectors(names,current)

    @property
    def observation_plane_name(self) -> str | None:
        return self._observation_plane_name

    @property
    def calibration_mode(self) -> str:
        return str(self.mode_combo.currentData() or "manual")

    def _mode_changed(self,*_args) -> None:
        self.mode_stack.setCurrentIndex(0 if self.calibration_mode=="manual" else 1)
        self.error_label.hide()

    def _manual_orientation_changed(self,*_args) -> None:
        self.manual_preview.set_orientation(str(self.manual_orientation_combo.currentData() or "identity"))

    def _compute(self) -> None:
        metric=bool(self.period_x_label.text().endswith("(µm)"))
        self.sigComputeRequested.emit({
            "grid_x":self.grid_x.value(),"grid_y":self.grid_y.value(),
            "period_x":self.period_x.value(),"period_y":self.period_y.value(),
            "period_unit":"um" if metric else "reference_px",
            "detector":self.measurement_view.current_detector or "",
        })

    def set_context(self,context: Mapping[str,Any]) -> None:
        calibration=context.get("calibration")
        if calibration is not None:
            index=self.manual_orientation_combo.findData(calibration.orientation.value)
            if index>=0:self.manual_orientation_combo.setCurrentIndex(index)
        self.grid_x.setValue(int(context.get("grid_x",5))); self.grid_y.setValue(int(context.get("grid_y",4)))
        metric=bool(context.get("metric_periods_available",False))
        self.period_x_label.setText("Period X (µm)" if metric else "Period X (reference px)")
        self.period_y_label.setText("Y (µm)" if metric else "Y (reference px)")
        x=context.get("period_x_um" if metric else "period_x_px"); y=context.get("period_y_um" if metric else "period_y_px")
        if x is not None:self.period_x.setValue(float(x))
        if y is not None:self.period_y.setValue(float(y))
        computed=bool(context.get("computed")); frame_active=bool(context.get("frame_active"))
        self.finish_button.setEnabled(bool(context.get("any_frame_active")))
        self.target_label.setText(
            "Target displayed. Acquire/load an image, then run localization." if frame_active else
            ("Target was computed but is no longer displayed. Compute it again before acquisition." if computed else "Compute a calibration target to begin.")
        )
        self.measurement_view.set_context(context.get("localization_context") or {})
        params=context.get("localization_parameters") or {}
        if params:self.measurement_view.set_parameters(params,invalidate_candidate=False)
        measurement=context.get("measurement")
        if measurement is None:
            self.measurement_view.clear_measurement(); self.measurement_view.clear_localization_result()
        elif self.measurement_view.measurement is not measurement:self.measurement_view.set_measurement(measurement)
        localization=context.get("localization")
        if localization is None:self.measurement_view.clear_localization_result()
        else:self.measurement_view.set_result(localization,params)
        analysis=context.get("analysis")
        if analysis is None:
            self.analysis_label.setText("Acquire/load and localize the calibration target first." if localization is None else "Localization ready. Click Analyze.")
            orientation=getattr(calibration,"orientation",None)
        else:
            self.analysis_label.setText(
                "Orientation: %s\nScore %.3g · margin %.3g · matched %d%s" % (
                    analysis.orientation.value,analysis.score,analysis.confidence_margin,
                    analysis.matched_count," · accepted" if analysis.accepted else " · ambiguous",
                )
            )
            orientation=analysis.orientation
        self.analysis_preview.set_orientation(getattr(orientation,"value",orientation) or "identity")
        self.analyze_button.setEnabled(bool(context.get("analyze_enabled")))
        self.save_button.setEnabled(bool(context.get("save_enabled")))
        self.manual_save_button.setEnabled(bool(context.get("observation_plane_name")))

    def set_error(self,error: Any) -> None:
        message=str(error or "Geometry calibration failed.")
        self.error_label.setText(message); self.error_label.show()
        if self.calibration_mode=="cgh_target":self.measurement_view.set_error(error)


__all__=["GeometryCalibrationDialog","GeometryOrientationPreview"]
