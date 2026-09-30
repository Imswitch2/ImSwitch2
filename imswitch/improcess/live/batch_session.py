"""Run a batch ``Reconstructor`` on a live stream, one complete stack at a time.

Most reconstructors are pure functions over a whole dataset
(``process(data_obj, params)``); only a few keep state and consume frames as
they arrive. The live runtime speaks the second contract. This adapter lets
it drive the first: it collects the frames of one logical stack, and when
the stack is whole it wraps it as an in-memory dataset and runs
``process()`` on it, so a batch reconstructor reconstructs "as soon as it is
done" -- at stack granularity -- through the same workers, buffer, cadence
and provenance as a streaming one.

Provenance is the worker's: it records one streaming node per session and
rewrites it with each snapshot's completion status. The adapter therefore
calls ``process()`` directly rather than :func:`run_reconstruction`, which
would add a second, disconnected reconstruct node to every result.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import (
    StreamInit,
    StreamPlan,
    StreamingSession,
)
from .sources import InMemoryStackWrapper


class StackBatchSession(StreamingSession):
    """A ``StreamingSession`` over a batch ``Reconstructor``.

    Frames are placed by their global index: index ``i`` belongs to stack
    ``i // frames_per_stack`` at position ``i % frames_per_stack``. A stack is
    reconstructed once every position holds a frame; a stack the stream moved
    past with holes in it (frames lost to an overflow) is dropped with a
    warning rather than reconstructed from garbage. :meth:`result` is the
    latest stack's result, the same object until the next stack completes,
    and ``None`` before the first one does.
    """

    def __init__(
        self,
        reconstructor,
        *,
        frames_per_stack: Optional[int] = None,
        process_partial_final_stack: bool = False,
    ) -> None:
        """
        Args:
            reconstructor: Any ``Reconstructor``; its ``process`` runs on the
                process thread once per complete stack.
            frames_per_stack: Overrides the stack size; by default it is the
                ``StackInfo``'s, else the size of the first stack.
            process_partial_final_stack: When the stream ends on an
                incomplete stack, reconstruct the frames it has (a
                contiguous run from the stack's start) instead of dropping
                it. Off by default: a reconstructor that needs the whole
                raster would refuse or misplace it.
        """
        self._reconstructor = reconstructor
        self._frames_per_stack = int(frames_per_stack) if frames_per_stack else None
        self._process_partial_final_stack = bool(process_partial_final_stack)
        self._logger = initLogger(self, tryInheritParent=False)

        self._buffer: Optional[np.ndarray] = None
        self._present: Optional[np.ndarray] = None
        self._frame_shape: tuple[int, ...] = ()
        self._stack_index = 0
        self._stacks_completed = 0
        self._stacks_dropped = 0
        self._latest: Optional[ProcessingResult] = None

        self._name = 'live'
        self._dataset_name = 'detector'
        self._attrs: dict[str, Any] = {}
        self._axis_labels: Optional[list[str]] = None
        self._params: dict[str, Any] = {}

    # -- introspection ----------------------------------------------------

    @property
    def reconstructor(self):
        return self._reconstructor

    @property
    def frames_per_stack(self) -> Optional[int]:
        return self._frames_per_stack

    @property
    def stacks_completed(self) -> int:
        """How many stacks have been reconstructed."""
        return self._stacks_completed

    @property
    def stacks_dropped(self) -> int:
        """How many stacks were abandoned with frames missing."""
        return self._stacks_dropped

    @property
    def current_stack_index(self) -> int:
        return self._stack_index

    @property
    def frames_in_current_stack(self) -> int:
        return int(self._present.sum()) if self._present is not None else 0

    # -- StreamingSession -------------------------------------------------

    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan:
        data = np.asarray(init_obj.data)
        if data.ndim < 2:
            raise ValueError(
                f'The first stack must be at least 2D (frames, ...); got {data.shape}'
            )
        if data.ndim == 2:
            data = data[np.newaxis]

        info = init_obj.stack_info
        fps = (
            self._frames_per_stack
            or int(getattr(info, 'frames_per_stack', None) or 0)
            or int(data.shape[0])
        )
        self._frames_per_stack = max(1, int(fps))
        self._frame_shape = tuple(int(v) for v in data.shape[1:])
        self._buffer = np.empty((self._frames_per_stack, *self._frame_shape), dtype=data.dtype)
        self._present = np.zeros(self._frames_per_stack, dtype=bool)
        self._stack_index = 0
        self._stacks_completed = 0
        self._stacks_dropped = 0
        self._latest = None

        self._name = init_obj.name or 'live'
        self._dataset_name = init_obj.dataset_name or 'detector'
        self._attrs = dict(init_obj.attrs or {})
        self._params = dict(params or {})
        layout = getattr(info, 'acquisition_layout', None)
        storage_axes = getattr(getattr(layout, 'layout', None), 'storage_axes', None)
        self._axis_labels = (
            [str(axis) for axis in storage_axes]
            if storage_axes and len(storage_axes) == data.ndim
            else None
        )

        # The first stack is what begin() is handed; it counts like any other.
        self.push(data, 0, int(data.shape[0]))

        if self._latest is not None and hasattr(self._latest, 'data'):
            out = np.asarray(self._latest.data)
            labels = list(getattr(self._latest, 'axis_labels', None) or _default_labels(out.ndim))
            return StreamPlan(
                out_shape=tuple(int(v) for v in out.shape),
                axis_labels=labels,
                view_modes=[ViewMode('Standard', tuple(range(out.ndim)))],
                dtype=np.dtype(out.dtype),
                scale_unit=str(getattr(self._latest, 'scale_unit', 'px') or 'px'),
                axis_scales=list(getattr(self._latest, 'axis_scales', None) or []) or None,
            )
        out_shape = (self._frames_per_stack, *self._frame_shape)
        return StreamPlan(
            out_shape=out_shape,
            axis_labels=_default_labels(len(out_shape)),
            view_modes=[ViewMode('Standard', tuple(range(len(out_shape))))],
            dtype=np.dtype(data.dtype),
        )

    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        if self._buffer is None or self._present is None:
            raise RuntimeError('Session not initialized; call begin() first')
        data = np.asarray(chunk)
        if data.ndim == len(self._frame_shape):
            data = data[np.newaxis]
        count = int(data.shape[0])
        if count != int(end) - int(start):
            raise ValueError(
                f'chunk holds {count} frames but covers [{start}:{end})'
            )
        fps = self._frames_per_stack
        for offset in range(count):
            index = int(start) + offset
            stack_index, position = divmod(index, fps)
            if stack_index != self._stack_index:
                self._abandon_current_stack(stack_index)
            self._buffer[position] = data[offset]
            self._present[position] = True
            if self._present.all():
                self._run_stack(self._buffer)
                self._advance_to(self._stack_index + 1)

    def result(self) -> Optional[ProcessingResult]:
        """The latest completed stack's result; ``None`` before the first."""
        return self._latest

    def finish(self) -> Optional[ProcessingResult]:
        """Reconstruct a trailing partial stack if allowed, then return the latest result."""
        if self._present is not None and self._present.any() and not self._present.all():
            present = int(self._present.sum())
            contiguous = bool(self._present[:present].all())
            if self._process_partial_final_stack and contiguous:
                self._logger.info(
                    f'Reconstructing the final stack from its first {present} of '
                    f'{self._frames_per_stack} frames'
                )
                self._run_stack(self._buffer[:present])
                self._advance_to(self._stack_index + 1)
            else:
                self._stacks_dropped += 1
                self._logger.warning(
                    f'Stream ended with stack {self._stack_index} holding {present} of '
                    f'{self._frames_per_stack} frames; it is not reconstructed'
                )
                self._present[:] = False
        return self._latest

    def close(self) -> None:
        self._buffer = None
        self._present = None

    # -- internals --------------------------------------------------------

    def _abandon_current_stack(self, next_index: int) -> None:
        if self._present is not None and self._present.any():
            self._stacks_dropped += 1
            self._logger.warning(
                f'Stack {self._stack_index} was left with '
                f'{int(self._present.sum())} of {self._frames_per_stack} frames '
                f'when stack {next_index} began; it is not reconstructed'
            )
        self._advance_to(next_index)

    def _advance_to(self, stack_index: int) -> None:
        self._stack_index = int(stack_index)
        if self._present is not None:
            self._present[:] = False

    def _run_stack(self, frames: np.ndarray) -> None:
        attrs = dict(self._attrs)
        attrs['recording:lapse_index'] = int(self._stack_index)
        # The buffer is reused for the next stack; a pass-through result would
        # otherwise show the next stack's frames under this stack's name.
        wrapper = InMemoryStackWrapper(
            self._name,
            self._dataset_name,
            np.array(frames, copy=True),
            attrs,
            axis_labels=self._axis_labels,
        )
        result = self._reconstructor.process(wrapper, dict(self._params))
        if result is None:
            raise RuntimeError(
                f'{type(self._reconstructor).__name__}.process() returned no result '
                f'for stack {self._stack_index}'
            )
        self._latest = result
        self._stacks_completed += 1


def _default_labels(ndim: int) -> list[str]:
    if ndim >= 3:
        return [*['C'] * (ndim - 3), 'T', 'Y', 'X'][-ndim:]
    return ['Y', 'X'][-ndim:]


__all__ = ['StackBatchSession']
