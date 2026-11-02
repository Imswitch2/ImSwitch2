"""Live mode: watch a root folder and reconstruct the raw data that appears in it."""

import math
import os
from dataclasses import dataclass, field
import re
import time
import h5py
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
        # The entries handed to the StreamWorker, kept across stops and starts
        # of monitoring, so that an entry is reconstructed only once. Only
        # Reset, or restarting ImSwitch, forgets them. An entry still queued
        # when monitoring stops isn't in it, so it is found again next time.
        self._seen_entries: set[str] = set()
        # set while winding down (stopAfterCurrent): run once monitoring stops
        self._on_stopped = None
        # workers that ran past STOP_TIMEOUT_S, with their threads: kept
        # referenced until they end, since a QThread must not be destroyed
        # while it runs
        self._stragglers: list[tuple[QtCore.QObject, QtCore.QThread]] = []
        self._widget.sigMonitorChanged.connect(self._on_monitor_changed)
        self._widget.sigSkipEntryClicked.connect(self.skipEntry)
        self._widget.sigResetClicked.connect(self.reset)

    def skipEntry(self):
        """Stop reading the entry being streamed and move on to the next one.

        The StreamWorker finishes the entry with the frames it has already
        read, so they are still processed and shown, and the next entry in
        the queue follows as usual. Without an entry being streamed, it does
        nothing.
        """
        if self._stream_worker is None or not self._stream_worker_busy:
            return
        self._logger.info("Skipping the current entry")
        self._stream_worker.finish_now()

    def reset(self):
        """Stop monitoring, as unticking Monitor does, and forget the entries
        already seen, so that the next watch reconstructs every entry in the
        root again.
        """
        self._widget.monitorFolderCheck.setChecked(False)
        self._stop_monitoring()  # if unchecking didn't (unchecked already)
        self._logger.info("Forgetting the entries seen")
        self._seen_entries.clear()

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
            seen=set(self._seen_entries),  # a copy: the OW lives on another thread
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
        self._seen_entries.add(entry)
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
        sleep_ms: int = 1000,
        seen: set[str] | None = None,
    ):
        super().__init__()
        self._logger = logger
        self._root = os.path.abspath(root)
        self._sleep_ms = sleep_ms
        # the entries emitted so far, starting with those the DWC has already
        # seen in earlier watches, which are never emitted again
        self._seen: set[str] = seen if seen is not None else set()
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

    Every form can also be an HDF5 file (.hdf5 or .h5) instead of a Zarr
    store, with the same groups inside. A Zarr store is streamed while it is
    being written. An HDF5 file is only read once ImControl has closed it:
    reading one that is still open for writing can break the recording on
    Windows, so the SW waits until the writer is done with it.
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

    # How far the SW reads ahead of the ProcessWorker in one read, at most
    # (but always at least the next item and one timepoint). The stores are
    # opened at most once per sleep_ms (see _read_block), so each read takes
    # as much as it can.
    READ_AHEAD_BYTES = 256 * 2**20

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
        self._current_entry: _EntryStream | None = None  # the entry being read
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
        # appear. The set stores the paths of the lapse's files, so that when
        # their own entries arrive they are skipped instead of streamed again.
        # A timepoint file that only appears after the lapse was finished
        # early (Skip entry) is skipped too, since its name matches files in
        # the set. For every other form it stays empty.
        self._lapse_siblings_read: set[str] = set()

        # The HDF5 files opened since the last _close_hdf5_files(). They are
        # closed again as soon as a step is done with them, since ImControl
        # reopens a file it has written (to finish it, or to add the next
        # timepoint of a lapse) and must not find it held open.
        self._open_hdf5_files: list[h5py.File] = []
        # The stores the last _get_data_groups() could not open, though they
        # are on disk: an HDF5 file ImControl still has open for writing, or a
        # Zarr store being created or having its metadata replaced.
        self._unopened_stores: list[str] = []
        # An entry (path, params) whose HDF5 file couldn't be read yet when it
        # arrived. start() tries it again on every timeout until it can.
        self._waiting_entry: tuple[str, dict] | None = None

    def start(self, entry: str, params: dict):
        # Starts streaming one entry. The DWC calls it through sigStartStreaming,
        # with the entry's path and a snapshot of the reconstructor parameters
        # from the panel.
        #
        # An entry that is a later timepoint of a lapse already read is skipped.
        # Otherwise the method describes the entry's raw data and asks the
        # reconstructor class for a contract. An entry without raw data, or one
        # the reconstructor refuses, is finished at once. For an accepted entry,
        # it sets up the entry's stream and starts the timer; from then on the
        # frames are read and sent by _read_more(). start() itself returns at
        # once, so stop() and finish_now() can be handled while the entry is
        # being streamed.
        #
        # An HDF5 file can't be opened while ImControl is still writing it, so
        # an HDF5 entry found during its recording can't be described yet. The
        # entry then waits: start() is called again for it on every timeout,
        # until the file has been closed and can be read.
        name = os.path.basename(entry)

        if any(store in self._lapse_siblings_read for store in self._stores(entry)):
            self._logger.debug(f"{name}: a timepoint of a lapse already read")
            self.sigEntryFinished.emit()
            return

        retrying = self._waiting_entry is not None
        self._waiting_entry = None
        if not retrying:
            self._logger.info(f"Streaming {name}")

        raw_data_info = self._get_raw_data_info(entry)
        self._close_hdf5_files()
        unopened_hdf5_files = [
            store for store in self._unopened_stores if _store_extension(store) != ".zarr"
        ]
        if raw_data_info is None and unopened_hdf5_files:
            if not retrying:
                self._logger.info(f"{name}: waiting until ImControl has finished writing it")
            self._waiting_entry = (entry, params)
            self._timer.start(self._sleep_ms)
            return

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
        self._current_entry = _EntryStream(
            path=entry,
            raw_data_info=raw_data_info,
            contract=contract,
            pending_frames=np.empty((0, *raw_data_info.frame_shape), raw_data_info.dtype),
        )
        self._timer.start(self._sleep_ms)
        self._read_more()  # read now: the timer's first timeout only comes after sleep_ms

    def stop(self):
        # Stops streaming for good, abandoning the current entry. It is safe to
        # call from any thread: it only emits sigStop, which _on_stop() handles
        # on this worker's thread, between reads.
        self.sigStop.emit()

    def finish_now(self):
        # Finishes the current entry early: no new frames are read for it, and
        # it is finished with the frames already read. The DWC calls it for
        # Skip entry and for a wind-down. It is safe to call from any thread:
        # it only emits sigFinishNow, which _on_finish_now() handles on this
        # worker's thread, between reads.
        self.sigFinishNow.emit()

    @QtCore.Slot()
    def _on_stop(self):
        # Handles stop() on this worker's thread. It stops the timer and drops
        # the current entry, so nothing more is read or sent for it. The entry
        # is abandoned, not finished: sigEntryFinished is not sent.
        self._timer.stop()
        self._current_entry = None
        self._waiting_entry = None

    @QtCore.Slot()
    def _on_finish_now(self):
        # Handles finish_now() on this worker's thread. From now on no new
        # frames are read for the current entry. What is already in hand is
        # still sent and processed, as if the recording had ended there, and
        # _send_next() then finishes the entry. An entry still waiting for its
        # HDF5 file has nothing in hand, so it is finished at once. Without a
        # current entry there is nothing to finish.
        if self._waiting_entry is not None:
            entry, _ = self._waiting_entry
            self._waiting_entry = None
            self._timer.stop()
            self._finish_entry(entry)
            return
        if self._current_entry is None:
            return
        self._current_entry.reading_stopped = True
        self._send_next()

    def _finish_entry(self, entry: str):
        # Reports an entry as finished to the DWC with sigEntryFinished, so the
        # next entry can be streamed. It is called for every entry the SW is
        # done with: one that was refused, and one that has been read, sent
        # and processed.
        #
        # Before that, it records the timepoint files of an E5/E6 lapse in the
        # root, the entry's own included, so that the later timepoints' own
        # entries are skipped when they arrive. The data groups are looked up
        # again rather than reused, since a live lapse has more timepoints by
        # now. Only files next to the entry count.
        root = os.path.dirname(entry)
        lapse_files = [
            dg_path for dg_path, _ in self._get_data_groups(entry)
            if dg_path != entry and os.path.dirname(dg_path) == root
        ]
        if lapse_files or _LAPSE_NAME.search(os.path.basename(entry)):
            self._lapse_siblings_read.update(lapse_files + [entry])
        self._close_hdf5_files()

        self.sigEntryFinished.emit()

    def _open_store(self, path: str) -> zarr.Group | h5py.File:
        # Opens a Zarr store or an HDF5 file for reading, and returns its
        # root group. Opening can fail, for example for a store still being
        # created; the caller then tries again on a later read.
        #
        # An HDF5 file is always opened without HDF5's file locking. A reader
        # that takes the lock makes ImControl's writer fail in the middle of a
        # recording on Windows ("Permission denied"). Without the lock, the
        # open fails for as long as the writer has the file open, so the SW
        # only ever reads HDF5 files the writer has closed. The file is kept
        # in self._open_hdf5_files, to be closed by _close_hdf5_files().
        if path.lower().endswith(".zarr"):
            return zarr.open_group(path, mode="r")
        hdf5_file = h5py.File(path, "r", locking=False)
        self._open_hdf5_files.append(hdf5_file)
        return hdf5_file

    def _close_hdf5_files(self):
        # Closes every HDF5 file opened since the last call. The steps that
        # open stores (describing an entry, reading a block, finishing an
        # entry) call it when they are done, so that the writer can reopen a
        # file it has written without finding it held open.
        for hdf5_file in self._open_hdf5_files:
            try:
                hdf5_file.close()
            except Exception:
                pass
        self._open_hdf5_files.clear()

    # --- describe ---------------------------------------------------------

    def _get_raw_data_info(self, entry: str) -> RawInfo | None:
        # Describes the raw data of an entry as a RawInfo, or returns None if
        # the entry holds no raw data the SW can stream.
        #
        # The description comes from the entry's first data group: its data
        # array gives the frame shape and dtype, and its attributes and metadata
        # give the recording mode and the frame counts. From those it works out
        # T and S, as explained below. The contract with the reconstructor is
        # based on this RawInfo.
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
        # Returns the name the entry's result is shown under: the entry's file
        # name without its extension (.zarr, .hdf5 or .h5). For a timepoint of
        # a lapse stored with a file per timepoint, the timepoint is left out
        # too, so that every timepoint gives the lapse's name: rec_Camera for
        # rec_time0_Camera.zarr, rec_time1_Camera.zarr, and so on.
        name = os.path.basename(entry)
        extension = _store_extension(name)
        if extension:
            name = name[:-len(extension)]
            matches = list(_LAPSE_NAME.finditer(name))
            if mode in StreamWorker.LAPSES and matches:
                name = name[:matches[-1].start()] + name[matches[-1].end() - 1:]
        return name

    def _get_data_groups(
        self, entry: str, skip_stores: set[str] = frozenset()
    ) -> list[tuple[str, zarr.Group | h5py.Group]]:
        # Returns the detector groups the entry's frames are read from, in
        # recording order, each paired with the store (Zarr store or HDF5
        # file) it is in. A single recording gives one group. A lapse gives
        # one per timepoint: a scanN group, or a store of its own.
        #
        # It opens every store of the entry and collects the groups that hold
        # both data and metadata, directly or one level down (scanN/<det>).
        # For an E5/E6 lapse in the root, it keeps only the stores whose
        # metadata says they belong to the same lapse as the entry. With
        # several detectors, only the one whose name sorts first is kept, for
        # now. HDF5 files it opens stay open until _close_hdf5_files().
        #
        # The stores in skip_stores are left out: _read_block passes the ones
        # it has already read to their end. The entry's own store is still
        # opened, since it tells which lapse the other stores must belong to.
        stores: list[str] = self._stores(entry)
        groups: list[tuple[str, zarr.Group | h5py.Group]] = []
        self._unopened_stores = []
        for store in stores:
            if store in skip_stores and store != entry:
                continue
            # a store still being created, a Zarr store whose metadata the
            # writer is replacing right now (Windows), or an HDF5 file still
            # open for writing, is left for the next call; _read_block keeps
            # the timepoints in order regardless
            try:
                root = self._open_store(store)
                for child in _child_groups(root):
                    if "data" in child and "metadata" in child:
                        groups.append((store, child))
                    else:  # scanN/<det>
                        groups += [(store, det) for det in _child_groups(child)
                                   if "data" in det and "metadata" in det]
            except Exception:
                # a store that exists but can't be opened may be readable
                # later; one that is gone never will be
                if os.path.exists(store):
                    self._unopened_stores.append(store)
                continue

        if not groups:
            return []

        if len(stores) > 1 and _store_extension(entry):
            # the files next to a lapse timepoint that share its name: keep
            # those whose metadata says they belong to the same lapse
            own = [self._lapse(group) for store, group in groups if store == entry]
            groups = [
                (store, group) for store, group in groups
                if store == entry or (own and own[0] and self._lapse(group) == own[0])
            ]

        groups = [(store, group) for store, group in groups if store not in skip_stores]
        if not groups:
            return []

        # several detectors: keep only the one whose name sorts first, for now
        detector = min(_group_name(group) for _, group in groups)
        groups = [(store, group) for store, group in groups if _group_name(group) == detector]

        # in recording order: a lapse's timepoints by their recorded index
        # (a single recording has none, and counts as 0)
        def lapse_index(store_and_group):
            _, group = store_and_group
            return _lapse_index(group)
        groups.sort(key=lapse_index)

        return groups

    @staticmethod
    def _stores(entry: str) -> list[str]:
        # Returns the paths of the stores that make up an entry. A store is a
        # Zarr store (.zarr) or an HDF5 file (.hdf5 or .h5):
        # - for a folder, the stores inside it (folder/E1 ... folder/E6);
        # - for a timepoint of an E5/E6 lapse in the root, the entry and the
        #   stores next to it whose names differ only in the time or scan index,
        #   <savename>_time<N>_<det>.zarr (or _scan<N>_, or .hdf5);
        # - for any other store, the entry itself (E1 to E4).
        # Anything else, such as a lone file that is not a store, gives an
        # empty list.
        if not _store_extension(entry):
            if not os.path.isdir(entry):
                return []  # a lone file that isn't a store
            return [os.path.join(entry, n) for n in os.listdir(entry)
                    if _store_extension(n)]

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

    def _lapse(self, group: zarr.Group | h5py.Group) -> tuple[str, int] | None:
        # Identifies the lapse a timepoint belongs to, as (mode, number of
        # timepoints), for a lapse stored with a file per timepoint. Stores with
        # the same identity are taken to be timepoints of the same lapse.
        # Anything else, including a lapse stored in a single file, gives None.
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
        # Handles pw.sigDone: the ProcessWorker has handled the last item it was
        # sent, so the next one may go. It clears the busy flag and reads and
        # sends right away, rather than waiting for the next timeout.
        self._pw_busy = False
        self._read_more()

    @QtCore.Slot()
    def _read_more(self):
        # Moves the current entry's stream forward. It is called on every
        # timeout of the timer, and whenever the ProcessWorker is done.
        #
        # It first reads frames if the next item needs them, then sends that
        # item if the ProcessWorker is free, and then reads again. The second
        # read means the next chunk is already in hand while the ProcessWorker
        # works on this one, so the ProcessWorker never waits for a read that
        # could have been done earlier.
        if self._waiting_entry is not None:
            self.start(*self._waiting_entry)  # its HDF5 file may be readable now
            return
        current_entry = self._current_entry
        if current_entry is None:
            return
        self._read_block(current_entry)
        self._send_next()
        if self._current_entry is current_entry:
            self._read_block(current_entry)

    def _send_next(self):
        # Sends the next item to the ProcessWorker, one item at a time. An
        # item is only sent once the ProcessWorker has confirmed the previous
        # one. The items come in this order: first the contract together
        # with the init frames, then every whole chunk starting from frame 0,
        # and last the end of the stream. There is always at least one init
        # frame, since a session begins on frames. Once the ProcessWorker has
        # confirmed the end of the stream, the entry has been processed and
        # the next entry can start.
        # Nothing to send, or the ProcessWorker is still busy with the
        # previous item.
        current_entry = self._current_entry
        if current_entry is None or self._pw_busy:
            return

        entry_contract = current_entry.contract
        init_frames = max(entry_contract.init_frames, 1)
        chunk_frames = entry_contract.chunk_frames
        no_more_frames = current_entry.completely_read or current_entry.reading_stopped

        # 1. The contract with the init frames. It is sent once enough
        # frames have been read. A recording that ends before that is
        # finished without anything having been sent.
        if not current_entry.init_frames_sent:
            if current_entry.read_frames >= init_frames:
                self.sigContractEstablished.emit(
                    current_entry.raw_data_info,
                    entry_contract,
                    current_entry.pending_frames[:init_frames],
                )
                current_entry.init_frames_sent = True
                self._pw_busy = True
            elif no_more_frames:
                if not current_entry.reading_stopped:
                    self._logger.info(
                        f"Recording finished with fewer than {init_frames} frames"
                    )
                self._end_entry_stream()
            return  # otherwise, wait for more frames

        # 2. The next whole chunk, as soon as one is in hand.
        if len(current_entry.pending_frames) >= chunk_frames:
            chunk_start = current_entry.sent_frames
            self.sigChunk.emit(
                chunk_start,
                chunk_start + chunk_frames,
                current_entry.pending_frames[:chunk_frames],
            )
            current_entry.pending_frames = current_entry.pending_frames[chunk_frames:]
            current_entry.sent_frames += chunk_frames
            self._pw_busy = True
            return
        if not no_more_frames:
            return  # wait for more frames

        # 3. The end of the stream, once every whole chunk has been sent.
        # Only whole chunks are sent, so the frames of a recording that
        # stopped mid-chunk are dropped: they don't meet the contract.
        if not current_entry.stream_finished:
            if len(current_entry.pending_frames):
                self._logger.info(
                    f"Dropped the last {len(current_entry.pending_frames)} frames: "
                    f"fewer than one chunk of {chunk_frames}"
                )
            self.sigStreamFinished.emit()
            current_entry.stream_finished = True
            self._pw_busy = True
            return

        # 4. The ProcessWorker has confirmed the end of the stream: the
        # entry has been processed.
        self._end_entry_stream()

    def _end_entry_stream(self):
        # Ends the stream of the current entry, once it has been read, sent and
        # processed, or once the recording turned out too short to send
        # anything. It stops the timer, drops the entry and reports it finished,
        # so the next entry can come.
        self._timer.stop()
        current_entry = self._current_entry
        self._current_entry = None
        self._finish_entry(current_entry.path)

    def _read_block(self, current_entry: "_EntryStream"):
        # Reads the next block of frames of the current entry from disk into
        # current_entry.pending_frames, from where the previous call stopped.
        #
        # It reads only when the frames in hand are not enough for the next
        # item that _send_next() has to send (the init frames, or a chunk).
        # It then reads a block of at least one timepoint, not just one
        # chunk: opening the stores costs far more than reading a frame,
        # so reading one chunk at a time would be very slow when chunks are
        # single frames. This also bounds memory: the SW never holds more
        # than one block ahead of the ProcessWorker.
        #
        # Frames are only read in recording order. Reading stops at the first
        # timepoint that is still being written, or that hasn't appeared on
        # disk yet, and continues on a later call. On Windows a read can
        # collide with the writer replacing a metadata file; that call then
        # reads nothing more, and a later call retries.
        #
        # Once the whole recording has been read, it sets
        # current_entry.completely_read.
        if current_entry.completely_read or current_entry.reading_stopped:
            return
        entry_contract = current_entry.contract
        needed_frames = entry_contract.chunk_frames
        if not current_entry.init_frames_sent:
            needed_frames = max(needed_frames, entry_contract.init_frames, 1)
        if len(current_entry.pending_frames) >= needed_frames:
            return  # the next item is in hand

        # The stores are opened at most once per sleep_ms (with some slack
        # for the timer). Every open reads the recording's metadata files,
        # and on Windows ImControl's Zarr writer fails when it replaces one
        # of them while it is being read; it has no retry. Fewer opens make
        # that rarer, though they can't rule it out.
        now = time.monotonic()
        if now - current_entry.last_open_time < 0.9 * self._sleep_ms / 1000:
            return
        current_entry.last_open_time = now

        # S is the shape of one timepoint in frames, so its product is the
        # number of frames in one timepoint (1 when S is ())
        raw_data_info = current_entry.raw_data_info
        frames_per_timepoint = math.prod(raw_data_info.S)
        frame_bytes = math.prod(raw_data_info.frame_shape) * np.dtype(raw_data_info.dtype).itemsize
        frames_in_read_ahead = max(self.READ_AHEAD_BYTES // frame_bytes, 1)
        target_frames = max(needed_frames, frames_per_timepoint, frames_in_read_ahead)

        # Reopened on every read, since Zarr reads its metadata on open, but
        # not the stores already read to their end: those are only counted.
        is_lapse = raw_data_info.mode in self.LAPSES
        data_groups = self._get_data_groups(
            current_entry.path, skip_stores=current_entry.stores_read
        )
        timepoint = current_entry.timepoints_in_stores_read  # the next one to read
        frames_in_earlier_groups = current_entry.frames_in_stores_read
        previous_store = None
        try:
            if not data_groups:
                # nothing could be opened this time (a store being replaced,
                # or an HDF5 file reopened by its writer), or the next
                # timepoint hasn't appeared yet; a later call retries
                return
            lapse_indices = [_lapse_index(group) for _, group in data_groups]
            for (store, group), lapse_index in zip(data_groups, lapse_indices):
                if previous_store is not None and store != previous_store:
                    # every group of the previous store has been read to its
                    # end, so later reads can skip that store
                    current_entry.stores_read.add(previous_store)
                    current_entry.timepoints_in_stores_read = timepoint
                    current_entry.frames_in_stores_read = frames_in_earlier_groups
                previous_store = store
                if lapse_index < timepoint:
                    continue  # a timepoint already read

                # Timepoints missing from a lapse: a later timepoint is on
                # disk, so ImControl has moved on without them. They are
                # filled with blank frames, so that the timepoints after them
                # keep their place and the stream doesn't stall. Not while a
                # store couldn't be opened, though: it may hold them.
                if lapse_index > timepoint:
                    if self._unopened_stores:
                        return
                    frames_in_earlier_groups += (lapse_index - timepoint) * frames_per_timepoint
                    missing = (
                        f"timepoint {timepoint} is missing" if lapse_index - timepoint == 1
                        else f"timepoints {timepoint} to {lapse_index - 1} are missing"
                    )
                    self._fill_with_blank_frames(current_entry, frames_in_earlier_groups, missing)
                    timepoint = lapse_index

                data = group["data"]
                committed_frames, writer_done = self._progress(data)
                if is_lapse:
                    committed_frames = min(committed_frames, frames_per_timepoint)
                # A timepoint is over once its writer is done with it, or, in
                # a lapse, once a later timepoint is on disk, even if its
                # writer never said so.
                timepoint_over = writer_done or lapse_index < lapse_indices[-1]
                first_unread_frame = max(current_entry.read_frames - frames_in_earlier_groups, 0)
                frames_wanted = target_frames - len(current_entry.pending_frames)
                read_until = min(committed_frames, first_unread_frame + frames_wanted)
                if read_until > first_unread_frame:
                    frames = _read_frames(data, first_unread_frame, read_until)
                    current_entry.pending_frames = np.concatenate(
                        [current_entry.pending_frames, frames]
                    )
                    current_entry.read_frames += len(frames)
                if read_until < committed_frames or not timepoint_over:
                    return  # more to read in this group, now or once written

                # Every timepoint of a lapse holds the same number of frames:
                # one that ended short is filled up with blank frames, for
                # the same reason as a missing one.
                if is_lapse:
                    frames_in_earlier_groups += frames_per_timepoint
                    self._fill_with_blank_frames(
                        current_entry, frames_in_earlier_groups,
                        f"timepoint {timepoint} holds only {committed_frames} "
                        f"of {frames_per_timepoint} frames",
                    )
                else:
                    frames_in_earlier_groups += committed_frames
                timepoint += 1
        except OSError:
            return
        finally:
            self._close_hdf5_files()

        if is_lapse and raw_data_info.T:
            current_entry.completely_read = timepoint >= raw_data_info.T
        else:
            current_entry.completely_read = True

    def _fill_with_blank_frames(self, current_entry: "_EntryStream", until_frame: int, reason: str):
        # Adds blank (all zero) frames to the current entry's pending frames,
        # as many as it takes for the frames read so far to reach until_frame.
        # It stands in for frames that will never be on disk: a timepoint of a
        # lapse that is missing, or that ended short. Since it counts from the
        # frames already read, calling it again for the same frames adds none.
        blank_frames = until_frame - current_entry.read_frames
        if blank_frames <= 0:
            return
        raw_data_info = current_entry.raw_data_info
        self._logger.warning(
            f"{raw_data_info.name}: {reason}; filled in with {blank_frames} blank frames"
        )
        blank = np.zeros((blank_frames, *raw_data_info.frame_shape), raw_data_info.dtype)
        current_entry.pending_frames = np.concatenate([current_entry.pending_frames, blank])
        current_entry.read_frames += blank_frames

    @staticmethod
    def _progress(data: zarr.Array | h5py.Dataset) -> tuple[int, bool]:
        # Returns how many frames of a data array are safely on disk, and
        # whether the writer has finished it.
        #
        # While ImControl writes a recording, it keeps a barrier for live
        # readers. The barrier is needed because the array's shape can already
        # cover chunks that are not written yet. In a Zarr store the barrier is
        # in the array's attributes: recording:frames_committed and writing.
        # In an HDF5 file it is in two small datasets next to the data,
        # frames_committed and stream_complete, since attributes can't change
        # while the file is written; the writing attribute is only rewritten
        # after the file has been closed, so stream_complete says more. An
        # array without the barrier was written in one go, so all of it is
        # there.
        if isinstance(data, h5py.Dataset):
            detector_group = data.parent
            if "frames_committed" in detector_group and "stream_complete" in detector_group:
                committed = int(detector_group["frames_committed"][0])
                return committed, bool(detector_group["stream_complete"][0])
            return math.prod(data.shape[:-2]), True

        if "recording:frames_committed" in data.attrs:
            committed = int(data.attrs["recording:frames_committed"])
            return committed, not data.attrs.get("writing", False)

        return math.prod(data.shape[:-2]), True


@dataclass
class _EntryStream:
    """What the StreamWorker keeps about the entry it is reading."""

    path: str  # the entry's path, as the ObserverWorker emitted it
    raw_data_info: RawInfo
    contract: Contract
    pending_frames: np.ndarray  # frames read but not yet sent in a chunk
    read_frames: int = 0  # frames read so far
    sent_frames: int = 0  # frames sent in chunks so far
    init_frames_sent: bool = False  # sent to the ProcessWorker with the contract
    completely_read: bool = False  # the recording has been read to its end
    reading_stopped: bool = False  # finish_now(): no new frames are read
    stream_finished: bool = False  # sigStreamFinished has been sent
    last_open_time: float = 0.0  # when _read_block last opened the stores
    # the stores read to their end, which later reads skip, and how many
    # timepoints and frames they hold
    stores_read: set[str] = field(default_factory=set)
    timepoints_in_stores_read: int = 0
    frames_in_stores_read: int = 0


def _read_frames(data, start: int, end: int) -> np.ndarray:
    # frames [start, end) of one array; they run row-major over every
    # axis but (Y, X)
    lead = data.shape[:-2]
    if len(lead) == 1:
        return np.asarray(data[start:end])

    return np.stack([data[np.unravel_index(i, lead)] for i in range(start, end)])


_LAPSE_NAME = re.compile(r"_(?:time|scan)(\d+)_", re.IGNORECASE)

_STORE_EXTENSIONS = (".zarr", ".hdf5", ".h5")


def _store_extension(path: str) -> str | None:
    # The extension that makes a path a store (.zarr, .hdf5 or .h5), or None
    # for anything else
    lowered = path.lower()
    return next((ext for ext in _STORE_EXTENSIONS if lowered.endswith(ext)), None)


def _child_groups(group: zarr.Group | h5py.Group) -> list:
    # The groups directly inside a Zarr or HDF5 group; the two libraries list
    # them differently
    if isinstance(group, h5py.Group):
        return [child for child in group.values() if isinstance(child, h5py.Group)]
    return [child for _, child in group.groups()]


def _group_name(group: zarr.Group | h5py.Group) -> str:
    # The last part of a Zarr or HDF5 group's path: the detector's name for a
    # detector group
    return group.name.rstrip("/").rsplit("/", 1)[-1]


def _lapse_index(group) -> int:
    # the timepoint a data group holds in its lapse; 0 for a single recording
    return int(group["data"].attrs.get("recording:lapse_index", 0))


def _positive_int(value) -> int | None:
    # a positive int, or None for anything else
    try:
        value = int(value)
        value = value if value > 0 else None
    except (TypeError, ValueError):
        return None

    return value


class ProcessWorker(QtCore.QObject):
    """Reconstructs one entry at a time with the active reconstructor, from the
    items the StreamWorker sends, and sends the result to the viewer.

    The PW holds the reconstructor instance; the StreamWorker only has its
    class, for contract(). So the reconstruction itself runs on this worker's
    thread alone, and no reconstructor instance is shared between threads.

    For each entry the StreamWorker sends three kinds of item, in this order:

    1. start(): the RawInfo, the contract and the init frames. The PW makes a
       fresh session and begins it on the init frames, with the parameters
       the contract resolved. begin() consumes those frames, so they are not
       pushed again.
    2. process(): one chunk of frames [start, end), any number of times, in
       recording order. The PW pushes the frames that begin() hasn't already
       consumed into the session.
    3. finish(): the recording has ended and every chunk has been sent. The
       PW finishes the session and sends the final result.

    After every item, whatever came of it (an error included), the PW emits
    sigDone. The StreamWorker sends the next item only then, so the PW is
    never handed frames while it is busy, and frames never pile up in its
    event queue.

    While an entry is being reconstructed, the viewer is kept up to date:
    the whole result on the first update, and from then on only the plane
    of the timepoint being written, which costs the same however long the
    recording runs. An update is sent at most every update_interval_s, and
    at once when a timepoint made of a stack of frames is complete. A session
    that can't give a single plane sends the whole result every time.

    If the session fails to begin or a push fails, the error is logged and
    the rest of the entry is ignored; the next entry starts afresh. When
    monitoring stops, skip_queued() makes the PW ignore the items still in
    its queue, and stop() closes the session once the thread has ended.
    """

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
        # a new entry: begin a fresh session on the init frames
        self._raw_data_info = raw_data_info
        if self._session is not None:
            self._session.close()
            self._session = None

        self._begun = len(init_frames)
        try:
            session = self._reconstructor.make_session()
            session.begin(self._stream_init(init_frames), contract.params)
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
        if info.T:
            # The recorder's recording:num_timepoints counts lapse timepoints,
            # so it is 1 for a recording that isn't a lapse, even a time
            # series of many frames. Sessions take it as the number of
            # timepoints to make room for, so it is replaced by the T the
            # StreamWorker worked out, which counts every kind of timepoint.
            attrs["recording:num_timepoints"] = info.T
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
                # 1 when S is (): every frame is a timepoint of its own
                frames_per_stack=per_timepoint,
                expected_frames=info.T * per_timepoint if info.T else None,
                source_format=str(info.attrs.get("recording:source_format", "zarr")).lower(),
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
