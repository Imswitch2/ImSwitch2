"""View-only live streaming session: raw frames, arranged, nothing else."""

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.result import ViewMode
from imswitch.improcess.reconstructors.base import StreamingSession, StreamPlan

from .reconstructor import ViewOnlyResult


#: Axis names for the output buffer. ``"T"`` is not cosmetic: the viewer finds
#: the timepoint axis by name -- ``ReconstructionViewController`` writes each
#: incremental plane at ``axis_labels.index("T")`` and advances the slider by
#: looking for it -- so renaming it silently disables live updates.
_AXIS_LABELS = ["T", "Z", "Y", "X"]


def _meta(attrs: dict, key: str):
    """Read a recorder key from flat attrs or a nested ``ImswitchData`` block.

    ImSwitch2's storer flattens metadata to top-level keys; legacy ImSwitch-1
    Zarr nests the same keys. Prefer the flat one.
    """
    if key in attrs:
        return attrs[key]
    nested = attrs.get("ImswitchData")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return None


def _positive_int(value) -> int | None:
    """A positive int from a recorder attribute, or ``None`` if there is none.

    Metadata arrives as numpy scalars, one-element arrays, bytes or text, and
    older files write the literal string ``"null"`` for a value they do not
    have -- which has to read as absent rather than reach an array shape.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        flat = np.asarray(value).flatten()
        if flat.size != 1:
            return None
        value = flat[0]
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "null", "none", "nan", "n/a", "na"}:
            return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number) or number <= 0:
        return None
    return max(1, int(number))


class ViewOnlyLiveSession(StreamingSession):
    """Arrange incoming raw frames for viewing, without reconstructing them.

    The pass-through of the streaming contract. Every frame the source yields
    is written unchanged into ``(T, S, Y, X)`` -- timepoint, frame within that
    timepoint's stack, then the frame -- so what reaches the viewer is the raw
    data as recorded.

    That shape is what lets one session serve every recording the watcher can
    find: a camera timepoint is a single frame (``S == 1``) and a scan
    timepoint is its whole stack, and both index the same way. Nothing is
    reduced, fitted or reassigned, so unlike a reconstructing session there is
    no geometry to resolve and no parameters to honour.
    """

    def __init__(self):
        self._logger = initLogger("ViewOnlyLiveSession")
        self.name = ""
        self.data = None
        self.frames_per_stack = 1
        self.num_timepoints = 1
        # The timepoint being written; live_plane() hands this one to the
        # viewer. Frames arrive in order, so it only ever moves forward.
        self._current_timepoint = 0
        self._warned_out_of_range = False

    def begin(self, init_obj, params: dict) -> StreamPlan:
        """Allocate the output buffer from the first stack's metadata.

        The buffer keeps the **source dtype** rather than promoting to float32:
        a pass-through has nothing to gain from the extra precision, and raw
        frames are typically ``uint16``, so promoting would double a buffer
        that is already the whole recording.
        """
        info = init_obj.stack_info
        attrs = init_obj.attrs or {}
        first = np.asarray(init_obj.data)
        if first.ndim != 3:
            raise ValueError(
                f"Expected a (frames, Y, X) first chunk, got shape {first.shape}"
            )

        self.name = init_obj.name
        frame_shape = tuple(
            getattr(info, "frame_shape", None) or first.shape[-2:]
        )
        dtype = np.dtype(getattr(info, "dtype", None) or first.dtype)

        # The source has already resolved this from the recorder's metadata;
        # the first chunk's own length is the fallback for a store that says
        # nothing, where one stack is all there is.
        self.frames_per_stack = (
            _positive_int(getattr(info, "frames_per_stack", None))
            or _positive_int(_meta(attrs, "recording:frames_per_stack"))
            or max(1, int(first.shape[0]))
        )
        self.num_timepoints = self._resolve_num_timepoints(
            attrs, info, self.frames_per_stack
        )

        self.data = np.zeros(
            (self.num_timepoints, self.frames_per_stack, *frame_shape),
            dtype=dtype,
        )
        self._current_timepoint = 0
        self._warned_out_of_range = False

        self._logger.info(
            f"Allocated view-only buffer: shape {self.data.shape}, dtype {dtype}"
        )

        # begin() consumes the first stack: the stream worker only pushes what
        # follows it, so these frames would otherwise never be written. The
        # write is a plain indexed assignment, so re-pushing them is harmless.
        self.push(first, 0, int(first.shape[0]))

        return StreamPlan(
            out_shape=self.data.shape,
            axis_labels=list(_AXIS_LABELS),
            view_modes=[ViewMode("Standard", tuple(range(self.data.ndim)))],
            dtype=dtype,
            scale_unit="px",
        )

    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        """Copy raw frames into their place in the buffer.

        ``start`` / ``end`` are GLOBAL frame indices across the whole
        recording, so the timepoint is ``start // frames_per_stack`` and the
        offset within it is the remainder. The loop exists because the same
        method serves two callers: :meth:`begin` hands over a whole stack, and
        the process worker hands over one frame at a time.

        A timepoint the buffer has no room for is dropped rather than raised
        on -- a recording that runs longer than its metadata promised should
        not take the run down with it.
        """
        if self.data is None:
            raise RuntimeError("Session not initialized; call begin() first")

        frames = np.asarray(chunk)
        if frames.ndim == 2:
            frames = frames[np.newaxis]

        remaining = min(int(frames.shape[0]), int(end) - int(start))
        index = int(start)
        while remaining > 0:
            time_index = index // self.frames_per_stack
            local = index % self.frames_per_stack
            take = min(remaining, self.frames_per_stack - local)

            if 0 <= time_index < self.data.shape[0]:
                offset = index - int(start)
                self.data[time_index, local:local + take] = frames[
                    offset:offset + take
                ]
                self._current_timepoint = time_index
            elif not self._warned_out_of_range:
                self._warned_out_of_range = True
                self._logger.warning(
                    f"Timepoint {time_index} exceeds the allocated "
                    f"{self.data.shape[0]}; dropping those frames"
                )

            index += take
            remaining -= take

    def result(self) -> ViewOnlyResult:
        """A snapshot of everything written so far.

        The copy is the thread boundary: the session goes on writing into its
        own buffer, so handing out the array itself would let the viewer read
        memory being mutated on the process thread.
        """
        if self.data is None:
            raise RuntimeError("Session not initialized; call begin() first")

        return ViewOnlyResult(
            name=self.name,
            data=self.data.copy(),
            axis_labels=list(_AXIS_LABELS),
            view_modes=[ViewMode("Standard", tuple(range(self.data.ndim)))],
            display_levels=None,
            scale_unit="px",
            identity_kind="derived",
        )

    def live_plane(self) -> "tuple[int, np.ndarray] | None":
        """The in-flight timepoint's slice, copied, for an incremental redraw.

        Costs one timepoint instead of the whole growing buffer that
        :meth:`result` copies, so the per-refresh cost stays flat however long
        the recording runs.
        """
        if self.data is None:
            return None
        index = self._current_timepoint
        if not 0 <= index < self.data.shape[0]:
            return None
        return index, self.data[index:index + 1].copy()

    def close(self) -> None:
        """Drop the buffer; there are no device resources to free."""
        self.data = None

    @staticmethod
    def _resolve_num_timepoints(attrs: dict, info, frames_per_stack: int) -> int:
        """How many timepoints to allocate room for.

        The recorder's own count wins. Failing that, the expected frame count
        divided by the stack size says how many stacks are coming. Failing
        that it is a single timepoint -- the right answer for a snap, a
        non-lapse recording, and anything carrying no metadata at all.
        """
        for key in ("recording:num_timepoints", "Rec:LapseTime"):
            timepoints = _positive_int(_meta(attrs, key))
            if timepoints is not None:
                return timepoints

        expected = _positive_int(getattr(info, "expected_frames", None))
        if expected is not None and frames_per_stack:
            return max(1, int(np.ceil(expected / frames_per_stack)))
        return 1


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
