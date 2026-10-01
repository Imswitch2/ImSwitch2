"""Running measurements off the GUI thread, safely.

Measuring 200 ROIs across 500 planes is not something to do inside a Qt slot,
but moving it to a worker introduces three ways to be wrong, and this module
exists to close all three:

* **the worker must not touch the viewer.**  A napari layer is not safe to read
  from another thread, so a job carries an :class:`ImagePlaneSource` — a pure
  reader built on the GUI thread and used only off it.  Nothing here imports
  napari or Qt, which is also what makes it testable without either.
* **a slow job must not overwrite a fast one.**  Results are published only if
  the job is still the one the panel is waiting for.
* **a moving image must not be measured half-and-half.**  The source's
  mutation token is frozen when the job is created and compared before
  publishing; live data is either snapshotted up front or refused, never
  measured opportunistically.

The measurement itself is injected, so this module knows how to *run* a
measurement without knowing what one is.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import numpy as np

#: Axis-labelled slice position, e.g. ``(("Z", 12), ("T", 3))``.
Position = tuple[tuple[str, int], ...]


class LiveDataError(RuntimeError):
    """A run was refused because the image can change while it is measured."""


class ImagePlaneSource(Protocol):
    """A pure, thread-safe reader for one image's planes."""

    #: Changes whenever the pixels change; frozen into the job at creation.
    mutation_token: str

    def read_plane(self, position: Position) -> np.ndarray:
        """The 2D plane at an axis-labelled position."""

    def close(self) -> None:
        """Release any file or array handle held open."""


@dataclass
class ArrayPlaneSource:
    """Reads planes out of an in-memory array.

    ``live`` marks an array that something else can write to — an acquisition
    or a live reconstruction. Such an array is snapshotted at construction, so
    the job measures one coherent moment rather than plane 1 from before an
    update and plane 40 from after it. ``refuse_live`` says to decline instead,
    which is the right answer when snapshotting would be larger than memory.
    """

    array: Any
    axis_labels: tuple[str, ...]
    plane_axes: tuple[str, str] = ("Y", "X")
    #: Supplied by the caller from something that actually tracks change — a
    #: result uid plus its revision, say. **Never invented here**: a buffer
    #: address and shape survive an in-place write, so deriving a "mutation"
    #: token from them would certify unchanged data that had changed. Empty
    #: means unknown, which disables caching and requires snapshot-or-refuse.
    mutation_token: str = ""
    live: bool = False
    refuse_live: bool = False

    def __post_init__(self):
        if self.live and self.refuse_live:
            raise LiveDataError(
                "the image can change while it is measured; measure a saved "
                "result, or allow a snapshot"
            )
        if self.live:
            # One coherent moment, taken now. The copy is ours and nothing
            # else can write to it, so a token for it is sound.
            self.array = np.array(self.array, copy=True)
            if not self.mutation_token:
                self.mutation_token = f"snapshot:{uuid.uuid4().hex}"

    def read_plane(self, position: Position) -> np.ndarray:
        return _index_plane(self.array, self.axis_labels, self.plane_axes, position)

    def close(self) -> None:
        self.array = None


@dataclass
class LazyPlaneSource:
    """Reads planes from a lazy/virtual array, one at a time.

    Chunked by construction: a long Multi Measure over a large stack never
    materialises more than the plane it is working on.
    """

    handle: Any
    axis_labels: tuple[str, ...]
    plane_axes: tuple[str, str] = ("Y", "X")
    #: As for ArrayPlaneSource: supplied, never invented. Object identity and
    #: shape are unchanged by a rewrite of the pixels behind the handle.
    mutation_token: str = ""

    def read_plane(self, position: Position) -> np.ndarray:
        return np.asarray(
            _index_plane(self.handle, self.axis_labels, self.plane_axes, position)
        )

    def close(self) -> None:
        self.handle = None


class PositionError(ValueError):
    """A position does not identify a plane of this image."""


def _index_plane(array, axis_labels, plane_axes, position: Position) -> np.ndarray:
    """Slice out the 2D plane at an axis-labelled position.

    By label, never by index: axis order is view-mode dependent, so indexing
    positionally would follow a transposition onto the wrong axis.

    Every disagreement is an error rather than a nearest-match. An unknown
    label, an out-of-range index, or a non-plane axis left unpinned used to be
    silently ignored, clamped, or resolved to index zero — so a position that
    did not belong to this image measured *some* plane and reported a number,
    which is the same failure as measuring a polygon's bounding box. The
    spatial contract calls that incompatible, and so does this.
    """
    shape = tuple(int(v) for v in np.shape(array))
    ndim = len(shape)
    labels = list(axis_labels)
    if len(labels) != ndim:
        raise PositionError(
            f"{len(labels)} axis labels for {ndim} dimensions"
        )
    lookup = {label: index for index, label in enumerate(labels)}

    indexer: list[Any] = [slice(None)] * ndim
    pinned: set[int] = set()
    for label, index in position:
        axis = lookup.get(label)
        if axis is None:
            raise PositionError(f"no axis {label!r} in {labels}")
        if not 0 <= int(index) < shape[axis]:
            raise PositionError(
                f"{label}={index} is outside this image's extent {shape[axis]}"
            )
        indexer[axis] = int(index)
        pinned.add(axis)

    plane_indices = {lookup[label] for label in plane_axes if label in lookup}
    unpinned = [
        labels[axis]
        for axis in range(ndim)
        if axis not in pinned and axis not in plane_indices
    ]
    if unpinned:
        raise PositionError(
            f"position does not say which {', '.join(unpinned)} to measure"
        )

    plane = np.asarray(array[tuple(indexer)])
    if plane.ndim != 2:
        raise PositionError(f"position yields a {plane.ndim}D slice, not a plane")
    return plane


class CancellationToken:
    """Cooperative cancellation: the worker checks, the caller sets."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class MeasurementJob:
    """An immutable description of one measurement run."""

    rois: tuple
    source: Any                      # ImagePlaneSource
    planes: tuple[Position, ...]
    measurements: tuple[str, ...] = ()
    config_revision: int = 0
    frame_uid: str = ""
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: The source's mutation token as it was when this job was created.
    #: Frozen: the cache is keyed on it and publication compares against it,
    #: and reading it lazily would let a job cache under one value and publish
    #: under another.
    token_at_start: str = ""

    @classmethod
    def create(cls, rois, source, planes, **kwargs) -> "MeasurementJob":
        return cls(
            rois=tuple(rois),
            source=source,
            planes=tuple(planes),
            token_at_start=getattr(source, "mutation_token", ""),
            **kwargs,
        )

    @property
    def total_steps(self) -> int:
        return max(1, len(self.planes)) * max(1, len(self.rois))


@dataclass(frozen=True)
class MeasurementResult:
    """What a completed job produced."""

    job_id: str
    rows: tuple[dict, ...]
    cancelled: bool = False
    stale: bool = False
    #: The run itself failed, as opposed to being cancelled or superseded.
    #: Kept apart from ``cancelled`` because the panel says something
    #: different about each, and "cancelled" for a crash is a lie.
    failed: bool = False

    @property
    def publishable(self) -> bool:
        return not self.cancelled and not self.stale and not self.failed


def cache_key(
    frame_uid: str,
    mutation_token: str,
    plane: Position,
    roi,
    config_revision: int,
) -> tuple | None:
    """The identity of one measured row, or None when it cannot be cached.

    One definition, used by both the background runner and the panel's
    synchronous refresh: two key builders that agree by inspection are two key
    builders that will one day disagree, and the failure would be a stale
    number shown as a fresh one.
    """
    if not mutation_token:
        # No trustworthy token: caching would be a guess, so there is none.
        return None
    return (
        frame_uid,
        mutation_token,
        tuple(plane),
        getattr(roi, "uid", ""),
        getattr(roi, "revision", 0),
        int(config_revision),
    )


class MeasurementCache:
    """Remembers measured rows so scrolling a stack does not re-measure it.

    Keyed on everything that can change an answer: which plane of which frame,
    which ROI *at which revision*, and the measurement configuration. Renaming
    an ROI deliberately does not bump its revision, so a rename costs nothing.
    """

    def __init__(self, limit: int = 20000):
        self._entries: dict[tuple, dict] = {}
        self._limit = int(limit)

    @staticmethod
    def key(job: MeasurementJob, plane: Position, roi) -> tuple | None:
        return cache_key(
            job.frame_uid, job.token_at_start, plane, roi, job.config_revision
        )

    def get(self, key):
        return self._entries.get(key) if key is not None else None

    def put(self, key, row) -> None:
        if key is None:
            return
        if len(self._entries) >= self._limit:
            self._entries.clear()
        self._entries[key] = row

    def invalidate_roi(self, uid: str) -> None:
        self._entries = {
            key: row for key, row in self._entries.items() if key[3] != uid
        }

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def run_job(
    job: MeasurementJob,
    measure: Callable[[np.ndarray, Any, Position], dict],
    *,
    decorate: Callable[[dict, Any, Position], dict] | None = None,
    cancellation: CancellationToken | None = None,
    progress: Callable[[int, int], None] | None = None,
    cache: MeasurementCache | None = None,
    on_error: Callable[[Any, Position, Exception], None] | None = None,
) -> MeasurementResult:
    """Measure every ROI on every plane, cancellably.

    Pure: given a source and a measure function it touches nothing else, which
    is what lets it run on a worker thread and be tested without a viewer.

    ``measure`` returns **measurement values only** and is the single thing
    that is cached. ``decorate`` turns those values into whatever the caller
    publishes — a row with a source name, an ROI name, plane columns. Keeping
    them apart is not tidiness: caching the decorated row means a cached row
    carries the name, plane and source it was first measured under, so a
    rename, or a second consumer keying the same measurement differently, gets
    stale identity attached to correct numbers.

    ``measure`` may return ``None`` for a pair it declines to measure — an ROI
    bound to one slice, asked about another. That is a skip, not a failure.
    """
    rows: list[dict] = []
    done = 0
    total = job.total_steps

    def _finish(**kwargs) -> MeasurementResult:
        return MeasurementResult(job.job_id, tuple(rows), **kwargs)

    for plane in job.planes or ((),):
        if cancellation is not None and cancellation.cancelled:
            return _finish(cancelled=True)
        image = None
        for roi in job.rois:
            if cancellation is not None and cancellation.cancelled:
                return _finish(cancelled=True)
            key = cache.key(job, plane, roi) if cache is not None else None
            values = cache.get(key) if cache is not None else None
            if values is None:
                try:
                    if image is None:
                        # Read the plane once for all the ROIs measured on it.
                        image = job.source.read_plane(plane)
                    values = measure(image, roi, plane)
                    if values is None:
                        # A deliberate skip, not a failure: an ROI bound to
                        # one slice does not belong on the others. Distinct
                        # from an exception so it is not reported as an error
                        # the user should act on.
                        done += 1
                        if progress is not None:
                            progress(done, total)
                        continue
                except Exception as exc:
                    # One ROI that cannot be measured — a line where an area is
                    # expected, a region clipped to nothing — costs its own row
                    # and nothing else. Letting it out of here would kill the
                    # worker with the other 199 ROIs unpublished and the panel
                    # still showing a progress bar.
                    if on_error is not None:
                        on_error(roi, plane, exc)
                    done += 1
                    if progress is not None:
                        progress(done, total)
                    continue
                if cache is not None:
                    cache.put(key, values)
            rows.append(decorate(values, roi, plane) if decorate else values)
            done += 1
            if progress is not None:
                progress(done, total)

    # Checked once more at the end: a run cancelled during its last (or only)
    # measurement had passed every in-loop check already, and would otherwise
    # report itself complete and publish numbers the user asked not to have.
    if cancellation is not None and cancellation.cancelled:
        return MeasurementResult(job.job_id, tuple(rows), cancelled=True)

    # The image may have moved under a long run; publishing those numbers
    # would attribute them to pixels that no longer exist.
    if getattr(job.source, "mutation_token", job.token_at_start) != job.token_at_start:
        return MeasurementResult(job.job_id, tuple(rows), stale=True)
    return MeasurementResult(job.job_id, tuple(rows))


class MeasurementRunner:
    """Runs jobs on a worker thread and hands back only the current one's rows.

    The panel keeps one runner. Starting a new job supersedes the previous one,
    so a slow Multi Measure started first cannot overwrite a quick Measure
    started after it.
    """

    def __init__(self, *, cache: MeasurementCache | None = None):
        self._cache = cache if cache is not None else MeasurementCache()
        self._current_job_id: str | None = None
        self._cancellation: CancellationToken | None = None
        # Every worker ever started, not just the newest: a superseded job is
        # asked to stop but stops cooperatively, so shutdown has to wait for
        # all of them. Keeping one reference let shutdown join the new worker
        # and return while an older one was still inside a measurement.
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    @property
    def cache(self) -> MeasurementCache:
        return self._cache

    @property
    def busy(self) -> bool:
        with self._lock:
            threads = list(self._threads)
        return any(thread.is_alive() for thread in threads)

    def cancel(self) -> None:
        with self._lock:
            if self._cancellation is not None:
                self._cancellation.cancel()
            self._current_job_id = None

    def wait(self, timeout: float | None = None) -> bool:
        """Block until every worker finishes; False if any is still running."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        alive = [thread for thread in threads if thread.is_alive()]
        with self._lock:
            self._threads = alive
        return not alive

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Cancel and wait, so no worker outlives the panel that started it.

        A measurement thread that survives its panel is the same class of
        problem as a viewer callback that does: it keeps running against state
        nobody is looking at any more, and it can still be inside numpy while
        the process is tearing down.
        """
        self.cancel()
        return self.wait(timeout)

    def submit(
        self,
        job: MeasurementJob,
        measure,
        *,
        on_done,
        decorate=None,
        on_progress=None,
        on_discarded=None,
        on_error=None,
        threaded: bool = True,
    ) -> CancellationToken:
        """Start ``job``; ``on_done`` receives only a current, publishable run.

        **Every callback runs on the worker thread.** A Qt caller must
        therefore marshal them onto the GUI thread — emit a signal, or use
        ``QMetaObject.invokeMethod`` — rather than touching widgets directly.
        Keeping this module free of Qt is what lets it be tested without one,
        so the marshalling belongs to the panel; it is stated here because
        getting it wrong is a crash, not a warning.

        ``on_error`` is called per failed ROI, and again with ``roi=None`` if
        the run itself fails. Exactly one of ``on_done`` / ``on_discarded``
        always fires, whatever happens — a panel that shows a progress bar
        while a job runs has no other way to take it down again.
        """
        with self._lock:
            if self._cancellation is not None:
                self._cancellation.cancel()
            cancellation = CancellationToken()
            self._cancellation = cancellation
            self._current_job_id = job.job_id

        def _work():
            try:
                result = run_job(
                    job,
                    measure,
                    decorate=decorate,
                    cancellation=cancellation,
                    progress=on_progress,
                    cache=self._cache,
                    on_error=on_error,
                )
            except BaseException as exc:  # noqa: BLE001 - see below
                # Anything that escapes run_job — a plane that cannot be read,
                # a source torn down mid-run — must still end the job, or the
                # panel keeps a progress bar and a disabled button for a
                # thread that is already dead.
                if on_error is not None:
                    on_error(None, (), exc)
                result = MeasurementResult(job.job_id, (), failed=True)
            with self._lock:
                superseded = self._current_job_id != job.job_id
            if superseded:
                result = MeasurementResult(
                    job.job_id,
                    result.rows,
                    cancelled=result.cancelled,
                    stale=True,
                    failed=result.failed,
                )
            # `on_done` is the *publish* hook, so it fires only for a result
            # that may be published. A cancelled or superseded run reaches
            # `on_discarded` instead, which is what the panel needs to clear a
            # progress bar without also clearing the numbers on screen.
            if result.publishable:
                on_done(result)
            elif on_discarded is not None:
                on_discarded(result)

        if not threaded:
            _work()
            return cancellation

        thread = threading.Thread(target=_work, name=f"roi-measure-{job.job_id[:8]}")
        thread.daemon = True
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()
        return cancellation


def planes_for_axis(frame, axis_label: str, *, positions=()) -> tuple[Position, ...]:
    """Every slice position along one axis, keeping the others fixed.

    What Multi Measure iterates over.
    """
    descriptor = frame.axis(axis_label) if frame is not None else None
    if descriptor is None:
        return ((),)
    fixed = tuple((label, index) for label, index in positions if label != axis_label)
    return tuple(
        tuple(sorted((*fixed, (axis_label, index))))
        for index in range(int(descriptor.size))
    )


def stack_axis_labels(frame) -> tuple[str, ...]:
    """Non-displayed axes with more than one slice — what can be stepped."""
    if frame is None:
        return ()
    displayed = set(frame.plane_axes)
    return tuple(
        axis.label
        for axis in frame.axes
        if axis.label not in displayed and int(axis.size) > 1
    )


__all__ = [
    "ArrayPlaneSource",
    "CancellationToken",
    "ImagePlaneSource",
    "LazyPlaneSource",
    "LiveDataError",
    "PositionError",
    "cache_key",
    "MeasurementCache",
    "MeasurementJob",
    "MeasurementResult",
    "MeasurementRunner",
    "planes_for_axis",
    "run_job",
    "stack_axis_labels",
]

# Kept for callers that want a deterministic id sequence in tests.
_JOB_COUNTER = itertools.count()
