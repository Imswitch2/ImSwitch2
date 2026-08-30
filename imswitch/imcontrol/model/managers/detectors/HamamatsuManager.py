from imswitch.imcommon.model import initLogger
from .DetectorManager import (
    DetectorManager, DetectorNumberParameter, DetectorListParameter
)
import copy

class HamamatsuManager(DetectorManager):
    """ DetectorManager that deals with the Hamamatsu parameters and frame
    extraction for a Hamamatsu camera.

    Manager properties:

    - ``cameraListIndex`` -- the camera's index in the Hamamatsu camera list
      (list indexing starts at 0); set this to an invalid value, e.g. the
      string "mock" to load a mocker
    - ``hamamatsu`` -- dictionary of DCAM API properties to pass to the driver
    - ``cameraPixelSizeUm`` -- optically effective (sample-plane) pixel size in
      micrometers, i.e. physical sensor pitch divided by total optical
      magnification. Exposed at runtime as the 'Camera pixel size' detector
      parameter. Default: 0.15 µm.
    """

    def __init__(self, detectorInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self.__name = name

        self._camera = self._getCameraObj(detectorInfo.managerProperties['cameraListIndex'])
        self._binning = 1

        # Let a simulated NIDAQ scan trigger frames on a mock camera the way the
        # hardware camera TTL would, so scan-once/scan-lapse recordings get the
        # expected number of frames with no real hardware. No-op for real
        # cameras (no mockTrigger) or a real NIDAQ (signal never fires).
        nidaqManager = _lowLevelManagers.get('nidaqManager')
        if (nidaqManager is not None and getattr(nidaqManager, 'isSimulated', False)
                and hasattr(self._camera, 'mockTrigger')):
            nidaqManager.sigSimScanFrameTrigger.connect(self.__onSimScanFrameTrigger)

        for propertyName, propertyValue in detectorInfo.managerProperties['hamamatsu'].items():
            self._camera.setPropertyValue(propertyName, propertyValue)

        fullShape = (self._camera.getPropertyValue('image_width')[0],
                     self._camera.getPropertyValue('image_height')[0])

        model = self._camera.camera_model.decode('utf-8')

        # Prepare parameters
        parameters = {
            'Set exposure time': DetectorNumberParameter(group='Timings', value=0,
                                                         valueUnits='s', editable=True),
            'Real exposure time': DetectorNumberParameter(group='Timings', value=0,
                                                          valueUnits='s', editable=False),
            'Internal frame interval': DetectorNumberParameter(group='Timings', value=0,
                                                               valueUnits='s', editable=False),
            'Readout time': DetectorNumberParameter(group='Timings', value=0,
                                                    valueUnits='s', editable=False),
            'Internal frame rate': DetectorNumberParameter(group='Timings', value=0,
                                                           valueUnits='fps', editable=False),
            'Trigger source': DetectorListParameter(group='Acquisition mode',
                                                    value='Internal trigger',
                                                    options=['Internal trigger',
                                                             'External "start-trigger"',
                                                             'External "frame-trigger"'],
                                                    editable=True),
            'Camera pixel size': DetectorManager.makeCameraPixelSizeParameter(detectorInfo)
        }

        super().__init__(detectorInfo, name, fullShape=fullShape, supportedBinnings=[1, 2, 4],
                         model=model, parameters=parameters, croppable=True)
        self._updatePropertiesFromCamera()
        super().setParameter('Set exposure time', self.parameters['Real exposure time'].value)

    def wait_and_get_NewFrame(self, properFrame=False):
        self.__logger.info("start wait_and_get_NewFrame")

        # Obtenir le compteur de frames actuel
        last_frame_count = self._camera.getAq_Info()[1]

        # Attendre la frame suivante (i.e., un nouveau f_count)
        self._camera.wait_next_frame(last_frame_count)

        if properFrame:
            # Forcer l'attente d'une frame de plus (ex. pour éviter une frame trop rapide juste après démarrage)
            new_last_frame_count = self._camera.getAq_Info()[1]
            self._camera.wait_next_frame(new_last_frame_count)

        img = self.getLatestFrame()
        new_frame = copy.deepcopy(img)

        self.__logger.info("end wait_and_get_NewFrame")
        return new_frame



    def getLatestFrame(self, is_save=True):
        #CHECK: is_save=False was default in old etsted-improvements, was there a reason?
        return self._camera.getLast()

    def getChunk(self):
        return self._camera.getFrames()[0]

    def flushBuffers(self):
        self._camera.updateIndices()

    def mockTrigger(self, n=1):
        if hasattr(self._camera, 'mockTrigger'):
            self._camera.mockTrigger(n)

    def mockStartScan(self, scanInfoDict, signalDict):
        self.flushBuffers()

    def mockStopScan(self):
        self.flushBuffers()

    def mockScanDone(self):
        return True

    def __onSimScanFrameTrigger(self, detectorName, nFrames):
        """Simulation only: forward a simulated NIDAQ scan's frame triggers to
        this detector's mock camera."""
        if detectorName == self.__name:
            self.mockTrigger(nFrames)

    def crop(self, hpos, vpos, hsize, vsize):
        """Crop the camera readout and keep the manager in sync with DCAM.

        Hamamatsu cameras collapse the writable subarray ranges to the full
        sensor while ``subarray_mode`` is off.  State restore runs before the
        first acquisition, so it cannot rely on ``captureSetup()`` to enable
        that mode: it must be enabled before writing a custom ROI.  Conversely,
        returning to the full chip explicitly disables subarray mode.
        """

        # DCAM's dcam_setgetpropertyvalue (called by CameraTIS/HamamatsuCamera's
        # setPropertyValue) sets AND reads back the value in one call, returning
        # whatever the driver actually applied. The subarray_* properties have a
        # hardware-defined step granularity (DCAM_PARAM_PROPERTYATTR.valuestep,
        # e.g. multiples of 4) -- a requested hpos/vpos/hsize/vsize that isn't
        # aligned to that step gets silently snapped by the driver to the nearest
        # valid value. Previously this method ignored the returned value and
        # trusted the *requested* numbers for self._frameStart/self._shape,
        # which then disagreed with the actual hardware ROI whenever the
        # requested value needed snapping -- causing position/size drift between
        # what was asked for and what frames actually look like (intermittent:
        # only shows up for off-step ROIs, not every crop). Now the
        # driver-applied values are used instead.
        requested = (int(hpos), int(vpos), int(hsize), int(vsize))
        fullFrame = (0, 0, int(self.fullShape[0]), int(self.fullShape[1]))
        isFullFrame = requested == fullFrame

        def cropAction():
            # DCAM must see ON before any custom subarray write.  Inferring the
            # mode from the current hsize/vsize (HamamatsuCamera.setSubArrayMode)
            # is a chicken-and-egg problem on a cold start: with mode OFF those
            # sizes can only read back as the full sensor.
            if not isFullFrame:
                self._camera.setPropertyValue('subarray_mode', b'ON')

            self._camera.setPropertyValue('subarray_vpos', 0)
            self._camera.setPropertyValue('subarray_hpos', 0)
            self._camera.setPropertyValue('subarray_vsize', self.fullShape[1])
            self._camera.setPropertyValue('subarray_hsize', self.fullShape[0])

            if not isFullFrame:
                self._camera.setPropertyValue('subarray_vsize', requested[3])
                self._camera.setPropertyValue('subarray_hsize', requested[2])
                self._camera.setPropertyValue('subarray_vpos', requested[1])
                self._camera.setPropertyValue('subarray_hpos', requested[0])
            else:
                # Switch OFF only after positions and sizes are back at their
                # full-frame values, while those values are still writable.
                self._camera.setPropertyValue('subarray_mode', b'OFF')

        self._performSafeCameraAction(cropAction)

        # Read the final hardware state rather than trusting either the request
        # or an individual set call.  This catches both normal DCAM step snapping
        # and writes that were accepted by the API but rejected/clamped by the
        # camera.
        applied = self._readSubarrayGeometry()
        modeIsOn = self._readSubarrayMode()

        # Keep the internal geometry truthful even when the restore is rejected;
        # SettingsController will refresh the widget from these values after it
        # records the raised exception as a restore warning.
        self._frameStart = applied[:2]
        self._shape = applied[2:]

        expectedModeIsOn = not isFullFrame

        if expectedModeIsOn and applied == fullFrame:
            raise RuntimeError(
                f'Hamamatsu camera rejected requested ROI '
                f'(hpos={requested[0]}, vpos={requested[1]}, hsize={requested[2]}, '
                f'vsize={requested[3]}); the camera remained at full frame '
                f'(hpos=0, vpos=0, hsize={fullFrame[2]}, vsize={fullFrame[3]}).'
            )

        # modeIsOn is None on a camera that reports no subarray mode at all;
        # there is then nothing to cross-check, and the geometry read above is
        # the only evidence of what the ROI actually is.
        if modeIsOn is not None and modeIsOn != expectedModeIsOn:
            expectedMode = 'ON' if expectedModeIsOn else 'OFF'
            raise RuntimeError(
                f'Hamamatsu camera did not set subarray_mode={expectedMode} for requested ROI '
                f'(hpos={requested[0]}, vpos={requested[1]}, hsize={requested[2]}, '
                f'vsize={requested[3]}); hardware reports '
                f'subarray_mode={"ON" if modeIsOn else "OFF"}.'
            )

        if applied != requested:
            self.__logger.warning(
                f'Hamamatsu camera snapped the requested ROI '
                f'(hpos={requested[0]}, vpos={requested[1]}, hsize={requested[2]}, '
                f'vsize={requested[3]}) to '
                f'(hpos={applied[0]}, vpos={applied[1]}, hsize={applied[2]}, '
                f'vsize={applied[3]}) due to hardware step constraints; '
                f'using the actually-applied values.'
            )

    def _readSubarrayGeometry(self):
        """The ROI as ``(hpos, vpos, hsize, vsize)``, as the camera reports it.

        ``HamamatsuCamera.getPropertyValue`` returns a bare ``False`` -- not a
        ``(value, type)`` pair -- for a property the camera does not expose, and
        some cameras omit ``subarray_hpos``/``subarray_vpos`` entirely while
        subarray mode is off. Subscripting that result would make every crop
        raise ``TypeError`` on those cameras, including the full-chip crop
        SettingsController performs while starting up, which would leave
        ImSwitch unable to start at all. A missing position therefore reads as
        0 and a missing size as the full chip -- which is what a camera that
        does not report them is actually doing.
        """

        def read(propertyName, fallback):
            try:
                return int(self._camera.getPropertyValue(propertyName)[0])
            except Exception:
                return fallback

        return (read('subarray_hpos', 0),
                read('subarray_vpos', 0),
                read('subarray_hsize', self.fullShape[0]),
                read('subarray_vsize', self.fullShape[1]))

    def _readSubarrayMode(self):
        """``True``/``False`` for a reported subarray mode, ``None`` when the
        camera exposes none (DCAM text form is ``ON``/``OFF``, numeric form is
        ``DCAMPROP_MODE__ON == 2``). ``None`` means "nothing to verify", not
        "off": treating an unreported mode as off would fail every custom ROI
        on such a camera. """
        try:
            value = self._camera.getPropertyValue('subarray_mode')[0]
        except Exception:
            return None

        if isinstance(value, bytes):
            value = value.decode(errors='replace')
        if isinstance(value, str):
            text = value.strip().upper()
            return text == 'ON' if text in ('ON', 'OFF') else None

        try:
            return int(value) == 2
        except (TypeError, ValueError):
            return None

    def setBinning(self, binning):
        super().setBinning(binning)

        binstring = f'{binning}x{binning}'
        coded = binstring.encode('ascii')

        self._performSafeCameraAction(
            lambda: self._camera.setPropertyValue('binning', coded)
        )

    def setParameter(self, name, value):
        if name == 'Set exposure time':
            self._setExposure(value)
            self._updatePropertiesFromCamera()
            # Exposure can be quantized by the camera.  Cache the read-back,
            # never a value that was merely requested.
            super().setParameter(name, self.parameters['Real exposure time'].value)
        elif name == 'Trigger source':
            try:
                self._setTriggerSource(value)
            except Exception:
                # A multi-property trigger write can fail part-way through.
                # Best-effort read-back keeps the cache truthful before the
                # exception is surfaced to SettingsController.
                try:
                    self._updateTriggerSourceFromCamera()
                except Exception as readbackError:
                    self.__logger.warning(
                        f'Could not read trigger state after a failed write: {readbackError}'
                    )
                raise

            actual = self._updateTriggerSourceFromCamera()
            if actual != value:
                raise RuntimeError(
                    f'Hamamatsu camera rejected trigger source {value!r}; '
                    f'hardware remains at {actual!r}.'
                )
        else:
            super().setParameter(name, value)

        return self.parameters

    def getParameter(self, name):
        """Gets a parameter value and returns the value.
        If the parameter doesn't exist, return 0."""
        try:
            value, _ = self._camera.getPropertyValue(name)
        except Exception as e:
            value = 0
            self.__logger.error(f'Camera parameter {name} not available: {e}. Returning default value 0.')
        return value

    def getAdvancedPropertyInfo(self):
        """
        Get detailed metadata for all camera properties.
        
        This manager-level method wraps the camera interface's property introspection,
        providing a UI-safe way to query all available DCAM properties without
        directly depending on DCAM internals.
        
        Returns:
            list: List of dictionaries containing property metadata:
                - name: Property name (str)
                - id: DCAM property ID (int)
                - value: Current value (int, float, or None)
                - type: Property type - 'MODE', 'LONG', 'REAL', or 'NONE' (str)
                - readable: Whether property is readable (bool)
                - writable: Whether property is writable (bool)
                - range: (min, max) tuple or None
                - text_options: Dict of text options or None
                - error: Error message or None
        
        Example:
            >>> props = manager.getAdvancedPropertyInfo()
            >>> for prop in props:
            ...     if prop['writable']:
            ...         print(f"{prop['name']}: {prop['value']}")
        """
        try:
            return self._camera.getAdvancedPropertyInfo()
        except Exception as e:
            self.__logger.error(f'Failed to get advanced property info: {e}')
            return []
    
    def setAdvancedProperty(self, propertyName, value):
        """
        Set an advanced camera property value safely.

        This manager-level method wraps the camera interface's setPropertyValue,
        providing safe property changes by stopping/restarting acquisition if needed.
        The method validates the property exists and logs all changes.

        When any ``subarray_*`` property is changed, the internal frame-start and
        shape state (``_frameStart`` / ``_shape``) are re-synchronised from the
        camera so that the non-advanced ROI code path always sees a consistent view.

        Args:
            propertyName: Name of the property to set (str)
            value: New value for the property (int, float, bytes, or str)
                  For text/MODE properties, can be text key (bytes/str) or numeric value

        Returns:
            dict: Result dictionary containing:
                - success: Whether the operation succeeded (bool)
                - value: The actual value set by camera (float) or None if failed
                - error: Error message if failed, None otherwise (str or None)

        Example:
            >>> result = manager.setAdvancedProperty('exposure_time', 0.05)
            >>> if result['success']:
            ...     print(f"Exposure set to {result['value']}")
            ... else:
            ...     print(f"Failed: {result['error']}")
        """
        try:
            # Log the change request
            self.__logger.info(f'Setting advanced property {propertyName} to {value}')

            # Convert string to bytes for text properties if needed
            if isinstance(value, str) and not isinstance(value, bytes):
                value = value.encode('utf-8')

            # Use safe camera action to handle acquisition stop/restart if needed
            result_value = None
            def set_property():
                nonlocal result_value
                result_value = self._camera.setPropertyValue(propertyName, value)

            self._performSafeCameraAction(set_property)

            # Keep internal ROI state in sync when any subarray property is changed.
            # Without this, _frameStart / _shape would be stale after an advanced-settings
            # edit, causing crop() and abortROI() to overwrite or misreport the ROI.
            if propertyName.startswith('subarray_'):
                self._syncSubarrayState()

            # Log success
            self.__logger.info(f'Successfully set {propertyName} to {result_value}')

            return {
                'success': True,
                'value': result_value,
                'error': None
            }

        except Exception as e:
            # Log failure
            error_msg = str(e)
            self.__logger.error(f'Failed to set {propertyName} to {value}: {error_msg}')

            return {
                'success': False,
                'value': None,
                'error': error_msg
            }

    def _syncSubarrayState(self):
        """Read the four subarray properties back from the camera and update
        ``_frameStart`` / ``_shape`` so that ImSwitch's internal state matches
        the hardware after any out-of-band subarray change (e.g. via the
        Advanced Properties tab).

        Missing properties (some cameras omit ``subarray_hpos`` / ``subarray_vpos``
        when subarray mode is off) are treated as 0 / full-chip size.
        """
        geometry = self._readSubarrayGeometry()
        self._frameStart = geometry[:2]
        self._shape = geometry[2:]
        self.__logger.debug(
            f'Synced subarray state: frameStart={self._frameStart}, shape={self._shape}'
        )

    def startAcquisition(self):
        self._camera.startAcquisition()

    def stopAcquisition(self):
        self._camera.stopAcquisition()

    def _setExposure(self, time):
        self._camera.setPropertyValue('exposure_time', time)

    def _setTriggerSource(self, source):
        if source == 'Internal trigger':
            def triggerAction():
                self._camera.setPropertyValue('trigger_source', 1)

        elif source == 'External "start-trigger"':
            def triggerAction():
                self._camera.setPropertyValue('trigger_source', 2)
                self._camera.setPropertyValue('trigger_mode', 6)

        elif source == 'External "frame-trigger"':
            def triggerAction():
                self._camera.setPropertyValue('trigger_source', 2)
                self._camera.setPropertyValue('trigger_mode', 1)
        else:
            raise ValueError(f'Invalid trigger source "{source}"')

        self._performSafeCameraAction(triggerAction)

    def _performSafeCameraAction(self, function):
        """ This method is used to change those camera properties that need
        the camera to be idle to be able to be adjusted.
        """
        try:
            function()
        except Exception:
            self.stopAcquisition()
            function()
            self.startAcquisition()

    def _updatePropertiesFromCamera(self):
        self.setParameter('Real exposure time', self._camera.getPropertyValue('exposure_time')[0])
        self.setParameter('Internal frame interval',
                          self._camera.getPropertyValue('internal_frame_interval')[0])
        self.setParameter('Readout time', self._camera.getPropertyValue('timing_readout_time')[0])
        self.setParameter('Internal frame rate',
                          self._camera.getPropertyValue('internal_frame_rate')[0])

        try:
            self._updateTriggerSourceFromCamera()
        except RuntimeError as error:
            # Some camera/setup combinations expose trigger states that are not
            # represented by the three UI choices.  Do not make an unrelated
            # timing refresh or manager construction fail because of that.
            self.__logger.warning(f'Could not map camera trigger state to the UI: {error}')

    def _updateTriggerSourceFromCamera(self):
        """Read the trigger configuration and make the cache authoritative."""
        triggerSource = self._camera.getPropertyValue('trigger_source')[0]
        if triggerSource == 1:
            actual = 'Internal trigger'
        else:
            triggerMode = self._camera.getPropertyValue('trigger_mode')[0]
            if triggerSource == 2 and triggerMode == 6:
                actual = 'External "start-trigger"'
            elif triggerSource == 2 and triggerMode == 1:
                actual = 'External "frame-trigger"'
            else:
                raise RuntimeError(
                    f'Unsupported Hamamatsu trigger state '
                    f'(trigger_source={triggerSource}, trigger_mode={triggerMode}).'
                )

        # super(), not self: this is a read-back and must not write the hardware.
        super().setParameter('Trigger source', actual)
        return actual

    def _getCameraObj(self, cameraId):
        try:
            from imswitch.imcontrol.model.interfaces.hamamatsu import HamamatsuCameraMR
            self.__logger.debug(f'Trying to initialize Hamamatsu camera {cameraId}')
            camera = HamamatsuCameraMR(cameraId)
            self._setConnected("Hamamatsu camera initialized")
        except Exception as e:
            self.__logger.warning(
                f'Failed to initialize Hamamatsu camera {cameraId}, loading mocker: {e}',
                exc_info=True
            )
            from imswitch.imcontrol.model.interfaces.hamamatsu_mock import MockHamamatsu
            camera = MockHamamatsu()
            if str(cameraId).strip().lower().startswith("mock"):
                self._setMockActive("Mock camera configured")
            else:
                self._setConnectionError(
                    e,
                    summary="Hamamatsu camera initialization failed; mock fallback active",
                    mock_active=True,
                )

        self.__logger.info(f'Initialized camera, model: {camera.camera_model}')
        return camera


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
