import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices import (
    DeviceId, DeviceLifecycleAction, DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError, DeviceLifecycleResult,
    DeviceRuntimeMode, HardwareDeviceId,
)
from .DetectorManager import DetectorManager, DetectorAction, DetectorNumberParameter


class _TISLifecycle:
    """Reconnect one TIS camera without replacing its detector manager."""

    def __init__(self, manager, detectorsManager, detectorName):
        self._manager = manager
        self._detectorsManager = detectorsManager
        self._detectorName = detectorName
        self.hardware_id = HardwareDeviceId(
            category='detector', key=f'detector:{detectorName}'
        )
        self.capabilities = DeviceLifecycleCapabilities(reconnect=True)

    def _unsupported(self, action):
        raise DeviceLifecycleNotSupportedError(
            f'TIS lifecycle does not yet support {action.value}.'
        )

    def connect(self):
        return self._unsupported(DeviceLifecycleAction.CONNECT)

    def disconnect(self):
        return self._unsupported(DeviceLifecycleAction.DISCONNECT)

    def probe(self):
        return self._unsupported(DeviceLifecycleAction.PROBE)

    def shutdown(self):
        return self._unsupported(DeviceLifecycleAction.SHUTDOWN)

    def reconnect(self):
        device_id = DeviceId('detector', self._detectorName)
        with self._detectorsManager.detectorLifecycleMaintenance(
            self._detectorName
        ):
            try:
                hardware_connected = self._manager._reconnectCameraBackend()
            except Exception as exc:
                self._manager._setConnectionError(
                    exc, summary='TIS camera reconnect failed'
                )
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary='TIS camera reconnect failed',
                    details=str(exc),
                    affected_device_ids=(device_id,),
                )

            # A completed backend replacement makes any stop failure from the
            # retired backend obsolete, including real -> mock fallback.
            self._detectorsManager.clearFaultAfterHardwareReplacement(
                self._detectorName
            )

        if hardware_connected:
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=True,
                summary='TIS camera reconnected; acquisition remains stopped',
                affected_device_ids=(device_id,),
            )

        if self._manager._configuredForMock:
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=True,
                summary='Mock TIS camera reinitialized; acquisition remains stopped',
                affected_device_ids=(device_id,),
            )

        return DeviceLifecycleResult(
            hardware_id=self.hardware_id,
            action=DeviceLifecycleAction.RECONNECT,
            success=False,
            summary='TIS hardware reconnect failed; mock fallback active',
            details=self._manager.connectionStatusDetails,
            affected_device_ids=(device_id,),
        )


class TISManager(DetectorManager):
    """ DetectorManager that deals with TheImagingSource cameras and the
    parameters for frame extraction from them.

    Manager properties:

    - ``cameraListIndex`` -- the camera's index in the TIS camera list (list
      indexing starts at 0); set this string to an invalid value, e.g. the
      string "mock" to load a mocker
    - ``tis`` -- dictionary of TIS camera properties
    - ``cameraPixelSizeUm`` -- optically effective (sample-plane) pixel size in
      micrometers, i.e. physical sensor pitch divided by total optical
      magnification. Exposed at runtime as the 'Camera pixel size' detector
      parameter. Default: 0.15 µm.
    """

    def __init__(self, detectorInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        self._cameraId = detectorInfo.managerProperties['cameraListIndex']
        self._startupCameraProperties = dict(
            detectorInfo.managerProperties['tis']
        )
        self._detectorLifecycle = None
        self._running = False
        self._adjustingParameters = False
        self.__image = None

        self._camera = self._getTISObj(self._cameraId)
        self._applyCameraProperties(self._camera, self._startupCameraProperties)

        fullShape = (self._camera.getPropertyValue('image_width'),
                     self._camera.getPropertyValue('image_height'))

        self.crop(hpos=0, vpos=0, hsize=fullShape[0], vsize=fullShape[1])

        # Prepare parameters. Hardware-facing values start from the same setup
        # values applied above, so reconnect can replay the manager's current
        # runtime state rather than silently reverting to hard-coded defaults.
        parameters = {
            'exposure': DetectorNumberParameter(
                group='Misc',
                value=self._startupCameraProperties.get('exposure', 100),
                valueUnits='ms', editable=True,
            ),
            'gain': DetectorNumberParameter(
                group='Misc',
                value=self._startupCameraProperties.get('gain', 1),
                valueUnits='arb.u.', editable=True,
            ),
            'brightness': DetectorNumberParameter(
                group='Misc',
                value=self._startupCameraProperties.get('brightness', 1),
                valueUnits='arb.u.', editable=True,
            ),
            'Camera pixel size': DetectorManager.makeCameraPixelSizeParameter(
                detectorInfo
            ),
        }

        # Resolve through the manager at call time. Binding directly to the
        # startup backend would leave this action pointing at a retired camera
        # after reconnect.
        actions = {
            'More properties': DetectorAction(
                group='Misc', func=self.openPropertiesDialog
            )
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

        super().setParameter(name, value)

        if name in self._CAMERA_PROPERTIES:
            self._camera.setPropertyValue(name, value)

        return self.parameters

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

    @property
    def _configuredForMock(self):
        return str(self._cameraId).strip().lower().startswith('mock')

    def _bindDetectorLifecycleHost(self, detectorsManager, detectorName):
        """Bind the acquisition owner after DetectorsManager construction."""
        self._detectorLifecycle = _TISLifecycle(
            self, detectorsManager, detectorName
        )

    def getDeviceLifecycle(self):
        """Return the already-bound lifecycle without touching hardware."""
        return self._detectorLifecycle

    @staticmethod
    def _applyCameraProperties(camera, properties):
        for propertyName, propertyValue in properties.items():
            camera.setPropertyValue(propertyName, propertyValue)

    def _closeCameraBackend(self, camera):
        if camera is None:
            return
        model = getattr(camera, 'model', 'unknown')
        try:
            close = getattr(camera, 'close', None)
            if callable(close):
                close()
            else:
                stop = getattr(camera, 'stop_live', None)
                if callable(stop):
                    stop()
        except Exception as exc:
            # Reconnect must still get a chance to create a fresh backend when
            # the disconnected SDK object itself refuses to close cleanly.
            self.__logger.warning(
                f'Error while retiring TIS camera {model}: {exc}'
            )

    def _reconnectCameraBackend(self):
        """Replace the TIS backend and replay ImSwitch-owned runtime state.

        Called only inside ``DetectorsManager.detectorLifecycleMaintenance``.
        The manager object, parameters and ROI therefore remain stable while
        the SDK camera object is replaced. The new backend always remains idle.
        Returns whether the replacement is real hardware (rather than mock).
        """
        runtime_properties = {
            name: self.parameters[name].value
            for name in self._CAMERA_PROPERTIES
            if name in self.parameters
        }
        hpos, vpos = self.frameStart
        hsize, vsize = self.shape

        old_camera = self._camera
        self._running = False
        self._adjustingParameters = True
        self.__image = None
        self._camera = None
        self._closeCameraBackend(old_camera)

        camera = None
        try:
            camera = self._getTISObj(self._cameraId)
            self._applyCameraProperties(camera, self._startupCameraProperties)
            self._applyCameraProperties(camera, runtime_properties)
            camera.setROI(hpos, vpos, hsize, vsize)
        except Exception:
            self._closeCameraBackend(camera)
            raise
        finally:
            self._adjustingParameters = False

        self._camera = camera
        self._setModel(camera.model)
        self._running = False
        self.__image = None
        if self.runtimeMode is DeviceRuntimeMode.REAL:
            self._setConnected('TIS camera reconnected')
        return self.runtimeMode is DeviceRuntimeMode.REAL

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
        camera, self._camera = self._camera, None
        try:
            self._closeCameraBackend(camera)
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
