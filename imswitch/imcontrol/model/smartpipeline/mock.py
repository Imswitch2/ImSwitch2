"""Mock ports: frames from an array, steps from functions, actions that log.

For the engine's tests and for an offline run of a pipeline against a
recording with the hardware stubbed. :data:`MOCK_CATALOG` is the v1
catalogue (:mod:`.catalog`: ``move-to``, ``snap``, ``log``), the same specs
the live backend performs for real.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any, Callable

import numpy as np

from imswitch.imcommon.model.smartpipeline import Frame, FrameIdentity, points_of
from imswitch.imcommon.model.workflow.steps import DEFAULT_PORT, Process
from imswitch.imcontrol.model.smartpipeline.catalog import axis_targets, catalog
from imswitch.imcontrol.model.smartpipeline.ports import ActionRequest, SimpleCompletion, StepError

MOCK_CATALOG = catalog()


class ArrayFrameSource:
    """Frames from a ``(T, Y, X)`` array (or a list of 2-D arrays), one per
    index, ``interval_s`` apart on the source clock."""

    def __init__(self, stack, *, detector: str = "Camera", interval_s: float = 1.0, identity: FrameIdentity | None = None,
                 scale=(), unit: str = "px", start_sequence: int = 0):
        self._stack = [np.asarray(plane) for plane in stack]
        self.detector = str(detector)
        self.interval_s = float(interval_s)
        self.identity = identity
        self.scale = tuple(scale)
        self.unit = unit
        self.start_sequence = int(start_sequence)
        self._stopped = threading.Event()
        self._closed = False
        self.yielded = 0

    def __len__(self) -> int:
        return len(self._stack)

    def frames(self) -> Iterator[Frame]:
        if self._closed:
            raise RuntimeError("the frame source is closed")
        for index, plane in enumerate(self._stack):
            if self._stopped.is_set():
                return
            self.yielded += 1
            yield Frame(image=plane, detector=self.detector, sequence=self.start_sequence + index,
                        t=index * self.interval_s, identity=self.identity, scale=self.scale, unit=self.unit,
                        frame_index=index)

    def stop(self) -> None:
        self._stopped.set()

    def close(self) -> None:
        self._stopped.set()
        self._closed = True


class FunctionStepRunner:
    """Processing steps as plain functions, by processor id:
    ``f(inputs, params, state) -> value`` (the ``out`` port) or
    ``-> {port: value}``. The source port carries the frame's array."""

    def __init__(self, functions: dict[str, Callable] | None = None):
        self.functions = dict(functions or {})
        self.calls: list[tuple[str, dict]] = []

    def image_result(self, frame: Frame):
        return frame.image

    def run_step(self, step, inputs: list, *, state: dict, frame: Frame | None = None) -> dict[str, Any]:
        if not isinstance(step, Process):
            raise StepError(f"{step.id}: a {getattr(step, 'kind', type(step).__name__)} step cannot run here")
        function = self.functions.get(step.processor)
        if function is None:
            raise StepError(f"{step.id}: no function for processor {step.processor!r}")
        self.calls.append((step.id, dict(step.params)))
        try:
            produced = function(list(inputs), dict(step.params), state)
        except Exception as exc:
            raise StepError(f"{step.id}: {step.processor!r} failed: {exc}") from exc
        if isinstance(produced, dict) and all(isinstance(k, str) for k in produced):
            return produced
        return {DEFAULT_PORT: produced}


class MockActions:
    """An action backend that records what it is asked and completes at
    once (``auto_complete``), or leaves the completion to the test
    (``pending`` holds it; settle it with ``complete`` / ``fail`` /
    ``cancel``). ``latency_s`` completes on a timer instead, so a run can
    be busy for a while. ``fail_on`` names actions that fail when started."""

    catalog = MOCK_CATALOG

    def __init__(self, *, auto_complete: bool = True, latency_s: float = 0.0, fail_on=(), refuse_on=()):
        self.auto_complete = auto_complete
        self.latency_s = float(latency_s)
        self.fail_on = set(fail_on)
        self.refuse_on = set(refuse_on)
        self.requests: list[ActionRequest] = []
        self.pending: list[SimpleCompletion] = []
        self.position: dict[str, dict[str, float]] = {}   # positioner -> axis -> value
        self.snaps: list[str] = []

    def start(self, request: ActionRequest) -> SimpleCompletion:
        self.requests.append(request)
        if request.action in self.refuse_on:
            raise RuntimeError(f"{request.action}: refused by the backend")
        completion = SimpleCompletion(request.epoch)
        result = self._perform(request)
        if request.action in self.fail_on:
            settle = lambda: completion.fail(f"{request.action}: failed in the mock")  # noqa: E731
        else:
            settle = lambda: completion.complete(result)  # noqa: E731
        if self.auto_complete and self.latency_s <= 0:
            settle()
        elif self.auto_complete:
            threading.Timer(self.latency_s, settle).start()
        else:
            self.pending.append(completion)
        return completion

    def _perform(self, request: ActionRequest) -> dict:
        if request.action == "move-to":
            coordinates = points_of(request.inputs[0])
            if coordinates.shape[0] == 0:
                return {"moved": False, "reason": "no points"}
            point = [float(v) for v in coordinates[0]]
            targets = axis_targets(request.params)
            moved: dict[str, dict[str, float]] = {}
            for (positioner, axis), value in zip(targets, point):
                axes = self.position.setdefault(positioner, {})
                if request.params.get("relative"):
                    value = axes.get(axis, 0.0) + value
                axes[axis] = value
                moved.setdefault(positioner, {})[axis] = value
            return {"moved": True, "relative": bool(request.params.get("relative")), "position": moved}
        if request.action == "snap":
            self.snaps.append(str(request.params.get("detector") or ""))
            return {"snapped": True, "detector": request.params.get("detector")}
        return {"logged": True}

    def settle_all(self) -> None:
        for completion in list(self.pending):
            completion.complete({"settled": True})
        self.pending.clear()


def sleep_clock(start: float = 0.0):
    """A clock for tests: ``clock()`` reads it, ``clock.advance(s)`` moves it."""

    class _Clock:
        def __init__(self):
            self.now = float(start)

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += float(seconds)

    return _Clock()


__all__ = ["MOCK_CATALOG", "ArrayFrameSource", "FunctionStepRunner", "MockActions", "sleep_clock"]


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
