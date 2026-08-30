import enum
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcontrol.model.devices.status import DeviceManagerStatusMixin
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

#: The same cap for a RAW consumer. A raw frame from a line-step 3-D scan is
#: ``S x Nz`` planes where a display frame is one, so an identical frame count
#: would mean a wildly different number of bytes. Kept small deliberately: a
#: raw consumer receives one frame per completed scan, not one per boundary,
#: so falling a thousand behind is not a backlog, it is a leak.
MAX_QUEUED_RAW_FRAMES = 16


class ChunkKind(enum.Enum):
    """Which representation of a chunk a consumer wants.

    ``DISPLAY`` is whatever the detector shows: for a point detector that is
    the reduced view its ``_linestep_view_mode`` asks for, which is a display
    preference and nothing more. ``RAW`` is the measurement — every axis, no
    reduction — which is what a recording must have and what it silently did
    not get before this existed.
    """

    DISPLAY = 'display'
    RAW = 'raw'


@dataclass
class ChunkPayload:
    """One destructive drain's worth of frames, in every form wanted.

    Both fields are frame-stacked ``(numFrames, ...)``. For a camera they are
    the same object, because a camera's chunk *is* the frames; only detectors
    that reduce something for display need them to differ.

    ``raw`` may be empty while ``display`` is not. A scan-driven detector
    publishes a display frame at every boundary — once per Z plane — but its
    raw volume is only whole when the scan ends, and handing out a
    half-written one is worse than handing out nothing.
    """

    display: Any
    raw: Any = None

    def of(self, kind: 'ChunkKind'):
        if kind is ChunkKind.RAW:
            return self.raw if self.raw is not None else _EMPTY_CHUNK
        return self.display


#: Stand-in for "this drain produced nothing of that kind".
_EMPTY_CHUNK = np.empty((0, 0, 0))


def scanPixelSizesToZYX(pixel_sizes: List[float]) -> List[float]:
    """ Convert a scan's per-axis step sizes into ``DetectorManager.pixelSizeUm``.

    Scan designers publish ``scanInfoDict['pixel_sizes']`` in scan-axis order,
    low dim to high dim -- ``[x, y]`` for a 2D scan and ``[x, y, z]`` when a
    slow axis is active. ``pixelSizeUm`` is the opposite convention and always
    exactly three entries: ``[Z, Y, X]``, with a non-scanned ``Z`` set to 1.

    Every scan-driven detector must funnel through this, because the two
    orderings are indistinguishable whenever the steps happen to be equal --
    which is the common case, so a transposition here survives casual testing
    and only shows up as a wrong pixel size in a saved file.
    """
    sizes = list(pixel_sizes or [])
    x = float(sizes[0]) if len(sizes) > 0 else 1.0
    y = float(sizes[1]) if len(sizes) > 1 else x
    z = float(sizes[2]) if len(sizes) > 2 else 1.0
    return [z, y, x]


def configuredCameraPixelSize(managerProperties) -> Optional[float]:
    """ The sample-plane pixel size the setup file declares, or ``None``.

    ``None`` means "the setup file does not usably declare one" -- the key is
    absent, misspelled, or unparseable. Callers must treat all three the same:
    the runtime value is then a default, not a configured calibration, so it is
    the user's to set and to persist.
    """
    try:
        raw = managerProperties.get(CAMERA_PIXEL_SIZE_KEY, None)
    except AttributeError:
        return None  # e.g. managerProperties is None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class ChunkConsumerOverflowError(RuntimeError):
    """A consumer fell behind and lost frames from its broker queue."""


class RawFrameUnavailableError(RuntimeError):
    """The measurement is not whole yet, so there is nothing honest to return.

    Raised rather than substituting a display frame or the accumulation buffer
    mid-fill: both look like data and neither is the measurement. A caller that
    wants whatever is on screen should ask for it, by not passing ``is_save``.
    """


class DetectorManager(DeviceManagerStatusMixin, SignalInterface):
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

        value = configuredCameraPixelSize(properties)
        if value is None:
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
        self._chunkConsumerKinds = {}
        self._chunkConsumersWarned = set()
        self._chunkConsumersOverflowed = set()
        self._chunkConsumersLock = Lock()
        # One cache per representation. Sharing one would let a display
        # consumer's frame answer a getLatestFrameShared(is_save=True), which
        # is the bug this whole contract exists to remove.
        self._latestRawImage = None

        self.__forAcquisition = detectorInfo.forAcquisition
        self.__forFocusLock = detectorInfo.forFocusLock
        if not detectorInfo.forAcquisition and not detectorInfo.forFocusLock:
            raise ValueError('At least one of forAcquisition and forFocusLock must be set in'
                             ' DetectorInfo.')

        # Acquisition-ownership mirrors — read-only for managers, written ONLY
        # by the DetectorsManager lease table (never conflate the two: leased
        # is bookkeeping, faulted is uncertain hardware state after a failed
        # stop).
        self._acquisitionLeased = False
        self._hardwareFaulted = False

        # Parameters whose authoritative value is the setup file, not the
        # runtime. Saved widget state must neither capture nor restore these:
        # a value the user edited into the config only takes effect if it wins
        # over whatever the last session happened to be running with.
        self.__configOwnedParameters = frozenset(
            name for name in (CAMERA_PIXEL_SIZE_PARAM,)
            if name in self.__parameters
            and configuredCameraPixelSize(
                getattr(detectorInfo, 'managerProperties', None)) is not None
        )

        self.setBinning(supportedBinnings[0])

    def updateLatestFrame(self, init):
        """ :meta private: """
        try:
            self.__image = self.getLatestFrameShared()
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
    def configOwnedParameters(self) -> frozenset:
        """ Names of parameters that the setup file declares and therefore owns.

        These are calibration constants of the instrument (currently the camera
        pixel size), not runtime settings. State persistence must skip them in
        both directions, so that editing the setup file is enough to change
        them and a stale snapshot can never reinstate the old value. """
        return self.__configOwnedParameters

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
    def isScanDriven(self) -> bool:
        """ Whether this detector is driven by the scan clock and produces its
        image *as* the scan runs (APD, PMT, TimeTagger), as opposed to a
        free-running detector that produces frames on its own schedule.

        This is the OWNERSHIP axis, and it is not the same question as how the
        frame clock is wired: a camera set to an external/scan trigger is still
        free-running by this definition (``isScanDriven=False``) — its frame
        clock changed, not who owns its acquisition. Only scan-driven detectors
        take part in the scan participant snapshot.
        """
        return False

    @property
    def acquisitionLeased(self) -> bool:
        """ Whether this detector holds at least one acquisition lease.
        Written only by the DetectorsManager lease table. """
        return self._acquisitionLeased

    @property
    def hardwareFaulted(self) -> bool:
        """ Whether this detector is quarantined because a hardware stop
        failed (uncertain hardware state — distinct from lease bookkeeping).
        Written only by the DetectorsManager lease table. """
        return self._hardwareFaulted

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

    @property
    def rawFrameIsDeferred(self) -> bool:
        """Whether this detector's raw frame only exists once a scan ends.

        False for a camera, whose every frame is complete on arrival. True for
        a scan-driven detector, which fills one buffer progressively and can
        only offer it whole afterwards -- so "not cached" means "not ready",
        not "go and fetch it".
        """
        return False

    def _chunkKinds(self):
        """Per-consumer kinds, or None on a manager that has no such state.

        Read straight from the instance dict rather than with ``getattr``: a
        manager may mirror the broker's state without running this class's
        ``__init__`` -- the in-tree test doubles do, and out-of-tree managers
        may too -- and ``SignalInterface`` answers a missing attribute with a
        ``RuntimeError``, which a ``getattr`` default does not catch. Such a
        manager gets DISPLAY for every consumer, exactly as it did before
        kinds existed.
        """
        return self.__dict__.get('_chunkConsumerKinds')

    def drainChunk(self) -> ChunkPayload:
        """One destructive drain, in every representation a consumer wants.

        ``getChunk()`` is destructive — draining it twice returns nothing the
        second time, which is the whole reason ``readChunk`` exists — so a
        detector that can offer more than one form of its data must produce
        them together, here.

        The default is correct for every camera without an override: a camera's
        chunk *is* its frames, so display and raw are the same object rather
        than a copy of one. Only a detector that reduces something on its way to
        the screen — a point detector collapsing line steps — has any reason to
        override this, and then only to say what it reduced away.
        """
        frames = self.getChunk()
        return ChunkPayload(display=frames, raw=frames)

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
        not polling has its queue capped at MAX_QUEUED_CONSUMER_FRAMES.
        Crossing that cap marks the stream incomplete: the oldest retained
        frames are discarded to bound memory, and the consumer's next read
        raises :class:`ChunkConsumerOverflowError` instead of silently
        returning a truncated recording.

        Returns a list of frames; an empty list when no new frames are
        queued for this consumer.
        """
        with self._chunkConsumersLock:
            queue = self._chunkConsumers.setdefault(consumerKey, [])
            kinds = self._chunkKinds()
            if kinds is not None:
                kinds.setdefault(consumerKey, ChunkKind.DISPLAY)
            self._distributeChunkLocked(self.drainChunk())
            if consumerKey in self._chunkConsumersOverflowed:
                raise ChunkConsumerOverflowError(
                    f'readChunk consumer "{consumerKey}" fell behind by more '
                    f'than {MAX_QUEUED_CONSUMER_FRAMES} frames; its stream is '
                    f'incomplete'
                )
            frames = list(queue)
            queue.clear()
            return frames

    def getLatestFrameShared(self, is_save=False) -> np.ndarray:
        """Read a latest frame without bypassing active chunk consumers.

        When Recording/BeadRec/etc. have registered broker queues, a direct
        SDK ``getLatestFrame`` call may destructively steal a pending hardware
        frame. In that state this method drains ``getChunk`` once, fans every
        frame out to the registered consumers, and returns the newest frame for
        display/focus/direct use. With no chunk consumers it delegates to the
        detector's ordinary latest-frame method under the same SDK lock.
        """
        with self._chunkConsumersLock:
            if self._chunkConsumers:
                # _distributeChunkLocked latches both representations, so any
                # drain refreshes them whichever participant performed it.
                self._distributeChunkLocked(self.drainChunk())
                # And honour the save hint, which this branch used to ignore
                # entirely -- so merely having a recording open silently
                # downgraded every other reader on the same detector.
                if is_save:
                    cachedRaw = self.__dict__.get('_latestRawImage')
                    if cachedRaw is not None:
                        return cachedRaw
                    if self.rawFrameIsDeferred:
                        # The detector publishes its measurement only when its
                        # scan completes, and that has not happened yet.
                        # Reaching past the latch to the accumulation buffer
                        # would hand back a half-filled volume -- the exact
                        # thing the latch exists to prevent -- and the display
                        # cache would be the ambiguity it exists to remove.
                        raise RawFrameUnavailableError(
                            f'{self.name}: no completed frame is available to '
                            'save yet; its scan has not finished'
                        )
                    # Nothing deferred: for a camera the raw frame *is* the
                    # displayed one, so asking the detector is exact.
                    try:
                        return self.getLatestFrame(is_save=True)
                    except TypeError:
                        return self.getLatestFrame()
                return self.__image

            try:
                frame = self.getLatestFrame(is_save=is_save)
            except TypeError:
                # Most legacy managers expose getLatestFrame() without the
                # optional save hint.
                frame = self.getLatestFrame()
            if is_save:
                self._latestRawImage = frame
            else:
                self.__image = frame
            return frame

    def _distributeChunkLocked(self, payload) -> None:
        """Fan one drain out, per consumer's kind; caller holds the lock.

        Accepts a :class:`ChunkPayload`, or a bare frame array for callers
        predating the split — those are display frames, which is what every
        such caller meant.
        """
        if not isinstance(payload, ChunkPayload):
            payload = ChunkPayload(display=payload, raw=payload)

        # Latch here rather than in the caller: any broker participant may be
        # the one that drains the hardware queue, so a faster chunk consumer
        # (BeadRec, say) must not leave the latest-frame readers on a stale
        # image. Each representation gets its own cache -- sharing one is how a
        # recording used to be handed a display plane, since whoever drained
        # last decided what everyone saw next.
        display = payload.of(ChunkKind.DISPLAY)
        if display is not None and len(display) > 0:
            self.__image = np.asarray(display[-1])
        raw = payload.of(ChunkKind.RAW)
        if raw is not None and len(raw) > 0:
            self._latestRawImage = np.asarray(raw[-1])

        kinds = self._chunkKinds() or {}
        for key, consumerQueue in self._chunkConsumers.items():
            kind = kinds.get(key, ChunkKind.DISPLAY)
            newFrames = payload.of(kind)
            # A raw drain is routinely empty while a display one is not: the
            # volume is not whole until the scan ends. That is the contract,
            # not a fault.
            if newFrames is None or len(newFrames) == 0:
                continue

            consumerQueue.extend(newFrames)
            cap = (MAX_QUEUED_RAW_FRAMES if kind is ChunkKind.RAW
                   else MAX_QUEUED_CONSUMER_FRAMES)
            excess = len(consumerQueue) - cap
            if excess <= 0:
                continue
            del consumerQueue[:excess]
            self._chunkConsumersOverflowed.add(key)
            if key in self._chunkConsumersWarned:
                continue
            self._chunkConsumersWarned.add(key)
            self.__logger.warning(
                f'readChunk consumer "{key}" is registered but not polling; '
                f'dropping its oldest frames (cap {cap}). The consumer will '
                f'fail rather than accept an incomplete stream; call '
                f'releaseChunkConsumer when done.'
            )

    def releaseChunkConsumer(self, consumerKey: str) -> None:
        """ Unregisters a readChunk() consumer and drops any frames still
        queued for it. Safe to call for a consumer that was never
        registered. """
        with self._chunkConsumersLock:
            self._chunkConsumers.pop(consumerKey, None)
            kinds = self._chunkKinds()
            if kinds is not None:
                kinds.pop(consumerKey, None)
            self._chunkConsumersWarned.discard(consumerKey)
            self._chunkConsumersOverflowed.discard(consumerKey)

    def startChunkConsumer(self, consumerKey: str,
                           kind: ChunkKind = ChunkKind.DISPLAY) -> None:
        """Start one consumer at an atomic "frames after now" boundary.

        Pre-boundary hardware frames are drained exactly once and delivered to
        consumers that were already registered. They are excluded only from
        the new consumer, unlike a global ``flushBuffers()`` which can discard
        another owner's pending data.

        ``kind`` says which representation this consumer is asking for.
        Defaulting to ``DISPLAY`` keeps every existing caller — the viewer, the
        focus lock, tiling's fresh-frame handshake — exactly as it was; a
        recording asks for ``RAW`` because it wants the measurement rather than
        the picture of it.
        """
        with self._chunkConsumersLock:
            self._chunkConsumers.pop(consumerKey, None)
            kinds = self._chunkKinds()
            if kinds is not None:
                kinds.pop(consumerKey, None)
            self._chunkConsumersWarned.discard(consumerKey)
            self._chunkConsumersOverflowed.discard(consumerKey)

            self._distributeChunkLocked(self.drainChunk())

            self._chunkConsumers[consumerKey] = []
            kinds = self._chunkKinds()
            if kinds is not None:
                kinds[consumerKey] = kind

    @abstractmethod
    def startAcquisition(self) -> None:
        """ Starts image acquisition. """
        pass

    @abstractmethod
    def stopAcquisition(self) -> None:
        """ Stops image acquisition.

        Detector stop contract: teardown MUST raise on failure (logging first
        is fine, swallowing is not) — the DetectorsManager is the only catcher
        and records a failure as a hardware fault that quarantines the
        detector. """
        pass

    def finishScan(self, mode: str, acknowledge) -> None:
        """ Graceful-finish contract hook, called by the scan-execution
        coordinator for every scan participant on every scan-iteration
        termination, regardless of lease refcounts (a detector that keeps
        other leases still needs its end-of-scan handling).

        ``mode`` is ``'graceful'`` (normal completion — e.g. the TimeTagger
        signals done and produces its final read/fit/emit) or ``'abort'``
        (abrupt termination). Distinct from stopAcquisition(), which is
        hardware teardown and only happens when the last lease is released.

        ``acknowledge`` is a zero-argument callable that MUST be invoked once
        this detector's end-of-scan work is complete — possibly later, from
        another thread. The coordinator holds the scan lease open until every
        participant acknowledges, so hardware teardown and the next repeat
        iteration cannot race a final read that is still in flight. A manager
        with nothing asynchronous to do simply acknowledges immediately, which
        is what this default does. """
        acknowledge()

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
