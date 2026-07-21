"""Contracts the real IC4 path depends on, exercised without the vendor SDK.

The IC4Camera class cannot run here — imagingcontrol4 ships no macOS wheel and
needs a GenTL producer besides. These tests pin the logic that does not need the
SDK, using fakes shaped like the parts of the API that were verified against the
vendor's own sources.
"""

import numpy as np
import pytest

from imswitch_device_tis._frame_queue import FrameQueue
from imswitch_device_tis._ic4_driver import (
    dtype_for_pixel_format, ensure_library_initialized,
)


class _FakeLibrary:
    """Mimics ic4.Library: init() raises RuntimeError if already called."""

    def __init__(self):
        self.calls = 0

    def init(self):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("Library.init was already called")


class _FakeIC4:
    def __init__(self):
        self.Library = _FakeLibrary()


def test_second_camera_does_not_fail_on_already_initialized_library():
    """ic4.Library.init() is not idempotent and has no public predicate.

    A setup with two TIS cameras constructs the driver twice in one process.
    Unguarded, the second raised RuntimeError — which the manager catches as its
    mock fallback, so the second camera came up silently showing synthetic
    frames instead of failing loudly.
    """
    ic4 = _FakeIC4()

    ensure_library_initialized(ic4)
    ensure_library_initialized(ic4)  # must not raise

    assert ic4.Library.calls == 2


def test_genuine_library_failures_still_propagate():
    """Only the already-initialized case is swallowed; a missing native library
    raises FileNotFoundError and must not be mistaken for it."""
    class Failing:
        Library = type('L', (), {
            'init': staticmethod(
                lambda: (_ for _ in ()).throw(FileNotFoundError("libic4core.so"))
            )
        })

    with pytest.raises(FileNotFoundError):
        ensure_library_initialized(Failing)


@pytest.mark.parametrize('pixel_format,expected', [
    ('Mono8', np.uint8),
    ('Mono10', np.uint16),
    ('Mono12p', np.uint16),
    ('Mono16', np.uint16),
    ('BayerRG8', np.uint8),
    (None, np.uint16),      # unknown -> the safe direction, never truncates
    ('NotAFormat', np.uint16),
])
def test_dtype_for_pixel_format(pixel_format, expected):
    assert dtype_for_pixel_format(pixel_format) == expected


class _FakeIntProperty:
    def __init__(self, minimum, maximum, increment):
        self.minimum, self.maximum, self.increment = minimum, maximum, increment


class _FakePropertyMap:
    """Only the surface _fit_to_property touches."""

    def __init__(self, props):
        self._props = props

    def find_integer(self, prop_id):
        return self._props[prop_id]


def _fit(value, minimum=0, maximum=2448, increment=4):
    """Call IC4Camera._fit_to_property unbound, against a fake property map."""
    from imswitch_device_tis._ic4_driver import IC4Camera

    camera = IC4Camera.__new__(IC4Camera)   # no SDK, no device
    camera._pm = _FakePropertyMap({'W': _FakeIntProperty(minimum, maximum, increment)})
    return IC4Camera._fit_to_property(camera, 'W', value)


def test_roi_value_is_rounded_down_to_the_sensor_increment():
    """Sensors constrain Width/Height to an increment (commonly 4 or 8 px) and
    IC4 raises on a violation. ImSwitch's ROI selector produces arbitrary pixel
    counts, so an unaligned width would throw out of the GUI's crop path."""
    assert _fit(513) == 512
    assert _fit(512) == 512


def test_roi_value_is_clamped_to_the_sensor_bounds():
    assert _fit(99999) == 2448
    assert _fit(-5) == 0


def test_roi_increment_is_measured_from_the_property_minimum():
    """A minimum that is not itself a multiple of the increment still yields a
    legal value: the increment applies from the minimum, not from zero."""
    assert _fit(20, minimum=2, maximum=100, increment=4) == 18


def test_roi_falls_back_to_the_raw_value_when_constraints_are_unavailable():
    """An unexpected SDK shape must degrade to previous behaviour, not break."""
    from imswitch_device_tis._ic4_driver import IC4Camera

    class Broken:
        def find_integer(self, prop_id):
            raise AttributeError('no such property')

    camera = IC4Camera.__new__(IC4Camera)
    camera._pm = Broken()
    assert IC4Camera._fit_to_property(camera, 'W', 513) == 513


def test_frame_queue_latest_survives_drain_but_not_clear():
    q = FrameQueue(maxlen=4)
    q.push(np.ones((2, 2)))

    q.drain()
    assert q.latest() is not None, 'live view must keep its frame across a drain'

    q.clear()
    assert q.latest() is None, 'crop must invalidate the retained frame'
