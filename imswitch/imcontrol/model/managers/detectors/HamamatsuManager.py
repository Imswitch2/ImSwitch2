from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices import (
    DeviceId, DeviceLifecycleAction, DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError, DeviceLifecycleResult,
    DeviceRuntimeMode, HardwareDeviceId,
)
from .DetectorManager import (
    DetectorManager, DetectorNumberParameter, DetectorListParameter
)
import copy


_HAMAMATSU_RECONNECT_HINT = (
    'If the camera was unplugged, keep USB connected, restart the camera, '
    'wait until it is ready, then press Reconnect again.'
)


class _HamamatsuLifecycle:
    """Reconnect one Hamamatsu camera without replacing its detector manager."""

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
            f'Hamamatsu lifecycle does not yet support {action.value}.'
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
                    exc, summary='Hamamatsu camera reconnect failed'
                )
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary='Hamamatsu camera reconnect failed',
                    details=f'{exc} {_HAMAMATSU_RECONNECT_HINT}',
                    affected_device_ids=(device_id,),
                )

            self._detectorsManager.clearFaultAfterHardwareReplacement(
                self._detectorName
            )

        if hardware_connected:
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=True,
                summary='Hamamatsu camera reconnected; acquisition remains stopped',
                affected_device_ids=(device_id,),
            )

        if self._manager._configuredForMock:
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=True,
                summary='Mock Hamamatsu camera reinitialized; acquisition remains stopped',
                affected_device_ids=(device_id,),
            )

        return DeviceLifecycleResult(
            hardware_id=self.hardware_id,
            action=DeviceLifecycleAction.RECONNECT,
            success=False,
            summary=(
                'Hamamatsu hardware reconnect failed; mock fallback active. '
                'Try restarting the camera with USB plugged in.'
            ),
            details=(
                f'{self._manager.connectionStatusDetails} '
                f'{_HAMAMATSU_RECONNECT_HINT}'
            ).strip(),
            affected_device_ids=(device_id,),
        )


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

        self._cameraId = detectorInfo.managerProperties['cameraListIndex']
        self._startupCameraProperties = dict(
            detectorInfo.managerProperties['hamamatsu']
        )
        self._mockSensorShape = self._configuredMockSensorShape(
            self._startupCameraProperties
        )
        self._detectorLifecycle = None
        self._advancedPropertyOverrides = {}
        # A failed reconnect with DCAM NOCONNECTION marks the legacy
        # process-global session stale. A later explicit reconnect may rebuild
        # that session (currently only for a single Hamamatsu camera).
        self._dcamSessionStale = False
        self._detectorsManagerForLifecycle = None
        self._camera = self._getCameraObj(self._cameraId)
        self._binning = 1

        # A simulated scan signal may outlive backend replacement. Connect the
        # manager callback once and let mockTrigger() decide whether the current
        # backend supports simulated frame triggers.
        nidaqManager = _lowLevelManagers.get('nidaqManager')
        if nidaqManager is not None and getattr(nidaqManager, 'isSimulated', False):
            nidaqManager.sigSimScanFrameTrigger.connect(self.__onSimScanFrameTrigger)

        self._applyCameraProperties(self._camera, self._startupCameraProperties)

        fullShape = self._cameraSensorShape(self._camera)
        model = self._cameraModel(self._camera)

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

    _ADVANCED_RECONNECT_EXCLUDED = frozenset({
        'exposure_time', 'trigger_source', 'trigger_mode', 'binning',
        'image_width', 'image_height', 'image_framebytes',
        'subarray_hpos', 'subarray_vpos', 'subarray_hsize',
        'subarray_vsize', 'subarray_mode',
    })

    @staticmethod
    def _cameraModel(camera):
        model = getattr(camera, 'camera_model', 'unknown')
        if isinstance(model, bytes):
            return model.decode('utf-8', errors='replace')
        return str(model)

    @staticmethod
    def _configuredMockSensorShape(properties):
        """Best available configured sensor size for hardware-free startup."""
        def positive(name, fallback=None):
            try:
                value = int(properties.get(name, fallback))
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        width = positive('image_width')
        height = positive('image_height')
        if width is None:
            hpos = positive('subarray_hpos', 0) or 0
            hsize = positive('subarray_hsize')
            width = hpos + hsize if hsize is not None else 2048
        if height is None:
            vpos = positive('subarray_vpos', 0) or 0
            vsize = positive('subarray_vsize')
            height = vpos + vsize if vsize is not None else 2048
        return int(width), int(height)

    @staticmethod
    def _cameraSensorShape(camera):
        """Return authoritative un-cropped sensor dimensions where available."""
        try:
            width = int(getattr(camera, 'max_width'))
            height = int(getattr(camera, 'max_height'))
            if width > 0 and height > 0:
                return width, height
        except (TypeError, ValueError, AttributeError):
            pass
        return (int(camera.getPropertyValue('image_width')[0]),
                int(camera.getPropertyValue('image_height')[0]))

    def _applyCameraProperties(self, camera, properties):
        for propertyName, propertyValue in properties.items():
            camera.setPropertyValue(propertyName, propertyValue)

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
            if result_value is False:
                raise RuntimeError(
                    f'Hamamatsu camera rejected property {propertyName!r}'
                )

            # Keep the manager's canonical state synchronized for properties
            # that also have dedicated ImSwitch controls. Other successful
            # advanced-property writes are remembered explicitly and replayed
            # after a backend replacement.
            if propertyName.startswith('subarray_'):
                self._syncSubarrayState()
            elif propertyName == 'exposure_time':
                self._updatePropertiesFromCamera()
                super().setParameter(
                    'Set exposure time', self.parameters['Real exposure time'].value
                )
            elif propertyName in ('trigger_source', 'trigger_mode'):
                try:
                    self._updateTriggerSourceFromCamera()
                except RuntimeError:
                    pass
            elif propertyName == 'binning':
                self._syncBinningFromCamera(result_value)
            elif propertyName not in self._ADVANCED_RECONNECT_EXCLUDED:
                self._advancedPropertyOverrides[propertyName] = result_value

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

    def _syncBinningFromCamera(self, value=None):
        if value is None:
            try:
                value = self._camera.getPropertyValue('binning')[0]
            except Exception:
                return
        if isinstance(value, bytes):
            value = value.decode(errors='replace')
        if isinstance(value, str):
            value = value.lower().split('x', 1)[0]
        try:
            binning = int(round(float(value)))
        except (TypeError, ValueError):
            return
        if binning in self.supportedBinnings:
            super().setBinning(binning)

    def startAcquisition(self):
        self._camera.startAcquisition()

    def stopAcquisition(self):
        self._camera.stopAcquisition()

    def _setExposure(self, time):
        self._camera.setPropertyValue('exposure_time', time)

    @staticmethod
    def _setTriggerSourceOnCamera(camera, source):
        if source == 'Internal trigger':
            camera.setPropertyValue('trigger_source', 1)
        elif source == 'External "start-trigger"':
            camera.setPropertyValue('trigger_source', 2)
            camera.setPropertyValue('trigger_mode', 6)
        elif source == 'External "frame-trigger"':
            camera.setPropertyValue('trigger_source', 2)
            camera.setPropertyValue('trigger_mode', 1)
        else:
            raise ValueError(f'Invalid trigger source "{source}"')

    def _setTriggerSource(self, source):
        self._performSafeCameraAction(
            lambda: self._setTriggerSourceOnCamera(self._camera, source)
        )

    def _performSafeCameraAction(self, function):
        """Change a property that may require an idle camera.

        Acquisition ownership is authoritative: if the first write fails,
        retry after a stop and restart only when a lease says the camera was
        actually in use. This is also safe during lifecycle maintenance, where
        the detector necessarily has zero leases and must remain stopped.
        """
        wasLeased = self.acquisitionLeased
        try:
            function()
        except Exception:
            self.stopAcquisition()
            function()
            if wasLeased:
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

    @property
    def _configuredForMock(self):
        return str(self._cameraId).strip().lower().startswith('mock')

    def _bindDetectorLifecycleHost(self, detectorsManager, detectorName):
        """Bind the acquisition owner after DetectorsManager construction."""
        self._detectorsManagerForLifecycle = detectorsManager
        self._detectorLifecycle = _HamamatsuLifecycle(
            self, detectorsManager, detectorName
        )

    def getDeviceLifecycle(self):
        """Return the already-bound lifecycle without touching hardware."""
        return self._detectorLifecycle

    @staticmethod
    def _isDcamNoConnectionError(exc):
        """Recognize the legacy DCAM NOCONNECTION failure narrowly."""
        current = exc
        while current is not None:
            text = str(current).lower()
            if '0x80000f07' in text or 'no camera connection' in text:
                return True
            current = current.__cause__ or current.__context__
        return False

    def _closeCameraBackend(self, camera):
        if camera is None:
            return True
        try:
            shutdown = getattr(camera, 'shutdown', None)
            if callable(shutdown):
                shutdown()
            return True
        except Exception as exc:
            self.__logger.warning(
                f'Error while closing Hamamatsu camera '
                f'{self._cameraModel(camera)}: {exc}'
            )
            return False

    def _configuredHamamatsuManagerNames(self):
        """Return configured Hamamatsu detector names, or ``None`` if unknown."""
        host = self._detectorsManagerForLifecycle
        get_names = getattr(host, 'getAllDeviceNames', None)
        if not callable(get_names):
            return None
        return get_names(
            condition=lambda manager: isinstance(manager, HamamatsuManager)
        )

    def _resetStaleDcamSessionSingleCamera(self):
        """Rebuild a stale process-global DCAM session for one camera only."""
        names = self._configuredHamamatsuManagerNames()
        if names is None:
            raise RuntimeError(
                'Cannot verify single-camera DCAM reset safety: detector host '
                'inventory is unavailable.'
            )
        if len(names) != 1 or names[0] != self.__name:
            raise RuntimeError(
                'Automatic DCAM session reset is currently supported only when '
                f'exactly one Hamamatsu camera is configured; found {names!r}.'
            )

        from imswitch.imcontrol.model.interfaces.hamamatsu import (
            resetDcamSessionSingleCamera,
        )
        discovered = resetDcamSessionSingleCamera()
        if discovered < 1:
            raise RuntimeError(
                'DCAM session reset completed but no Hamamatsu camera was discovered.'
            )

        self._dcamSessionStale = False
        return discovered

    def _makeRealCamera(self):
        from imswitch.imcontrol.model.interfaces.hamamatsu import HamamatsuCameraMR
        return HamamatsuCameraMR(self._cameraId)

    def _makeMockCamera(self):
        from imswitch.imcontrol.model.interfaces.hamamatsu_mock import MockHamamatsu
        return MockHamamatsu(*self._mockSensorShape)

    def _configureReplacementCamera(
        self, camera, *, runtimeExposure, runtimeTrigger, runtimeBinning,
        runtimeAdvanced, oldFrameStart, oldShape, oldFullShape, wasFullFrame
    ):
        """Replay ImSwitch-owned state onto one candidate replacement backend."""
        self._applyCameraProperties(camera, self._startupCameraProperties)
        newFullShape = self._cameraSensorShape(camera)
        self._camera = camera
        self._setFullShape(newFullShape)

        try:
            # Dedicated manager state is authoritative for properties that have
            # normal ImSwitch controls. Advanced-property overrides follow.
            camera.setPropertyValue(
                'binning', f'{runtimeBinning}x{runtimeBinning}'.encode('ascii')
            )
            camera.setPropertyValue('exposure_time', runtimeExposure)
            self._setTriggerSourceOnCamera(camera, runtimeTrigger)
            self._applyCameraProperties(camera, runtimeAdvanced)

            if wasFullFrame:
                self.crop(0, 0, newFullShape[0], newFullShape[1])
            else:
                self.crop(
                    oldFrameStart[0], oldFrameStart[1],
                    oldShape[0], oldShape[1]
                )

            self._setModel(self._cameraModel(camera))
            self._updatePropertiesFromCamera()
            super().setParameter(
                'Set exposure time', self.parameters['Real exposure time'].value
            )
        except Exception:
            if self._camera is camera:
                self._camera = None
            self._setFullShape(oldFullShape)
            self._frameStart = oldFrameStart
            self._shape = oldShape
            raise

    def _reconnectCameraBackend(self):
        """Replace the backend and replay runtime state without replacing manager.

        If a reconnect fails with DCAM NOCONNECTION, mark the process-global
        legacy DCAM3 session stale. The next explicit reconnect rebuilds that
        session before opening the camera, currently only when exactly one
        Hamamatsu manager is configured.
        """
        runtimeExposure = self.parameters['Set exposure time'].value
        runtimeTrigger = self.parameters['Trigger source'].value
        runtimeBinning = self.binning
        runtimeAdvanced = dict(self._advancedPropertyOverrides)
        oldFrameStart = tuple(self.frameStart)
        oldShape = tuple(self.shape)
        oldFullShape = tuple(self.fullShape)
        wasFullFrame = oldFrameStart == (0, 0) and oldShape == oldFullShape
        replay = dict(
            runtimeExposure=runtimeExposure,
            runtimeTrigger=runtimeTrigger,
            runtimeBinning=runtimeBinning,
            runtimeAdvanced=runtimeAdvanced,
            oldFrameStart=oldFrameStart,
            oldShape=oldShape,
            oldFullShape=oldFullShape,
            wasFullFrame=wasFullFrame,
        )

        oldCamera, self._camera = self._camera, None

        if self._configuredForMock:
            self._closeCameraBackend(oldCamera)
            camera = self._makeMockCamera()
            self._configureReplacementCamera(camera, **replay)
            self._setMockActive('Mock camera configured')
            return False

        camera = None
        try:
            if not self._closeCameraBackend(oldCamera):
                raise RuntimeError('Could not close the previous Hamamatsu camera.')

            if self._dcamSessionStale:
                self._resetStaleDcamSessionSingleCamera()

            camera = self._makeRealCamera()
            self._configureReplacementCamera(camera, **replay)
        except Exception as exc:
            if self._isDcamNoConnectionError(exc):
                self._dcamSessionStale = True

            self.__logger.warning(f'Hamamatsu hardware reconnect failed: {exc}')
            self._closeCameraBackend(camera)

            camera = self._makeMockCamera()
            try:
                self._configureReplacementCamera(camera, **replay)
            except Exception:
                self._closeCameraBackend(camera)
                raise

            detail = f'{exc} {_HAMAMATSU_RECONNECT_HINT}'
            self._setConnectionError(
                RuntimeError(detail),
                summary=(
                    'Hamamatsu camera reconnect failed; mock fallback active. '
                    'Try restarting the camera with USB plugged in.'
                ),
                mock_active=True,
            )
            self.__logger.info(
                f'Initialized camera, model: {self._cameraModel(camera)}'
            )
            return False

        self._dcamSessionStale = False
        self._setConnected('Hamamatsu camera reconnected')
        self.__logger.info(
            f'Initialized camera, model: {self._cameraModel(camera)}'
        )
        return True

    def _getCameraObj(self, cameraId):
        try:
            if str(cameraId).strip().lower().startswith('mock'):
                raise RuntimeError('Mock camera configured')
            camera = self._makeRealCamera()
            self._setConnected("Hamamatsu camera initialized")
        except Exception as e:
            if not str(cameraId).strip().lower().startswith('mock'):
                self.__logger.warning(
                    f'Failed to initialize Hamamatsu camera {cameraId}, loading mocker: {e}',
                    exc_info=True
                )
            camera = self._makeMockCamera()
            if str(cameraId).strip().lower().startswith("mock"):
                self._setMockActive("Mock camera configured")
            else:
                self._setConnectionError(
                    e,
                    summary="Hamamatsu camera initialization failed; mock fallback active",
                    mock_active=True,
                )

        self.__logger.info(f'Initialized camera, model: {self._cameraModel(camera)}')
        return camera

    def finalize(self):
        self.close()

    def close(self):
        camera, self._camera = self._camera, None
        if camera is None:
            return
        self.__logger.info(
            f'Shutting down Hamamatsu camera, model: {self._cameraModel(camera)}'
        )
        self._closeCameraBackend(camera)


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
