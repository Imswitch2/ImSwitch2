"""A detector as a frame source: the EVENT_STREAM lease, the frame envelope
and the coalescing inbox between the GUI thread and the engine's worker.

The detectors manager polls every leased detector and the comm channel
delivers ``sigUpdateImage(detectorName, image, init, scale, isCurrent)`` on
the GUI thread; the controller connects that signal to :meth:`on_image`,
which does the little that must happen there -- filter by detector name,
copy the array (the manager hands out its latched buffer), stamp the
envelope -- and pushes into the inbox, never blocking. The engine pulls
from the inbox on its own thread.

What the signal does not carry, this source adds: a sequence number of its
own (the poll has none), the engine clock's time, the detector's exposure
(read tolerantly from its parameters), and the pixel grid as a
:class:`~imswitch.imcommon.model.smartpipeline.FrameIdentity` -- the
manager's ``shape`` is ``(width, height)`` and ``frameStart`` ``(x, y)``,
so both are turned round. A frame the poll re-reads unchanged (no new
exposure since the last tick) is recognised by a strided sample and counted
as a duplicate rather than ticked twice.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import numpy as np

from imswitch.imcommon.model.smartpipeline import Frame, FrameIdentity
from imswitch.imcontrol.model.smartpipeline.ports import CoalescingInbox

#: Parameter names a detector may keep its exposure under, and the factor to
#: seconds for the units they declare.
_EXPOSURE_KEYS = ("exposure", "Exposure", "exposure_time", "Exposure time", "ExposureTime")
_SECONDS_PER_UNIT = {"s": 1.0, "sec": 1.0, "seconds": 1.0, "ms": 1e-3, "msec": 1e-3, "us": 1e-6, "µs": 1e-6, "usec": 1e-6}


def exposure_seconds(detector) -> float | None:
    """The detector's exposure in seconds, from whichever parameter holds it,
    or ``None`` when it has none the source recognises."""
    parameters = getattr(detector, "parameters", None) or {}
    for key in _EXPOSURE_KEYS:
        parameter = parameters.get(key)
        if parameter is None:
            continue
        try:
            value = float(getattr(parameter, "value", parameter))
        except (TypeError, ValueError):
            continue
        unit = str(getattr(parameter, "valueUnits", "") or "ms").strip().lower()
        return value * _SECONDS_PER_UNIT.get(unit, 1e-3)
    return None


def frame_identity_of(detector) -> FrameIdentity:
    """The pixel grid of ``detector``'s frames (rows, cols; binning; ROI
    offset as (y, x))."""
    shape = tuple(int(v) for v in (getattr(detector, "shape", None) or ()))
    rows_cols = (shape[1], shape[0]) if len(shape) >= 2 else shape
    binning = getattr(detector, "binning", 1) or 1
    try:
        binning = int(binning)
    except (TypeError, ValueError):
        binning = 1
    start = tuple(int(v) for v in (getattr(detector, "frameStart", None) or (0, 0)))
    offset = (start[1], start[0]) if len(start) >= 2 else (0, 0)
    return FrameIdentity(shape=rows_cols, binning=(binning, binning), offset=offset)


class LeasePurposeMissing(RuntimeError):
    """The detectors manager has no EVENT_STREAM lease purpose."""


class LiveFrameSource:
    """Frames of one detector, for the engine.

    ``start()`` takes the EVENT_STREAM lease (the detector is polled without
    a live view); ``on_image`` is the GUI-thread slot; ``frames()`` is what
    the engine iterates; ``stop()`` ends it from any thread; ``close()``
    releases the lease -- that can block up to the manager's poll-thread
    join and may time out, in which case ``release_pending`` stays true and
    :meth:`retry_release` is for the controller's shutdown poll.
    """

    def __init__(self, detectorsManager, detector: str, *, clock=time.monotonic, max_age_s: float | None = None,
                 duplicate_check: bool = True, logger=None):
        self._manager = detectorsManager
        self.detector = str(detector)
        self._clock = clock
        self._logger = logger
        self._inbox = CoalescingInbox(max_age_s=max_age_s, clock=clock)
        self._duplicate_check = bool(duplicate_check)
        self._lease = None
        self._lock = threading.Lock()
        self._sequence = -1
        self._last_sample = None
        self._last_shape = None
        self.release_pending = False
        self.identity: FrameIdentity | None = None
        self.duplicates = 0
        self.ignored = 0   # frames of other detectors
        #: A scan-driven detector (an APD): its frames come one per scan,
        #: through :meth:`capture_latest` after the scan completed, never
        #: from the signal (which also carries the partial frames of a scan
        #: in progress); it needs no lease.
        self.scan_driven = False

    # -- the lease --------------------------------------------------------------

    @property
    def leased(self) -> bool:
        return self._lease is not None

    @property
    def stats(self):
        return self._inbox.stats

    def start(self) -> None:
        """Take the EVENT_STREAM lease; raises whatever the manager raises
        (an unknown, unplugged or faulted detector)."""
        from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose

        purpose = getattr(LeasePurpose, "EVENT_STREAM", None)
        if purpose is None:
            raise LeasePurposeMissing("the detectors manager has no EVENT_STREAM lease purpose")
        with self._lock:
            if self._lease is not None or self.scan_driven:
                return
            detector = self._manager[self.detector]
            if bool(getattr(detector, "isScanDriven", False)):
                self.scan_driven = True
                self.identity = None
                return
            self.identity = frame_identity_of(detector)
            self._lease = self._manager.acquire([self.detector], purpose)
            self.release_pending = False

    def _release(self) -> bool:
        with self._lock:
            lease, self._lease = self._lease, None
        if lease is None:
            return True
        try:
            self._manager.release(lease)
        except TimeoutError:
            with self._lock:
                self._lease = lease
            self.release_pending = True
            if self._logger is not None:
                self._logger.warning("Smart pipeline: the detector poll did not stop in time; will retry")
            return False
        except ValueError:
            pass   # already released (the manager's shutdown ended the leases)
        self.release_pending = False
        return True

    def retry_release(self) -> bool:
        """Try the pending release again; ``True`` once the lease is gone."""
        return self._release()

    # -- frames ----------------------------------------------------------------

    def on_image(self, detectorName, image, init=False, scale=None, isCurrentDetector=False) -> bool:
        """The ``sigUpdateImage`` slot (GUI thread). Returns whether the
        frame was queued for the engine."""
        if str(detectorName) != self.detector:
            self.ignored += 1
            return False
        if self.scan_driven or self._lease is None:
            return False
        array = np.asarray(image)
        if array.ndim < 2:
            return False
        if self._duplicate_check and self._is_duplicate(array):
            self.duplicates += 1
            self._inbox.stats.duplicates += 1
            return False
        self._sequence += 1
        detector = None
        try:
            detector = self._manager[self.detector]
        except Exception:
            pass
        identity = self.identity if self.identity is not None else FrameIdentity.of_image(array)
        frame = Frame(
            image=np.array(array, copy=True), detector=self.detector, sequence=self._sequence, t=self._clock(),
            exposure_s=exposure_seconds(detector) if detector is not None else None, identity=identity,
            scale=tuple(float(v) for v in (scale or ())), unit="um" if scale else "px",
            metadata={"init": bool(init), "current": bool(isCurrentDetector)},
        )
        return self._inbox.push(frame)

    def _is_duplicate(self, array: np.ndarray) -> bool:
        step = max(1, min(array.shape[-2:]) // 32)
        sample = np.ascontiguousarray(array[..., ::step, ::step])
        if self._last_sample is not None and self._last_shape == array.shape and np.array_equal(sample, self._last_sample):
            return True
        self._last_sample = sample.copy()
        self._last_shape = array.shape
        return False

    def capture_latest(self, *, metadata: dict | None = None) -> bool:
        """The detector's latest frame as a tick, with the scan geometry it
        carries (a scan-driven detector, after its scan completed)."""
        detector = self._manager[self.detector]
        # The broker-safe reader (never the bare one, which can steal a
        # recording's frames); is_save=False is the frame with its geometry.
        reader = getattr(detector, "getLatestFrameShared", None)
        latest = reader(is_save=False) if callable(reader) else None
        if latest is None:
            return False
        array = np.asarray(latest)
        if array.ndim < 2:
            return False
        geometry = getattr(latest, "geometry", None)
        geometry = geometry.to_dict() if geometry is not None and hasattr(geometry, "to_dict") else None
        scale = getattr(detector, "scale", None) or ()
        try:
            scale = tuple(float(v) for v in scale)
        except (TypeError, ValueError):
            scale = ()
        self._sequence += 1
        frame = Frame(
            image=np.array(array, copy=True), detector=self.detector, sequence=self._sequence, t=self._clock(),
            scale=scale, unit="um" if scale else "px",
            metadata={"scan_driven": True, "geometry": geometry, **dict(metadata or {})},
        )
        return self._inbox.push(frame)

    def frames(self) -> Iterator[Frame]:
        return self._inbox.frames()

    def stop(self) -> None:
        self._inbox.stop()

    def close(self) -> None:
        self._inbox.close()
        self._release()


__all__ = ["LeasePurposeMissing", "LiveFrameSource", "exposure_seconds", "frame_identity_of"]


# Copyright (C) 2020-2026 ImSwitch developers
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
