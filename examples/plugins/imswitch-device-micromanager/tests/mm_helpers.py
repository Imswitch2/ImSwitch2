"""Shared constants and wait helpers of the micromanager plugin tests."""
import threading
import time

DEMO_CONFIG = "MMConfig_demo.cfg"      # shipped in the adapter folder; MMCore resolves the relative name
FRAME_SIDE = 512                       # the demo cameras' default image is 512 x 512
CAMERAS = ("Camera", "Cam2")
#: Per-frame exposure of the demo cameras in the tests: slow enough that a pop
#: loop on a loaded CI machine keeps up, fast enough that 200 frames take ~1 s.
TEST_EXPOSURE_MS = 5
#: Upper bound for anything a test waits for; a timeout is a failure, not a retry.
DEADLINE_S = 20.0
#: Poll interval of the tests' own wait loops (tests may poll with a deadline).
POLL_S = 0.002


def wait_until(predicate, timeout=DEADLINE_S, message="condition"):
    """Poll ``predicate`` until it is truthy or the deadline passes."""
    end = time.monotonic() + timeout
    pause = threading.Event()
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout} s waiting for {message}")
        pause.wait(POLL_S)


def collect_records(host, label, count, timeout=DEADLINE_S):
    """Pop ``label``'s queue until at least ``count`` records arrived."""
    records = []

    def enough():
        records.extend(host.pop_records(label))
        return len(records) >= count

    wait_until(enough, timeout, f"{count} frames from {label}")
    return records


def epochs(numbers):
    """Split ImageNumbers at every restart; each epoch must be 0, 1, 2, ...

    MMCore clears its per-camera counters when any camera starts, so a camera
    that was already running restarts at 0 (see ``_core._start_locked``).
    """
    runs, current = [], []
    for number in numbers:
        if current and number != current[-1] + 1:
            runs.append(current)
            current = []
        current.append(number)
    if current:
        runs.append(current)
    return runs


def make_demo_cameras(host, exposure_ms=TEST_EXPOSURE_MS):
    """Camera (from the cfg) + Cam2 (loaded inline), both 512 x 512, 16 bit, registered."""
    host.load_device("Cam2", "DemoCamera", "DCam")
    for label in CAMERAS:
        host.call("setProperty", label, "OnCameraCCDXSize", FRAME_SIDE)
        host.call("setProperty", label, "OnCameraCCDYSize", FRAME_SIDE)
        host.call("setProperty", label, "PixelType", "16bit")
        host.call("setExposure", label, exposure_ms)
        host.register_camera(label)


def start(host, label, shape=(FRAME_SIDE, FRAME_SIDE), dtype="uint16"):
    """``host.start_stream`` with the demo cameras' default format."""
    return host.start_stream(label, shape, dtype)
