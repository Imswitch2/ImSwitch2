from __future__ import annotations

from typing import Any,Mapping

import numpy as np
from PIL import Image
from qtpy import QtCore,QtWidgets

from ...core.measurement import create_image_measurement
from ..calibration.geometry_calibration_dialog import GeometryCalibrationDialog


class GeometryCalibrationCoordinator(QtCore.QObject):
    """Reusable Qt launcher for the shared geometry-calibration service.

    Standalone and embedded frontends share the same application service while
    keeping separate widget lifetimes.  This lets the Calibration workspace
    embed the workflow without changing the existing Feedback entry point.
    """

    sigSaved=QtCore.Signal(str,str)

    def __init__(self,qt_session,parent=None) -> None:
        super().__init__(parent or qt_session)
        self.qt_session=qt_session
        self.service=qt_session.geometry_calibration_service
        self._dialogs: dict[tuple[str,bool],GeometryCalibrationDialog]={}
        self._plane_by_context: dict[tuple[str,bool],str | None]={}

    @staticmethod
    def _key(section_key: str,embedded: bool=False) -> tuple[str,bool]:
        return str(section_key),bool(embedded)

    def dialog(self,section_key: str,*,embedded: bool=False):
        return self._dialogs.get(self._key(section_key,embedded))

    def open(
        self,section_key: str,observation_plane_name: str | None=None,parent=None,
        *,embedded: bool=False,
    ) -> GeometryCalibrationDialog:
        section=str(section_key); key=self._key(section,embedded)
        names=self.service.observation_plane_names()
        plane=str(observation_plane_name or "").strip() or None
        if plane is None and len(names)==1:
            plane=names[0]
        if plane is not None:
            self.service.observation_planes.get(plane)
        self._plane_by_context[key]=plane
        sources=self.service.available_sources(section)
        current=self.service.preferred_source(section,plane) if plane else (sources[0] if sources else None)
        context=self.service.context(section,plane)
        dialog=self._dialogs.get(key)
        if dialog is None:
            dialog=GeometryCalibrationDialog(
                section_key=section,observation_planes=names,current_observation_plane=plane,
                detectors=sources,current_detector=current,
                localization_parameters=context.get("localization_parameters") or {},
                section_title=self.qt_session.section_host.section_title(section),
                parent=parent,embedded=embedded,
            )
            self._dialogs[key]=dialog
            dialog.sigObservationPlaneChanged.connect(
                lambda value,k=key:self._observation_changed(k,value)
            )
            dialog.sigComputeRequested.connect(lambda values,k=key:self._compute(k,dict(values or {})))
            dialog.sigAcquireRequested.connect(lambda source,k=key:self._acquire(k,source))
            dialog.sigLoadRequested.connect(lambda k=key:self._load(k))
            dialog.sigLocalizationRequested.connect(lambda params,k=key:self._localize(k,dict(params or {})))
            dialog.sigAnalyzeRequested.connect(lambda k=key:self._analyze(k))
            dialog.sigSaveRequested.connect(lambda k=key:self._save(k))
            dialog.sigManualSaveRequested.connect(lambda orientation,k=key:self._save_manual(k,orientation))
            dialog.sigFinishRequested.connect(lambda k=key:self._finish(k))
            if not embedded:
                # Closing a standalone window is also leaving the workflow.
                dialog.finished.connect(lambda _result,k=key:self._finish(k))
            dialog.destroyed.connect(lambda _obj=None,k=key:self._dialog_destroyed(k))
        else:
            dialog.configure_observation_planes(names,plane)
            dialog.configure_detectors(sources,current)
        self._render(key)
        if not embedded:
            dialog.show(); dialog.raise_(); dialog.activateWindow()
        else:
            dialog.show()
        return dialog

    def close(self,section_key: str,*,embedded: bool=False,delete: bool=True) -> None:
        key=self._key(section_key,embedded)
        dialog=self._dialogs.pop(key,None)
        self._plane_by_context.pop(key,None)
        try:
            self.service.finish(str(section_key))
        except Exception as error:
            self.qt_session.sigError.emit("Finishing geometry calibration failed",error)
        if dialog is not None:
            dialog.hide()
            if delete:
                dialog.setParent(None); dialog.deleteLater()

    def close_all(self) -> None:
        for section,embedded in tuple(self._dialogs):
            self.close(section,embedded=embedded)
        self._dialogs.clear(); self._plane_by_context.clear()

    def _dialog_destroyed(self,key: tuple[str,bool]) -> None:
        self._dialogs.pop(key,None); self._plane_by_context.pop(key,None)

    def _plane(self,key: tuple[str,bool]) -> str | None:
        return self._plane_by_context.get(key)

    def _render(self,key: tuple[str,bool]) -> None:
        dialog=self._dialogs.get(key)
        if dialog is None:return
        section,_embedded=key; plane=self._plane(key)
        context=self.service.context(section,plane)
        sources=self.service.available_sources(section)
        current=context.get("detector_name") or (self.service.preferred_source(section,plane) if plane else None)
        dialog.configure_detectors(sources,current)
        dialog.set_context(context)

    def _error(self,title: str,error: Exception,key: tuple[str,bool] | None=None) -> None:
        if key is not None:
            dialog=self._dialogs.get(key)
            if dialog is not None:dialog.set_error(error)
        self.qt_session.sigError.emit(str(title),error)

    def _observation_changed(self,key: tuple[str,bool],value) -> None:
        plane=str(value or "").strip() or None
        self._plane_by_context[key]=plane; self._render(key)

    def _compute(self,key: tuple[str,bool],values: Mapping[str,Any]) -> None:
        section,_embedded=key; plane=self._plane(key)
        if plane is None:
            self._error("Geometry calibration failed",RuntimeError("Select an observation plane first."),key); return
        try:
            px=float(values.get("period_x")); py=float(values.get("period_y"))
            if str(values.get("period_unit") or "")=="um":
                px=self.service.period_from_um(section,px,"x")
                py=self.service.period_from_um(section,py,"y")
            self.service.compute_target(
                section,plane,grid_x=int(values.get("grid_x",5)),grid_y=int(values.get("grid_y",4)),
                period_x_px=px,period_y_px=py,source=str(values.get("detector") or ""),
                on_complete=lambda success,error,k=key:self._compute_done(k,success,error),
            )
        except Exception as error:
            self._error("Geometry calibration failed",error,key); self._render(key)

    def _compute_done(self,key: tuple[str,bool],success: bool,error: Exception | None) -> None:
        self._render(key)
        if not success and error is not None:
            self._error("Geometry calibration failed",error,key)
        elif success:
            dialog=self._dialogs.get(key)
            if dialog is not None:
                dialog.measurement_view.set_measurement_status(
                    "Geometry calibration target is displayed. Acquire/load and localize."
                )

    def _acquire(self,key: tuple[str,bool],source: str) -> None:
        section,_embedded=key; plane=self._plane(key)
        if plane is None:
            self._error("Geometry calibration acquisition failed",RuntimeError("Select an observation plane first."),key); return
        try:
            self.service.acquire(
                section,plane,source,
                on_complete=lambda success,error,k=key:self._acquire_done(k,success,error),
            )
        except Exception as error:self._error("Geometry calibration acquisition failed",error,key)

    def _acquire_done(self,key: tuple[str,bool],success: bool,error: Exception | None) -> None:
        self._render(key)
        if not success and error is not None:self._error("Geometry calibration acquisition failed",error,key)

    def _load(self,key: tuple[str,bool]) -> None:
        section,_embedded=key; plane=self._plane(key)
        if plane is None:
            self._error("Geometry calibration image load failed",RuntimeError("Select an observation plane first."),key); return
        dialog=self._dialogs.get(key)
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
            self.service.set_measurement(section,plane,measurement); self._render(key)
        except Exception as error:self._error("Geometry calibration image load failed",error,key)

    def _localize(self,key: tuple[str,bool],parameters: Mapping[str,Any]) -> None:
        section,_embedded=key; plane=self._plane(key)
        if plane is None:
            self._error("Geometry calibration localization failed",RuntimeError("Select an observation plane first."),key); return
        try:self.service.localize(section,plane,parameters); self._render(key)
        except Exception as error:self._error("Geometry calibration localization failed",error,key)

    def _analyze(self,key: tuple[str,bool]) -> None:
        section,_embedded=key; plane=self._plane(key)
        if plane is None:return
        try:self.service.analyze(section,plane); self._render(key)
        except Exception as error:self._error("Geometry calibration analysis failed",error,key)

    def _save(self,key: tuple[str,bool]) -> None:
        section,_embedded=key; plane=self._plane(key)
        if plane is None:return
        try:
            self.service.save(section,plane); self._render(key)
            self.qt_session.synchronize_section(section); self.sigSaved.emit(section,plane)
        except Exception as error:self._error("Saving geometry calibration failed",error,key)

    def _save_manual(self,key: tuple[str,bool],orientation: str) -> None:
        section,_embedded=key; plane=self._plane(key)
        if plane is None:
            self._error("Saving manual geometry failed",RuntimeError("Select an observation plane first."),key); return
        try:
            self.service.save_manual(section,plane,orientation); self._render(key)
            self.qt_session.synchronize_section(section); self.sigSaved.emit(section,plane)
        except Exception as error:self._error("Saving manual geometry failed",error,key)

    def _finish(self,key: tuple[str,bool]) -> None:
        section,_embedded=key
        try:self.service.finish(section); self._render(key)
        except Exception as error:self._error("Finishing geometry calibration failed",error,key)


__all__=["GeometryCalibrationCoordinator"]
