"""Mock variant of :class:`MicroscopeFacade` for headless workflow tests.

Every method call is recorded in ``calls`` (a list of ``(name, args, kwargs)``
tuples) so tests can assert on the exact interaction sequence. Camera
``get_data()`` returns a configurable canned ndarray.

Usage::

    facade = build_mock_facade()
    facade.cam.set_canned_data(np.zeros((10, 64, 64), dtype=np.uint16))
    wf = SomeWorkflow(facade, params)
    wf.run()
    assert ("laser_con.set_constant_power", (["488"], [50.0]), {}) in facade.calls
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import numpy as np

from imswitch.imcontrol.model.workflows.facade import (
    MicroscopeFacade,
    RotatorPresets,
)


CallRecord = Tuple[str, tuple, dict]


@dataclass
class _Recorder:
    """Shared call-recording target. One instance per mock facade."""

    calls: List[CallRecord] = field(default_factory=list)

    def record(self, name: str, args: tuple = (), kwargs: Optional[dict] = None) -> None:
        self.calls.append((name, args, dict(kwargs or {})))


class _MockLaserCon:
    def __init__(self, recorder: _Recorder) -> None:
        self._r = recorder

    def laser_off(self, names):
        self._r.record("laser_con.laser_off", (list(names),))

    def laser_on(self, names):
        self._r.record("laser_con.laser_on", (list(names),))

    def set_constant_power(self, names, powers):
        self._r.record("laser_con.set_constant_power", (list(names), list(powers)))

    def set_triggered_mode(self, names, powers):
        self._r.record("laser_con.set_triggered_mode", (list(names), list(powers)))

    def set_modulation_mode(self, names):
        self._r.record(
            "laser_con.set_modulation_mode",
            (None if names is None else list(names),),
        )


class _MockCam:
    def __init__(self, recorder: _Recorder) -> None:
        self._r = recorder
        self._canned: Optional[np.ndarray] = None
        self.expo: float = 50000.0

    def set_canned_data(self, arr: np.ndarray) -> None:
        """Set the array returned by :meth:`get_data` / :meth:`wait_for_frame`."""
        self._canned = arr

    def prepare_acquisition(self, n_frames: int) -> None:
        self._r.record("cam.prepare_acquisition", (int(n_frames),))

    def start_acquisition(self) -> None:
        self._r.record("cam.start_acquisition")

    def stop_acquisition(self) -> None:
        self._r.record("cam.stop_acquisition")

    def prepare_live(self) -> None:
        self._r.record("cam.prepare_live")

    def start_live(self) -> None:
        self._r.record("cam.start_live")

    def stop_live(self) -> None:
        self._r.record("cam.stop_live")

    def get_data(self):
        self._r.record("cam.get_data")
        return self._canned

    def wait_for_frame(self, timeout_s: float = 2.0) -> bool:
        self._r.record("cam.wait_for_frame", (float(timeout_s),))
        return self._canned is not None and self._canned.size > 0


class _MockTrig:
    def __init__(self, recorder: _Recorder) -> None:
        self._r = recorder
        self.connected = True

    def snap_trigger(self, laser_pin: int, camera_pin: int, exposure_us: int) -> None:
        self._r.record(
            "trig.snap_trigger",
            (),
            {"laser_pin": laser_pin, "camera_pin": camera_pin, "exposure_us": exposure_us},
        )

    def command(self, *args, **kwargs):
        self._r.record("trig.command", args, kwargs)
        n = 4
        return tuple(np.zeros(n) for _ in range(4))

    def Sendsignal(self, *args, **kwargs):
        self._r.record("trig.Sendsignal", args, kwargs)


class _MockStageCon:
    def __init__(self, recorder: _Recorder) -> None:
        self._r = recorder
        self._pos = (0.0, 0.0)

    def move_to(self, x: float, y: float) -> None:
        self._r.record("stage_con.move_to", (float(x), float(y)))
        self._pos = (float(x), float(y))

    def get_position(self) -> tuple:
        self._r.record("stage_con.get_position")
        return self._pos

    def jog_start(self, axis: str, sign: int) -> None:
        self._r.record("stage_con.jog_start", (axis, int(sign)))

    def jog_stop(self, axis: str) -> None:
        self._r.record("stage_con.jog_stop", (axis,))


class _MockZStageCon:
    def __init__(self, recorder: _Recorder, pos_range_um: tuple = (0.0, 100.0)) -> None:
        self._r = recorder
        self._pos = 50.0
        self.pos_range_um = pos_range_um

    def read_pos_um(self) -> float:
        self._r.record("z_stage_con.read_pos_um")
        return self._pos

    def set_pos_um(self, value_um: float) -> None:
        self._r.record("z_stage_con.set_pos_um", (float(value_um),))
        self._pos = float(value_um)

    def activate_ext_control(self) -> None:
        self._r.record("z_stage_con.activate_ext_control")

    def deactivate_ext_control(self) -> None:
        self._r.record("z_stage_con.deactivate_ext_control")


class _MockRotator:
    def __init__(self, recorder: _Recorder, label: str, presets: Optional[RotatorPresets] = None) -> None:
        self._r = recorder
        self._label = label
        self._presets = presets or RotatorPresets(h_deg=0.0, v_deg=90.0)
        self._pos = 0.0

    @property
    def presets(self) -> RotatorPresets:
        return self._presets

    def move_abs(self, deg: float) -> None:
        self._r.record(f"{self._label}.move_abs", (float(deg),))
        self._pos = float(deg)

    def move_rel(self, deg: float) -> None:
        self._r.record(f"{self._label}.move_rel", (float(deg),))
        self._pos += float(deg)

    def position(self) -> float:
        return self._pos

    def move_to_h(self) -> None:
        self._r.record(f"{self._label}.move_to_h")
        self._pos = self._presets.h_deg

    def move_to_v(self) -> None:
        self._r.record(f"{self._label}.move_to_v")
        self._pos = self._presets.v_deg

    def chained_move_to_h(self, follow_up_callable) -> None:
        self.move_to_h()
        if follow_up_callable is not None:
            follow_up_callable()

    def chained_move_to_v(self, follow_up_callable) -> None:
        self.move_to_v()
        if follow_up_callable is not None:
            follow_up_callable()


class MockMicroscopeFacade(MicroscopeFacade):
    """A :class:`MicroscopeFacade` with mock sub-facades and a shared call log.

    Attributes:
        calls: Flat list of every recorded ``(name, args, kwargs)``.
    """

    def __init__(self) -> None:
        recorder = _Recorder()
        super().__init__(
            laser_con=_MockLaserCon(recorder),         # type: ignore[arg-type]
            cam=_MockCam(recorder),                    # type: ignore[arg-type]
            trig=_MockTrig(recorder),                  # type: ignore[arg-type]
            stage_con=_MockStageCon(recorder),         # type: ignore[arg-type]
            z_stage_con=_MockZStageCon(recorder),      # type: ignore[arg-type]
            rotator_hwp=_MockRotator(recorder, "rotator_hwp"),  # type: ignore[arg-type]
            rotator_qwp=_MockRotator(recorder, "rotator_qwp"),  # type: ignore[arg-type]
        )
        self._recorder = recorder

    @property
    def calls(self) -> List[CallRecord]:
        return self._recorder.calls

    def call_names(self) -> List[str]:
        """Just the method names, for quick ordering assertions."""
        return [c[0] for c in self._recorder.calls]


def build_mock_facade() -> MockMicroscopeFacade:
    """Convenience constructor — returns a fresh :class:`MockMicroscopeFacade`."""
    return MockMicroscopeFacade()


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
