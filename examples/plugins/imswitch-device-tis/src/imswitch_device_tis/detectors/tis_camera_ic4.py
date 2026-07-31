"""DetectorManager for The Imaging Source cameras via IC Imaging Control 4."""

from __future__ import annotations

import numpy as np

from imswitch.pluginapi import (
    DetectorManager, DetectorListParameter, DetectorNumberParameter,
)

try:
    from imswitch.imcommon.model import initLogger
except Exception:  # pragma: no cover - only when run outside ImSwitch
    import logging

    def initLogger(owner, instanceName=None):
        name = type(owner).__name__
        if instanceName:
            name = f"{name}.{instanceName}"
        return logging.getLogger(name)


class TISCameraIC4Manager(DetectorManager):
    """DetectorManager for TIS cameras on the IC4 SDK.

    Replaces the in-tree ``TISManager``, which drives the same hardware through
    the legacy ``pyicic``/IC3 wrapper and cannot capture one distinct frame per
    hardware trigger. See ``docs/design/plans/tis-camera-ic4-migration.md``.

    This manager deliberately does **not** claim the ``TISManager`` name. The
    registry resolves plugin contributions ahead of in-tree managers, so an alias
    would silently swap every existing TIS setup onto this driver on ``pip
    install`` — a hardware-affecting change with nothing but a log warning.
    Migration is opt-in per setup file until the rig signs off.

    Manager properties:

    - ``cameraSerial`` -- camera serial number (str or null). null opens the
      first available camera; a value starting with ``"MOCK_"`` loads the mock
      camera for headless testing. Serial rather than list index on purpose: an
      index is positional and silently rebinds when USB enumeration order
      changes.
    - ``cameraPixelSizeUm`` -- optically effective (sample-plane) pixel size in
      micrometers. Exposed as the ``'Camera pixel size'`` parameter. Default 0.15.
    - ``maxQueuedFrames`` -- bound on retained undrained frames. Default 256.
    - ``pixelFormat`` -- GenICam PixelFormat, e.g. ``"Mono8"`` / ``"Mono16"``.
      null leaves the device default.
    - ``defaults`` -- dict of initial values:
        - ``exposure_us`` (default 5000), ``gain`` (default 0)
        - ``trigger_mode``: ``'Off'`` | ``'Hardware'`` (default ``'Off'``)
        - ``trigger_source``: e.g. ``'Line1'`` (default null = device default)
    """

    _TRIGGER_MODES = ('Off', 'Hardware')

    def __init__(self, detectorInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        props = detectorInfo.managerProperties
        # An empty string means the user cleared the field: the config editor
        # saves a blank text box as "", not null. Read that as "first available
        # camera" rather than hunting for a camera whose serial is empty.
        serial = props.get('cameraSerial') or None
        maxQueued = int(props.get('maxQueuedFrames', 256))
        pixelFormat = props.get('pixelFormat', None)
        defaults = props.get('defaults', {})

        self._camera = self._initCamera(serial, maxQueued, pixelFormat)
        self._triggerSource = defaults.get('trigger_source', None)
        self._triggerActivation = defaults.get('trigger_activation', None)

        fullShape = (
            self._camera.sensor_width_pixels,
            self._camera.sensor_height_pixels,
        )

        parameters = {
            'Exposure': DetectorNumberParameter(
                group='Acquisition', value=defaults.get('exposure_us', 5000),
                valueUnits='µs', editable=True,
            ),
            'Gain': DetectorNumberParameter(
                group='Acquisition', value=defaults.get('gain', 0),
                valueUnits='dB', editable=True,
            ),
            'Trigger Mode': DetectorListParameter(
                group='Trigger', value=defaults.get('trigger_mode', 'Off'),
                options=list(self._TRIGGER_MODES), editable=True,
            ),
            'Camera pixel size': DetectorManager.makeCameraPixelSizeParameter(
                detectorInfo
            ),
        }

        super().__init__(
            detectorInfo, name, fullShape=fullShape, supportedBinnings=[1],
            model=self._camera.model, parameters=parameters, croppable=True,
        )

        for paramName in ('Exposure', 'Gain', 'Trigger Mode'):
            self.setParameter(paramName, self.parameters[paramName].value)

        self.__logger.info(
            f'Initialized {self._camera.model}, serial: {self._camera.serial}'
        )

    def _initCamera(self, serial, maxQueued, pixelFormat):
        """Open the camera, falling back to the mock on any failure."""
        from .._ic4_driver import IC4Camera, MockIC4Camera

        useMock = serial is not None and str(serial).startswith('MOCK_')

        if not useMock:
            try:
                return IC4Camera(
                    serial=serial,
                    max_queued_frames=maxQueued,
                    pixel_format=pixelFormat,
                )
            except Exception as e:
                # Deliberately broad. The SDK signals *every* device-level
                # failure as ``IC4Exception``, which derives straight from
                # Exception — including 'permission denied', raised whenever
                # another process (a second ImSwitch, IC Capture) holds the
                # camera. Enumerating exception types here previously let that
                # case escape the constructor and take the detector down at
                # startup, which is precisely what the mock fallback exists to
                # prevent. exc_info because the message alone rarely says which
                # of those it was.
                self.__logger.warning(
                    f'Failed to initialize IC4 TIS camera: {e}. Loading mock '
                    f'camera — frames will be synthetic.', exc_info=True
                )

        return MockIC4Camera(
            serial=serial, max_queued_frames=maxQueued, pixel_format=pixelFormat
        )

    # -- frames -----------------------------------------------------------

    @property
    def dtype(self):
        """Authoritative frame dtype, taken from the camera's PixelFormat.

        Overrides the base implementation, which infers from the last live-view
        frame and reports uint16 until one exists. That default is wrong for a
        Mono8 camera at exactly the wrong moment: ``dtype`` is the storer's
        single source of truth for the recording dataset, and a recording can be
        started before live view has ever run.
        """
        if self._camera is None:
            return super().dtype
        return self._camera.dtype

    def getChunk(self):
        """Drain every frame retained since the last call, as ``(N, H, W)``.

        Two invariants, both learned the hard way on the sibling ThorCam TSI
        manager (see ``docs/advanced_scan_triggered_recording_audit.md``):

        1. **Never fabricate.** An empty ``(0, H, W)`` is returned when no
           trigger has fired. ``getLatestFrame`` may synthesize a frame for live
           view; doing that here would write fabricated data into a recording.
        2. **Never return 2-D.** ``DetectorManager.readChunk`` does
           ``list.extend(...)``, which iterates axis 0 — a 2-D return silently
           decomposes one image into H row vectors, and the recording completes
           early with one mangled frame.
        """
        frames = self._camera.pop_frames()
        if not frames:
            return np.empty(
                (0, self._camera.image_height_pixels,
                 self._camera.image_width_pixels),
                dtype=self.dtype,
            )
        return np.stack(frames, axis=0)

    def getLatestFrame(self, is_save=False):
        """Newest retained frame for live view, without draining.

        Non-destructive on purpose: live view runs on a timer and must not steal
        frames from a concurrent recording or BeadRec scan, both of which pull
        through ``readChunk``.
        """
        frame = self._camera.latest_frame()
        if frame is None:
            return np.zeros(
                (self._camera.image_height_pixels,
                 self._camera.image_width_pixels),
                dtype=self.dtype,
            )
        return frame

    def flushBuffers(self):
        self._camera.flush()

    # -- parameters -------------------------------------------------------

    def setParameter(self, name, value):
        super().setParameter(name, value)

        # Store back what the device took, not what was asked for. Exposure is
        # clamped to the sensor's range (and to the frame period, which the
        # trigger state moves), and gain to its own range. Leaving the request
        # in the parameter makes the settings tree, the saved snapshot and the
        # recording metadata all report a value the camera never ran with.
        if name == 'Exposure':
            self._storeApplied(name, self._camera.set_exposure_us(value))
        elif name == 'Gain':
            self._storeApplied(name, self._camera.set_gain(value))
        elif name == 'Trigger Mode':
            self.setTriggerEnabled(value == 'Hardware')

        return self.parameters

    def _storeApplied(self, name, applied):
        """Record the device-reported value, ignoring drivers that report none."""
        if applied is None:
            return
        try:
            self.parameters[name].value = float(applied)
        except (TypeError, ValueError):
            self.__logger.debug(
                f'Camera reported a non-numeric {name} ({applied!r}); keeping '
                f'the requested value.'
            )

    def getParameter(self, name):
        if name not in self.parameters:
            raise AttributeError(f'Non-existent parameter "{name}" specified')
        return self.parameters[name].value

    def setTriggerEnabled(self, enabled: bool):
        """Arm or disarm the hardware trigger in software.

        Exposed as a method as well as a parameter so a scan can arm before a
        bead scan and disarm afterwards, replacing the manual step in the
        vendor's properties dialog that the legacy IC3 path required.
        """
        self._camera.set_trigger_enabled(
            enabled,
            source=self._triggerSource,
            activation=self._triggerActivation,
        )
        self.parameters['Trigger Mode'].value = 'Hardware' if enabled else 'Off'

    # -- acquisition ------------------------------------------------------

    def startAcquisition(self):
        self._camera.start_stream()

    def stopAcquisition(self):
        self._camera.stop_stream()

    def stopAcquisitionForROIChange(self):
        self._camera.stop_stream()

    def crop(self, hpos, vpos, hsize, vsize):
        wasStreaming = self._camera.is_streaming
        self._camera.stop_stream()
        # The sensor constrains Width/Height/Offset to an increment, so the
        # applied region is routinely not the requested one. Record what the
        # camera took: frameStart/shape feed the settings display, the viewer
        # scale, the recording shape and the OME metadata, and storing the
        # request there makes every one of them describe frames that do not
        # exist. (HamamatsuManager.crop reads back for the same reason.)
        applied = self._camera.set_roi(hpos, vpos, hsize, vsize)
        # Stale frames in the queue still carry the old geometry; stacking those
        # with post-crop frames in getChunk would raise on mismatched shapes.
        self._camera.flush()
        appliedHpos, appliedVpos, appliedHsize, appliedVsize = (
            self._resolveAppliedRoi(applied, (hpos, vpos, hsize, vsize))
        )
        if (appliedHpos, appliedVpos, appliedHsize, appliedVsize) != (
                hpos, vpos, hsize, vsize):
            self.__logger.warning(
                f'Camera snapped the requested ROI (x0={hpos}, y0={vpos}, '
                f'width={hsize}, height={vsize}) to (x0={appliedHpos}, '
                f'y0={appliedVpos}, width={appliedHsize}, '
                f'height={appliedVsize}) due to sensor step/range constraints; '
                f'using the actually-applied values.'
            )
        self._frameStart = (appliedHpos, appliedVpos)
        self._shape = (appliedHsize, appliedVsize)
        if wasStreaming:
            self._camera.start_stream()

    def _resolveAppliedRoi(self, applied, requested):
        """The ROI the camera reports, or the request when it reports nothing.

        A driver that returns ``None`` (an older stub, or a mock written before
        ``set_roi`` had a return value) must degrade to the previous behaviour
        rather than corrupting frameStart/shape with a ``TypeError``.
        """
        try:
            x0, y0, width, height = applied
            return int(x0), int(y0), int(width), int(height)
        except (TypeError, ValueError):
            self.__logger.debug(
                'Camera driver did not report the applied ROI; assuming the '
                'requested one was taken.'
            )
            return requested

    def setBinning(self, binning):
        super().setBinning(binning)

    def finalize(self):
        super().finalize()
        if self._camera is not None:
            self._camera.dispose()
            self._camera = None
