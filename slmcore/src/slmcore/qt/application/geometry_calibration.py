from __future__ import annotations

from typing import Any,Mapping

import numpy as np
from PIL import Image
from qtpy import QtCore,QtWidgets

from ...core.measurement import create_image_measurement
from ..calibration.geometry_calibration_dialog import GeometryCalibrationDialog


class GeometryCalibrationCoordinator(QtCore.QObject):
    """Reusable Qt launcher for the shared geometry-calibration service."""

    sigSaved=QtCore.Signal(str,str)

    def __init__(self,qt_session,parent=None) -> None:
        super().__init__(parent or qt_session)
        self.qt_session=qt_session
        self.service=qt_session.geometry_calibration_service
        self._dialogs: dict[str,GeometryCalibrationDialog]={}
        self._plane_by_section: dict[str,str | None]={}

    def dialog(self,section_key: str):
        return self._dialogs.get(str(section_key))

    def open(self,section_key: str,plane_name: str | None=None,parent=None) -> None:
        section=str(section_key)
        names=self.service.observation_plane_names()
        plane=str(plane_name or "").strip() or None
        if plane is None and len(names)==1:
            plane=names[0]
        if plane is not None:
            self.service.observation_planes.get(plane)
        self._plane_by_section[section]=plane
        sources=self.service.available_sources(section)
        current=self.service.preferred_source(section,plane) if plane else (sources[0] if sources else None)
        context=self.service.context(section,plane)
        dialog=self._dialogs.get(section)
        if dialog is None:
            dialog=GeometryCalibrationDialog(
                section_key=section,observation_planes=names,current_observation_plane=plane,
                detectors=sources,current_detector=current,
                localization_parameters=context.get("localization_parameters") or {},
                section_title=self.qt_session.section_host.section_title(section),
                parent=parent,
            )
            self._dialogs[section]=dialog
            dialog.sigObservationPlaneChanged.connect(lambda value,key=section:self._observation_changed(key,value))
            dialog.sigComputeRequested.connect(lambda values,key=section:self._compute(key,dict(values or {})))
            dialog.sigAcquireRequested.connect(lambda source,key=section:self._acquire(key,source))
            dialog.sigLoadRequested.connect(lambda key=section:self._load(key))
            dialog.sigLocalizationRequested.connect(lambda params,key=section:self._localize(key,dict(params or {})))
            dialog.sigAnalyzeRequested.connect(lambda key=section:self._analyze(key))
            dialog.sigSaveRequested.connect(lambda key=section:self._save(key))
            dialog.sigManualSaveRequested.connect(
                lambda orientation,key=section:self._save_manual(key,orientation)
            )
            dialog.sigFinishRequested.connect(lambda key=section:self._finish(key))
            # Closing the reusable geometry window is also leaving the
            # calibration workflow.  Restore the experiment CGH just as the
            # explicit Finish action does.  finish() is idempotent once the
            # transient calibration frame has already been restored.
            dialog.finished.connect(lambda _result,key=section:self._finish(key))
            dialog.destroyed.connect(lambda _obj=None,key=section:self._dialogs.pop(key,None))
        else:
            dialog.configure_observation_planes(names,plane)
            dialog.configure_detectors(sources,current)
        self._render(section)
        dialog.show(); dialog.raise_(); dialog.activateWindow()

    def close_all(self) -> None:
        for dialog in tuple(self._dialogs.values()):
            dialog.close(); dialog.deleteLater()
        self._dialogs.clear(); self._plane_by_section.clear()

    def _plane(self,section: str) -> str | None:
        return self._plane_by_section.get(str(section))

    def _render(self,section: str) -> None:
        dialog=self._dialogs.get(str(section))
        if dialog is None:return
        plane=self._plane(section)
        context=self.service.context(section,plane)
        sources=self.service.available_sources(section)
        current=context.get("detector_name") or (self.service.preferred_source(section,plane) if plane else None)
        dialog.configure_detectors(sources,current)
        dialog.set_context(context)

    def _error(self,title: str,error: Exception,section: str | None=None) -> None:
        if section is not None:
            dialog=self._dialogs.get(str(section))
            if dialog is not None:
                dialog.set_error(error)
        self.qt_session.sigError.emit(str(title),error)

    def _observation_changed(self,section: str,value) -> None:
        plane=str(value or "").strip() or None
        self._plane_by_section[str(section)]=plane
        self._render(section)

    def _compute(self,section: str,values: Mapping[str,Any]) -> None:
        plane=self._plane(section)
        if plane is None:
            self._error("Geometry calibration failed",RuntimeError("Select an observation plane first."),section); return
        try:
            px=float(values.get("period_x")); py=float(values.get("period_y"))
            if str(values.get("period_unit") or "")=="um":
                px=self.service.period_from_um(section,px,"x")
                py=self.service.period_from_um(section,py,"y")
            self.service.compute_target(
                section,plane,grid_x=int(values.get("grid_x",5)),grid_y=int(values.get("grid_y",4)),
                period_x_px=px,period_y_px=py,source=str(values.get("detector") or ""),
                on_complete=lambda success,error,key=section:self._compute_done(key,success,error),
            )
        except Exception as error:
            self._error("Geometry calibration failed",error,section); self._render(section)

    def _compute_done(self,section: str,success: bool,error: Exception | None) -> None:
        self._render(section)
        if not success and error is not None:
            self._error("Geometry calibration failed",error,section)
        elif success:
            dialog=self._dialogs.get(section)
            if dialog is not None:
                dialog.measurement_view.set_measurement_status(
                    "Geometry calibration target is displayed. Acquire/load and localize."
                )

    def _acquire(self,section: str,source: str) -> None:
        plane=self._plane(section)
        if plane is None:
            self._error("Geometry calibration acquisition failed",RuntimeError("Select an observation plane first."),section); return
        try:
            self.service.acquire(
                section,plane,source,
                on_complete=lambda success,error,key=section:self._acquire_done(key,success,error),
            )
        except Exception as error:
            self._error("Geometry calibration acquisition failed",error,section)

    def _acquire_done(self,section: str,success: bool,error: Exception | None) -> None:
        self._render(section)
        if not success and error is not None:
            self._error("Geometry calibration acquisition failed",error,section)

    def _load(self,section: str) -> None:
        plane=self._plane(section)
        if plane is None:
            self._error("Geometry calibration image load failed",RuntimeError("Select an observation plane first."),section); return
        dialog=self._dialogs.get(section)
        path,_=QtWidgets.QFileDialog.getOpenFileName(
            dialog,"Select geometry calibration image","",
            "Images (*.tif *.tiff *.png *.jpg *.jpeg *.bmp);;All files (*)",
        )
        if not path:return
        try:
            image=np.asarray(Image.open(path).convert("F"),dtype=np.float64)
            measurement=create_image_measurement(
                image,source="file",metadata={"path":str(path),"workflow":"geometry_orientation_calibration","observation_plane":plane},
            )
            self.service.set_measurement(section,plane,measurement); self._render(section)
        except Exception as error:
            self._error("Geometry calibration image load failed",error,section)

    def _localize(self,section: str,parameters: Mapping[str,Any]) -> None:
        plane=self._plane(section)
        if plane is None:
            self._error("Geometry calibration localization failed",RuntimeError("Select an observation plane first."),section); return
        try:
            self.service.localize(section,plane,parameters); self._render(section)
        except Exception as error:
            self._error("Geometry calibration localization failed",error,section)

    def _analyze(self,section: str) -> None:
        plane=self._plane(section)
        if plane is None:return
        try:
            self.service.analyze(section,plane); self._render(section)
        except Exception as error:
            self._error("Geometry calibration analysis failed",error,section)

    def _save(self,section: str) -> None:
        plane=self._plane(section)
        if plane is None:return
        try:
            self.service.save(section,plane); self._render(section)
            self.qt_session.synchronize_section(section)
            self.sigSaved.emit(section,plane)
        except Exception as error:
            self._error("Saving geometry calibration failed",error,section)

    def _save_manual(self,section: str,orientation: str) -> None:
        plane=self._plane(section)
        if plane is None:
            self._error(
                "Saving manual geometry failed",
                RuntimeError("Select an observation plane first."),section,
            )
            return
        try:
            self.service.save_manual(section,plane,orientation)
            self._render(section)
            self.qt_session.synchronize_section(section)
            self.sigSaved.emit(section,plane)
        except Exception as error:
            self._error("Saving manual geometry failed",error,section)

    def _finish(self,section: str) -> None:
        try:
            self.service.finish(section); self._render(section)
        except Exception as error:
            self._error("Finishing geometry calibration failed",error,section)


__all__=["GeometryCalibrationCoordinator"]
