"""Two-thread acknowledged incremental execution, independent of Session UI.

The reader owns disk I/O and never touches the reconstructor instance.  The
processor owns its StreamingSession and runs begin/push/finish/close exclusively
on its QThread.  All inter-thread requests are queued Qt signals.  There is
at most one processing item in flight; each is acknowledged after processing.
"""

from __future__ import annotations

from dataclasses import replace
import time

from qtpy import QtCore

from imswitch.improcess.reconstructors.base import StreamInit

from .reader import IncrementalReader


class StreamReaderWorker(QtCore.QObject):
    sigBegin = QtCore.Signal(int, object, object, object, object)
    sigPush = QtCore.Signal(int, int, int, object)
    sigFinish = QtCore.Signal(int)
    sigFailed = QtCore.Signal(int, str)
    sigStopped = QtCore.Signal(int)

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
                 dataset_name: str | None = None, preview_interval_s: float = 0.2):
        super().__init__()
        self._generation = generation
        self._reconstructor = reconstructor
        self._name = name
        self._dataset_name = dataset_name
        self._session = None
        self._info = None
        self._last_preview = 0.0
        self._preview_sent = False
        self._preview_interval_s = float(preview_interval_s)
        self._stopped = False

    @QtCore.Slot(int, object, object, object, object)
    def begin(self, generation, info, contract, init_frames, stack_info):
        if self._stopped or generation != self._generation:
            return
        try:
            self._info = info
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
            self._session = self._reconstructor.make_session()
            self._session.begin(
                StreamInit(name=self._name,
                           dataset_name=self._dataset_name or info.detector_name or "raw",
                           data=init_frames, attrs=attrs,
                           source_path=info.source_path, stack_info=effective_stack),
                contract.params,
            )
            self._preview(force=True)
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
            per_timepoint = self._info.frames_per_timepoint
            done = end // per_timepoint
            if end % per_timepoint == 0:
                self.sigTimepoint.emit(generation, done)
            self._preview(force=(end % per_timepoint == 0 and per_timepoint > 1))
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

    def _preview(self, *, force: bool = False):
        if self._session is None:
            return
        now = time.monotonic()
        if not force and now - self._last_preview < self._preview_interval_s:
            return
        plane = self._session.live_plane() if self._preview_sent else None
        if plane is not None:
            # live_plane() contract says the ndarray is already copied.
            self.sigPreview.emit(self._generation, None, plane)
        else:
            self.sigPreview.emit(self._generation, self._session.result(), None)
            self._preview_sent = True
        self._last_preview = now

    def _fail(self, exc):
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
