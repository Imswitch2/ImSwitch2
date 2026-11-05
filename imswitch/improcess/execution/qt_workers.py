"""Two-thread acknowledged incremental execution, independent of Session UI.

The reader owns disk I/O and never touches the reconstructor instance.  The
processor owns its StreamingSession and runs begin/push/finish/close exclusively
on its QThread.  All inter-thread requests are queued Qt signals.  There is
at most one processing item in flight; each is acknowledged after processing.
"""

from __future__ import annotations

from dataclasses import replace
import logging
import time

from qtpy import QtCore

from imswitch.improcess.reconstructors.base import StreamInit

from .reader import IncrementalReader


_logger = logging.getLogger(__name__)


class StreamReaderWorker(QtCore.QObject):
    sigBegin = QtCore.Signal(int, object, object, object, object)
    sigPush = QtCore.Signal(int, int, int, object)
    sigFinish = QtCore.Signal(int)
    sigFailed = QtCore.Signal(int, str)
    sigStopped = QtCore.Signal(int)
    sigCoverage = QtCore.Signal(int, object)  # source-declared missing raw intervals

    def __init__(self, generation: int, reader: IncrementalReader, poll_ms: int = 100):
        super().__init__()
        self._generation = generation
        self._reader = reader
        self._poll_ms = max(1, int(poll_ms))
        self._timer = None
        self._stopped = False

    @QtCore.Slot()
    def start(self):
        try:
            self._reader.open()
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(self._poll_ms)
            self._timer.timeout.connect(self._tick)
            self._timer.start()
            self._tick()
        except Exception as exc:
            self._fail(exc)

    @QtCore.Slot()
    def _tick(self):
        if self._stopped:
            return
        try:
            self._reader.prefetch()
            item = self._reader.next_item()
            if item is None:
                return
            if item.kind == "begin":
                self.sigBegin.emit(self._generation, self._reader.info,
                                   self._reader.contract, item.frames.data,
                                   self._reader.stack_info)
            elif item.kind == "push":
                self.sigPush.emit(self._generation, item.frames.start,
                                  item.frames.end, item.frames.data)
            elif item.kind == "finish":
                self.sigCoverage.emit(self._generation, self._reader.missing_ranges)
                self.sigFinish.emit(self._generation)
            else:
                raise RuntimeError(f"Unknown streaming item {item.kind}")
        except Exception as exc:
            self._fail(exc)

    @QtCore.Slot(int)
    def acknowledge(self, generation: int):
        if self._stopped or generation != self._generation:
            return
        try:
            self._reader.acknowledge()
            if self._reader.done:
                self._stop()
            else:
                self._tick()
        except Exception as exc:
            self._fail(exc)

    @QtCore.Slot(bool)
    def set_paused(self, paused: bool):
        if not self._stopped:
            self._reader.request_pause(paused)
            if not paused:
                self._tick()

    @QtCore.Slot()
    def cancel(self):
        self._stop()

    def _fail(self, exc):
        _logger.error(
            "Incremental reader failed (generation=%s)",
            self._generation, exc_info=(type(exc), exc, exc.__traceback__),
        )
        self.sigFailed.emit(self._generation, f"{type(exc).__name__}: {exc}")
        self._stop()

    def _stop(self):
        if self._stopped:
            return
        self._stopped = True
        if self._timer is not None:
            self._timer.stop()
        try:
            self._reader.close()
        except Exception as exc:
            self.sigFailed.emit(self._generation,
                                f"Source close failed: {type(exc).__name__}: {exc}")
        finally:
            self.sigStopped.emit(self._generation)


class StreamProcessWorker(QtCore.QObject):
    sigAcknowledged = QtCore.Signal(int)
    sigFailed = QtCore.Signal(int, str)
    sigResult = QtCore.Signal(int, object)
    sigPreview = QtCore.Signal(int, object, object)  # (generation, result|None, (T,plane)|None)
    sigTimepoint = QtCore.Signal(int, int)  # completed logical count
    sigStopped = QtCore.Signal(int)

    def __init__(self, generation: int, reconstructor, *, name: str,
                 dataset_name: str | None = None, preview_interval_s: float = 0.2,
                 enable_preview: bool = True):
        super().__init__()
        self._generation = generation
        self._reconstructor = reconstructor
        self._name = name
        self._dataset_name = dataset_name
        self._session = None
        self._info = None
        self._stream_init = None
        self._contract = None
        self._frames_processed = 0
        self._last_preview = 0.0
        self._preview_sent = False
        self._last_preview_revision = None
        self._preview_interval_s = float(preview_interval_s)
        self._enable_preview = bool(enable_preview)
        self._stopped = False

    @QtCore.Slot(int, object, object, object, object)
    def begin(self, generation, info, contract, init_frames, stack_info):
        if self._stopped or generation != self._generation:
            return
        try:
            self._info = info
            self._contract = contract
            self._frames_processed = len(init_frames)
            attrs = dict(info.attrs)
            attrs.update(info.metadata)
            if info.T is not None:
                # Single camera frame series may store lapse count = 1;
                # the stream's canonical timepoints take precedence.
                attrs["recording:num_timepoints"] = info.T
            # The initial StackInfo can have a legacy reader fallback of
            # frames_per_stack=all camera frames.  Correct for S=().
            effective_stack = replace(
                stack_info, frames_per_stack=info.frames_per_timepoint,
                planned_timepoints=info.T or stack_info.planned_timepoints,
            )
            self._stream_init = StreamInit(
                name=self._name,
                dataset_name=self._dataset_name or info.detector_name or "raw",
                data=init_frames, attrs=attrs,
                source_path=info.source_path, stack_info=effective_stack,
            )
            self._session = self._reconstructor.make_session()
            self._session.begin(self._stream_init, contract.params)
            self._preview(force=True)
            if len(init_frames) % info.frames_per_timepoint == 0:
                self.sigTimepoint.emit(
                    generation, len(init_frames) // info.frames_per_timepoint,
                )
        except Exception as exc:
            self._fail(exc)
            return
        self.sigAcknowledged.emit(generation)

    @QtCore.Slot(int, int, int, object)
    def push(self, generation, start, end, frames):
        if self._stopped or generation != self._generation:
            return
        try:
            if self._session is None:
                raise RuntimeError("push received without begin")
            self._session.push(frames, start, end)
            self._frames_processed = end
            per_timepoint = self._info.frames_per_timepoint
            done = end // per_timepoint
            self._preview(force=(end % per_timepoint == 0 and per_timepoint > 1))
            if end % per_timepoint == 0:
                self.sigTimepoint.emit(generation, done)
        except Exception as exc:
            self._fail(exc)
            return
        self.sigAcknowledged.emit(generation)

    @QtCore.Slot(int)
    def finish(self, generation):
        if self._stopped or generation != self._generation:
            return
        try:
            if self._session is None:
                raise RuntimeError("finish received without begin")
            result = self._session.finish()
            self._record_snapshot(result, "complete")
            self.sigResult.emit(generation, result)
        except Exception as exc:
            self._fail(exc)
            return
        # The acknowledgement is posted before the process thread is asked
        # to quit. The reader only terminates after this final acknowledgement.
        self.sigAcknowledged.emit(generation)
        self._stop()  # GPU/device resources closed on the owning QThread

    @QtCore.Slot()
    def cancel(self):
        self._stop()

    def _record_snapshot(self, result, status: str):
        # Match the existing live reconstruction provenance path, including
        # partial/complete status and a stable reconstruction node per session.
        from imswitch.improcess.reconstructors.run import record_snapshot

        expected = (
            self._info.T * self._info.frames_per_timepoint
            if self._info is not None and self._info.T is not None else None
        )
        record_snapshot(
            result, self._reconstructor, self._contract.params,
            self._stream_init, session=self._session, status=status,
            frames_committed=self._frames_processed, expected_frames=expected,
        )

    def _preview(self, *, force: bool = False):
        if self._session is None or not self._enable_preview:
            return
        now = time.monotonic()
        if not force and now - self._last_preview < self._preview_interval_s:
            return
        # Sessions with atomic scientific outputs (unified MoNaLISA Fast)
        # need not emit an unchanged full-volume snapshot on every raw chunk.
        # Older per-frame sessions have no revision hook and are unchanged.
        revision_getter = getattr(self._session, "preview_revision", None)
        revision = revision_getter() if callable(revision_getter) else None
        if revision is not None and revision == self._last_preview_revision:
            return
        plane = self._session.live_plane() if self._preview_sent else None
        if plane is not None:
            # live_plane() contract says the ndarray is already copied.
            self.sigPreview.emit(self._generation, None, plane)
        else:
            result = self._session.result()
            self._record_snapshot(result, "partial")
            self.sigPreview.emit(self._generation, result, None)
            self._preview_sent = True
        self._last_preview = now
        self._last_preview_revision = revision

    def _fail(self, exc):
        _logger.error(
            "Incremental processor failed (generation=%s)",
            self._generation, exc_info=(type(exc), exc, exc.__traceback__),
        )
        self.sigFailed.emit(self._generation, f"{type(exc).__name__}: {exc}")
        self._stop()

    def _stop(self):
        if self._stopped:
            return
        self._stopped = True
        try:
            if self._session is not None:
                self._session.close()
                self._session = None
        except Exception as exc:
            self.sigFailed.emit(self._generation,
                                f"Session close failed: {type(exc).__name__}: {exc}")
        finally:
            self.sigStopped.emit(self._generation)
