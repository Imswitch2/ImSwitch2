"""Live mode: watch a root folder and reconstruct the raw data that appears in it."""

import math
import os
from dataclasses import dataclass
import re
import time
import zarr
import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.logging import (
    initLogger,
    LoggerAdapter,
)
from imswitch.improcess.model.image_sources import flatten_metadata_attrs
from imswitch.improcess.reconstructors.base import (
    Contract,
    Incompatible,
    RawInfo,
    StackInfo,
    StreamInit,
    StreamingReconstructor,
    StreamingSession,
)
from .basecontrollers import ImProcessWidgetController


class DirectoryWatcherController(ImProcessWidgetController):
    """Orchestrates the watching: creates the three workers, each on its own
    thread, when the user ticks Monitor, and shuts them down on untick.

        ObserverWorker -- entry --> (queue) --> StreamWorker
        StreamWorker   -- contract, init frames, chunks --> ProcessWorker
        ProcessWorker  -- results --> viewer
    """

    # how long stopping waits for the ObserverWorker and the StreamWorker,
    # each; they answer stop() as soon as the read in progress returns, so
    # only a read that hangs (a slow network share) runs past it.
    STOP_TIMEOUT_S = 3.0

    def __init__(self, *args, main_controller=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main_controller = main_controller
        self._observer_worker: ObserverWorker | None = None
        self._stream_worker: StreamWorker | None = None
        self._process_worker: ProcessWorker | None = None
        self._observer_thread: QtCore.QThread | None = None
        self._stream_thread: QtCore.QThread | None = None
        self._process_thread: QtCore.QThread | None = None
        # entries found while the StreamWorker is busy wait here: FIFO
        self._entry_queue: list[str] = []
        self._stream_worker_busy = False
        # set while winding down (stopAfterCurrent): run once monitoring stops
        self._on_stopped = None
        # workers that ran past STOP_TIMEOUT_S, with their threads: kept
        # referenced until they end, since a QThread must not be destroyed
        # while it runs
        self._stragglers: list[tuple[QtCore.QObject, QtCore.QThread]] = []
        self._widget.sigMonitorChanged.connect(self._on_monitor_changed)

    def closeEvent(self):
        # ImProcess is closing: stop the watch as unticking Monitor does, and
        # be done before the factory's shutdown barrier moves on. A wind-down
        # in progress is cut short, and what it was for (a plugin reload)
        # is not run.
        self._on_stopped = None
        self._stop_monitoring()
        # one more wait for a worker left running by an earlier stop. If its
        # read still hangs, there is no safe way to end it: Qt aborts the
        # process when it destroys a running QThread on exit.
        for worker, thread in self._stragglers:
            if not thread.wait(int(self.STOP_TIMEOUT_S * 1000)):
                self._logger.error(
                    f"{type(worker).__name__} is still running as ImProcess "
                    f"closes; its read has not returned"
                )

    def isLiveReconstructionRunning(self) -> bool:
        # asked before plugins are reloaded under a running watch; a watch
        # winding down is still running
        return self._observer_worker is not None

    def stopAfterCurrent(self, on_stopped):
        """Wind the watch down, finishing what is in hand, then call
        ``on_stopped()``.

        No new entries, and no new frames: the StreamWorker ends its entry
        with the frames already read, the ProcessWorker processes them and
        shows the result, and once the entry is finished monitoring stops.
        The Monitor box is disabled meanwhile.
        """
        if self._observer_worker is None:
            on_stopped()
            return
        winding_down = self._on_stopped is not None
        self._on_stopped = on_stopped
        if winding_down:
            return
        self._logger.info("Finishing the current entry, then stopping")
        self._widget.monitorFolderCheck.setEnabled(False)
        self._observer_worker.stop()
        self._entry_queue.clear()
        self._stream_worker.finish_now()
        if not self._stream_worker_busy:
            self._end_wind_down()

    def _on_monitor_changed(self, enabled: bool):
        if enabled:
            self._start_monitoring()
        else:
            self._stop_monitoring()

    def _start_monitoring(self):
        root = self._widget.path
        reconstructor = getattr(self._main_controller, "_activeReconstructor", None)
        if not os.path.isdir(root):
            error = f"Not a folder: {root!r}"
        elif not isinstance(reconstructor, StreamingReconstructor):
            error = "The selected reconstructor doesn't support streaming"
        else:
            error = None
        if error:
            self._logger.error(error)
            self._widget.showError(error)
            self._widget.monitorFolderCheck.setChecked(False)
            return

        if self._observer_worker is not None:
            # the checkbox can't start a running watch: this is a bug, so
            # say so rather than restart over it
            self._logger.error(
                "Monitoring started while already running: sigMonitorChanged "
                "is connected more than once"
            )
            return

        # workers
        self._observer_worker = ObserverWorker(
            initLogger("DirectoryWatcherController", instanceName="ObserverWorker"),
            root,
        )
        # the StreamWorker gets the class, for contract(), and the
        # ProcessWorker the instance: no instance shared across threads
        self._stream_worker = StreamWorker(
            initLogger("DirectoryWatcherController", instanceName="StreamWorker"),
            type(reconstructor),
        )
        self._process_worker = ProcessWorker(
            initLogger("DirectoryWatcherController", instanceName="ProcessWorker"),
            reconstructor,
        )

        # threads
        self._observer_thread = QtCore.QThread()
        self._stream_thread = QtCore.QThread()
        self._process_thread = QtCore.QThread()

        # worker to threads
        self._observer_worker.moveToThread(self._observer_thread)
        self._stream_worker.moveToThread(self._stream_thread)
        self._process_worker.moveToThread(self._process_thread)

        # signals
        ow, sw, pw = self._observer_worker, self._stream_worker, self._process_worker
        self._observer_thread.started.connect(ow.start)
        ow.sigEntryFound.connect(self._entry_found)
        sw.sigStartStreaming.connect(sw.start)
        sw.sigEntryFinished.connect(self._entry_finished)
        sw.sigContractEstablished.connect(pw.start)
        sw.sigChunk.connect(pw.process)
        sw.sigStreamFinished.connect(pw.finish)
        pw.sigDone.connect(sw.on_process_done)
        if self._commChannel is not None:
            # connections via the CC to the reconstruction viewer
            pw.sigResult.connect(self._commChannel.sigLiveResultUpdated)
            pw.sigTimepoint.connect(self._commChannel.sigLiveTimepointUpdated)
            pw.sigTimepointDone.connect(self._commChannel.sigLiveTimepointDone)

        # downstream first, so nothing is emitted to a thread not yet running
        self._process_thread.start()
        self._stream_thread.start()
        self._observer_thread.start()
        self._logger.info(f"Monitoring {root}")

    def _stop_monitoring(self):
        if self._observer_worker is None:
            return
        self._stragglers = [(w, t) for w, t in self._stragglers if t.isRunning()]
        # nothing from these workers reaches this controller again, not even
        # from one that is late to stop
        self._observer_worker.sigEntryFound.disconnect(self._entry_found)
        self._stream_worker.sigEntryFinished.disconnect(self._entry_finished)

        # upstream first: no new entries, then no new chunks. stop() ends the
        # polling, so its thread's event loop can quit.
        # One that doesn't within the timeout is left to end on its own: its
        # stop is queued, so it does once its read returns.
        for worker, thread in (
            (self._observer_worker, self._observer_thread),
            (self._stream_worker, self._stream_thread),
        ):
            worker.stop()
            thread.quit()
            if not thread.wait(int(self.STOP_TIMEOUT_S * 1000)):
                self._logger.warning(
                    f"{type(worker).__name__} did not stop within "
                    f"{self.STOP_TIMEOUT_S:g} s; it ends on its own once its "
                    f"current read returns"
                )
                self._stragglers.append((worker, thread))

        # then processing, with no timeout: the chunk being processed is
        # finished, the ones still queued are skipped (quit() alone would
        # still deliver them), and the session is closed once the thread has
        # ended, so never in the middle of one
        self._process_worker.skip_queued()
        self._process_thread.quit()
        if not self._process_thread.wait(int(self.STOP_TIMEOUT_S * 1000)):
            self._logger.info("Waiting for the chunk being processed to finish")
            self._process_thread.wait()
        self._process_worker.stop()

        self._observer_worker = self._stream_worker = self._process_worker = None
        self._observer_thread = self._stream_thread = self._process_thread = None
        self._entry_queue.clear()
        self._stream_worker_busy = False
        self._logger.info("Monitoring stopped")

        # a wind-down ends here too if something stopped the watch first
        self._widget.monitorFolderCheck.setEnabled(True)
        on_stopped, self._on_stopped = self._on_stopped, None
        if on_stopped is not None:
            on_stopped()

    def _end_wind_down(self):
        # the entry in hand has been processed: the wind-down is complete
        self._widget.monitorFolderCheck.setChecked(False)
        self._stop_monitoring()  # if unchecking didn't (no signal, or unchecked already)

    def _entry_found(self, entry: str):
        if self._stream_worker is None or self._on_stopped is not None:
            return  # queued before the workers were stopped, or winding down
        self._logger.info(f"Adding entry: {os.path.basename(entry)} to the queue")
        self._entry_queue.append(entry)
        self._stream_entry()

    def _entry_finished(self):
        if self._stream_worker is None:
            return  # queued before the workers were stopped
        self._stream_worker_busy = False
        if self._on_stopped is not None:
            # winding down: the entry in hand has been processed
            self._end_wind_down()
            return
        self._stream_entry()

    def _stream_entry(self):
        # hand the oldest entry to the StreamWorker, unless it is busy
        if self._stream_worker_busy or not self._entry_queue:
            return
        self._stream_worker_busy = True
        entry = self._entry_queue.pop(0)
        params = self._get_reconstructor_params()
        self._stream_worker.sigStartStreaming.emit(entry, params)

    def _get_reconstructor_params(self) -> dict:
        # the user's values, read here on the main thread since they live
        # in the GUI: one snapshot per entry
        widget = getattr(self._main_controller, "_widget", None)
        getter = getattr(widget, "getReconstructionParams", None)
        return dict(getter()) if callable(getter) else {}


class ObserverWorker(QtCore.QObject):
    """Polls a root folder and emits each new entry in it, once."""

    sigEntryFound = QtCore.Signal(str)  # absolute path
    sigStop = QtCore.Signal()

    def __init__(
        self,
        logger: LoggerAdapter,
        root: str,
        sleep_ms: int = 1000
    ):
        super().__init__()
        self._logger = logger
        self._root = os.path.abspath(root)
        self._sleep_ms = sleep_ms
        self._seen: set[str] = set()
        # the timer is a child of the OW, and when the OW is moved to its
        # thread (ow_thread) via moveToThread() in the DWC, the timer moves
        # to the thread as well
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._check)
        # _check is a @Slot, so it runs on the OW's thread: see the SW
        # connected now, so a stop() before start() is queued, not lost
        self.sigStop.connect(self._timer.stop)

    def start(self):
        self._timer.start(self._sleep_ms)
        # check now: the timer's first timeout only comes after sleep_ms
        self._check()

    def stop(self):
        # a QTimer must be stopped on its own thread, so this is queued there
        self.sigStop.emit()

    @QtCore.Slot()
    def _check(self):
        try:
            names = sorted(os.listdir(self._root))
        except OSError as e:
            self._logger.warning(f"Can't list {self._root}: {e}")
            return
        for name in names:
            path = os.path.join(self._root, name)
            if path not in self._seen:
                self._logger.info(f"Found entry: {path}")
                self._seen.add(path)
                self.sigEntryFound.emit(path)


class StreamWorker(QtCore.QObject):
    """Streams one entry at a time: describes its raw data, agrees to a contract
    with the reconstructor, then reads the frames as they land on disk and
    sends them to the ProcessWorker, the init frames first, then in chunks.

    An ImControl recording takes one of six forms, where a recording form is
    denoted as an entry Ei for i = 1,...,6 and each <cam>/ holds {data, metadata/}:

    - SpecFrames, SpecTime, ScanOnce, UntilStop:
        E1. rec.zarr/{<camA>, <camB>, ...}/
        E2. rec_<cam>.zarr/<cam>/

    - CameraLapse, ScanLapse, the lapse in one file:
        E3. rec.zarr/scanN/{<camA>, <camB>, ...}/
        E4. rec_<cam>.zarr/scanN/<cam>/

    - CameraLapse (timeN) or ScanLapse (scanN), a file per timepoint:
        E5. rec_timeN.zarr/{<camA>, <camB>, ...}/
        E6. rec_timeN_<cam>.zarr/<cam>/

    Each form sits either in the root or in one folder the user made in the root:
    folder/E1 ... folder/E6, where a folder is one entry.

    In the root, each container is an entry of its own, so a lapse of form E5
    or E6 arrives as one entry per timepoint. These are not streamed as
    independent entries but grouped into one. From the first timepoint's
    entry the SW infers, by its metadata, that it is a lapse, and finds the
    other timepoints' containers by its name with the time or scan index
    changed, reading each one's frames as it appears. The OW still emits
    each later timepoint as an entry of its own, and the DWC still passes it
    to the SW, but the SW skips it: its frames were already read with the lapse.

    Zarr only, for now.
    """

    sigStartStreaming = QtCore.Signal(str, object)                  # (entry, params)
    sigEntryFinished = QtCore.Signal()                              # read, sent and processed
    sigContractEstablished = QtCore.Signal(object, object, object)  # (RawInfo, Contract, init frames)
    sigChunk = QtCore.Signal(int, int, object)                      # (start, end, frames [start, end))
    sigStreamFinished = QtCore.Signal()
    # emitted by stop() and finish_now(), from any thread: handled on this one
    sigStop = QtCore.Signal()
    sigFinishNow = QtCore.Signal()

    LAPSES = ("CameraLapse", "ScanLapse")

    def __init__(
        self,
        logger: LoggerAdapter,
        reconstructor_class: type[StreamingReconstructor],
        sleep_ms: int = 1000,
    ):
        super().__init__()
        self._logger = logger
        self._reconstructor_class = reconstructor_class  # for contract() only
        self._sleep_ms = sleep_ms
        self._current: _EntryStream | None = None  # the entry being read
        self._finishing = False  # finish_now(): no new frames
        self._pw_busy = False  # an item sent, not yet confirmed by the ProcessWorker

        # the timer is a child of the SW, and when the SW is moved to its
        # thread via moveToThread() in the DWC, the timer moves to the
        # thread as well
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._read_more)
        # the slots connected here, before moveToThread(), are @Slot: PyQt5
        # runs a plain method on the thread it was connected on instead
        # connected now, so a stop() or finish_now() before start() is
        # queued, not lost
        self.sigStop.connect(self._on_stop)
        self.sigFinishNow.connect(self._on_finish_now)

        # This set exists for one edge case only: a lapse recorded with a
        # file per timepoint (E5, E6) directly in the root. There, every
        # timepoint file is an entry of its own, but the SW streams the whole
        # lapse from the first one, reading the later timepoint files as they
        # appear. The set stores the paths of those later files, so that when
        # their own entries arrive they are skipped instead of streamed again.
        # For every other form it stays empty.
        self._lapse_siblings_read: set[str] = set()

    def start(self, entry: str, params: dict):
        name = os.path.basename(entry)

        if entry in self._lapse_siblings_read:
            self._logger.debug(f"{name}: a timepoint of a lapse already read")
            self.sigEntryFinished.emit()
            return

        self._logger.info(f"Streaming {name}")

        raw_data_info = self._get_raw_data_info(entry)
        if raw_data_info is None:
            self._logger.info(f"{name}: no raw data found")
            self._finish_entry(entry)
            return

        try:
            contract = self._reconstructor_class.contract(raw_data_info, params)
        except Incompatible as e:
            self._logger.info(f"{name}: incompatible: {e}")
            self._finish_entry(entry)
            return

        self._logger.info(f"{name}: {contract}")
        # read now and then on every timeout, until the entry has finished;
        # start() itself returns at once, so stop() and finish_now() are
        # handled in between reads
        self._current = _EntryStream(
            entry=entry,
            raw_data_info=raw_data_info,
            contract=contract,
            pending=np.empty((0, *raw_data_info.frame_shape), raw_data_info.dtype),
        )
        self._timer.start(self._sleep_ms)
        self._read_more()  # read now: the timer's first timeout only comes after sleep_ms

    def stop(self):
        # safe from any thread: queued to this one, where it runs between reads
        self.sigStop.emit()

    def finish_now(self):
        # safe from any thread: queued to this one, where it runs between reads
        self.sigFinishNow.emit()

    @QtCore.Slot()
    def _on_stop(self):
        # the entry is abandoned, not finished: nothing more is sent
        self._timer.stop()
        self._current = None

    @QtCore.Slot()
    def _on_finish_now(self):
        # end the entry with the frames already read, as if the recording
        # had finished there: what is in hand is still sent and processed
        self._finishing = True
        self._send_next()

    def _finish_entry(self, entry: str):
        # looked up again, not reused: a live lapse has more timepoints by now.
        # Only files next to the entry count: that is the root-level E5/E6 case
        root = os.path.dirname(entry)
        for dg_path, _ in self._get_data_groups(entry):
            if dg_path != entry and os.path.dirname(dg_path) == root:
                self._lapse_siblings_read.add(dg_path)

        self.sigEntryFinished.emit()

    # --- describe ---------------------------------------------------------

    def _get_raw_data_info(self, entry: str) -> RawInfo | None:
        data_groups = self._get_data_groups(entry)
        if not data_groups:
            return None

        _, first_group = data_groups[0]  # the first timepoint's detector group
        data = first_group.get("data")
        metadata = flatten_metadata_attrs(first_group.get("metadata"))
        mode = metadata.get("Rec:Mode")
        frames_per_stack = _positive_int(data.attrs.get("recording:frames_per_stack"))
        expected_frames = _positive_int(data.attrs.get("recording:expected_frames"))

        # The raw data is described in the canonical shape (T, *S, Y, X),
        # where only T grows while a recording is being written.
        # T is the number of timepoints: how many times the recording
        # repeats its inner structure. None means it isn't known up front,
        # as for an UntilStop recording or a lapse whose metadata doesn't
        # say how many timepoints it will have.
        # S is the shape of one timepoint, in frames: (n,) for a stack or
        # scan of n frames, and () when every frame is a timepoint of its
        # own. So one timepoint holds prod(S) frames (1 for ()).
        #
        # ImControl stores every data group as (N, Y, X): N frames in the
        # order they were written, each a 2D detector frame (Y, X). Only N
        # carries structure, and it splits into T and S. The shapes to
        # expect, per recording mode:
        #
        #   Mode             groups  frames per group  (T, *S, Y, X)
        #   SpecFrames       1       expected_frames   (T, Y, X), T known
        #   SpecTime         1       known at the end  (T, Y, X)
        #   UntilStop        1       grows, unknown    (T, Y, X), T unknown
        #   ScanOnce         1       scan positions    (1, n, Y, X)
        #   repeated stacks  1       k * stack         (T, stack, Y, X)
        #   CameraLapse      T       frames_per_stack  (T, n, Y, X)
        #   ScanLapse        T       scan positions    (T, n, Y, X)
        #
        # So S is () when every frame is a timepoint, and (n,) when a
        # timepoint is a stack or scan of n frames. The scan axes that make
        # up n (Z, or a Y x X raster) are named only in the AcquisitionLayout,
        # which RawInfo.attrs carries; S keeps them flat. T is the frame
        # count for the single-group time series, the number of groups (one
        # per timepoint) for a lapse.
        T: int | None = None
        S: tuple[int, ...] = ()
        if mode in self.LAPSES:
            T = _positive_int(data.attrs.get("recording:num_timepoints"))
            per_timepoint = frames_per_stack or expected_frames
            if per_timepoint is None:
                self._logger.debug(f"{mode}: frames per timepoint unknown")
                return None
            S = (per_timepoint,)
        elif len(data_groups) > 1:
            self._logger.debug(
                f"{len(data_groups)} recordings that are not one lapse: skipped"
            )
            return None
        elif frames_per_stack:
            # repeated stacks
            T = expected_frames // frames_per_stack if expected_frames else None
            S = (frames_per_stack,)
        elif expected_frames:
            # SpecFrames: a time series of known length
            T = expected_frames
            S = ()
        else:
            committed, finished = self._progress(data)
            if finished:
                # SpecTime, or any finished one: a time series
                T = committed
                S = ()
            else:
                # until stopped: each frame is a timepoint
                T = None
                S = ()

        raw_data_info = RawInfo(
            T=T,
            S=S,
            frame_shape=tuple(data.shape[-2:]),
            dtype=data.dtype,
            name=self._name(entry, mode),
            mode=mode,
            attrs=dict(data.attrs),  # a copy: RawInfo goes to other threads
            metadata=metadata,
        )
        self._logger.debug(f"raw_data_info = {raw_data_info}")
        return raw_data_info

    @staticmethod
    def _name(entry: str, mode: str | None) -> str:
        # the entry's name, without ".zarr"; for a timepoint of a lapse not
        # in a single file, without the timepoint: rec_Camera for
        # rec_time0_Camera.zarr, rec_time1_Camera.zarr, ...
        name = os.path.basename(entry)
        if name.lower().endswith(".zarr"):
            name = name[:-len(".zarr")]
            matches = list(_LAPSE_NAME.finditer(name))
            if mode in StreamWorker.LAPSES and matches:
                name = name[:matches[-1].start()] + name[matches[-1].end() - 1:]
        return name

    def _get_data_groups(self, entry: str) -> list[tuple[str, zarr.Group]] | list[None]:
        stores: list[str] = self._zarr_stores(entry)
        groups: list[tuple[str, zarr.Group]] | list[None] = []
        for store in stores:
            # a store still being created, or one whose metadata the
            # writer is replacing right now (Windows), is left for the next
            # call; _fill keeps the timepoints in order regardless
            try:
                root = zarr.open_group(store, mode="r")
                for _, child in root.groups():
                    if "data" in child and "metadata" in child:
                        groups.append((store, child))
                    else:  # scanN/<det>
                        groups += [(store, det) for _, det in child.groups()
                                   if "data" in det and "metadata" in det]
            except Exception:
                continue

        if not groups:
            return []

        if len(stores) > 1 and entry.lower().endswith(".zarr"):
            # the files next to a lapse timepoint that share its name: keep
            # those whose metadata says they belong to the same lapse
            own = [self._lapse(group) for store, group in groups if store == entry]
            groups = [
                (store, group) for store, group in groups
                if store == entry or (own and own[0] and self._lapse(group) == own[0])
            ]

        # several detectors: keep only the one whose name sorts first, for now
        detector = min(group.basename for _, group in groups)
        groups = [(store, group) for store, group in groups if group.basename == detector]

        # in recording order: a lapse's timepoints by their recorded index
        # (a single recording has none, and counts as 0)
        def lapse_index(store_and_group):
            _, group = store_and_group
            return int(group["data"].attrs.get("recording:lapse_index", 0))
        groups.sort(key=lapse_index)

        return groups

    @staticmethod
    def _zarr_stores(entry: str) -> list[str]:
        if not entry.lower().endswith(".zarr"):
            if not os.path.isdir(entry):
                return []  # a lone file that isn't a Zarr store
            return [os.path.join(entry, n) for n in os.listdir(entry)
                    if n.lower().endswith(".zarr")]

        folder_name, file_name = os.path.split(entry)
        matches = list(_LAPSE_NAME.finditer(file_name))

        if not matches:
            return [entry]

        index = matches[-1].span(1)
        siblings = re.compile(
            re.escape(file_name[:index[0]]) + r"\d+" + re.escape(file_name[index[1]:]) + "$",
            re.IGNORECASE,
        )

        try:
            names = os.listdir(folder_name)
        except OSError:
            return [entry]

        return [entry if n == file_name else os.path.join(folder_name, n)
                for n in names if siblings.match(n)]

    def _lapse(self, group: zarr.Group) -> tuple[str, int] | None:
        # which lapse a timepoint belongs to, for one not in a single file;
        # None for anything else
        attrs = group["data"].attrs
        if attrs.get("recording:single_lapse_file", False):
            return None

        mode = group["metadata"]["Rec"].attrs.get("Mode", None)
        if mode is None or mode not in self.LAPSES:
            return None

        n_timepoint = attrs.get("recording:num_timepoints", None)
        if n_timepoint is None:
            return None

        return str(mode), _positive_int(n_timepoint)

    # --- stream -----------------------------------------------------------

    def on_process_done(self):
        # pw.sigDone: the ProcessWorker has handled the last item it was
        # sent, so the next one can go
        self._pw_busy = False
        self._read_more()

    @QtCore.Slot()
    def _read_more(self):
        # on the timer, and whenever the ProcessWorker is done: read, send
        # the next item if the ProcessWorker is free, then read on, so the
        # next chunk is in hand while the ProcessWorker works on this one
        current = self._current
        if current is None:
            return
        self._fill(current)
        self._send_next()
        if self._current is current:
            self._fill(current)

    def _send_next(self):
        # one item at a time, each only once the ProcessWorker has confirmed
        # the last: the contract with the init frames (at least one: a
        # session begins on frames), then every whole chunk from frame 0,
        # then the end of the stream. Once that is confirmed too, the entry
        # has been processed.
        current = self._current
        if current is None or self._pw_busy:
            return
        contract = current.contract
        init_frames = max(contract.init_frames, 1)
        n = contract.chunk_frames
        no_more_frames = current.finished or self._finishing
        if not current.announced:
            if current.read >= init_frames:
                self.sigContractEstablished.emit(
                    current.raw_data_info, contract, current.pending[:init_frames]
                )
                current.announced = True
            elif no_more_frames:
                if not self._finishing:
                    self._logger.info(
                        f"Recording finished with fewer than {init_frames} frames"
                    )
                self._end_entry_stream()
                return
            else:
                return  # waiting for frames
        elif len(current.pending) >= n:
            self.sigChunk.emit(current.sent, current.sent + n, current.pending[:n])
            current.pending = current.pending[n:]
            current.sent += n
        elif not no_more_frames:
            return  # waiting for frames
        elif not current.stream_finished:
            # only whole chunks are sent: a recording stopped mid-chunk
            # leaves a tail that doesn't meet the contract
            if len(current.pending):
                self._logger.info(
                    f"Dropped the last {len(current.pending)} frames: "
                    f"fewer than one chunk of {n}"
                )
            self.sigStreamFinished.emit()
            current.stream_finished = True
        else:
            self._end_entry_stream()
            return
        self._pw_busy = True

    def _end_entry_stream(self):
        # read, sent and processed: the next entry can come
        self._timer.stop()
        current, self._current = self._current, None
        self._finish_entry(current.entry)

    def _fill(self, current: "_EntryStream"):
        # read on from where the last call stopped, but only once the next
        # item can't be sent from what is in hand, and then a block of at
        # least one timepoint: opening the stores costs far more than reading
        # a frame, so a read per chunk would crawl when chunks are single
        # frames. The SW never holds more than that block ahead of the
        # ProcessWorker. Sets current.finished once the recording has been
        # read to its end. Frames count only up to the first timepoint still
        # being written, or missing, so they stay in order. On Windows a
        # read can collide with the writer replacing a metadata file: that
        # call reads nothing more, the next one retries.
        if current.finished or self._finishing:
            return
        contract = current.contract
        needed = contract.chunk_frames
        if not current.announced:
            needed = max(needed, contract.init_frames, 1)
        if len(current.pending) >= needed:
            return  # the next item is in hand
        target = max(needed, math.prod(current.raw_data_info.S))

        data_groups = self._get_data_groups(current.entry)  # reopened: Zarr reads its metadata on open
        offset = 0
        try:
            for index, (_, group) in enumerate(data_groups):
                data = group["data"]
                if int(data.attrs.get("recording:lapse_index", 0)) != index:
                    return  # a timepoint not readable yet
                committed, done = self._progress(data)
                new_from = max(current.read - offset, 0)
                end = min(committed, new_from + target - len(current.pending))
                if end > new_from:
                    frames = _read_frames(data, new_from, end)
                    current.pending = np.concatenate([current.pending, frames])
                    current.read += len(frames)
                offset += committed
                if end < committed or not done:
                    return  # more to read here, now or once written
        except OSError:
            return

        raw_data_info = current.raw_data_info
        if raw_data_info.mode in self.LAPSES and raw_data_info.T:
            current.finished = len(data_groups) >= raw_data_info.T
        else:
            current.finished = True

    @staticmethod
    def _progress(data: zarr.Array) -> tuple[int, bool]:
        # how many frames are safely on disk, and whether the writer is
        # done. The writer keeps this barrier for live readers, since the
        # shape alone can cover chunks not yet written; an array without
        # it was written in one go.
        if "recording:frames_committed" in data.attrs:
            committed = int(data.attrs["recording:frames_committed"])
            return committed, not data.attrs.get("writing", False)

        return math.prod(data.shape[:-2]), True


@dataclass
class _EntryStream:
    """What the StreamWorker keeps about the entry it is reading."""

    entry: str
    raw_data_info: RawInfo
    contract: Contract
    pending: np.ndarray  # frames read but not yet sent in a chunk
    read: int = 0  # frames read so far
    sent: int = 0  # frames sent in chunks so far
    announced: bool = False  # the init frames have been sent with the contract
    finished: bool = False  # the recording has been read to its end
    stream_finished: bool = False  # sigStreamFinished has been sent


def _read_frames(data, start: int, end: int) -> np.ndarray:
    # frames [start, end) of one array; they run row-major over every
    # axis but (Y, X)
    lead = data.shape[:-2]
    if len(lead) == 1:
        return np.asarray(data[start:end])

    return np.stack([data[np.unravel_index(i, lead)] for i in range(start, end)])


_LAPSE_NAME = re.compile(r"_(?:time|scan)(\d+)_", re.IGNORECASE)


def _positive_int(value) -> int | None:
    # a positive int, or None for anything else
    try:
        value = int(value)
        value = value if value > 0 else None
    except (TypeError, ValueError):
        return None

    return value


class ProcessWorker(QtCore.QObject):
    """Reconstructs one entry at a time: begins a session on the init frames,
    pushes it every chunk after them, and sends its result to the viewer --
    the whole result once, then one timepoint at a time, at most every
    update_interval_s and whenever a timepoint completes."""

    sigResult = QtCore.Signal(object)  # ProcessingResult: the whole of it
    sigTimepoint = QtCore.Signal(int, object)  # (timepoint, plane)
    sigTimepointDone = QtCore.Signal(int)  # a timepoint is complete
    # after every start(), process() and finish(), whatever came of it: the
    # StreamWorker sends the next item only then
    sigDone = QtCore.Signal()

    def __init__(
        self,
        logger: LoggerAdapter,
        reconstructor: StreamingReconstructor,
        update_interval_s: float = 0.2,
    ):
        super().__init__()
        self._logger = logger
        self._reconstructor = reconstructor
        self._update_interval_s = update_interval_s
        self._raw_data_info: RawInfo | None = None
        self._contract: Contract | None = None
        self._params: dict | None = None
        self._session: StreamingSession | None = None
        self._skipping = False  # skip_queued(): stopping, process nothing more
        self._begun = 0  # frames begin() consumed: pushed from there on
        self._shown = False  # the viewer has the whole result
        self._last_update = 0.0

    def start(
        self,
        raw_data_info: RawInfo,
        contract: Contract,
        init_frames: np.ndarray,
    ):
        try:
            self._start(raw_data_info, contract, init_frames)
        finally:
            self.sigDone.emit()

    def process(self, start: int, end: int, frames: np.ndarray):
        try:
            self._process(start, end, frames)
        finally:
            self.sigDone.emit()

    def finish(self):
        try:
            self._finish()
        finally:
            self.sigDone.emit()

    def _start(self, raw_data_info: RawInfo, contract: Contract, init_frames: np.ndarray):
        # a new entry: resolve the parameters the init frames answer, then
        # begin a fresh session on them
        self._raw_data_info = raw_data_info
        self._contract = contract
        if self._session is not None:
            self._session.close()
            self._session = None

        self._begun = len(init_frames)
        try:
            self._params = contract.params
            if contract.init_frames:
                self._params = self._reconstructor.complete(contract.params, init_frames)
            session = self._reconstructor.make_session()
            session.begin(self._stream_init(init_frames), self._params)
        except Exception as e:
            self._logger.error(f"{raw_data_info.name}: could not begin: {e}")
            return
        self._session = session
        self._shown = False
        self._logger.info(f"Reconstructing {raw_data_info.name}")
        self._update(force=True)

    def _process(self, start: int, end: int, frames: np.ndarray):
        # frames [start, end). begin() consumed the frames before
        # self._begun, so only what follows them is pushed
        if self._session is None or self._skipping:
            return
        skip = max(self._begun - start, 0)
        if skip < end - start:
            try:
                self._session.push(frames[skip:], start + skip, end)
            except Exception as e:
                self._logger.error(f"{self._raw_data_info.name}: push failed: {e}")
                self._session = None
                return
        per_timepoint = math.prod(self._raw_data_info.S)
        done = end % per_timepoint == 0
        if done:
            self.sigTimepointDone.emit(end // per_timepoint - 1)
        # a completed stack is shown at once; single-frame timepoints (a
        # recording until stopped) keep to the interval
        self._update(force=done and per_timepoint > 1)

    def _finish(self):
        # the recording has finished, and every chunk has been pushed
        if self._session is None or self._skipping:
            return
        self.sigResult.emit(self._session.finish())
        self._logger.info(f"Reconstructed {self._raw_data_info.name}")

    def skip_queued(self):
        # safe from any thread: the item being processed is finished, and
        # one that still arrives (from a StreamWorker late to stop) is not
        # -- the watch is stopping
        self._skipping = True

    def stop(self):
        # called once the thread has ended, so never mid-chunk
        if self._session is not None:
            self._session.close()
            self._session = None

    def _update(self, force: bool = False):
        # the viewer: the whole result on the first update, then the
        # timepoint being written, which costs one plane however long the
        # recording runs. A session that can't give a plane gives the whole.
        now = time.monotonic()
        if not force and now - self._last_update < self._update_interval_s:
            return
        plane = self._session.live_plane() if self._shown else None
        if plane is None:
            self.sigResult.emit(self._session.result())
            self._shown = True
        else:
            self.sigTimepoint.emit(*plane)
        self._last_update = now

    def _stream_init(self, init_frames: np.ndarray) -> StreamInit:
        # begin() still takes the StreamInit the earlier live sources built:
        # the RawInfo, in its shape
        info = self._raw_data_info
        attrs = {**info.attrs, **info.metadata}
        per_timepoint = math.prod(info.S)
        return StreamInit(
            name=info.name,
            dataset_name=info.name,
            data=init_frames,
            attrs=attrs,
            stack_info=StackInfo(
                frame_shape=info.frame_shape,
                dtype=info.dtype,
                attrs=attrs,
                frames_per_stack=per_timepoint if info.S else None,
                expected_frames=info.T * per_timepoint if info.T else None,
                source_format="zarr",
            ),
        )


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
