import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import initLogger


@dataclass
class DetectorAction:
    """ An action that is made available for the user to execute. """

    group: str
    """ The group to place the action in (does not need to be
    pre-defined). """

    func: callable
    """ The function that is called when the action is executed. """


@dataclass
class DetectorParameter(ABC):
    """ Abstract base class for detector parameters that are made available for
    the user to view/edit. """

    group: str
    """ The group to place the parameter in (does not need to be
    pre-defined). """

    value: Any
    """ The value of the parameter. """

    editable: bool
    """ Whether it is possible to edit the value of the parameter. """


@dataclass
class DetectorNumberParameter(DetectorParameter):
    """ A detector parameter with a numerical value. """

    value: float
    """ The value of the parameter. """

    valueUnits: str
    """ Parameter value units, e.g. "nm" or "fps". """


@dataclass
class DetectorListParameter(DetectorParameter):
    """ A detector parameter with a value from a list of options. """

    value: str
    """ The value of the parameter. """

    options: List[str]
    """ The available values to pick from. """


#: Standard parameter name for the optically effective (sample-plane) pixel
#: size of camera detectors. Used by ``DetectorManager.pixelSizeUm`` and by
#: downstream modules such as tiling, scale bars and stitching.
CAMERA_PIXEL_SIZE_PARAM = 'Camera pixel size'

#: Setup-file ``managerProperties`` key that seeds CAMERA_PIXEL_SIZE_PARAM.
CAMERA_PIXEL_SIZE_KEY = 'cameraPixelSizeUm'

#: Maximum frames retained per readChunk consumer queue. Only relevant for a
#: consumer that registered but stopped polling while another consumer keeps
#: draining; bounds the memory leak in that case.
MAX_QUEUED_CONSUMER_FRAMES = 1000


class DetectorManager(SignalInterface):
    """ Abstract base class for managers that control detectors. Each type of
    detector corresponds to a manager derived from this class. """

    sigImageUpdated = Signal(np.ndarray, bool, list)
    sigNewFrame = Signal()

    @staticmethod
    def makeCameraPixelSizeParameter(detectorInfo,
                                     default: float = 0.15
                                     ) -> 'DetectorNumberParameter':
        """ Build the standard ``'Camera pixel size'`` parameter for camera
        detector managers.

        IMPORTANT: this is the *optically effective* pixel size at the sample
        plane (physical sensor pitch divided by total optical magnification),
        in micrometers. It is **not** the physical sensor pitch — downstream
        consumers (tiling, scale bars, stitching, ...) only care about the
        sample-plane value, so that is what is exposed at runtime.

        The default value is read from
        ``detectorInfo.managerProperties['cameraPixelSizeUm']`` if present,
        otherwise falls back to ``default`` (0.15 µm, a typical high-mag
        value).

        A misspelled key or an unparseable value would otherwise fall back to
        ``default`` silently, which looks exactly like "the pixel size keeps
        resetting itself". Both cases are logged instead.
        """
        logger = initLogger('DetectorManager')
        properties = detectorInfo.managerProperties
        raw = properties.get(CAMERA_PIXEL_SIZE_KEY, None)

        if raw is None:
            # A key that only differs in case or separators is a typo, not an
            # omission -- say so rather than quietly using the default.
            def _canonical(key):
                return key.replace('_', '').replace(' ', '').lower()

            wanted = _canonical(CAMERA_PIXEL_SIZE_KEY)
            nearMisses = [key for key in properties if _canonical(key) == wanted]
            if nearMisses:
                logger.warning(
                    f'Manager property "{nearMisses[0]}" is not the pixel size'
                    f' setting -- the key must be spelled exactly'
                    f' "{CAMERA_PIXEL_SIZE_KEY}". Using the default'
                    f' {default} µm instead of the configured value.'
                )
            return DetectorNumberParameter(
                group='Miscellaneous', value=float(default),
                valueUnits='µm', editable=True,
            )

        try:
            value = float(raw)
        except (TypeError, ValueError):
            # e.g. "0,082" typed with a decimal comma.
            logger.warning(
                f'Manager property "{CAMERA_PIXEL_SIZE_KEY}" is {raw!r}, which'
                f' is not a number (a decimal comma instead of a point is the'
                f' usual cause). Using the default {default} µm.'
            )
            value = float(default)

        return DetectorNumberParameter(
            group='Miscellaneous', value=value,
            valueUnits='µm', editable=True,
        )

    @abstractmethod
    def __init__(self, detectorInfo, name: str, fullShape: Tuple[int, int],
                 supportedBinnings: List[int], model: str, *,
                 parameters: Optional[Dict[str, DetectorParameter]] = None,
                 actions: Optional[Dict[str, DetectorAction]] = None,
                 croppable: bool = True) -> None:
        """
        Args:
            detectorInfo: See setup file documentation.
            name: The unique name that the device is identified with in the
              setup file.
            fullShape: Maximum image size as a tuple ``(width, height)``.
            supportedBinnings: Supported binnings as a list.
            model: Detector device model name.
            parameters: Parameters to make available to the user to view/edit.
            actions: Actions to make available to the user to execute.
            croppable: Whether the detector image can be cropped.
        """

        super().__init__()
        self.__logger = initLogger(self, instanceName=name)

        self._detectorInfo = detectorInfo

        self._frameStart = (0, 0)
        self._shape = fullShape

        self.__name = name
        self.__model = model
        self.__parameters = parameters if parameters is not None else {}
        self.__actions = actions if actions is not None else {}
        self.__croppable = croppable

        self.__fullShape = fullShape
        self.__supportedBinnings = supportedBinnings
        self.__image = np.array([])

        # Multi-consumer chunk distribution state (see readChunk)
        self._chunkConsumers = {}
        self._chunkConsumersWarned = set()
        self._chunkConsumersLock = Lock()

        self.__forAcquisition = detectorInfo.forAcquisition
        self.__forFocusLock = detectorInfo.forFocusLock
        if not detectorInfo.forAcquisition and not detectorInfo.forFocusLock:
            raise ValueError('At least one of forAcquisition and forFocusLock must be set in'
                             ' DetectorInfo.')

        self.setBinning(supportedBinnings[0])

    def updateLatestFrame(self, init):
        """ :meta private: """
        try:
            self.__image = self.getLatestFrame()
        except Exception:
            self.__logger.error(traceback.format_exc())
        else:
            self.sigImageUpdated.emit(self.__image, init, self.scale)

    def setParameter(self, name: str, value: Any) -> Dict[str, DetectorParameter]:
        """ Sets a parameter value and returns the updated list of parameters.
        If the parameter doesn't exist, i.e. the parameters field doesn't
        contain a key with the specified parameter name, an AttributeError will
        be raised. """

        if name not in self.__parameters:
            raise AttributeError(f'Non-existent parameter "{name}" specified')

        self.__parameters[name].value = value
        return self.parameters

    def setBinning(self, binning: int) -> None:
        """ Sets the detector's binning. """

        if binning not in self.__supportedBinnings:
            raise ValueError(f'Specified binning value "{binning}" not supported by the detector')

        self._binning = binning

    @property
    def name(self) -> str:
        """ Unique detector name, defined in the detector's setup info. """
        return self.__name

    @property
    def model(self) -> str:
        """ Detector model name. """
        return self.__model

    @property
    def binning(self) -> int:
        """ Current binning. """
        return self._binning

    @property
    def supportedBinnings(self) -> List[int]:
        """ Supported binnings as a list. """
        return self.__supportedBinnings

    @property
    def frameStart(self) -> Tuple[int, int]:
        """ Position of the top left corner of the current frame as a tuple
        ``(x, y)``. """
        return self._frameStart

    @property
    def shape(self) -> Tuple[int, ...]:
        """ Current image size as a tuple ``(width, height, ...)``. """
        return self._shape

    @property
    def fullShape(self) -> Tuple[int, ...]:
        """ Maximum image size as a tuple ``(width, height, ...)``. """
        return self.__fullShape

    @property
    def image(self) -> np.ndarray:
        """ Latest LiveView image. """
        return self.__image

    @property
    def parameters(self) -> Dict[str, DetectorParameter]:
        """ Dictionary of available parameters. """
        return self.__parameters

    @property
    def actions(self) -> Dict[str, DetectorAction]:
        """ Dictionary of available actions. """
        return self.__actions

    @property
    def croppable(self) -> bool:
        """ Whether the detector supports frame cropping. """
        return self.__croppable

    @property
    def forAcquisition(self) -> bool:
        """ Whether the detector is used for acquisition. """
        return self.__forAcquisition

    @property
    def forFocusLock(self) -> bool:
        """ Whether the detector is used for focus lock. """
        return self.__forFocusLock

    @property
    def scale(self) -> List[float]:
        """ The pixel sizes in micrometers, all axes, in the format high dim
        to low dim (ex. [..., 'Z', 'Y', 'X']). Override in managers handling
        >3 dim images (e.g. APDManager). """
        return self.pixelSizeUm[1:]

    @property
    def pixelSizeUm(self) -> List[float]:
        """ The optically effective pixel size in micrometers, in 3D, in the
        format ``[Z, Y, X]``. Non-scanned ``Z`` set to 1.

        Default implementation reads from the standard
        ``'Camera pixel size'`` parameter (see
        :meth:`makeCameraPixelSizeParameter`) — this is the value used by
        all camera detectors. Scan-driven detectors (APD, PMT, Swabian, ...)
        override this property to derive pixel sizes from the scan
        configuration instead.
        """
        param = self.__parameters.get(CAMERA_PIXEL_SIZE_PARAM)
        if param is None:
            return [1.0, 1.0, 1.0]
        v = float(param.value)
        return [1.0, v, v]

    @property
    def dtype(self) -> np.dtype:
        """ The authoritative data type for this detector's recorded frames.
        
        This is the single source of truth for the storer's dataset dtype.
        For camera detectors, returns the dtype of the latest frame when
        available, otherwise defaults to uint16 (a sensible camera default).
        Scan-driven detectors (APD, PMT, Swabian) override this to return
        their known buffer dtype.
        """
        if self.__image is not None and getattr(self.__image, "size", 0) > 0:
            return np.dtype(self.__image.dtype)
        return np.dtype(np.uint16)

    @property
    def bitDepth(self) -> int:
        """ The bit depth of this detector's data type, derived from the
        authoritative dtype. """
        return int(np.dtype(self.dtype).itemsize * 8)

    @abstractmethod
    def crop(self, hpos: int, vpos: int, hsize: int, vsize: int) -> None:
        """ Crop the frame read out by the detector. """
        pass

    @abstractmethod
    def getLatestFrame(self) -> np.ndarray:
        """ Returns the frame that represents what the detector currently is
        capturing. The returned object is a numpy array of shape
        (height, width). """
        pass

    @abstractmethod
    def getChunk(self) -> np.ndarray:
        """ Returns the frames captured by the detector since getChunk was last
        called, or since the buffers were last flushed (whichever happened
        last). The returned object is a numpy array of shape
        (numFrames, height, width). """
        pass

    @abstractmethod
    def flushBuffers(self) -> None:
        """ Flushes the detector buffers so that getChunk starts at the last
        frame captured at the time that this function was called. """
        pass

    def readChunk(self, consumerKey: str) -> List[np.ndarray]:
        """ Multi-consumer variant of getChunk().

        getChunk() is a destructive read: when several components poll it on
        the same detector (e.g. the RecordingManager and BeadRec during a
        scan-once recording), each frame goes to whichever caller drained
        first and every consumer sees an incomplete stream. readChunk drains
        the hardware chunk once and distributes the frames to EVERY
        registered consumer, returning (and clearing) the queue of the
        calling consumer.

        The consumer is auto-registered on its first call. Call
        releaseChunkConsumer() when done, so frames stop being retained for
        a consumer that no longer polls. A consumer that is registered but
        not polling has its queue capped at MAX_QUEUED_CONSUMER_FRAMES
        (oldest frames dropped, warned once).

        Returns a list of frames; an empty list when no new frames are
        queued for this consumer.
        """
        with self._chunkConsumersLock:
            queue = self._chunkConsumers.setdefault(consumerKey, [])
            newFrames = self.getChunk()
            if newFrames is not None and len(newFrames) > 0:
                for key, consumerQueue in self._chunkConsumers.items():
                    consumerQueue.extend(newFrames)
                    excess = len(consumerQueue) - MAX_QUEUED_CONSUMER_FRAMES
                    if excess > 0:
                        del consumerQueue[:excess]
                        if key not in self._chunkConsumersWarned:
                            self._chunkConsumersWarned.add(key)
                            self.__logger.warning(
                                f'readChunk consumer "{key}" is registered '
                                f'but not polling; dropping its oldest '
                                f'frames (cap {MAX_QUEUED_CONSUMER_FRAMES}). '
                                f'Call releaseChunkConsumer when done.'
                            )
            frames = list(queue)
            queue.clear()
            return frames

    def releaseChunkConsumer(self, consumerKey: str) -> None:
        """ Unregisters a readChunk() consumer and drops any frames still
        queued for it. Safe to call for a consumer that was never
        registered. """
        with self._chunkConsumersLock:
            self._chunkConsumers.pop(consumerKey, None)
            self._chunkConsumersWarned.discard(consumerKey)

    @abstractmethod
    def startAcquisition(self) -> None:
        """ Starts image acquisition. """
        pass

    @abstractmethod
    def stopAcquisition(self) -> None:
        """ Stops image acquisition. """
        pass

    def finalize(self) -> None:
        """ Close/cleanup detector. """
        pass


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
