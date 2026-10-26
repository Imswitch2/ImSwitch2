"""Live mode: watch a root folder and reconstruct the raw data that appears in it."""

import math
import os
import re
import time
import zarr
import numpy as np
from qtpy import QtCore

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
        StreamWorker   -- contract, buffer, chunks --> ProcessWorker
        ProcessWorker  -- results --> viewer
    """

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
        self._widget.sigMonitorChanged.connect(self._on_monitor_changed)

    def _on_monitor_changed(self, enabled: bool) -> None:
        if enabled:
            self._start_monitoring()
        else:
            self._stop_monitoring()

    def _start_monitoring(self):
        root = self._widget.path
        reconstructor = getattr(self._main_controller, "_activeReconstructor", None)
        if not root or not os.path.isdir(root):
            error = f"Not a folder: {root!r}"
        elif not isinstance(reconstructor, StreamingReconstructor):
            error = "The selected reconstructor doesn't support streaming"
        else:
            error = None
        if error:
            self._logger.error(error)
            self._widget.monitorFolderCheck.setChecked(False)
            return

        self._stop_monitoring()  # never two sets of workers

        # workers
        self._observer_worker = ObserverWorker(self._logger, root)
        self._stream_worker = StreamWorker(self._logger, reconstructor)
        self._process_worker = ProcessWorker(self._logger, reconstructor)

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
        if self._commChannel is not None:  # the viewer
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
        # upstream first: no new entries, then no new chunks. stop() is what
        # lets a polling loop return, so its thread's event loop can quit.
        for worker, thread in (
            (self._observer_worker, self._observer_thread),
            (self._stream_worker, self._stream_thread),
        ):
            worker.stop()
            thread.quit()
            thread.wait()
        # then processing: the session is closed once the thread has ended,
        # so never in the middle of a chunk
        self._process_thread.quit()
        self._process_thread.wait()
        self._process_worker.stop()

        self._observer_worker = self._stream_worker = self._process_worker = None
        self._observer_thread = self._stream_thread = self._process_thread = None
        self._entry_queue.clear()
        self._stream_worker_busy = False
        self._logger.info("Monitoring stopped")

    def _entry_found(self, entry: str):
        if self._stream_worker is None:
            return  # queued before the workers were stopped
        self._logger.info(f"Adding entry: {os.path.basename(entry)} to the queue")
        self._entry_queue.append(entry)
        self._stream_entry()

    def _entry_finished(self):
        if self._stream_worker is None:
            return  # queued before the workers were stopped
        self._stream_worker_busy = False
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

    def __init__(self, logger, root: str, sleep_ms: int = 1000):
        super().__init__()
        self._logger = logger
        self._root = os.path.abspath(root)
        self._sleep_ms = sleep_ms
        self._seen: set[str] = set()
        # set here, not in start(): a stop() that comes before the thread
        # has started must still win
        self._running = True

    def start(self):
        while self._running:
            try:
                names = sorted(os.listdir(self._root))
            except OSError as e:
                self._logger.warning(f"Can't list {self._root}: {e}")
                names = []
            for name in names:
                path = os.path.join(self._root, name)
                if path not in self._seen:
                    self._seen.add(path)
                    self.sigEntryFound.emit(path)
            QtCore.QThread.msleep(self._sleep_ms)

    def stop(self):
        # safe from any thread: the loop sees it within one sleep
        self._running = False


class StreamWorker(QtCore.QObject):
    """Streams one entry at a time: describes its raw data, agrees a contract
    with the reconstructor, then copies the frames into a RawDataBuffer as
    they land on disk and tells the ProcessWorker which ones are there.

    An entry holds detector groups, each <det>/{data, metadata/}:

        P1  rec.zarr/<det>/                 one recording
        P2  rec.zarr/scanN/<det>/           a lapse in one container
        P3  folder/rec_timeN.zarr/<det>/    a lapse, one container per timepoint

    ImControl's default lapse is P3 without the folder: rec_time0_<det>.zarr,
    rec_time1_<det>.zarr, ... side by side in the root. Each arrives as an
    entry of its own, and is streamed as the lapse it belongs to: the files
    that would have been in that folder. The ones after the first are then
    skipped as already streamed.

    Zarr only, for now.
    """

    sigStartStreaming = QtCore.Signal(str, object)  # (entry, params)
    sigEntryFinished = QtCore.Signal()
    sigContractEstablished = QtCore.Signal(object, object, object)  # (RawInfo, Contract, RawDataBuffer)
    sigChunk = QtCore.Signal(int, int)  # frames [start, end) are in the buffer
    sigStreamFinished = QtCore.Signal()

    LAPSES = ("CameraLapse", "ScanLapse")

    def __init__(
        self,
        logger,
        reconstructor: StreamingReconstructor,
        sleep_ms: int = 1000,
    ):
        super().__init__()
        self._logger = logger
        self._reconstructor = reconstructor
        self._sleep_ms = sleep_ms
        self._stopped = False
        # containers already streamed as part of an entry: the timepoints of
        # a lapse not in a single file each arrive as an entry of their own
        self._streamed: set[str] = set()

    def start(self, entry: str, params: dict):
        name = os.path.basename(entry)
        if _key(entry) in self._streamed:
            self._logger.info(f"{name}: part of a lapse already streamed")
            self._entry_finished()
            return
        self._logger.info(f"Streaming {name}")
        raw_data_info = self._get_raw_data_info(entry)
        contract = self._establish_contract(raw_data_info, params)
        if contract is not None and not self._stream(entry, raw_data_info, contract):
            return  # stopped: the entry is abandoned, not finished
        self._streamed.update(_key(c) for c, _ in self._pieces(entry))
        self._entry_finished()

    def stop(self):
        # safe from any thread: the loop sees it within one sleep
        self._stopped = True

    def _entry_finished(self):
        self.sigEntryFinished.emit()

    # --- describe ---------------------------------------------------------

    def _get_raw_data_info(self, entry: str) -> RawInfo | None:
        pieces = self._pieces(entry)
        if not pieces:
            return None

        first = pieces[0][1]
        data = first["data"]
        attrs = dict(data.attrs)
        metadata = flatten_metadata_attrs(first["metadata"])
        mode = metadata.get("Rec:Mode")
        per_stack = _positive(attrs.get("recording:frames_per_stack"))
        expected = _positive(attrs.get("recording:expected_frames"))

        if mode in self.LAPSES:
            # the timepoints are grouped into one recording: scanN groups
            # (P2) or one container each (P3), each holding S
            T = _positive(attrs.get("recording:num_timepoints"))
            per_timepoint = per_stack or expected
            if per_timepoint is None:
                self._logger.info(f"{mode}: frames per timepoint unknown")
                return None
            S = (per_timepoint,)
        elif len(pieces) > 1:
            self._logger.info(
                f"{len(pieces)} recordings that are not one lapse: skipped"
            )
            return None
        elif per_stack:  # repeated stacks
            T, S = (expected // per_stack if expected else None), (per_stack,)
        elif expected:  # a known number of frames
            T, S = 1, (expected,)
        else:
            committed, finished = self._progress(data)
            if finished:  # all the frames there will be
                T, S = 1, (committed,)
            else:  # until stopped: each frame is a timepoint
                T, S = None, ()

        raw_data_info = RawInfo(
            T=T,
            S=S,
            frame_shape=tuple(data.shape[-2:]),
            dtype=data.dtype,
            name=self._name(entry, mode),
            mode=mode,
            attrs=attrs,
            metadata=metadata,
        )
        self._logger.info(f"raw_data_info = {raw_data_info}")
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

    def _pieces(self, entry: str) -> list[tuple[str, zarr.Group]]:
        # the entry's detector groups, with the container each is in: one
        # per timepoint for a lapse, in order. Several detectors: the first
        # one, for now.
        containers = self._containers(entry)
        groups = []
        for container in containers:
            # a container still being created, or one whose metadata the
            # writer is replacing right now (Windows), is left for the next
            # call; _fill keeps the timepoints in order regardless
            try:
                root = zarr.open_group(container, mode="r")
                for _, child in root.groups():
                    if "data" in child and "metadata" in child:
                        groups.append((container, child))
                    else:  # scanN/<det>
                        groups += [
                            (container, det) for _, det in child.groups()
                            if "data" in det and "metadata" in det
                        ]
            except Exception:
                continue
        if not groups:
            return []

        if len(containers) > 1 and entry.lower().endswith(".zarr"):
            # the files next to a lapse timepoint that share its name: keep
            # those whose metadata says they are the same lapse
            own = [self._lapse(g) for c, g in groups if c == entry]
            groups = [
                (c, g) for c, g in groups
                if c == entry or (own and own[0] and self._lapse(g) == own[0])
            ]

        detector = min(g.basename for _, g in groups)
        return sorted(
            ((c, g) for c, g in groups if g.basename == detector),
            key=lambda cg: int(cg[1]["data"].attrs.get("recording:lapse_index", 0)),
        )

    @staticmethod
    def _containers(entry: str) -> list[str]:
        # the Zarr stores that make up the entry: a folder's (P3); the
        # entry itself (P1, P2); or, for a timepoint of a lapse not in a
        # single file, it and the files next to it that share its name,
        # <savename>_time<N>_<det>.zarr (or _scan<N>_): the P3 folder
        # ImControl didn't make
        if not entry.lower().endswith(".zarr"):
            if not os.path.isdir(entry):
                return []  # a lone file that isn't a Zarr store
            return [
                os.path.join(entry, n) for n in os.listdir(entry)
                if n.lower().endswith(".zarr")
            ]
        folder, name = os.path.split(entry)
        matches = list(_LAPSE_NAME.finditer(name))
        if not matches:
            return [entry]
        index = matches[-1].span(1)
        siblings = re.compile(
            re.escape(name[:index[0]]) + r"\d+" + re.escape(name[index[1]:]) + "$",
            re.IGNORECASE,
        )
        try:
            names = os.listdir(folder)
        except OSError:
            return [entry]
        return [
            entry if n == name else os.path.join(folder, n)
            for n in names if siblings.match(n)
        ]

    def _lapse(self, group: zarr.Group) -> tuple | None:
        # which lapse a timepoint belongs to, for one not in a single file;
        # None for anything else
        attrs = group["data"].attrs
        if attrs.get("recording:single_lapse_file", False):
            return None
        mode = group["metadata"]["Rec"].attrs.get("Mode") if "Rec" in group["metadata"] else None
        if mode not in self.LAPSES:
            return None
        return mode, attrs.get("recording:num_timepoints")

    def _establish_contract(
        self, raw_data_info: RawInfo | None, params: dict
    ) -> Contract | None:
        # let the reconstructor decide: a Contract if it can
        # reconstruct the raw data, else None
        if raw_data_info is None:
            self._logger.info("No raw data found")
            return None
        try:
            contract = self._reconstructor.contract(raw_data_info, params)
        except Incompatible as e:
            self._logger.info(f"Incompatible: {e}")
            return None
        self._logger.info(f"contract: {contract}")
        return contract

    # --- stream -----------------------------------------------------------

    def _stream(self, entry: str, raw_data_info: RawInfo, contract: Contract) -> bool:
        # copy the frames into the buffer as they land on disk; once the
        # init frames are there (at least one: a session begins on frames),
        # hand the buffer to the ProcessWorker, then tell it about every
        # whole chunk, from frame 0.
        # False if stopped: the entry is abandoned, not finished.
        raw_data_buffer = RawDataBuffer(raw_data_info)
        n = contract.chunk_frames
        init_frames = max(contract.init_frames, 1)
        announced = False
        sent = 0
        while not self._stopped:
            finished = self._fill(entry, raw_data_info, raw_data_buffer)
            if not announced and len(raw_data_buffer) >= init_frames:
                self.sigContractEstablished.emit(raw_data_info, contract, raw_data_buffer)
                announced = True
            while announced and len(raw_data_buffer) - sent >= n:
                self.sigChunk.emit(sent, sent + n)
                sent += n
            if finished:
                break
            QtCore.QThread.msleep(self._sleep_ms)
        else:
            return False

        if not announced:
            self._logger.info(
                f"Recording finished with fewer than {init_frames} frames"
            )
            return True
        # only whole chunks are sent: a recording stopped mid-chunk leaves
        # a tail that doesn't meet the contract
        if len(raw_data_buffer) > sent:
            self._logger.info(
                f"Dropped the last {len(raw_data_buffer) - sent} frames: "
                f"fewer than one chunk of {n}"
            )
        self.sigStreamFinished.emit()
        return True

    def _fill(self, entry: str, raw_data_info: RawInfo, raw_data_buffer: "RawDataBuffer") -> bool:
        # append the frames that have landed on disk since the last call;
        # True once the recording has finished. Frames count only up to the
        # first timepoint still being written, or missing, so they stay in
        # order. On Windows a read can collide with the writer replacing a
        # metadata file: that call reads nothing more, the next one retries.
        pieces = self._pieces(entry)  # reopened: Zarr reads its metadata on open
        offset = 0
        finished = True
        try:
            for index, (_, group) in enumerate(pieces):
                data = group["data"]
                if int(data.attrs.get("recording:lapse_index", 0)) != index:
                    return False  # a timepoint not readable yet
                committed, done = self._progress(data)
                new_from = max(len(raw_data_buffer) - offset, 0)
                if committed > new_from:
                    raw_data_buffer.append(_read_frames(data, new_from, committed))
                offset += committed
                if not done:
                    return False
        except OSError:
            return False
        if raw_data_info.mode in self.LAPSES and raw_data_info.T:
            finished = len(pieces) >= raw_data_info.T
        return finished

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


class RawDataBuffer:
    """The raw frames of one entry, in memory and in recording order: the
    storage for the entry's whole streaming and processing.

    The StreamWorker appends each frame once, as it lands on disk; the
    ProcessWorker reads the frames [start, end) it has been told are there.
    Sized from the RawInfo when T is known, grown when it isn't.
    """

    def __init__(self, raw_data_info: RawInfo):
        self.raw_data_info = raw_data_info
        info = raw_data_info
        self._per_timepoint = math.prod(info.S)
        capacity = info.T * self._per_timepoint if info.T else 64
        self._frames = np.empty((capacity, *info.frame_shape), info.dtype)
        self._count = 0

    def __len__(self) -> int:
        return self._count

    def append(self, frames: np.ndarray) -> None:
        end = self._count + len(frames)
        if end > len(self._frames):
            grown = np.empty(
                (max(end, 2 * len(self._frames)), *self._frames.shape[1:]),
                self._frames.dtype,
            )
            grown[:self._count] = self._frames[:self._count]
            self._frames = grown
        self._frames[self._count:end] = frames
        self._count = end  # after the copy: readers never see half a frame

    def frames(self, start: int, end: int) -> np.ndarray:
        # frames [start, end), flat: (end - start, Y, X)
        assert end <= self._count, f"frames up to {end} asked, {self._count} there"
        return self._frames[start:end]

    def array(self) -> np.ndarray:
        # the whole timepoints so far, as (T, *S, Y, X)
        t = self._count // self._per_timepoint
        return self._frames[:t * self._per_timepoint].reshape(
            t, *self.raw_data_info.S, *self.raw_data_info.frame_shape
        )


def _read_frames(data, start: int, end: int) -> np.ndarray:
    # frames [start, end) of one array; they run row-major over every
    # axis but (Y, X)
    lead = data.shape[:-2]
    if len(lead) == 1:
        return np.asarray(data[start:end])
    return np.stack([data[np.unravel_index(i, lead)] for i in range(start, end)])


_LAPSE_NAME = re.compile(r"_(?:time|scan)(\d+)_", re.IGNORECASE)


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _positive(value) -> int | None:
    # a positive int, or None for anything else
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class ProcessWorker(QtCore.QObject):
    """Reconstructs one entry at a time: begins a session on the init frames,
    pushes it every chunk after them, and sends its result to the viewer --
    the whole result once, then one timepoint at a time, at most every
    update_interval_s and whenever a timepoint completes."""

    sigResult = QtCore.Signal(object)  # ProcessingResult: the whole of it
    sigTimepoint = QtCore.Signal(int, object)  # (timepoint, plane)
    sigTimepointDone = QtCore.Signal(int)  # a timepoint is complete

    def __init__(
        self,
        logger,
        reconstructor: StreamingReconstructor,
        update_interval_s: float = 0.2,
    ):
        super().__init__()
        self._logger = logger
        self._reconstructor = reconstructor
        self._update_interval_s = update_interval_s
        self._raw_data_info: RawInfo | None = None
        self._contract: Contract | None = None
        self._raw_data_buffer: RawDataBuffer | None = None
        self._params: dict | None = None
        self._session: StreamingSession | None = None
        self._begun = 0  # frames begin() consumed: pushed from there on
        self._shown = False  # the viewer has the whole result
        self._last_update = 0.0

    def start(
        self,
        raw_data_info: RawInfo,
        contract: Contract,
        raw_data_buffer: RawDataBuffer,
    ):
        # a new entry: resolve the parameters the init frames answer, then
        # begin a fresh session on them
        self._raw_data_info = raw_data_info
        self._contract = contract
        self._raw_data_buffer = raw_data_buffer
        if self._session is not None:
            self._session.close()
            self._session = None

        self._begun = max(contract.init_frames, 1)
        init_frames = raw_data_buffer.frames(0, self._begun)
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

    def process(self, start: int, end: int):
        # begin() consumed the frames before self._begun, so only what
        # follows them is pushed
        if self._session is None:
            return
        start = max(start, self._begun)
        if start < end:
            try:
                self._session.push(self._raw_data_buffer.frames(start, end), start, end)
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

    def finish(self):
        # the recording has finished, and every chunk has been pushed
        if self._session is None:
            return
        self.sigResult.emit(self._session.finish())
        self._logger.info(f"Reconstructed {self._raw_data_info.name}")

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
