from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.transforms import AcquisitionContext
from imswitch.imcommon.model import dirtools, initLogger
from imswitch.imcommon.view import guitools
from imswitch.imcontrol.controller.display_transform import (
    display_transform_from_properties,
)
from imswitch.imcontrol.model import configfiletools
from imswitch.imcontrol.model.SetupInfo import (
    CalibrationsInfo,
    TransformCalibrationSetupInfo,
)
from imswitch.imcontrol.model.transform_correspondence import (
    ulenses_grid_strategy_spec,
)
from ..basecontrollers import ImConWidgetController


class TransformCalibrationController(ImConWidgetController):
    """ImControl host adapter for the shared TransformCalibrationWidget.

    Calibration/fitting remains in imcommon. This controller supplies live
    displayed images, ImControl's uLens localizer, napari registration targets,
    setup persistence and detector-context validation.
    """

    calibrationKey = "image_registration"

    def __init__(self, *args, imageWidget=None, ulensesWidget=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)
        self._imageWidget = imageWidget
        self._ulensesWidget = ulensesWidget
        self._appliedTarget = None

        self.calibrationDir = os.path.join(
            dirtools.UserFileDirs.Root,
            "calibrations",
            "transforms",
        )
        os.makedirs(self.calibrationDir, exist_ok=True)

        self._widget.registerCorrespondenceStrategy(
            ulenses_grid_strategy_spec(self._ulensesGuessProvider),
            select=True,
        )
        self._widget.setHostIntegrationEnabled(True)

        self._widget.sigSnapSourceRequested.connect(self.snapMoving)
        self._widget.sigSnapTargetRequested.connect(self.snapReference)
        self._widget.sigTransformChanged.connect(self._transformChanged)
        self._widget.sigApplyToggled.connect(self.setApplyEnabled)
        self._widget.sigApplyTargetChanged.connect(self._applyTargetChanged)
        self._widget.sigSetStartupRequested.connect(self.setAsStartupCalibration)

        if self._imageWidget is None:
            self._widget.setStatus(
                "Transform calibration requires the Image widget to be enabled."
            )
            self._widget.setHostIntegrationEnabled(False)
            return

        targets_changed = getattr(
            self._imageWidget, "sigTransformRegistrationTargetsChanged", None
        )
        if targets_changed is not None:
            targets_changed.connect(self._refreshTargets)

        self._refreshTargets()
        self._loadStartupCalibrationIfConfigured()

    def snapMoving(self):
        self._snap("source")

    def snapReference(self):
        self._snap("target")

    def _snap(self, role):
        try:
            detector_name = self._master.detectorsManager.getCurrentDetectorName()
            image, scale = self._imageWidget.getLiveLayerSnapshot(detector_name)
            context = AcquisitionContext(
                detector=detector_name,
                image_shape=tuple(image.shape),
                pixel_size_um=tuple(float(v) for v in scale[-2:]),
                extra={"display_transform": self._displayTransformDict(detector_name)},
            )
        except Exception as error:
            self.__logger.warning("Could not snap transform-calibration image: %s", error)
            self._widget.setStatus(f"Could not snap image: {error}")
            return

        if role == "source":
            self._widget.setSourceImage(image, detector_name, context=context)
        else:
            self._widget.setTargetImage(image, detector_name, context=context)

    def _ulensesGuessProvider(self):
        if self._ulensesWidget is None:
            return None
        try:
            _x, _y, pixel_size, period_x, period_y = self._ulensesWidget.getParameters()
            if pixel_size <= 0:
                return None
            return float(period_x / pixel_size), float(period_y / pixel_size)
        except Exception:
            return None

    def _transformChanged(self, transform):
        preferred = None
        if transform is not None and transform.source_context.detector:
            preferred = {
                "kind": "live_source",
                "name": transform.source_context.detector,
            }
        self._refreshTargets(preferred=preferred)

        if transform is None:
            self._clearAppliedTarget()
            self._widget.setApplyChecked(False)
            return
        if self._widget.applyCheck.isChecked():
            self._applyCurrentTarget()

    def _refreshTargets(self, preferred=None):
        if self._imageWidget is None:
            return
        targets = self._imageWidget.getTransformRegistrationTargets()
        descriptors = [target for _label, target in targets]
        if self._appliedTarget is not None and self._appliedTarget not in descriptors:
            self._clearAppliedTarget()
            self._widget.setApplyChecked(False)
        current = preferred if preferred is not None else self._widget.selectedApplyTarget()
        self._widget.setApplyTargets(targets, preferred=current)

    def setApplyEnabled(self, enabled):
        if not enabled:
            self._clearAppliedTarget()
            return
        if self._widget.transform is None:
            self._clearAppliedTarget()
            self._widget.setApplyChecked(False)
            self._widget.setStatus("No transform loaded")
            return
        self._applyCurrentTarget()

    def _applyTargetChanged(self, _target):
        if self._widget.applyCheck.isChecked():
            self._applyCurrentTarget()

    def _applyCurrentTarget(self):
        transform = self._widget.transform
        target = self._widget.selectedApplyTarget()
        if transform is None or target is None:
            self._clearAppliedTarget()
            self._widget.setApplyChecked(False)
            return

        matrix = transform.as_matrix()
        if matrix is None:
            self._clearAppliedTarget()
            self._widget.setApplyChecked(False)
            self._widget.setStatus(
                f"{transform.kind} cannot be applied as a non-resampling display affine"
            )
            return

        problem = self._validateDisplayContext(transform, target)
        if problem is not None:
            self._clearAppliedTarget()
            self._widget.setApplyChecked(False)
            self._widget.setStatus(f"Calibration context mismatch: {problem}")
            QtWidgets.QMessageBox.warning(
                self._widget,
                "Transform calibration",
                f"Calibration was not applied because {problem}.",
            )
            return

        try:
            if self._appliedTarget is not None and self._appliedTarget != target:
                self._clearAppliedTarget()
            target_scale = transform.target_context.pixel_size_um
            self._imageWidget.applyTransformRegistration(
                target,
                matrix,
                target_scale=target_scale,
            )
        except Exception as error:
            self.__logger.warning("Could not apply transform calibration: %s", error)
            self._clearAppliedTarget()
            self._widget.setApplyChecked(False)
            self._widget.setStatus(f"Apply failed: {error}")
            return

        self._appliedTarget = dict(target)
        self._widget.setStatus(
            f"Applied {transform.source_frame} -> {transform.target_frame} to "
            f"{target['kind'].replace('_', ' ')} {target['name']}"
        )

    def _clearAppliedTarget(self):
        if self._appliedTarget is None or self._imageWidget is None:
            self._appliedTarget = None
            return
        target = self._appliedTarget
        self._appliedTarget = None
        try:
            self._imageWidget.clearTransformRegistration(target)
        except Exception as error:
            self.__logger.warning(
                "Could not clear transform registration from %s: %s", target, error
            )

    def _validateDisplayContext(self, transform, apply_target):
        for label, context in (
            ("moving", transform.source_context),
            ("reference", transform.target_context),
        ):
            detector = context.detector
            expected = context.extra.get("display_transform") if context.extra else None
            if detector and expected is not None:
                current = self._displayTransformDict(detector)
                if dict(expected) != current:
                    return (
                        f"the {label} detector {detector!r} displayTransform changed "
                        f"from {dict(expected)} to {current}"
                    )

        if apply_target.get("kind") == "live_source":
            detector = apply_target.get("name")
            if detector == transform.source_context.detector:
                try:
                    image, scale = self._imageWidget.getLiveLayerSnapshot(detector)
                except Exception as error:
                    return f"the moving live source is unavailable ({error})"
                if transform.source_context.image_shape is not None:
                    if tuple(image.shape) != tuple(transform.source_context.image_shape):
                        return (
                            f"the moving detector shape changed from "
                            f"{tuple(transform.source_context.image_shape)} to {tuple(image.shape)}"
                        )
                expected_scale = transform.source_context.pixel_size_um
                if expected_scale is not None and not np.allclose(
                    np.asarray(scale[-2:], dtype=float),
                    np.asarray(expected_scale[-2:], dtype=float),
                    rtol=1e-6,
                    atol=1e-12,
                ):
                    return (
                        f"the moving detector pixel scale changed from "
                        f"{tuple(expected_scale[-2:])} to {tuple(scale[-2:])}"
                    )
        return None

    def _displayTransformDict(self, detector_name):
        info = self._setupInfo.detectors.get(detector_name)
        transform = display_transform_from_properties(
            None if info is None else info.managerProperties
        )
        return {
            "rotation": int(transform.rotation),
            "flip_x": bool(transform.flip_x),
            "flip_y": bool(transform.flip_y),
        }

    def _loadStartupCalibrationIfConfigured(self):
        info = self._startupInfo(create=False)
        if info is None or not info.auto_load or not info.calibration_file:
            return
        path = self._resolveCalibrationPath(info.calibration_file)
        if not os.path.isfile(path):
            self.__logger.warning("Startup transform calibration not found: %s", path)
            self._widget.setStatus(f"Startup calibration not found: {os.path.basename(path)}")
            return
        self._widget.loadTransformFromPath(path, prefix=f"Loaded startup {Path(path).name}")

    def setAsStartupCalibration(self):
        transform = self._widget.transform
        if transform is None:
            return
        path = self._widget.currentTransformPath
        if path is None or not path.is_file():
            default_name = datetime.now().strftime(
                "%Y-%m-%d_%H%M%S_image_registration.json"
            )
            suggested = os.path.join(self.calibrationDir, default_name)
            chosen = guitools.askForFilePath(
                self._widget,
                "Save startup transform calibration",
                defaultFolder=suggested,
                nameFilter="Transforms (*.json *.h5 *.hdf5);;All files (*)",
                isSaving=True,
            )
            if not chosen:
                return
            saved = self._widget.saveTransformToPath(chosen)
            if saved is None:
                return
            path = saved

        try:
            self._startupInfo(create=True)
            self._setupInfo.calibrations.transforms[self.calibrationKey] = (
                TransformCalibrationSetupInfo(
                    auto_load=True,
                    calibration_file=self._storedCalibrationPath(path),
                )
            )
            configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        except Exception as error:
            self.__logger.warning("Failed to save transform startup setting: %s", error)
            self._widget.setStatus(f"Could not set startup calibration: {error}")
            return
        self._widget.setStatus(f"Startup calibration: {Path(path).name}")

    def _startupInfo(self, create):
        calibrations = getattr(self._setupInfo, "calibrations", None)
        if calibrations is None:
            if not create:
                return None
            calibrations = CalibrationsInfo()
            setattr(self._setupInfo, "calibrations", calibrations)
        elif isinstance(calibrations, dict):
            transforms = {}
            for key, value in calibrations.get("transforms", {}).items():
                transforms[str(key)] = self._coerceStartupInfo(value)
            calibrations = CalibrationsInfo(transforms=transforms)
            setattr(self._setupInfo, "calibrations", calibrations)

        transforms = getattr(calibrations, "transforms", None)
        if transforms is None:
            if not create:
                return None
            transforms = {}
            object.__setattr__(calibrations, "transforms", transforms)

        info = transforms.get(self.calibrationKey)
        if info is None:
            if not create:
                return None
            info = TransformCalibrationSetupInfo()
            transforms[self.calibrationKey] = info
        elif isinstance(info, dict):
            info = self._coerceStartupInfo(info)
            transforms[self.calibrationKey] = info
        return info

    @staticmethod
    def _coerceStartupInfo(value):
        if isinstance(value, TransformCalibrationSetupInfo):
            return value
        if not isinstance(value, dict):
            value = {}
        return TransformCalibrationSetupInfo(
            auto_load=bool(value.get("auto_load", False)),
            calibration_file=value.get("calibration_file"),
        )

    def _storedCalibrationPath(self, value):
        path = Path(value).expanduser().resolve()
        calibration_dir = Path(self.calibrationDir).expanduser().resolve()
        try:
            return str(path.relative_to(calibration_dir))
        except ValueError:
            return str(path)

    def _resolveCalibrationPath(self, value):
        path = os.path.expanduser(str(value))
        if not os.path.isabs(path):
            path = os.path.join(self.calibrationDir, path)
        return os.path.abspath(path)

    def closeEvent(self):
        self._clearAppliedTarget()
        return super().closeEvent()
