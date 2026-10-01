"""In-process stand-in for a Photometrics camera behind pyvcam.

Answers the PVCAM surface ``PhotometricsManager`` uses -- and nothing more --
so the manager can be constructed, configured and read without pyvcam or a
camera. The manager used to fall back to ``MockHamamatsu`` here, which has
none of that surface: the fallback meant to let a rig without pyvcam start
raised ``AttributeError: 'MockHamamatsu' object has no attribute 'name'``
one line later, and with it the whole imcontrol module failed to load.

Trigger-aware like ``MockHamamatsu``: with the internal trigger, frames
free-run on the wall clock at the exposure cadence; in either external mode a
frame exists only once ``mockTrigger(n)`` has queued it.
"""

import time

import numpy as np

# PVCAM PL_EXPOSURE_MODES, the extended trigger codes: (7 + n) << 8.
EXT_TRIG_INTERNAL = 1792       # internal timing
EXT_TRIG_TRIG_FIRST = 2048     # the first edge starts, then internal timing
EXT_TRIG_EDGE_RISING = 2304    # one exposure per rising edge
EXT_TRIG_LEVEL = 2560          # exposure lasts while the line is high


class MockPhotometrics:
    """The slice of ``pyvcam.camera.Camera`` that ``PhotometricsManager`` reads."""

    def __init__(self, sensor_size=(512, 512), scan_line_time_ns=10_000, seed=0):
        self.name = 'Mock Photometrics camera'
        self.sensor_size = tuple(int(v) for v in sensor_size)  # (width, height)
        self.scan_line_time = int(scan_line_time_ns)
        self.exp_time = 10  # ms
        self.exp_mode = EXT_TRIG_INTERNAL
        self.readout_port = 0
        self.binning = 1
        self.roi = (0, self.sensor_size[0], 0, self.sensor_size[1])  # (x1, x2, y1, y2)
        self.is_open = True
        self._live = False
        self._queued = 0
        self._last_frame_time = 0.0
        self._frame_count = 0
        self._rng = np.random.default_rng(seed)

    # -- lifecycle ---------------------------------------------------------
    def open(self):
        self.is_open = True

    def close(self):
        self._live = False
        self.is_open = False

    def start_live(self, exp_time=None):
        if exp_time is not None:
            self.exp_time = int(exp_time)
        self._live = True
        self._last_frame_time = time.monotonic()

    def abort(self):
        self._live = False

    def finish(self):
        self._live = False

    # -- frames --------------------------------------------------------------
    def mockTrigger(self, n=1):
        """Queue ``n`` externally triggered frames."""
        self._queued += int(n)

    def _frame_shape(self):
        x1, x2, y1, y2 = self.roi
        binning = max(1, int(self.binning))
        return (max(1, (y2 - y1) // binning), max(1, (x2 - x1) // binning))

    def _frame_is_due(self):
        if not self._live:
            return False
        if self.exp_mode == EXT_TRIG_INTERNAL:
            return time.monotonic() - self._last_frame_time >= self.exp_time / 1000.0
        return self._queued > 0

    def check_frame_status(self):
        if not self._live:
            return 'READOUT_NOT_ACTIVE'
        return 'FRAME_AVAILABLE' if self._frame_is_due() else 'EXPOSURE_IN_PROGRESS'

    def poll_frame(self, timeout_ms=None):
        """``(frame, fps, frame_count)`` as pyvcam returns it; raises when none is due."""
        if not self._live:
            raise RuntimeError('Camera is not acquiring')
        if not self._frame_is_due():
            raise RuntimeError('No frame available')
        if self.exp_mode != EXT_TRIG_INTERNAL:
            self._queued -= 1
        self._last_frame_time = time.monotonic()
        self._frame_count += 1
        frame = self._rng.poisson(100, size=self._frame_shape()).astype(np.uint16)
        return ({'pixel_data': frame}, 0.0, self._frame_count)
