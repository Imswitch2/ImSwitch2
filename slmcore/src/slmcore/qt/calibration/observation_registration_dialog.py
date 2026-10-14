from __future__ import annotations

from typing import Any,Sequence

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore,QtWidgets

from ...core.cgh.localization import spot_ordering_label


_MUTED_COLOR="#888"
_WARNING_COLOR="#a66a00"


class ObservationRegistrationAlignmentView(QtWidgets.QWidget):
    """Point-alignment workbench using the same visual language as feedback.

    Observation registration relates two detector coordinate frames, so the raw
    native and canonical coordinates should not be overlaid directly.  Instead
    the view shows either native -> canonical or canonical -> native and compares
    transformed source points with the target points in that frame.
    """

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
        root.addLayout(controls)

        self.graphics=pg.GraphicsLayoutWidget(self)
        self.graphics.setMinimumHeight(300)
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

        self.direction_combo.currentIndexChanged.connect(self._refresh)
        self._update_overlay_labels()

    def clear(self) -> None:
        self._diagnostics=None
        self.target_item.setData(x=[],y=[])
        self.transformed_item.setData(x=[],y=[])
        self.residual_item.setData([],[])
        self.summary_label.setText("Fit a registration to inspect alignment.")

    def set_diagnostics(self,diagnostics: Any) -> None:
        self._diagnostics=diagnostics
        self._refresh(reset_view=True)

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

    def _refresh(self,*_args,reset_view: bool=False) -> None:
        self._update_overlay_labels()
        diagnostics=self._diagnostics
        if diagnostics is None:
            self.clear()
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
            self.view_box.autoRange()

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


# Backward-compatible public name retained for downstream imports/tests while the
# old custom QPainter preview is replaced by the shared feedback-style workbench.
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
        root=QtWidgets.QVBoxLayout(self); root.setContentsMargins(8,8,8,8); root.setSpacing(8)

        heading=QtWidgets.QLabel("Observation registration — %s / %s" % (observation_plane_name,native_section_title))
        font=heading.font(); font.setBold(True); heading.setFont(font); root.addWidget(heading)
        intro=QtWidgets.QLabel(
            "Fit this section's native detector coordinates into the coordinate frame of a raw named position reference. "
            "The reference may be session or persistent and may originate from another SLM. It is not resolved through position feedback."
        )
        intro.setWordWrap(True); root.addWidget(intro)
        self.error_label=QtWidgets.QLabel(""); self.error_label.setWordWrap(True); self.error_label.setStyleSheet("color: #b00020;"); self.error_label.hide(); root.addWidget(self.error_label)

        reference_row=QtWidgets.QHBoxLayout(); reference_row.addWidget(QtWidgets.QLabel("Canonical reference:"))
        self.reference_combo=QtWidgets.QComboBox(); reference_row.addWidget(self.reference_combo,1); root.addLayout(reference_row)
        self._set_reference_choices(references,canonical_reference_name)

        localizations=QtWidgets.QHBoxLayout(); localizations.setSpacing(8)
        self.native_group,self.native_status,self.native_use,self.native_localize=self._build_native_group(native_section_title)
        self.reference_group=QtWidgets.QGroupBox("Canonical reference geometry")
        reference_layout=QtWidgets.QVBoxLayout(self.reference_group)
        self.reference_title=QtWidgets.QLabel("—"); font=self.reference_title.font(); font.setBold(True); self.reference_title.setFont(font); reference_layout.addWidget(self.reference_title)
        self.reference_status=QtWidgets.QLabel("Select a saved or session reference."); self.reference_status.setWordWrap(True); reference_layout.addWidget(self.reference_status,1)
        localizations.addWidget(self.native_group,1); localizations.addWidget(self.reference_group,1); root.addLayout(localizations)

        correspondence_group=QtWidgets.QGroupBox("Correspondence")
        correspondence_layout=QtWidgets.QVBoxLayout(correspondence_group)
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
        root.addWidget(correspondence_group)

        fit_row=QtWidgets.QHBoxLayout(); fit_row.addWidget(QtWidgets.QLabel("Model:"))
        self.model_combo=QtWidgets.QComboBox(); self.model_combo.addItem("Affine","affine"); self.model_combo.addItem("Polynomial","polynomial"); fit_row.addWidget(self.model_combo)
        fit_row.addWidget(QtWidgets.QLabel("Degree:")); self.degree_combo=QtWidgets.QComboBox()
        for degree in (2,3,4):self.degree_combo.addItem(str(degree),degree)
        fit_row.addWidget(self.degree_combo)
        self.fit_button=QtWidgets.QPushButton("Fit"); fit_row.addWidget(self.fit_button); fit_row.addStretch(1); root.addLayout(fit_row)

        self.diagnostics_label=QtWidgets.QLabel("Use a native localization and select a canonical reference, then Fit.")
        self.diagnostics_label.setWordWrap(True); self.diagnostics_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse); root.addWidget(self.diagnostics_label)
        self.preview=ObservationRegistrationAlignmentView(self); root.addWidget(self.preview,1)

        buttons=QtWidgets.QHBoxLayout(); buttons.addStretch(1)
        self.cancel_button=QtWidgets.QPushButton("Cancel"); self.save_button=QtWidgets.QPushButton("Save registration")
        buttons.addWidget(self.cancel_button); buttons.addWidget(self.save_button); root.addLayout(buttons)

        self.reference_combo.currentIndexChanged.connect(self._reference_changed)
        self.correspondence_combo.currentIndexChanged.connect(self._correspondence_changed)
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        self.fit_button.clicked.connect(self._fit)
        self.save_button.clicked.connect(self.sigSaveRequested.emit)
        self.cancel_button.clicked.connect(self.sigCancelRequested.emit)
        self.native_use.clicked.connect(self.sigUseLatestRequested.emit)
        self.native_localize.clicked.connect(self.sigLocalizeRequested.emit)
        self._model_changed(); self._reference_changed()

        self._refresh_timer=QtCore.QTimer(self); self._refresh_timer.setInterval(800)
        self._refresh_timer.timeout.connect(self.sigRefreshRequested.emit); self._refresh_timer.start()

    @property
    def canonical_reference_name(self) -> str:
        return str(self.reference_combo.currentData() or "")

    @property
    def correspondence(self) -> str:
        return str(self.correspondence_combo.currentData() or "auto")

    def _build_native_group(self,section_title: str):
        group=QtWidgets.QGroupBox("Native localization"); layout=QtWidgets.QVBoxLayout(group)
        section=QtWidgets.QLabel(section_title); font=section.font(); font.setBold(True); section.setFont(font); layout.addWidget(section)
        status=QtWidgets.QLabel("No accepted localization available."); status.setWordWrap(True); layout.addWidget(status,1)
        row=QtWidgets.QHBoxLayout(); use=QtWidgets.QPushButton("Use latest localization"); localize=QtWidgets.QPushButton("Localize…")
        row.addWidget(use); row.addWidget(localize); layout.addLayout(row)
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

    def _set_reference_choices(self,references: Sequence[Any],selected: str | None=None) -> None:
        current=str(selected or self.canonical_reference_name if hasattr(self,"reference_combo") else selected or "")
        blocker=QtCore.QSignalBlocker(self.reference_combo)
        try:
            self.reference_combo.clear()
            for record in references:
                name=str(getattr(record,"name","") or "")
                if name:self.reference_combo.addItem(self._reference_label(record),name)
            index=self.reference_combo.findData(current) if current else -1
            if index>=0:self.reference_combo.setCurrentIndex(index)
            elif self.reference_combo.count():self.reference_combo.setCurrentIndex(0)
        finally:
            del blocker

    def _reference_changed(self,*_args) -> None:
        self.reference_title.setText(self.reference_combo.currentText() or "—")
        if not self._setting_context:
            self.sigReferenceChanged.emit(self.canonical_reference_name)

    def _correspondence_changed(self,*_args) -> None:
        if not self._setting_context and self.correspondence_combo.count():
            self.sigCorrespondenceChanged.emit(self.correspondence)

    def _model_changed(self,*_args) -> None:
        self.degree_combo.setEnabled(str(self.model_combo.currentData())=="polynomial")

    def _fit(self) -> None:
        self.sigFitRequested.emit(str(self.model_combo.currentData() or "affine"),int(self.degree_combo.currentData() or 2))

    @staticmethod
    def _correspondence_label(value: str,resolved: str) -> str:
        if value=="auto":
            try:return "Auto — %s" % spot_ordering_label(resolved)
            except Exception:return "Auto"
        try:return spot_ordering_label(value)
        except Exception:return value.replace("_"," ").title()

    def _set_correspondence_context(self,context: Any) -> None:
        requested=str(getattr(context,"correspondence_requested","auto") or "auto")
        resolved=str(getattr(context,"correspondence_resolved","") or "canonical")
        options=tuple(str(value) for value in (getattr(context,"correspondence_options",()) or ()))
        if "auto" not in options:options=("auto",)+options
        if "canonical" not in options:options=options+("canonical",)
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
            native is not None and canonical is not None and self.correspondence_combo.count()>0
        )

        diagnostics=dict(getattr(context,"correspondence_diagnostics",{}) or {})
        if not diagnostics:
            self.correspondence_info.setText(
                "Use a native localization and canonical reference to resolve lattice correspondence."
                if native is None or canonical is None else "Correspondence unavailable."
            )
            self.correspondence_info.setStyleSheet("color: %s;" % _MUTED_COLOR)
            return
        resolved_value=str(diagnostics.get("resolved") or resolved)
        try:resolved_label=spot_ordering_label(resolved_value)
        except Exception:resolved_label=resolved_value.replace("_"," ").title()
        details=["Auto → %s" % resolved_label if requested=="auto" else resolved_label]
        fit_count=diagnostics.get("fit_count")
        rotation=diagnostics.get("relative_rotation_deg")
        rms=diagnostics.get("rms_px")
        median=diagnostics.get("median_displacement_px")
        if fit_count is not None:details.append("%d matched sites" % int(fit_count))
        if rotation is not None:details.append("Δrot %+.2f°" % float(rotation))
        if rms is not None:details.append("screening RMS %.3g px" % float(rms))
        if median is not None:details.append("median shift %.3g px" % float(median))
        reflection=bool(diagnostics.get("reflection",False))
        if reflection:details.append("reflection")
        self.correspondence_info.setText(" · ".join(details))
        self.correspondence_info.setStyleSheet(
            "color: %s;" % (_WARNING_COLOR if reflection else _MUTED_COLOR)
        )

    def set_context(self,context: Any) -> None:
        self._setting_context=True
        try:
            selected=str(getattr(context,"canonical_reference_name","") or self.canonical_reference_name)
            self._set_reference_choices(getattr(context,"available_references",()) or (),selected)
            self.reference_title.setText(self.reference_combo.currentText() or "—")
            native=getattr(context,"native_snapshot",None); canonical=getattr(context,"canonical_snapshot",None)
            self._set_native_status(native,bool(getattr(context,"latest_native_available",False)))
            if canonical is None:
                self.reference_status.setText("No compatible named reference selected.")
            else:
                self.reference_status.setText(
                    "%s reference · %d/%d usable lattice sites\nSource: %s / %s\nSample policy: %s" % (
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
            diagnostics=getattr(context,"diagnostics",None)
            if diagnostics is None:
                common=int(getattr(context,"common_site_count",0) or 0)
                self.diagnostics_label.setText(
                    "Native localization and reference selected. %d common valid lattice sites." % common
                    if native is not None and canonical is not None
                    else "Use a native localization and select a canonical reference, then Fit."
                )
                self.preview.clear()
            else:
                candidate=getattr(context,"candidate",None)
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
        finally:
            self._setting_context=False

    def _set_native_status(self,snapshot: Any,available: bool) -> None:
        self.native_use.setEnabled(bool(available))
        if snapshot is not None:
            self.native_status.setText("Selected measurement %s\n%d genuinely measured spots" % (getattr(snapshot,"measurement_id","") or "—",int(getattr(snapshot,"matched_count",0))))
        elif available:
            self.native_status.setText("Latest accepted localization is available.")
        else:
            self.native_status.setText("No accepted localization available. Use Localize… to open the normal feedback workflow.")

    def clear_error(self) -> None:
        self.error_label.clear(); self.error_label.hide()

    def set_error(self,error: Any) -> None:
        self.error_label.setText(str(error or "Observation-registration calibration failed.")); self.error_label.show()


__all__=[
    "ObservationRegistrationCalibrationWidget",
    "ObservationRegistrationAlignmentView","ObservationRegistrationPreview",
]
