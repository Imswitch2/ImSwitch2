import numpy as np

from imswitch.imcommon.model import initLogger
from .DetectorManager import DetectorManager, DetectorAction, DetectorNumberParameter


class TISManager(DetectorManager):
    """ DetectorManager that deals with TheImagingSource cameras and the
    parameters for frame extraction from them.

    Manager properties:

    - ``cameraListIndex`` -- the camera's index in the TIS camera list (list
      indexing starts at 0); set this string to an invalid value, e.g. the
      string "mock" to load a mocker
    - ``tis`` -- dictionary of TIS camera properties. Only ``image_width``
      and ``image_height`` are used. ``exposure``, ``gain`` and ``brightness``
      are not applied at startup: the camera keeps its own settings, and the
      parameters show what it reports
    - ``cameraPixelSizeUm`` -- optically effective (sample-plane) pixel size in
      micrometers, i.e. physical sensor pitch divided by total optical
      magnification. Exposed at runtime as the 'Camera pixel size' detector
      parameter. Default: 0.15 µm.
    """

    def __init__(self, detectorInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        self._camera = self._getTISObj(detectorInfo.managerProperties['cameraListIndex'])
        
        self._running = False
        self._adjustingParameters = False
        self.__image = None

        notApplied = []
        for propertyName, propertyValue in detectorInfo.managerProperties['tis'].items():
            if propertyName in self._CAMERA_PROPERTIES:
                # These never reached the camera before (CameraTIS assigned
                # them to the wrong object), so shipped setups carry
                # placeholder zeros here. Applying them now would start the
                # camera at minimum exposure.
                notApplied.append(propertyName)
                continue
            self._camera.setPropertyValue(propertyName, propertyValue)
        if notApplied:
            self.__logger.info(
                f'Not applying startup {", ".join(notApplied)} from '
                f'managerProperties["tis"]; the camera keeps its current settings.'
            )

        fullShape = (self._camera.getPropertyValue('image_width'),
                     self._camera.getPropertyValue('image_height'))

        self.crop(hpos=0, vpos=0, hsize=fullShape[0], vsize=fullShape[1])

        # Prepare parameters, starting from what the camera reports
        parameters = {
            'exposure': DetectorNumberParameter(
                group='Misc', value=self._readCameraProperty('exposure', 100),
                valueUnits='ms', editable=True),
            'gain': DetectorNumberParameter(
                group='Misc', value=self._readCameraProperty('gain', 1),
                valueUnits='arb.u.', editable=True),
            'brightness': DetectorNumberParameter(
                group='Misc', value=self._readCameraProperty('brightness', 1),
                valueUnits='arb.u.', editable=True),
            'Camera pixel size': DetectorManager.makeCameraPixelSizeParameter(
                detectorInfo
            ),
        }

        # Prepare actions
        actions = {
            'More properties': DetectorAction(group='Misc',
                                              func=self.openPropertiesDialog)
        }

        super().__init__(detectorInfo, name, fullShape=fullShape, supportedBinnings=[1],
                         model=self._camera.model, parameters=parameters, actions=actions, croppable=True)

    # NOTE: do NOT override `scale` here. As a camera detector, TIS must inherit
    # DetectorManager.scale, which derives the napari layer scale from the
    # 'Camera pixel size' parameter (set above). The old `return [1, 1]` ignored
    # the configured pixel size, so the TIS layer rendered at 1 µm/px while other
    # cameras (e.g. Hamamatsu) used their real pixel size — making the TIS image
    # appear much larger in the viewer for the same physical field of view.

    def getLatestFrame(self, is_save=True):
        if not self._adjustingParameters:
            frame = self._camera.grabFrame()
            # None means the driver had no frame yet; keep the previous one
            # rather than poisoning the cache with it.
            if frame is not None:
                self.__image = frame
        return self.__image

    # Real TIS hardware properties CameraTIS.setPropertyValue understands (see
    # tiscamera.py). 'Camera pixel size' and any other DetectorManager-level
    # bookkeeping parameter are NOT camera properties: forwarding them hits
    # CameraTIS's "does not exist" fallback, which logs a warning and returns
    # False. That matters beyond the log noise — SettingsController.
    # setDetectorParameter does `c.setParameter(...) and updateParamsFromDetector(...)`,
    # so a falsy return (also true for gain/brightness set to 0) silently
    # skipped the GUI refresh. Allow-list forwarding, mirroring HamamatsuManager.
    _CAMERA_PROPERTIES = frozenset({'gain', 'brightness', 'exposure'})

    def setParameter(self, name, value):
        """Sets a parameter value and returns the updated parameters dict.
        If the parameter doesn't exist, i.e. the parameters field doesn't
        contain a key with the specified parameter name, an error will be
        raised."""

        if name in self._CAMERA_PROPERTIES:
            # The camera clamps to its range (and rounds gain/brightness to
            # integer device units): keep what it reports, not the request.
            # A rejected write raises before the parameter changes.
            value = self._camera.setPropertyValue(name, value)

        super().setParameter(name, value)

        return self.parameters

    def _readCameraProperty(self, name, fallback):
        """The camera's current value of ``name``, or ``fallback``."""
        try:
            value = self._camera.getPropertyValue(name)
        except Exception as e:
            # pyicic's IC_Exception carries its text in .message, not str().
            self.__logger.warning(
                f'Could not read {name} from the camera: {getattr(e, "message", e)}'
            )
            return fallback
        # CameraTIS reports an unknown property as False.
        return fallback if value is None or isinstance(value, bool) else value

    def getParameter(self, name):
        """Gets a parameter value and returns the value.
        If the parameter doesn't exist, i.e. the parameters field doesn't
        contain a key with the specified parameter name, an error will be
        raised."""

        if name not in self._camera.properties:
            raise AttributeError(f'Non-existent parameter "{name}" specified')

        value = self._camera.getPropertyValue(name)
        return value

    def setBinning(self, binning):
        super().setBinning(binning)

    def getChunk(self):
        frame = self._camera.grabFrame()
        if frame is None:
            # No new frame: report an empty chunk rather than repeating the
            # previous one, which would duplicate it into a recording.
            if self.__image is None:
                return np.empty((0, 0, 0), dtype=np.uint8)
            return np.empty((0, *self.__image.shape),
                            dtype=self.__image.dtype)
        return frame[np.newaxis, :, :]

    def flushBuffers(self):
        pass

    def startAcquisition(self):
        if not self._running:
            self._camera.start_live()
            self._running = True

    def stopAcquisition(self):
        if self._running:
            self._camera.suspend_live()
            self._running = False

    def stopAcquisitionForROIChange(self):
        self._camera.stop_live()
        self._running = False

    def crop(self, hpos, vpos, hsize, vsize):
        def cropAction():
            self._camera.setROI(hpos, vpos, hsize, vsize)

        self._performSafeCameraAction(cropAction)
        # TODO: unsure if frameStart is needed? Try without.
        # This should be the only place where self.frameStart is changed
        self._frameStart = (hpos, vpos)
        # Only place self.shapes is changed
        self._shape = (hsize, vsize)

    def _performSafeCameraAction(self, function):
        """ This method is used to change those camera properties that need
        the camera to be idle to be able to be adjusted.
        """
        self._adjustingParameters = True
        wasrunning = self._running
        self.stopAcquisitionForROIChange()
        function()
        if wasrunning:
            self.startAcquisition()
        self._adjustingParameters = False

    def openPropertiesDialog(self):
        self._camera.openPropertiesGUI()
        # The vendor dialog writes the camera directly; pick up its changes.
        for name in self._CAMERA_PROPERTIES:
            parameter = self.parameters[name]
            parameter.value = self._readCameraProperty(name, parameter.value)

    def _getTISObj(self, cameraId):
        try:
            from imswitch.imcontrol.model.interfaces.tiscamera import CameraTIS
            camera = CameraTIS(cameraId)
            self._setConnected("TIS camera initialized")
        except Exception as e:
            self.__logger.warning(
                f'Failed to initialize TIS camera {cameraId}, loading mocker: {e}',
                exc_info=True
            )
            from imswitch.imcontrol.model.interfaces.tiscamera_mock import MockCameraTIS
            camera = MockCameraTIS()
            if str(cameraId).strip().lower().startswith("mock"):
                self._setMockActive("Mock camera configured")
            else:
                self._setConnectionError(
                    e,
                    summary="TIS camera initialization failed; mock fallback active",
                    mock_active=True,
                )

        self.__logger.info(f'Initialized camera, model: {camera.model}')
        return camera

    def finalize(self):
        self.close()

    def close(self):
        model = getattr(self._camera, "model", "unknown")
        self.__logger.info(f"Shutting down TIS camera, model: {model}")

        self._running = False
        self._adjustingParameters = True

        try:
            close = getattr(self._camera, "close", None)
            if callable(close):
                close()
            else:
                try:
                    self._camera.stop_live()
                except Exception:
                    pass
        except Exception as e:
            self.__logger.warning(f"Error while shutting down TIS camera {model}: {e}")
        finally:
            self._adjustingParameters = False
            self.__image = None


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
