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
    dtype_for_pixel_format, ensure_library_initialized, normalize_frame,
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


# --- Frame shape ------------------------------------------------------------
#
# Measured on a DMK 33UX250: ImageBuffer.numpy_copy() returns (2048, 2448, 1)
# uint8 for Mono8 — a trailing channel axis. The mock originally produced a
# clean (H, W), so the whole suite passed against a shape the hardware never
# emits, and getChunk would have stacked (N, H, W, 1) into the recorder.


def test_mono_channel_axis_is_dropped():
    frame = np.zeros((2048, 2448, 1), dtype=np.uint8)

    assert normalize_frame(frame).shape == (2048, 2448)


def test_two_dimensional_frames_pass_through_unchanged():
    frame = np.zeros((48, 64), dtype=np.uint16)

    assert normalize_frame(frame) is frame


def test_multi_channel_frames_are_left_alone():
    """Only a *singleton* channel axis is squeezed; a real colour frame must
    keep its channels rather than being silently mangled."""
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    assert normalize_frame(frame).shape == (48, 64, 3)


# --- Auto exposure / auto gain ----------------------------------------------
#
# Observed on the rig: exposure kept snapping back to 1/30 s and gain to 48 dB,
# and the only cure was opening IC Capture and unticking the auto boxes by hand.
# The cause is not leftover configuration — loading the Default user set (which
# the driver does on every open) sets both autos to Continuous, so opening the
# camera is what turned them back on.


class _FakeEnumEntry:
    def __init__(self, name):
        self.name = name


class _FakeEnumProperty:
    def __init__(self, entries, value=None):
        self.entries = [_FakeEnumEntry(n) for n in entries]
        self.value = value if value is not None else entries[0]


class _RecordingPropertyMap:
    """Property map that records writes, shaped like the IC4 surface used here."""

    def __init__(self, enums=None, values=None):
        self.writes = []
        self._enums = enums or {}
        self.values = values or {}

    def try_set_value(self, prop_id, value):
        self.writes.append((prop_id, value))
        self.values[prop_id] = value
        return True

    def set_value(self, prop_id, value):
        self.writes.append((prop_id, value))
        self.values[prop_id] = value

    def get_value_str(self, prop_id):
        return self.values[prop_id]

    def get_value_float(self, prop_id):
        return float(self.values[prop_id])

    def find_enumeration(self, prop_id):
        if prop_id not in self._enums:
            raise KeyError(prop_id)
        return self._enums[prop_id]


class _FakePropIds:
    EXPOSURE_AUTO = 'ExposureAuto'
    GAIN_AUTO = 'GainAuto'
    EXPOSURE_TIME = 'ExposureTime'
    GAIN = 'Gain'
    TRIGGER_MODE = 'TriggerMode'
    TRIGGER_SELECTOR = 'TriggerSelector'


class _FakeIC4Module:
    PropId = _FakePropIds


def _camera_with(pm):
    """An IC4Camera bound to a fake property map, with no SDK and no device."""
    from imswitch_device_tis._ic4_driver import IC4Camera

    camera = IC4Camera.__new__(IC4Camera)
    camera._pm = pm
    camera._ic4 = _FakeIC4Module
    return camera


def test_autos_are_switched_off_before_an_exposure_is_written():
    """The write is worthless while the auto algorithm owns the property."""
    from imswitch_device_tis._ic4_driver import IC4Camera

    pm = _RecordingPropertyMap(values={'ExposureTime': 5000.0})
    IC4Camera.set_exposure_us(_camera_with(pm), 5000)

    assert ('ExposureAuto', 'Off') in pm.writes
    assert ('GainAuto', 'Off') in pm.writes
    assert pm.writes.index(('ExposureAuto', 'Off')) < \
        pm.writes.index(('ExposureTime', 5000.0)), 'auto must go off first'


def test_autos_are_switched_off_before_a_gain_is_written():
    from imswitch_device_tis._ic4_driver import IC4Camera

    pm = _RecordingPropertyMap(values={'Gain': 0.0})
    IC4Camera.set_gain(_camera_with(pm), 0)

    assert ('GainAuto', 'Off') in pm.writes
    assert pm.writes.index(('GainAuto', 'Off')) < pm.writes.index(('Gain', 0.0))


def test_every_trigger_selector_is_disarmed_not_just_frame_start():
    """TriggerMode is per-selector: disarming only FrameStart leaves another
    armed trigger holding the stream while every read says Off."""
    from imswitch_device_tis._ic4_driver import IC4Camera

    pm = _RecordingPropertyMap(enums={
        'TriggerSelector': _FakeEnumProperty(
            ['AcquisitionStart', 'FrameStart', 'ExposureStart']
        )
    })
    IC4Camera._disarm_all_triggers(_camera_with(pm))

    disarmed = [sel for sel, mode in zip(
        [w[1] for w in pm.writes if w[0] == 'TriggerSelector'],
        [w[1] for w in pm.writes if w[0] == 'TriggerMode'],
    ) if mode == 'Off']
    assert set(disarmed) == {'AcquisitionStart', 'FrameStart', 'ExposureStart'}
    # The selector is left where set_trigger_enabled and any scan expect it.
    assert pm.writes[-1] == ('TriggerSelector', 'FrameStart')


def test_disarm_falls_back_when_the_model_has_no_trigger_selector():
    from imswitch_device_tis._ic4_driver import IC4Camera

    pm = _RecordingPropertyMap()
    IC4Camera._disarm_all_triggers(_camera_with(pm))

    assert pm.writes == [('TriggerMode', 'Off')]


def test_trigger_transition_is_bracketed_by_a_state_snapshot():
    """Exposure and gain are reported to move across a trigger switch, and
    nothing in set_trigger_enabled writes them. The snapshot either side is what
    tells the frame-rate/exposure coupling apart from GainAuto re-engaging apart
    from a merely stale cached parameter, so the 'before' one has to be taken
    before any write lands -- a snapshot read after the fact proves nothing."""
    from imswitch_device_tis._ic4_driver import IC4Camera

    pm = _RecordingPropertyMap(values={'TriggerMode': 'Off'})
    camera = _camera_with(pm)
    snapshots = []
    camera._log_state = lambda when, **kwargs: snapshots.append(
        (when, len(pm.writes))
    )

    IC4Camera.set_trigger_enabled(camera, True)

    assert len(snapshots) == 2, 'the transition must be logged either side'
    assert snapshots[0][1] == 0, 'the "before" snapshot must precede every write'
    assert snapshots[1][1] == len(pm.writes), 'the "after" snapshot must follow them'
    assert 'before' in snapshots[0][0] and 'after' in snapshots[1][0]


def test_mock_starts_with_the_autos_off():
    """Not cosmetic: the mock loads the Default user set's state on construction
    exactly as the real camera does, so this pins the fix at open time."""
    from imswitch_device_tis._ic4_driver import MockIC4Camera

    camera = MockIC4Camera(serial="MOCK_X")

    assert camera.get_exposure_us() != MockIC4Camera.AUTO_EXPOSURE_US
    assert camera.get_gain() != MockIC4Camera.AUTO_GAIN_DB


def test_written_exposure_survives_the_vendor_gui_re_enabling_auto():
    """IC Capture can re-tick the auto boxes on a camera ImSwitch has open, so
    recovery has to happen on the write path, not only at open."""
    from imswitch_device_tis._ic4_driver import MockIC4Camera

    camera = MockIC4Camera(serial="MOCK_X")
    camera.simulate_auto_reenabled()
    assert camera.get_exposure_us() == MockIC4Camera.AUTO_EXPOSURE_US  # broken

    camera.set_exposure_us(2500)
    camera.set_gain(3)

    assert camera.get_exposure_us() == 2500
    assert camera.get_gain() == 3


def test_mock_emits_the_shape_hardware_emits():
    """The mock must go through the same normalization as the real listener.

    If it fabricated a clean (H, W) directly, it would keep passing tests that
    the camera fails — which is exactly how the channel axis reached a commit.
    """
    from imswitch_device_tis._ic4_driver import MockIC4Camera

    camera = MockIC4Camera(serial="MOCK_X", pixel_format="Mono8")
    camera.start_stream()
    camera.simulate_hardware_trigger(1)

    frame = camera.pop_frames()[0]

    assert frame.ndim == 2
    assert frame.dtype == np.uint8
