from __future__ import annotations

from typing import Any,Sequence

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore,QtWidgets

from ...core.cgh.localization import spot_ordering_label


_MUTED_COLOR="#888"
_WARNING_COLOR="#a66a00"


def _same_points(left: Any,right: Any) -> bool:
    if left is None or right is None:
        return left is right
    a=np.asarray(left,dtype=np.float64)
    b=np.asarray(right,dtype=np.float64)
    return a.shape==b.shape and np.array_equal(a,b)


class ObservationRegistrationAlignmentView(QtWidgets.QWidget):
    """Pan/zoom comparison of transformed calibration samples and their target."""

    def __init__(self,parent=None) -> None:
        super().__init__(parent)
        self._diagnostics=None
        root=QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(4)

        controls=QtWidgets.QHBoxLayout()
        controls.setContentsMargins(0,0,0,0)
        controls.addWidget(QtWidgets.QLabel("View:"))
        self.direction_combo=QtWidgets.QComboBox()
        self.direction_combo.addItem("Native → Canonical","forward")
        self.direction_combo.addItem("Canonical → Native","reverse")
        controls.addWidget(self.direction_combo)
        controls.addStretch(1)
        self.reset_button=QtWidgets.QPushButton("Reset View")
        self.reset_button.setFixedHeight(22)
        self.reset_button.clicked.connect(self.reset_view)
        controls.addWidget(self.reset_button)
        root.addLayout(controls)

        self.graphics=pg.GraphicsLayoutWidget(self)
        self.graphics.setMinimumHeight(360)
        self.view_box=self.graphics.addViewBox(row=0,col=0)
        self.view_box.setAspectLocked(True)
        self.view_box.invertY(True)
        self.view_box.setMouseMode(pg.ViewBox.PanMode)

        # Match PositionReferenceView conventions: reference/target is a magenta
        # open circle, transformed/predicted source is a cyan plus, residuals red.
        self.target_item=pg.ScatterPlotItem(
            symbol="o",size=10,pen=pg.mkPen("#e06cdb",width=1.3),brush=None,
        )
        self.transformed_item=pg.ScatterPlotItem(
            symbol="+",size=12,pen=pg.mkPen("#55c7d9",width=1.8),
        )
        self.residual_item=pg.PlotCurveItem(
            pen=pg.mkPen("#cc6666",width=1.1),
        )
        for item in (self.residual_item,self.target_item,self.transformed_item):
            self.view_box.addItem(item)
        root.addWidget(self.graphics,1)

        overlays=QtWidgets.QHBoxLayout()
        overlays.setContentsMargins(0,2,0,0)
        overlays.setSpacing(8)
        self.target_checkbox=QtWidgets.QCheckBox("○  Canonical")
        self.transformed_checkbox=QtWidgets.QCheckBox("+  Transformed native")
        self.residual_checkbox=QtWidgets.QCheckBox("—  Residuals")
        self.target_checkbox.setStyleSheet("color: #e06cdb;")
        self.transformed_checkbox.setStyleSheet("color: #55c7d9;")
        self.residual_checkbox.setStyleSheet("color: #cc6666;")
        self.target_checkbox.setChecked(True)
        self.transformed_checkbox.setChecked(True)
        self.residual_checkbox.setChecked(False)
        for checkbox in (
            self.target_checkbox,self.transformed_checkbox,self.residual_checkbox,
        ):
            checkbox.toggled.connect(self._apply_visibility)
            overlays.addWidget(checkbox)
        overlays.addStretch(1)
        root.addLayout(overlays)

        self.summary_label=QtWidgets.QLabel("Fit a registration to inspect alignment.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.summary_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        root.addWidget(self.summary_label)

        self.direction_combo.currentIndexChanged.connect(self._direction_changed)
        self._update_overlay_labels()
        self.reset_button.setEnabled(False)

    def clear(self) -> None:
        self._diagnostics=None
        self.target_item.setData(x=[],y=[])
        self.transformed_item.setData(x=[],y=[])
        self.residual_item.setData([],[])
        self.summary_label.setText("Fit a registration to inspect alignment.")
        self.reset_button.setEnabled(False)

    def set_diagnostics(self,diagnostics: Any) -> None:
        previous=self._diagnostics
        reset=(
            previous is None
            or not _same_points(
                getattr(previous,"native_points_px",None),
                getattr(diagnostics,"native_points_px",None),
            )
            or not _same_points(
                getattr(previous,"canonical_points_px",None),
                getattr(diagnostics,"canonical_points_px",None),
            )
        )
        self._diagnostics=diagnostics
        self.reset_button.setEnabled(diagnostics is not None)
        self._refresh(reset_view=reset)

    @staticmethod
    def _residual_curve(source: np.ndarray,target: np.ndarray) -> tuple[np.ndarray,np.ndarray]:
        count=int(source.shape[1])
        if count<=0:
            return np.empty(0),np.empty(0)
        x=np.empty(count*3,dtype=np.float64)
        y=np.empty(count*3,dtype=np.float64)
        x[0::3]=source[0]; x[1::3]=target[0]; x[2::3]=np.nan
        y[0::3]=source[1]; y[1::3]=target[1]; y[2::3]=np.nan
        return x,y

    def _direction_changed(self,*_args) -> None:
        # A direction change switches coordinate frames, so reset once. Routine
        # timer/context refreshes keep the user's current pan/zoom unchanged.
        self._refresh(reset_view=True)

    def reset_view(self,*_args) -> None:
        if self._diagnostics is not None:
            self.view_box.autoRange()

    def _refresh(self,*_args,reset_view: bool=False) -> None:
        self._update_overlay_labels()
        diagnostics=self._diagnostics
        if diagnostics is None:
            return
        direction=str(self.direction_combo.currentData() or "forward")
        if direction=="reverse":
            target=np.asarray(diagnostics.native_points_px,dtype=np.float64)
            transformed=np.asarray(diagnostics.transformed_canonical_points_px,dtype=np.float64)
            rms=float(diagnostics.reverse_rms_residual_px)
            maximum=float(diagnostics.reverse_max_residual_px)
            label="Canonical → Native"
        else:
            target=np.asarray(diagnostics.canonical_points_px,dtype=np.float64)
            transformed=np.asarray(diagnostics.transformed_native_points_px,dtype=np.float64)
            rms=float(diagnostics.forward_rms_residual_px)
            maximum=float(diagnostics.forward_max_residual_px)
            label="Native → Canonical"
        self.target_item.setData(x=target[0],y=target[1])
        self.transformed_item.setData(x=transformed[0],y=transformed[1])
        x,y=self._residual_curve(transformed,target)
        self.residual_item.setData(x,y,connect="finite")
        self.summary_label.setText(
            "%s · %d points · RMS %.4g px · max %.4g px"
            % (label,target.shape[1],rms,maximum)
        )
        self._apply_visibility()
        if reset_view:
            self.reset_view()

    def _update_overlay_labels(self) -> None:
        reverse=str(self.direction_combo.currentData() or "forward")=="reverse"
        if reverse:
            self.target_checkbox.setText("○  Native")
            self.transformed_checkbox.setText("+  Transformed canonical")
            self.target_checkbox.setToolTip("Measured native calibration points.")
            self.transformed_checkbox.setToolTip(
                "Canonical reference points transformed into the native detector frame."
            )
            self.residual_checkbox.setToolTip("Transformed canonical → native residual vectors.")
        else:
            self.target_checkbox.setText("○  Canonical")
            self.transformed_checkbox.setText("+  Transformed native")
            self.target_checkbox.setToolTip("Canonical reference calibration points.")
            self.transformed_checkbox.setToolTip(
                "Native localization points transformed into the canonical detector frame."
            )
            self.residual_checkbox.setToolTip("Transformed native → canonical residual vectors.")

    def _apply_visibility(self,*_args) -> None:
        self.target_item.setVisible(self.target_checkbox.isChecked())
        self.transformed_item.setVisible(self.transformed_checkbox.isChecked())
        self.residual_item.setVisible(self.residual_checkbox.isChecked())


class ObservationRegistrationFieldView(QtWidgets.QWidget):
    """FOV-style sampled view of the continuous observation transform in pixels."""

    DEFAULT_VECTOR_SCALE=2.0

    def __init__(self,parent=None) -> None:
        super().__init__(parent)
        self._diagnostics=None
        self._colormap=pg.colormap.get("viridis")
        root=QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(4)

        controls=QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("View:"))
        self.direction_combo=QtWidgets.QComboBox()
        self.direction_combo.addItem("Native → Canonical","forward")
        self.direction_combo.addItem("Canonical → Native","reverse")
        controls.addWidget(self.direction_combo)
        controls.addStretch(1)
        controls.addWidget(QtWidgets.QLabel("Vector scale:"))
        self.vector_scale=QtWidgets.QDoubleSpinBox()
        self.vector_scale.setRange(1.0,1000.0)
        self.vector_scale.setDecimals(1)
        self.vector_scale.setSingleStep(1.0)
        self.vector_scale.setValue(self.DEFAULT_VECTOR_SCALE)
        self.vector_scale.setSuffix("×")
        self.vector_scale.setFixedWidth(82)
        self.vector_scale.setToolTip(
            "Display-only magnification of registration vectors. Reported pixel "
            "displacements remain unscaled."
        )
        controls.addWidget(self.vector_scale)
        self.reset_button=QtWidgets.QPushButton("Reset View")
        self.reset_button.setFixedHeight(22)
        self.reset_button.clicked.connect(self.reset_view)
        controls.addWidget(self.reset_button)
        root.addLayout(controls)

        self.graphics=pg.GraphicsLayoutWidget(self)
        self.graphics.setMinimumHeight(360)
        self.view_box=self.graphics.addViewBox(row=0,col=0)
        self.view_box.setAspectLocked(True)
        self.view_box.invertY(True)
        self.view_box.setMouseMode(pg.ViewBox.PanMode)
        self.vector_item=pg.PlotCurveItem(pen=pg.mkPen(_MUTED_COLOR,width=1))
        self.source_item=pg.ScatterPlotItem(size=7,pen=None)
        self.endpoint_item=pg.ScatterPlotItem(size=4,pen=None,brush=pg.mkBrush("#dddddd"))
        for item in (self.vector_item,self.source_item,self.endpoint_item):
            self.view_box.addItem(item)
        root.addWidget(self.graphics,1)

        legend=QtWidgets.QHBoxLayout()
        legend.addWidget(QtWidgets.QLabel("Color = |Δpx|"))
        gradient=QtWidgets.QFrame()
        gradient.setFixedSize(78,10)
        gradient.setStyleSheet(
            "QFrame {background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #440154, stop:0.5 #21918c, stop:1 #fde725);"
            "border: 1px solid rgba(160,160,160,90);}"
        )
        legend.addWidget(gradient)
        self.range_label=QtWidgets.QLabel("0 → —")
        self.range_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        legend.addWidget(self.range_label)
        legend.addStretch(1)
        root.addLayout(legend)
        self.summary_label=QtWidgets.QLabel("Fit a registration to inspect its transform field.")
        self.summary_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.summary_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        root.addWidget(self.summary_label)

        self.direction_combo.currentIndexChanged.connect(self._direction_changed)
        self.vector_scale.valueChanged.connect(self._refresh)
        self.reset_button.setEnabled(False)

    def clear(self) -> None:
        self._diagnostics=None
        self.vector_item.setData([],[])
        self.source_item.setData(x=[],y=[])
        self.endpoint_item.setData(x=[],y=[])
        self.range_label.setText("0 → —")
        self.summary_label.setText("Fit a registration to inspect its transform field.")
        self.reset_button.setEnabled(False)
        self.vector_scale.setEnabled(False)

    def set_diagnostics(self,diagnostics: Any) -> None:
        previous=self._diagnostics
        reset=(
            previous is None
            or not _same_points(
                getattr(previous,"native_points_px",None),
                getattr(diagnostics,"native_points_px",None),
            )
            or not _same_points(
                getattr(previous,"canonical_points_px",None),
                getattr(diagnostics,"canonical_points_px",None),
            )
        )
        self._diagnostics=diagnostics
        self.reset_button.setEnabled(diagnostics is not None)
        self.vector_scale.setEnabled(diagnostics is not None)
        self._refresh(reset_view=reset)

    def _direction_changed(self,*_args) -> None:
        self._refresh(reset_view=True)

    def reset_view(self,*_args) -> None:
        if self._diagnostics is not None:
            self.view_box.autoRange()

    def _refresh(self,*_args,reset_view: bool=False) -> None:
        diagnostics=self._diagnostics
        if diagnostics is None:
            return
        reverse=str(self.direction_combo.currentData() or "forward")=="reverse"
        if reverse:
            source=np.asarray(diagnostics.canonical_points_px,dtype=np.float64)
            transformed=np.asarray(diagnostics.transformed_canonical_points_px,dtype=np.float64)
            label="Canonical → Native"
        else:
            source=np.asarray(diagnostics.native_points_px,dtype=np.float64)
            transformed=np.asarray(diagnostics.transformed_native_points_px,dtype=np.float64)
            label="Native → Canonical"
        displacement=transformed-source
        magnitudes=np.linalg.norm(displacement,axis=0)
        maximum=float(np.max(magnitudes)) if magnitudes.size else 0.0
        normalized=magnitudes/maximum if maximum>0.0 else np.zeros_like(magnitudes)
        brushes=[pg.mkBrush(color) for color in self._colormap.map(normalized,mode="qcolor")]
        scale=float(self.vector_scale.value())
        endpoints=source+scale*displacement
        count=source.shape[1]
        x=np.empty(2*count,dtype=np.float64); y=np.empty(2*count,dtype=np.float64)
        x[0::2],x[1::2]=source[0],endpoints[0]
        y[0::2],y[1::2]=source[1],endpoints[1]
        self.vector_item.setData(x=x,y=y,connect="pairs")
        self.source_item.setData(x=source[0],y=source[1],size=7,brush=brushes,pen=None)
        self.endpoint_item.setData(x=endpoints[0],y=endpoints[1])
        self.range_label.setText("0 → %.4g px" % maximum)
        median=float(np.median(magnitudes)) if magnitudes.size else 0.0
        self.summary_label.setText(
            "%s · %d samples · |Δ| median %.4g px · max %.4g px · vectors shown at %.3g×"
            % (label,count,median,maximum,scale)
        )
        if reset_view:
            self.reset_view()


# Backward-compatible public name retained for downstream imports/tests.
ObservationRegistrationPreview=ObservationRegistrationAlignmentView


class ObservationRegistrationCalibrationWidget(QtWidgets.QWidget):
    """Embedded UI for registration from native localization to a named reference."""

    sigReferenceChanged=QtCore.Signal(str)
    sigUseLatestRequested=QtCore.Signal()
    sigLocalizeRequested=QtCore.Signal()
    sigCorrespondenceChanged=QtCore.Signal(str)
    sigFitRequested=QtCore.Signal(str,int)
    sigSaveRequested=QtCore.Signal()
    sigCancelRequested=QtCore.Signal()
    sigNewCalibrationRequested=QtCore.Signal()
    sigRefreshRequested=QtCore.Signal()

    def __init__(
        self,*,native_section_key: str,native_section_title: str,
        observation_plane_name: str,references: Sequence[Any]=(),
        canonical_reference_name: str | None=None,parent=None,
    ) -> None:
        super().__init__(parent)
        self.native_section_key=str(native_section_key)
        self.observation_plane_name=str(observation_plane_name)
        self._setting_context=False
        self._candidate_identity=None
        root=QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6,6,6,6)
        root.setSpacing(6)

        self.error_label=QtWidgets.QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b00020;")
        self.error_label.hide()
        root.addWidget(self.error_label)

        self.workflow_splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal,self)
        self.workflow_splitter.setChildrenCollapsible(False)
        root.addWidget(self.workflow_splitter,1)

        # Visualization owns the expanding area, following the FOV calibration
        # workspace rather than stacking controls above a compressed plot.
        self.viewer_tabs=QtWidgets.QTabWidget(self.workflow_splitter)
        self.viewer_tabs.setMinimumSize(500,420)
        self.preview=ObservationRegistrationAlignmentView(self.viewer_tabs)
        self.field_view=ObservationRegistrationFieldView(self.viewer_tabs)
        self.viewer_tabs.addTab(self.preview,"Measured vs canonical")
        self.viewer_tabs.addTab(self.field_view,"Registration field")
        self.workflow_splitter.addWidget(self.viewer_tabs)

        controls=QtWidgets.QWidget(self.workflow_splitter)
        controls.setMinimumWidth(285)
        controls.setMaximumWidth(380)
        side=QtWidgets.QVBoxLayout(controls)
        side.setContentsMargins(6,0,2,0)
        side.setSpacing(7)

        context_label=QtWidgets.QLabel("%s · %s" % (observation_plane_name,native_section_title))
        context_font=context_label.font(); context_font.setBold(True); context_label.setFont(context_font)
        context_label.setToolTip(
            "Fit this section's native detector coordinates into the coordinate frame "
            "of a raw named position reference. The reference can be session or persistent "
            "and can originate from another SLM."
        )
        side.addWidget(context_label)

        reference_group=QtWidgets.QGroupBox("Canonical reference")
        reference_layout=QtWidgets.QVBoxLayout(reference_group)
        reference_layout.setContentsMargins(7,7,7,7)
        reference_layout.setSpacing(4)
        self.reference_combo=QtWidgets.QComboBox()
        reference_layout.addWidget(self.reference_combo)
        self.reference_status=QtWidgets.QLabel("Select a saved or session reference.")
        self.reference_status.setWordWrap(True)
        self.reference_status.setStyleSheet("color: %s;" % _MUTED_COLOR)
        reference_layout.addWidget(self.reference_status)
        side.addWidget(reference_group)
        self._set_reference_choices(references,canonical_reference_name)

        self.native_group,self.native_status,self.native_use,self.native_localize=(
            self._build_native_group(native_section_title)
        )
        side.addWidget(self.native_group)

        correspondence_group=QtWidgets.QGroupBox("Correspondence")
        correspondence_layout=QtWidgets.QVBoxLayout(correspondence_group)
        correspondence_layout.setContentsMargins(7,7,7,7)
        correspondence_layout.setSpacing(4)
        correspondence_row=QtWidgets.QHBoxLayout()
        correspondence_row.addWidget(QtWidgets.QLabel("Mode:"))
        self.correspondence_combo=QtWidgets.QComboBox()
        self.correspondence_combo.setToolTip(
            "Discrete finite-lattice correspondence used only to pair calibration spots. "
            "Auto uses the same exact-lattice resolver as Position Feedback."
        )
        correspondence_row.addWidget(self.correspondence_combo,1)
        correspondence_layout.addLayout(correspondence_row)
        self.correspondence_info=QtWidgets.QLabel("")
        self.correspondence_info.setWordWrap(True)
        self.correspondence_info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.correspondence_info.setStyleSheet("color: %s;" % _MUTED_COLOR)
        correspondence_layout.addWidget(self.correspondence_info)
        side.addWidget(correspondence_group)

        fit_group=QtWidgets.QGroupBox("Registration fit")
        fit_layout=QtWidgets.QVBoxLayout(fit_group)
        fit_layout.setContentsMargins(7,7,7,7)
        fit_layout.setSpacing(4)
        fit_row=QtWidgets.QHBoxLayout()
        fit_row.addWidget(QtWidgets.QLabel("Model:"))
        self.model_combo=QtWidgets.QComboBox()
        self.model_combo.addItem("Affine","affine")
        self.model_combo.addItem("Polynomial","polynomial")
        fit_row.addWidget(self.model_combo,1)
        self.degree_label=QtWidgets.QLabel("Order:")
        fit_row.addWidget(self.degree_label)
        self.degree_combo=QtWidgets.QComboBox()
        for degree in (2,3,4):
            self.degree_combo.addItem(str(degree),degree)
        fit_row.addWidget(self.degree_combo)
        self.fit_button=QtWidgets.QPushButton("Fit")
        fit_row.addWidget(self.fit_button)
        fit_layout.addLayout(fit_row)
        self.diagnostics_label=QtWidgets.QLabel(
            "Use a native localization and select a canonical reference, then Fit."
        )
        self.diagnostics_label.setWordWrap(True)
        self.diagnostics_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        fit_layout.addWidget(self.diagnostics_label)
        side.addWidget(fit_group)
        side.addStretch(1)

        self.new_calibration_button=QtWidgets.QPushButton("New calibration…")
        self.new_calibration_button.setToolTip(
            "Start a fresh calibration from a current localization and named reference. "
            "This is required to change correspondence."
        )
        self.new_calibration_button.hide()
        side.addWidget(self.new_calibration_button)

        buttons=QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button=QtWidgets.QPushButton("Cancel")
        self.save_button=QtWidgets.QPushButton("Save registration")
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        side.addLayout(buttons)

        self.workflow_splitter.addWidget(controls)
        self.workflow_splitter.setStretchFactor(0,1)
        self.workflow_splitter.setStretchFactor(1,0)
        self.workflow_splitter.setSizes([820,330])

        self.reference_combo.currentIndexChanged.connect(self._reference_changed)
        self.correspondence_combo.currentIndexChanged.connect(self._correspondence_changed)
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        self.fit_button.clicked.connect(self._fit)
        self.save_button.clicked.connect(self.sigSaveRequested.emit)
        self.cancel_button.clicked.connect(self.sigCancelRequested.emit)
        self.new_calibration_button.clicked.connect(self.sigNewCalibrationRequested.emit)
        self.native_use.clicked.connect(self.sigUseLatestRequested.emit)
        self.native_localize.clicked.connect(self.sigLocalizeRequested.emit)
        self._model_changed(); self._reference_changed()

        self._refresh_timer=QtCore.QTimer(self)
        self._refresh_timer.setInterval(800)
        self._refresh_timer.timeout.connect(self.sigRefreshRequested.emit)
        self._refresh_timer.start()

    @property
    def canonical_reference_name(self) -> str:
        return str(self.reference_combo.currentData() or "")

    @property
    def correspondence(self) -> str:
        return str(self.correspondence_combo.currentData() or "auto")

    def _build_native_group(self,section_title: str):
        group=QtWidgets.QGroupBox("Native localization")
        layout=QtWidgets.QVBoxLayout(group)
        layout.setContentsMargins(7,7,7,7)
        layout.setSpacing(4)
        section=QtWidgets.QLabel(section_title)
        font=section.font(); font.setBold(True); section.setFont(font)
        layout.addWidget(section)
        status=QtWidgets.QLabel("No accepted localization available.")
        status.setWordWrap(True)
        status.setStyleSheet("color: %s;" % _MUTED_COLOR)
        layout.addWidget(status)
        row=QtWidgets.QHBoxLayout()
        use=QtWidgets.QPushButton("Use latest")
        use.setToolTip("Use the latest accepted localization from this section.")
        localize=QtWidgets.QPushButton("Localize…")
        row.addWidget(use); row.addWidget(localize); row.addStretch(1)
        layout.addLayout(row)
        return group,status,use,localize

    @staticmethod
    def _reference_label(record: Any) -> str:
        reference=getattr(record,"reference",None)
        name=str(getattr(record,"name","") or getattr(reference,"name","") or "—")
        lifetime=getattr(getattr(record,"lifetime",None),"value",str(getattr(record,"lifetime","") or ""))
        metadata=dict(getattr(reference,"metadata",{}) or {}) if reference is not None else {}
        serial=str(metadata.get("source_slm_serial") or "").strip()
        section=str(metadata.get("source_section") or "").strip()
        source=" / ".join(value for value in (serial,section) if value)
        suffix=" · ".join(value for value in (str(lifetime),source) if value)
        return name if not suffix else "%s   [%s]" % (name,suffix)

    def _set_reference_choices(
        self,references: Sequence[Any],selected: str | None=None,*,allow_missing_selected: bool=False,
    ) -> None:
        current=str(selected or self.canonical_reference_name if hasattr(self,"reference_combo") else selected or "")
        blocker=QtCore.QSignalBlocker(self.reference_combo)
        try:
            self.reference_combo.clear()
            for record in references:
                name=str(getattr(record,"name","") or "")
                if name:
                    self.reference_combo.addItem(self._reference_label(record),name)
            index=self.reference_combo.findData(current) if current else -1
            if index<0 and current and allow_missing_selected:
                self.reference_combo.addItem("%s   [saved provenance]" % current,current)
                index=self.reference_combo.findData(current)
            if index>=0:
                self.reference_combo.setCurrentIndex(index)
            elif self.reference_combo.count():
                self.reference_combo.setCurrentIndex(0)
        finally:
            del blocker

    def _reference_changed(self,*_args) -> None:
        if not self._setting_context:
            self.sigReferenceChanged.emit(self.canonical_reference_name)

    def _correspondence_changed(self,*_args) -> None:
        if not self._setting_context and self.correspondence_combo.count():
            self.sigCorrespondenceChanged.emit(self.correspondence)

    def _model_changed(self,*_args) -> None:
        polynomial=str(self.model_combo.currentData())=="polynomial"
        self.degree_label.setVisible(polynomial)
        self.degree_combo.setVisible(polynomial)

    def _fit(self) -> None:
        self.sigFitRequested.emit(
            str(self.model_combo.currentData() or "affine"),
            int(self.degree_combo.currentData() or 2),
        )

    @staticmethod
    def _correspondence_label(value: str,resolved: str) -> str:
        if value=="auto":
            try:
                return "Auto — %s" % spot_ordering_label(resolved)
            except Exception:
                return "Auto"
        try:
            return spot_ordering_label(value)
        except Exception:
            return value.replace("_"," ").title()

    def _set_correspondence_context(self,context: Any) -> None:
        saved_mode=bool(getattr(context,"saved_edit_mode",False))
        requested=str(getattr(context,"correspondence_requested","auto") or "auto")
        resolved=str(getattr(context,"correspondence_resolved","") or "canonical")
        options=tuple(str(value) for value in (getattr(context,"correspondence_options",()) or ()))
        if not saved_mode:
            if "auto" not in options:
                options=("auto",)+options
            if "canonical" not in options:
                options=options+("canonical",)
        elif requested not in options:
            options=(requested,)
        blocker=QtCore.QSignalBlocker(self.correspondence_combo)
        try:
            self.correspondence_combo.clear()
            for value in options:
                self.correspondence_combo.addItem(self._correspondence_label(value,resolved),value)
            index=self.correspondence_combo.findData(requested)
            self.correspondence_combo.setCurrentIndex(index if index>=0 else 0)
        finally:
            del blocker
        native=getattr(context,"native_snapshot",None)
        canonical=getattr(context,"canonical_snapshot",None)
        self.correspondence_combo.setEnabled(
            not saved_mode and native is not None and canonical is not None
            and self.correspondence_combo.count()>0
        )
        self.correspondence_combo.setToolTip(
            "Correspondence is fixed for a saved registration. Start a new calibration to change it."
            if saved_mode else
            "Discrete finite-lattice correspondence used only to pair calibration spots. "
            "Auto uses the same exact-lattice resolver as Position Feedback."
        )

        diagnostics=dict(getattr(context,"correspondence_diagnostics",{}) or {})
        if not diagnostics:
            self.correspondence_info.setText(
                "Correspondence is fixed for this saved registration."
                if saved_mode else
                ("Use a native localization and canonical reference to resolve lattice correspondence."
                 if native is None or canonical is None else "Correspondence unavailable.")
            )
            self.correspondence_info.setStyleSheet("color: %s;" % _MUTED_COLOR)
            return
        resolved_value=str(diagnostics.get("resolved") or resolved)
        try:
            resolved_label=spot_ordering_label(resolved_value)
        except Exception:
            resolved_label=resolved_value.replace("_"," ").title()
        details=["Auto → %s" % resolved_label if requested=="auto" else resolved_label]
        fit_count=diagnostics.get("fit_count")
        rotation=diagnostics.get("relative_rotation_deg")
        rms=diagnostics.get("rms_px")
        median=diagnostics.get("median_displacement_px")
        if fit_count is not None:
            details.append("%d paired sites" % int(fit_count) if saved_mode else "%d matched sites" % int(fit_count))
        if not saved_mode:
            if rotation is not None:
                details.append("Δrot %+.2f°" % float(rotation))
            if rms is not None:
                details.append("screening RMS %.3g px" % float(rms))
            if median is not None:
                details.append("median shift %.3g px" % float(median))
        reflection=bool(diagnostics.get("reflection",False))
        if reflection:
            details.append("reflection")
        if saved_mode:
            details.insert(0,"Fixed after save")
        self.correspondence_info.setText(" · ".join(details))
        self.correspondence_info.setStyleSheet(
            "color: %s;" % (_WARNING_COLOR if reflection else _MUTED_COLOR)
        )

    def set_context(self,context: Any) -> None:
        self._setting_context=True
        try:
            saved_mode=bool(getattr(context,"saved_edit_mode",False))
            selected=str(getattr(context,"canonical_reference_name","") or self.canonical_reference_name)
            self._set_reference_choices(
                getattr(context,"available_references",()) or (),selected,
                allow_missing_selected=saved_mode,
            )
            self.reference_combo.setEnabled(not saved_mode)
            native=getattr(context,"native_snapshot",None)
            canonical=getattr(context,"canonical_snapshot",None)
            candidate=getattr(context,"candidate",None)

            self.native_group.setVisible(not saved_mode)
            self.new_calibration_button.setVisible(saved_mode)
            self.fit_button.setText("Refit" if saved_mode else "Fit")
            self.save_button.setText("Save update" if saved_mode else "Save registration")
            self.cancel_button.setText("Close" if saved_mode else "Cancel")

            if saved_mode:
                frame=getattr(candidate,"canonical_frame",None)
                provenance=dict(getattr(candidate,"provenance",{}) or {}) if candidate is not None else {}
                source_name=str(provenance.get("canonical_reference_name") or selected or "").strip()
                frame_text="%s / %s" % (
                    getattr(frame,"slm_serial","—"),getattr(frame,"section_key","—")
                )
                if source_name:
                    self.reference_status.setText(
                        "Saved calibration source: %s\nCanonical frame: %s" % (source_name,frame_text)
                    )
                else:
                    self.reference_status.setText("Saved canonical frame: %s" % frame_text)
            else:
                self._set_native_status(native,bool(getattr(context,"latest_native_available",False)))
                if canonical is None:
                    self.reference_status.setText("No compatible named reference selected.")
                else:
                    self.reference_status.setText(
                        "%s · %d/%d sites · %s / %s\n%s" % (
                            getattr(getattr(canonical,"lifetime",None),"value",getattr(canonical,"lifetime","")),
                            int(getattr(canonical,"valid_count",0)),int(getattr(canonical,"total_count",0)),
                            getattr(getattr(canonical,"frame",None),"slm_serial","—"),
                            getattr(getattr(canonical,"frame",None),"section_key","—"),
                            getattr(canonical,"sample_policy","—"),
                        )
                    )

            self._set_correspondence_context(context)
            self.fit_button.setEnabled(bool(getattr(context,"fit_enabled",False)))
            self.save_button.setEnabled(bool(getattr(context,"save_enabled",False)))

            candidate_identity=None
            if candidate is not None:
                candidate_identity=(
                    str(getattr(candidate,"created_at","") or ""),
                    str(getattr(candidate,"transform_model","") or ""),
                    getattr(candidate,"polynomial_degree",None),
                    int(getattr(candidate,"point_count",0) or 0),
                )
            if candidate_identity is not None and candidate_identity!=self._candidate_identity:
                model=str(getattr(candidate,"transform_model","") or "")
                index=self.model_combo.findData(model)
                if index>=0:self.model_combo.setCurrentIndex(index)
                degree=getattr(candidate,"polynomial_degree",None)
                if degree is not None:
                    degree_index=self.degree_combo.findData(int(degree))
                    if degree_index>=0:self.degree_combo.setCurrentIndex(degree_index)
                self._candidate_identity=candidate_identity
                self._model_changed()

            diagnostics=getattr(context,"diagnostics",None)
            if diagnostics is None:
                common=int(getattr(context,"common_site_count",0) or 0)
                self.diagnostics_label.setText(
                    "%d common valid lattice sites." % common
                    if native is not None and canonical is not None
                    else "Use a native localization and select a canonical reference, then Fit."
                )
                self.preview.clear()
                self.field_view.clear()
            else:
                model=str(getattr(candidate,"transform_model","") or "")
                degree=getattr(candidate,"polynomial_degree",None)
                model_text=model if degree is None else "%s %s" % (model,degree)
                self.diagnostics_label.setText(
                    "%s · %d points\nForward RMS %.4g px · max %.4g px\nReverse RMS %.4g px · max %.4g px" % (
                        model_text,diagnostics.point_count,
                        diagnostics.forward_rms_residual_px,diagnostics.forward_max_residual_px,
                        diagnostics.reverse_rms_residual_px,diagnostics.reverse_max_residual_px,
                    )
                )
                self.preview.set_diagnostics(diagnostics)
                self.field_view.set_diagnostics(diagnostics)
        finally:
            self._setting_context=False

    def _set_native_status(self,snapshot: Any,available: bool) -> None:
        self.native_use.setEnabled(bool(available))
        if snapshot is not None:
            self.native_status.setText(
                "%s · %d genuinely measured spots" % (
                    getattr(snapshot,"measurement_id","") or "Selected measurement",
                    int(getattr(snapshot,"matched_count",0)),
                )
            )
        elif available:
            self.native_status.setText("Latest accepted localization is available.")
        else:
            self.native_status.setText(
                "No accepted localization available. Use Localize… to open feedback."
            )

    def clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def set_error(self,error: Any) -> None:
        self.error_label.setText(str(error or "Observation-registration calibration failed."))
        self.error_label.show()


__all__=[
    "ObservationRegistrationCalibrationWidget",
    "ObservationRegistrationAlignmentView","ObservationRegistrationFieldView",
    "ObservationRegistrationPreview",
]
