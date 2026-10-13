from __future__ import annotations

from typing import Any,Mapping,Sequence

import numpy as np
from qtpy import QtCore,QtGui,QtWidgets


class ObservationRegistrationPreview(QtWidgets.QWidget):
    """Compact visualization of canonical points and registration residuals."""

    def __init__(self,parent=None) -> None:
        super().__init__(parent)
        self._canonical=np.empty((2,0),dtype=float)
        self._transformed=np.empty((2,0),dtype=float)
        self.setMinimumSize(420,300)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Expanding)

    def clear(self) -> None:
        self._canonical=np.empty((2,0),dtype=float)
        self._transformed=np.empty((2,0),dtype=float)
        self.update()

    def set_points(self,canonical_points: Any,transformed_native_points: Any) -> None:
        canonical=np.asarray(canonical_points,dtype=float)
        transformed=np.asarray(transformed_native_points,dtype=float)
        if canonical.ndim!=2 or transformed.ndim!=2 or canonical.shape!=transformed.shape or canonical.shape[0]!=2:
            self.clear(); return
        self._canonical=np.array(canonical,copy=True)
        self._transformed=np.array(transformed,copy=True)
        self.update()

    def paintEvent(self,event) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        painter=QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing,True)
        rect=self.rect().adjusted(10,10,-10,-10)
        painter.fillRect(rect,self.palette().base())
        painter.setPen(QtGui.QPen(self.palette().mid().color(),1)); painter.drawRect(rect)
        painter.setPen(self.palette().text().color())
        title=painter.font(); title.setBold(True); painter.setFont(title)
        painter.drawText(rect.adjusted(8,6,-8,-6),QtCore.Qt.AlignTop|QtCore.Qt.AlignLeft,"Registration fit — canonical frame")
        title.setBold(False); painter.setFont(title)
        if self._canonical.shape[1]==0:
            painter.drawText(rect,QtCore.Qt.AlignCenter,"Fit a registration to inspect residuals.")
            painter.end(); return

        all_points=np.concatenate((self._canonical,self._transformed),axis=1)
        xmin,ymin=np.min(all_points,axis=1); xmax,ymax=np.max(all_points,axis=1)
        dx=max(float(xmax-xmin),1.0); dy=max(float(ymax-ymin),1.0)
        plot=rect.adjusted(28,36,-18,-28)
        scale=min(plot.width()/dx,plot.height()/dy)*0.88
        cx=float((xmin+xmax)/2.0); cy=float((ymin+ymax)/2.0)
        center=plot.center()

        def map_point(x: float,y: float) -> QtCore.QPointF:
            return QtCore.QPointF(center.x()+(x-cx)*scale,center.y()-(y-cy)*scale)

        residual_pen=QtGui.QPen(self.palette().mid().color(),1.3)
        canonical_pen=QtGui.QPen(self.palette().highlight().color(),1.6)
        transformed_pen=QtGui.QPen(self.palette().text().color(),1.4)
        for index in range(self._canonical.shape[1]):
            tx,ty=self._transformed[:,index]; cxp,cyp=self._canonical[:,index]
            p0=map_point(float(tx),float(ty)); p1=map_point(float(cxp),float(cyp))
            painter.setPen(residual_pen); painter.drawLine(p0,p1)
            painter.setPen(transformed_pen)
            painter.drawLine(QtCore.QPointF(p0.x()-4,p0.y()),QtCore.QPointF(p0.x()+4,p0.y()))
            painter.drawLine(QtCore.QPointF(p0.x(),p0.y()-4),QtCore.QPointF(p0.x(),p0.y()+4))
            painter.setPen(canonical_pen); painter.setBrush(QtCore.Qt.NoBrush); painter.drawEllipse(p1,4,4)
        painter.setPen(self.palette().text().color())
        painter.drawText(rect.adjusted(8,0,-8,-6),QtCore.Qt.AlignBottom|QtCore.Qt.AlignLeft,"Cross: transformed native   Circle: canonical   Line: residual")
        painter.end()


class ObservationRegistrationCalibrationWidget(QtWidgets.QWidget):
    """Embedded UI for registration from native localization to a named reference."""

    sigReferenceChanged=QtCore.Signal(str)
    sigUseLatestRequested=QtCore.Signal()
    sigLocalizeRequested=QtCore.Signal()
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

        fit_row=QtWidgets.QHBoxLayout(); fit_row.addWidget(QtWidgets.QLabel("Model:"))
        self.model_combo=QtWidgets.QComboBox(); self.model_combo.addItem("Affine","affine"); self.model_combo.addItem("Polynomial","polynomial"); fit_row.addWidget(self.model_combo)
        fit_row.addWidget(QtWidgets.QLabel("Degree:")); self.degree_combo=QtWidgets.QComboBox()
        for degree in (2,3,4):self.degree_combo.addItem(str(degree),degree)
        fit_row.addWidget(self.degree_combo)
        self.fit_button=QtWidgets.QPushButton("Fit"); fit_row.addWidget(self.fit_button); fit_row.addStretch(1); root.addLayout(fit_row)

        self.diagnostics_label=QtWidgets.QLabel("Use a native localization and select a canonical reference, then Fit.")
        self.diagnostics_label.setWordWrap(True); self.diagnostics_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse); root.addWidget(self.diagnostics_label)
        self.preview=ObservationRegistrationPreview(self); root.addWidget(self.preview,1)

        buttons=QtWidgets.QHBoxLayout(); buttons.addStretch(1)
        self.cancel_button=QtWidgets.QPushButton("Cancel"); self.save_button=QtWidgets.QPushButton("Save registration")
        buttons.addWidget(self.cancel_button); buttons.addWidget(self.save_button); root.addLayout(buttons)

        self.reference_combo.currentIndexChanged.connect(self._reference_changed)
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
        self.reference_combo.blockSignals(True)
        self.reference_combo.clear()
        for record in references:
            name=str(getattr(record,"name","") or "")
            if name:self.reference_combo.addItem(self._reference_label(record),name)
        index=self.reference_combo.findData(current) if current else -1
        if index>=0:self.reference_combo.setCurrentIndex(index)
        elif self.reference_combo.count():self.reference_combo.setCurrentIndex(0)
        self.reference_combo.blockSignals(False)

    def _reference_changed(self,*_args) -> None:
        self.reference_title.setText(self.reference_combo.currentText() or "—")
        self.sigReferenceChanged.emit(self.canonical_reference_name)

    def _model_changed(self,*_args) -> None:
        self.degree_combo.setEnabled(str(self.model_combo.currentData())=="polynomial")

    def _fit(self) -> None:
        self.sigFitRequested.emit(str(self.model_combo.currentData() or "affine"),int(self.degree_combo.currentData() or 2))

    def set_context(self,context: Any) -> None:
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
        self.fit_button.setEnabled(bool(getattr(context,"fit_enabled",False)))
        self.save_button.setEnabled(bool(getattr(context,"save_enabled",False)))
        diagnostics=getattr(context,"diagnostics",None)
        if diagnostics is None:
            common=int(getattr(context,"common_site_count",0) or 0)
            self.diagnostics_label.setText("Native localization and reference selected. %d common valid lattice sites." % common if native is not None and canonical is not None else "Use a native localization and select a canonical reference, then Fit.")
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
            self.preview.set_points(diagnostics.canonical_points_px,diagnostics.transformed_native_points_px)

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


__all__=["ObservationRegistrationCalibrationWidget","ObservationRegistrationPreview"]
