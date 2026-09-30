"""A ``LiveSource`` over the detector chunk queue.

``DetectorManager.readChunk(consumerKey)`` already fans every frame out to
every registered consumer with its own bounded queue -- it is what the
recorder and BeadRec poll in process. :class:`DetectorChunkLiveSource` wraps
one such consumer in the ``open()/poll()/is_complete()/close()`` contract the
ImProcess live workers drive, so the existing streaming runtime
(``LiveStreamWorker`` → ``RawDataBuffer`` → ``LiveProcessWorker``) runs on a
live acquisition with no file in between. This is the "file-less live source"
the recording data-flow plan parked behind the ``ChunkBroker`` rewrite; the
fan-out it needs exists today, so nothing in the detector layer changes.

Threading: ``poll()`` runs on the stream worker's thread; ``readChunk`` and
``startChunkConsumer`` take the detector's consumer lock, exactly as BeadRec's
worker calls them. :meth:`arm` and :meth:`mark_stream_ended` are called from
the controller thread and only set plain attributes.

End of stream: the controller marks it (the scan ended, or the user
switched live off); frames arriving within a short grace after that are
taken, then the source is complete whatever the detector goes on producing,
since a free-running camera does not stop with the scan.

Loss policy: a consumer that falls behind its byte budget has its queue
cleared and its next read raises ``ChunkConsumerOverflowError``. The recorder
fails the recording on that; a live preview re-registers, counts the loss and
carries on, and the run is reported incomplete. A recording running beside
this source is unaffected: it has its own queue.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    ChunkConsumerOverflowError,
    ChunkKind,
)
from imswitch.imcontrol.model.managers.recording_metadata import SOURCE_FORMAT_MEMORY
from imswitch.improcess.live.sources import LiveSource
from imswitch.improcess.reconstructors.base import Chunk, StackInfo

#: Consumer key the live reconstruction registers on the detector.
DEFAULT_CONSUMER_KEY = 'LiveRecon'

#: How long after the stream is marked ended late frames are still collected
#: before the source reports completion (BeadRec drains for the same span).
#: The deadline is fixed from the end mark: a free-running camera does not
#: stop with the scan, and waiting for it to go quiet would wait forever.
DEFAULT_DRAIN_GRACE_S = 0.5


def frame_shape_for_detector(detector, frame_transform=None,
                             dtype=None) -> tuple[int, ...]:
    """The array shape of one frame the detector delivers.

    A camera's ``DetectorManager.shape`` is ``(width, height)``; its frame
    array is the reverse, ``(height, width)``. A scan-driven detector (APD,
    PMT, TimeTagger) stores its assembled volume's shape in array order
    already, ``(..., Ny, Nx)``, and delivers that whole volume as one frame;
    it is set when the scan is built, so read it after that. When frames
    are transformed for display (a 90° rotation swaps the axes) the
    transformed shape is what the consumer sees, so it is measured on a
    blank frame rather than guessed.
    """
    raw = tuple(int(v) for v in tuple(detector.shape))
    shape = raw if getattr(detector, 'isScanDriven', False) else tuple(reversed(raw))
    if frame_transform is None:
        return shape
    blank = np.zeros(shape, dtype=np.dtype(dtype) if dtype is not None else np.uint16)
    return tuple(int(v) for v in np.asarray(frame_transform(blank)).shape)


def build_live_stack_info(
    detector_name: str,
    frame_shape: Sequence[int],
    dtype: Any,
    attrs: Mapping[str, Any],
    *,
    frames_per_stack: Optional[int],
    num_timepoints: Optional[int] = None,
    dataset_path: Optional[str] = None,
) -> StackInfo:
    """The ``StackInfo`` a live stream presents to the ImProcess runtime.

    Mirrors what the file-backed sources build on ``open()``: the attrs are
    the block :func:`recording_metadata.build_recording_attrs` produced for
    this detector, the layout is resolved from them with ImProcess's own
    resolver, and the expected frame count is the stack size times the
    timepoint count when that is known, else ``None`` (the stream ends when
    the controller says so).
    """
    from imswitch.improcess.model.acquisition_layout_resolver import (
        AcquisitionLayoutResolutionError,
        resolve_acquisition_layout,
    )

    attrs = dict(attrs or {})
    frame_shape = tuple(int(v) for v in frame_shape)
    fps = int(frames_per_stack) if frames_per_stack else None
    expected = fps * int(num_timepoints) if fps and num_timepoints else None
    dataset_path = dataset_path or attrs.get('recording:dataset_path') or f'/{detector_name}/data'
    attrs.setdefault('recording:dataset_path', dataset_path)
    resolution_frames = fps or 1
    try:
        resolved = resolve_acquisition_layout(
            attrs,
            shape=(resolution_frames, *frame_shape),
            detector=detector_name,
            dataset_path=dataset_path,
        )
    except AcquisitionLayoutResolutionError as error:
        # The scan source's layout does not describe what this detector
        # delivers (a frame-stream layout for a detector that assembles a
        # volume, say). A recording would carry it and be refused later; a
        # live run is better served by the older metadata and the array
        # shape, and by saying so.
        initLogger('LiveReconStackInfo').warning(
            f'{detector_name}: the declared acquisition layout does not fit the '
            f'live stream ({error}); reconstructing from the other metadata'
        )
        for key in ('AcquisitionLayout:schema', 'AcquisitionLayout:json'):
            attrs.pop(key, None)
        resolved = resolve_acquisition_layout(
            attrs,
            shape=(resolution_frames, *frame_shape),
            detector=detector_name,
            dataset_path=dataset_path,
        )
    return StackInfo(
        frame_shape=frame_shape,
        dtype=np.dtype(dtype),
        attrs=attrs,
        frames_per_stack=fps,
        expected_frames=expected,
        detector_name=detector_name,
        dataset_path=dataset_path,
        source_format=attrs.get('recording:source_format') or SOURCE_FORMAT_MEMORY,
        acquisition_layout=resolved,
    )


@dataclass(frozen=True)
class LiveStreamStats:
    """What the stream delivered, for the status line and the run record."""

    frames_received: int
    frames_expected: Optional[int]
    frames_discarded: int
    overflow_events: int
    ended: bool

    @property
    def incomplete(self) -> bool:
        """Frames are known to be missing from what the consumer saw."""
        if self.overflow_events:
            return True
        return (
            self.frames_expected is not None
            and self.ended
            and self.frames_received < self.frames_expected
        )


class DetectorChunkLiveSource(LiveSource):
    """Frames of one detector, read from its chunk queue as they arrive."""

    #: A scan lapse pauses between stacks; the runtime's stall watchdog must
    #: not mistake that for a crashed writer.
    idles_between_stacks = True

    def __init__(
        self,
        detectorsManager,
        detectorName: str,
        stack_info: StackInfo,
        *,
        consumer_key: str = DEFAULT_CONSUMER_KEY,
        chunk_kind: ChunkKind = ChunkKind.RAW,
        frame_transform: Optional[Callable[[np.ndarray], np.ndarray]] = None,
        drain_grace_s: float = DEFAULT_DRAIN_GRACE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            detectorsManager: The ``DetectorsManager``; frames are read through
                ``execOn(detectorName, ...)`` so the manager validates the name.
            detectorName: Which detector's queue to follow.
            stack_info: What the stream is expected to deliver
                (:func:`build_live_stack_info`).
            consumer_key: The chunk-consumer key registered on the detector.
                Two sources on one detector need distinct keys.
            chunk_kind: ``RAW`` asks for the measurement (the recorder's
                choice); ``DISPLAY`` for what the viewer shows. Identical for
                cameras; a point detector delivers its whole volume once, at
                the scan's end, in RAW.
            frame_transform: Optional per-frame callable (the detector's
                display transform, as BeadRec applies it). ``stack_info``
                must already describe the transformed frame shape.
            drain_grace_s: How long after :meth:`mark_stream_ended` empty
                reads are tolerated before the stream counts as complete.
            clock: Monotonic clock, injectable for tests.
        """
        self._detectorsManager = detectorsManager
        self._detectorName = str(detectorName)
        self._stack_info = stack_info
        self._consumer_key = str(consumer_key)
        self._chunk_kind = chunk_kind
        self._transform = frame_transform
        self._drain_grace_s = float(drain_grace_s)
        self._clock = clock
        self._logger = initLogger(self, tryInheritParent=False)

        self._lock = threading.Lock()
        self._armed = False
        self._opened = False
        self._cursor = 0
        self._discarded = 0
        self._overflow_events = 0
        self._ended_at: Optional[float] = None
        self._last_frame_at: Optional[float] = None
        self.name = f'{self._detectorName} live'

    # -- identity ---------------------------------------------------------

    @property
    def detector_name(self) -> str:
        return self._detectorName

    @property
    def consumer_key(self) -> str:
        return self._consumer_key

    @property
    def stack_info(self) -> StackInfo:
        return self._stack_info

    @property
    def stats(self) -> LiveStreamStats:
        return LiveStreamStats(
            frames_received=self._cursor,
            frames_expected=self._stack_info.expected_frames,
            frames_discarded=self._discarded,
            overflow_events=self._overflow_events,
            ended=self._ended_at is not None,
        )

    # -- lifecycle --------------------------------------------------------

    def arm(self) -> None:
        """Open the consumer boundary now: frames from here on are ours.

        Call this on the controller thread as soon as the detector is leased
        (before any scan TTL output), not from the worker thread later: the
        boundary is "frames after now", so every frame produced before it is
        excluded, and a worker thread spinning up after the scan started
        would miss the first frames.
        """
        with self._lock:
            self._detectorsManager.execOn(
                self._detectorName,
                lambda d: d.startChunkConsumer(self._consumer_key, self._chunk_kind),
            )
            self._armed = True

    def open(self, path_or_handle: Any = None) -> StackInfo:
        """Return the planned stack metadata; arms the consumer if not yet armed."""
        if not self._armed:
            self.arm()
        self._opened = True
        return self._stack_info

    def mark_stream_ended(self) -> None:
        """The producer is done (scan ended or the user stopped): drain, then complete.

        Frames that arrive within the drain grace are still taken (a camera's
        last frames land after the scan's end signal); once it has passed the
        stream is complete whatever the detector goes on producing.
        Idempotent; the first call starts the grace period.
        """
        if self._ended_at is None:
            self._ended_at = self._clock()

    def _past_drain_deadline(self) -> bool:
        ended_at = self._ended_at
        return ended_at is not None and (self._clock() - ended_at) >= self._drain_grace_s

    def poll(self) -> list[Chunk]:
        """Return the frames queued since the previous poll as one chunk."""
        if not self._opened or self._past_drain_deadline():
            return []
        try:
            frames = self._read()
        except ChunkConsumerOverflowError as exc:
            self._on_overflow(exc)
            return []
        if not frames:
            return []

        self._last_frame_at = self._clock()
        if self._transform is not None:
            frames = [self._transform(frame) for frame in frames]
        data = np.stack([np.asarray(frame) for frame in frames])
        self._check_frame_shape(data)

        expected = self._stack_info.expected_frames
        if expected is not None:
            room = expected - self._cursor
            if room <= 0:
                self._note_discarded(data.shape[0])
                return []
            if data.shape[0] > room:
                self._note_discarded(data.shape[0] - room)
                data = data[:room]

        start = self._cursor
        end = start + int(data.shape[0])
        self._cursor = end
        return [Chunk(data=data, start=start, end=end)]

    def is_complete(self) -> bool:
        """All planned frames are in, or the stream ended and the drain grace passed."""
        expected = self._stack_info.expected_frames
        if expected is not None and self._cursor >= expected:
            return True
        return self._past_drain_deadline()

    def close(self) -> None:
        """Release the consumer so the detector stops retaining frames for it."""
        with self._lock:
            if not self._armed:
                self._opened = False
                return
            try:
                self._detectorsManager.execOn(
                    self._detectorName,
                    lambda d: d.releaseChunkConsumer(self._consumer_key),
                )
            except Exception as exc:  # the detector may already be gone
                self._logger.debug(
                    f'Could not release chunk consumer {self._consumer_key!r} '
                    f'on {self._detectorName}: {exc}'
                )
            self._armed = False
            self._opened = False

    # -- internals --------------------------------------------------------

    def _read(self) -> list:
        with self._lock:
            if not self._armed:
                return []
            return self._detectorsManager.execOn(
                self._detectorName,
                lambda d: d.readChunk(self._consumer_key),
            ) or []

    def _on_overflow(self, exc: Exception) -> None:
        """The queue overflowed and was cleared: re-arm and keep going, incompletely."""
        self._overflow_events += 1
        self._logger.warning(
            f'{self._detectorName}: live reconstruction fell behind and lost '
            f'frames ({exc}); continuing with an incomplete stream'
        )
        try:
            self.arm()
        except Exception as arm_exc:
            self._logger.error(
                f'{self._detectorName}: could not re-register the live '
                f'consumer after an overflow: {arm_exc}'
            )

    def _note_discarded(self, count: int) -> None:
        if count <= 0:
            return
        first = self._discarded == 0
        self._discarded += int(count)
        if first:
            self._logger.warning(
                f'{self._detectorName}: received more frames than the '
                f'{self._stack_info.expected_frames} planned; extra frames '
                f'are discarded'
            )

    def _check_frame_shape(self, data: np.ndarray) -> None:
        expected = tuple(self._stack_info.frame_shape)
        got = tuple(int(v) for v in data.shape[1:])
        if got != expected:
            raise ValueError(
                f'{self._detectorName}: frame shape {got} differs from the '
                f'planned {expected}; the detector settings changed while '
                f'the live reconstruction was running'
            )
