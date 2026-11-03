"""ImProcess's side of a smart pipeline's ports.

The pipeline engine (ImControl) runs a pipeline tick by tick against two
duck-typed ports: a *frame source* that hands it one
:class:`~imswitch.imcommon.model.smartpipeline.values.Frame` at a time, and a
*step runner* that turns a frame into a result and runs one processing
step over results. This module is ImProcess's implementation of both --
:class:`ProcessorStepRunner` runs a ``Process`` step exactly the way the
workflow runner does (same registry, same ``run_processor``, same port and
state semantics), :class:`RecordingFrameSource` replays a recording as
frames -- so a pipeline tested against a file runs unchanged against a
detector. Nothing here imports ImControl: the contracts are the method
names, and the engine never sees a result class.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from imswitch.imcommon.model.smartpipeline.values import Frame
from imswitch.imcommon.model.workflow.output_spec import call_output_spec
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.result import ProcessingResult, result_kind
from imswitch.improcess.workflows.runner import DEFAULT_PORT, _with_state
from imswitch.improcess.workflows.sources import (
    SourceSpec,
    close_source,
    open_source,
    parse_binding,
)
from imswitch.improcess.workflows.steps import Process

#: Step kinds a workflow has that a pipeline tick cannot run: they read a
#: recording, merge runs or write files, none of which happens per frame.
_UNSUPPORTED_KINDS = ("source", "reconstruct", "consolidate", "save")

#: Axis labels of a frame's image, by dimensionality.
_FRAME_AXES = {2: ["Y", "X"], 3: ["Z", "Y", "X"]}


class StepError(RuntimeError):
    """A step could not run on this tick; the message says which and why."""


# --------------------------------------------------------------------------
# frame -> result
# --------------------------------------------------------------------------

def frame_to_result(frame) -> ProcessingResult:
    """An image result for one :class:`Frame`, as ImProcess's processors take it.

    ``frame.image`` is a (Y, X) plane or a (Z, Y, X) stack; the result is an
    :class:`~imswitch.improcess.model.array_result.ArrayProcessingResult`
    named ``"<detector> #<sequence>"`` with the frame's per-axis ``scale``
    (when given) and ``unit`` as its calibration. Where the frame came from
    travels in the metadata: ``frame_index`` at the top level, so a threshold
    and a regionprops step write it on the points they measure, and the whole
    envelope -- detector, sequence, clock, exposure, identity -- under
    ``"frame"``.
    """
    image = np.asarray(frame.image)
    if image.ndim not in _FRAME_AXES:
        raise StepError(
            f"frame #{frame.sequence} of {frame.detector!r}: the image must be (Y, X) or (Z, Y, X), "
            f"got shape {image.shape}"
        )
    axis_labels = list(_FRAME_AXES[image.ndim])
    scales = _axis_scales_of(frame, image.ndim)
    unit = str(getattr(frame, "unit", "") or "px")
    identity = getattr(frame, "identity", None)
    envelope = {
        "detector": str(frame.detector),
        "sequence": int(frame.sequence),
        "t": float(frame.t),
        "frame_index": None if frame.frame_index is None else int(frame.frame_index),
        "exposure_s": None if frame.exposure_s is None else float(frame.exposure_s),
        "identity": identity.to_dict() if identity is not None else None,
    }
    metadata: dict[str, Any] = {"frame": envelope}
    if frame.frame_index is not None:
        metadata["frame_index"] = int(frame.frame_index)
    if getattr(frame, "metadata", None):
        metadata["frame"]["metadata"] = dict(frame.metadata)
    return ArrayProcessingResult(
        f"{frame.detector} #{frame.sequence}",
        image,
        axis_labels,
        display_levels=finite_range(image),
        axis_scales=scales,
        scale_unit=unit,
        metadata=metadata,
    )


def _axis_scales_of(frame, ndim: int) -> list[float]:
    """The per-axis pixel size of a frame's image: ``frame.scale`` on the
    trailing axes when given, 1.0 elsewhere."""
    scales = [1.0] * ndim
    given = [float(value) for value in (getattr(frame, "scale", None) or ())]
    if given:
        given = given[-ndim:]
        scales[ndim - len(given):] = given
    return scales


# --------------------------------------------------------------------------
# the step runner port
# --------------------------------------------------------------------------

class ProcessorStepRunner:
    """Runs one ``Process`` step per tick with ImProcess's processors.

    What the engine hands it is the step as the pipeline document holds it
    (:class:`~imswitch.imcommon.model.workflow.steps.Process`, the Python
    step included) and the results on its input ports; what it gets back is
    ``{port: result}``, the way a workflow run fills its report for that
    step. The calls are the workflow runner's own, so a step behaves on a
    tick exactly as it does in a headless run: defaults under the step's
    params, inputs the processor must accept, one run per input for a
    single-input processor over several inputs (ports suffixed with the
    input's index), output ports checked against the processor's declared
    spec. ``state`` is the dict the processor finds under ``params["_state"]``
    -- the very dict, so what it stores is what it finds next tick.
    """

    def __init__(self, registry, *, logger=None):
        self._registry = registry
        self._logger = logger or _log()

    def image_result(self, frame) -> ProcessingResult:
        """The image result of ``frame`` (:func:`frame_to_result`)."""
        return frame_to_result(frame)

    def run_step(self, step, inputs: list, *, state: dict, frame=None) -> dict[str, Any]:
        """``{port: result}`` of running ``step`` over ``inputs``.

        Refuses, with :class:`StepError`, a step that is not a ``process``
        step, a processor the registry does not have, an input the processor
        does not accept, and a run that failed (the processor's own exception
        is the cause) or produced a port outside its declared spec.
        """
        where = "" if frame is None else f" (frame #{getattr(frame, 'sequence', '?')})"
        kind = str(getattr(step, "kind", "") or type(step).__name__.lower())
        step_id = getattr(step, "id", "?")
        if kind in _UNSUPPORTED_KINDS:
            raise StepError(f"{step_id}: a {kind!r} step cannot run in a pipeline, only per-frame processing can")
        if not isinstance(step, Process) or kind != "process":
            raise StepError(f"{step_id}: a {kind!r} step is not a process step; a pipeline tick runs processors only")
        try:
            plugin = self._registry.get_processor(step.processor)
        except Exception as exc:  # noqa: BLE001 - the registry's own wording is the message
            raise StepError(f"{step_id}: no processor {step.processor!r}: {exc}") from exc
        inputs = list(inputs or [])
        params = {**type(plugin).default_params(), **step.params}
        if step.restriction:
            from imswitch.improcess.analysis.roi_restriction import ROI_PARAM, ROIRestriction

            if not getattr(plugin, "accepts_roi", False):
                raise StepError(f"{step_id}: {step.processor!r} cannot be restricted to ROIs")
            if step.restriction.get("by_reference"):
                raise StepError(
                    f"{step_id}: the ROI restriction was recorded by reference only "
                    "(its geometry was too large to store); it cannot be run"
                )
            params[ROI_PARAM] = ROIRestriction.from_provenance(step.restriction)
        for item in inputs:
            if not plugin.accepts(item):
                shape = tuple(getattr(getattr(item, "data", None), "shape", ()) or ())
                raise StepError(
                    f"{step_id}: {step.processor!r} does not accept result {getattr(item, 'name', item)!r}{where} "
                    f"(kind {getattr(item, 'kind', 'image')!r}, axes "
                    f"{list(getattr(item, 'axis_labels', []) or [])}, shape {shape})"
                )
        from imswitch.improcess.processors.run import run_processor

        spec = call_output_spec(plugin, params, tuple(result_kind(item) for item in inputs))
        fan_out = len(inputs) > 1 and getattr(plugin, "max_inputs", 1) == 1
        groups = [[item] for item in inputs] if fan_out else [inputs]
        runnable = plugin if state is None else _with_state(plugin, state)
        produced: dict[str, Any] = {}
        for input_index, group in enumerate(groups):
            results, failures = run_processor(runnable, group, params, self._logger)
            if failures:
                who, why = failures[0]
                message = f"{step_id}: {step.processor!r} failed on {getattr(who, 'name', who)!r}{where}: {why}"
                cause = getattr(why, "exception", None)
                if cause is not None:
                    raise StepError(message) from cause
                raise StepError(message)
            if not results:
                raise StepError(f"{step_id}: {step.processor!r} produced no results{where}")
            for result in results:
                port = _port_of(result)
                if not spec.matches(port):
                    raise StepError(
                        f"{step_id}: {step.processor!r} produced port {port!r}, outside its declared {spec.describe()}"
                    )
                if fan_out and input_index > 0:
                    port = f"{port}{input_index}"
                if port in produced:
                    raise StepError(f"{step_id}: {step.processor!r} produced port {port!r} twice")
                produced[port] = result
        return produced


def _port_of(result) -> str:
    """The output port a result was produced on, from its provenance."""
    from imswitch.improcess.model.provenance import graph_of, output_of

    graph = graph_of(result)
    return output_of(graph)[1] if graph else DEFAULT_PORT


# --------------------------------------------------------------------------
# the frame source port
# --------------------------------------------------------------------------

class RecordingFrameSource:
    """A recording replayed as the frames a detector would hand the engine.

    Opened lazily with :func:`~imswitch.improcess.workflows.sources.open_source`
    (a :class:`SourceSpec`, or a path, ``"path::dataset"`` included). The
    planes along the leading non-spatial axis are the frames: every index of
    the first axis of a ``TYX`` / ``ZYX`` stack, the one plane of a 2-D
    image, in order. Each :class:`Frame` is numbered by its plane
    (``sequence`` and ``frame_index``), clocked at ``index * interval_s``,
    and calibrated from the recording's pixel size when it has one
    (``scale`` per Y, X axis in ``um``) and in pixels otherwise.

    :meth:`stop` ends the iteration before the next frame, from any thread;
    :meth:`close` releases the file and is final.
    """

    def __init__(self, spec_or_path, *, detector: str = "recording", interval_s: float = 0.0,
                 source_root=None, dataset=None):
        if isinstance(spec_or_path, SourceSpec):
            spec = spec_or_path
        else:
            spec = parse_binding(str(spec_or_path))
        if dataset is not None:
            spec = spec.with_path(spec.path, str(dataset))
        self._spec = spec
        self._source_root = source_root
        self.detector = str(detector)
        self.interval_s = float(interval_s)
        self._data_obj = None
        self._closed = False
        self._stop = threading.Event()

    # -- opening -----------------------------------------------------------

    @property
    def spec(self) -> SourceSpec:
        return self._spec

    @property
    def path(self) -> Path:
        return Path(self._spec.resolved(self._source_root).path)

    def open(self):
        """The recording's ``DataObj``, opened on first use."""
        if self._closed:
            raise RuntimeError(f"{self.path.name}: the recording source is closed")
        if self._data_obj is None:
            self._data_obj = open_source(self._spec, source_root=self._source_root)
        return self._data_obj

    def _handle(self):
        """The array to read planes from: the lazy handle when the source
        has one, else the loaded array."""
        data_obj = self.open()
        handle = getattr(data_obj, "data_handle", None)
        if handle is None or not hasattr(handle, "shape"):
            handle = np.asarray(data_obj.data)
        return handle

    @property
    def frame_count(self) -> int | None:
        """How many frames the recording has, ``None`` when it is not an image."""
        shape = tuple(getattr(self._handle(), "shape", ()) or ())
        if len(shape) < 2:
            return None
        return 1 if len(shape) == 2 else int(shape[0])

    def __len__(self) -> int:
        count = self.frame_count
        if count is None:
            raise TypeError(f"{self.path.name}: the recording has no frames")
        return count

    def _calibration(self) -> tuple[tuple[float, ...], str]:
        """``(scale, unit)`` of a frame's (Y, X) axes: the recording's pixel
        size in micrometres when it records one, pixels otherwise."""
        data_obj = self.open()
        scales = list(getattr(data_obj, "axis_scales", None) or [])
        unit = str(getattr(data_obj, "scale_unit", "") or "px")
        if len(scales) < 2 or unit == "px":
            return (), "px"
        return (float(scales[-2]), float(scales[-1])), unit

    # -- iterating ---------------------------------------------------------

    def frames(self):
        """Yield the recording's frames in order, until the end or :meth:`stop`."""
        if self._closed:
            raise RuntimeError(f"{self.path.name}: the recording source is closed")
        handle = self._handle()
        shape = tuple(handle.shape)
        if len(shape) < 2:
            raise RuntimeError(f"{self.path.name}: a recording of shape {shape} has no (Y, X) planes")
        scale, unit = self._calibration()
        count = 1 if len(shape) == 2 else int(shape[0])
        for index in range(count):
            if self._stop.is_set() or self._closed:
                return
            plane = np.asarray(handle if len(shape) == 2 else handle[index])
            yield Frame(
                image=plane,
                detector=self.detector,
                sequence=index,
                t=index * self.interval_s,
                frame_index=index,
                scale=scale,
                unit=unit,
            )

    def __iter__(self):
        return self.frames()

    def stop(self) -> None:
        """End the iteration before its next frame; safe from any thread."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- closing -----------------------------------------------------------

    def close(self) -> None:
        """Release the recording. Idempotent; the source cannot be reopened."""
        self._stop.set()
        if self._closed:
            return
        self._closed = True
        data_obj, self._data_obj = self._data_obj, None
        if data_obj is not None:
            close_source(data_obj)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _log():
    from imswitch.imcommon.model import initLogger

    return initLogger("ImProcessSmartPipeline", tryInheritParent=False)


__all__ = ["ProcessorStepRunner", "RecordingFrameSource", "StepError", "frame_to_result"]


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
