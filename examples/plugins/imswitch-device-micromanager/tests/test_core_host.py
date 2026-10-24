"""MMCoreHost against the real MMCore and the DemoCamera adapters (no mocks)."""
import threading

import numpy as np
import pytest

from mm_helpers import (
    CAMERAS, DEADLINE_S, DEMO_CONFIG, FRAME_SIDE, collect_records, epochs, make_demo_cameras, start,
    wait_until,
)
from imswitch_device_micromanager import _core
from imswitch_device_micromanager._core import (
    FrameShapeMismatchError, FramesLostError, MMAdaptersNotFoundError, MMConfigFileNotFoundError,
    MMCoreHost, MMCoreHostError, StreamFaultKind,
)

FRAME_COUNT = 200
SMALL_SIDE = 256


def stall_drain(host, seconds):
    """Hold the host lock so the drain thread cannot pop: the buffer fills."""
    with host.lock:
        threading.Event().wait(seconds)


# ------------------------------------------------------------------ connection
def test_core_uses_the_psygnal_signaler_and_the_hub_settings(host):
    assert type(host.core.events).__name__ == "CMMCoreSignaler"
    assert host.adapter_source == "adapterPaths"
    assert "Device API version" in host.version_info
    assert host.call("getAutoShutter") is False        # the demo cfg turns it on; the host turns it off
    assert host.call("getCircularBufferMemoryFootprint") == 250   # MMCore's default, left alone


def test_signaler_is_psygnal_with_a_qapplication_alive(adapter_dir):
    qtwidgets = pytest.importorskip("PyQt5.QtWidgets")
    app = qtwidgets.QApplication.instance() or qtwidgets.QApplication([])
    _KEEP_ALIVE.append(app)       # dropping the last reference tears down every Python-owned QObject
    h = MMCoreHost("qt", config_file=DEMO_CONFIG, adapter_paths=(adapter_dir,))
    h.connect()
    try:
        assert type(h.core.events).__name__ == "CMMCoreSignaler"
    finally:
        h.shutdown()


_KEEP_ALIVE = []


def test_circular_buffer_size_is_applied_only_when_given(adapter_dir):
    h = MMCoreHost("buf", adapter_paths=(adapter_dir,), circular_buffer_mb=33)
    h.connect()
    try:
        assert h.call("getCircularBufferMemoryFootprint") == 33
    finally:
        h.shutdown()


def test_log_dir_receives_the_mmcore_log(adapter_dir, tmp_path):
    h = MMCoreHost("log", adapter_paths=(adapter_dir,), log_dir=str(tmp_path / "logs"))
    h.connect()
    try:
        assert h.call("getPrimaryLogFile").startswith(str(tmp_path / "logs"))
    finally:
        h.shutdown()


def test_adapter_path_resolution_order(adapter_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(_core.MICROMANAGER_PATH_ENV, adapter_dir)
    h = MMCoreHost("env", config_file=DEMO_CONFIG)
    h.connect()
    try:
        assert h.adapter_source == _core.MICROMANAGER_PATH_ENV
    finally:
        h.shutdown()
    explicit = MMCoreHost("explicit", adapter_paths=(adapter_dir,))
    explicit.connect()
    try:
        assert explicit.adapter_source == "adapterPaths"        # explicit wins over the environment
    finally:
        explicit.shutdown()


def test_missing_adapter_folder_raises_one_host_error(tmp_path):
    h = MMCoreHost("none", adapter_paths=(str(tmp_path / "does-not-exist"),))
    with pytest.raises(MMAdaptersNotFoundError, match="does-not-exist"):
        h.connect()
    assert not h.is_connected


def test_missing_config_file_raises_and_leaves_the_host_unconnected(adapter_dir, tmp_path):
    h = MMCoreHost("nocfg", config_file=str(tmp_path / "missing.cfg"), adapter_paths=(adapter_dir,))
    with pytest.raises(MMConfigFileNotFoundError, match="missing.cfg"):
        h.connect()
    assert not h.is_connected
    with pytest.raises(MMCoreHostError, match="not connected"):
        h.core


def test_config_with_a_failing_device_raises_the_mmcore_message(adapter_dir, tmp_path):
    cfg = tmp_path / "broken.cfg"
    cfg.write_text("Device,Cam,NoSuchAdapter,NoSuchDevice\n")
    h = MMCoreHost("broken", config_file=str(cfg), adapter_paths=(adapter_dir,))
    with pytest.raises(MMCoreHostError, match="NoSuchAdapter"):
        h.connect()
    assert not h.is_connected


def test_connect_twice_is_refused(host):
    with pytest.raises(MMCoreHostError, match="already connected"):
        host.connect()


def test_adopt_existing_core_unloads_only_what_it_loaded(adapter_dir):
    from pymmcore_plus import CMMCorePlus

    mine = CMMCorePlus.instance()
    mine.setDeviceAdapterSearchPaths([adapter_dir])
    mine.loadDevice("Theirs", "DemoCamera", "DCam")
    mine.initializeDevice("Theirs")
    h = MMCoreHost("adopt", adapter_paths=(adapter_dir,), adopt_existing_core=True)
    h.connect()
    try:
        assert h.core is mine
        h.load_device("Ours", "DemoCamera", "DCam")
        assert h.has_device("Ours")
    finally:
        h.shutdown()
        try:
            assert "Theirs" in mine.getLoadedDevices()
            assert "Ours" not in mine.getLoadedDevices()
        finally:
            mine.unloadAllDevices()


def test_shutdown_unloads_everything(host):
    core = host.core
    start(host, "Camera")
    host.shutdown()
    assert not host.is_connected
    assert tuple(core.getLoadedDevices()) == ("Core",)
    assert not core.isSequenceRunning("Camera") if "Camera" in core.getLoadedDevices() else True
    assert host.snapshot()["drain_thread_alive"] is False
    host.shutdown()                                   # idempotent


def test_host_can_connect_again_after_shutdown(host, adapter_dir):
    host.shutdown()
    host.connect()
    assert host.has_device("Camera")
    host.load_device("Cam2", "DemoCamera", "DCam")
    host.call("setProperty", "Cam2", "OnCameraCCDXSize", FRAME_SIDE)
    # registrations survive; their state was reset
    assert host.stream("Camera").streaming is False
    start(host, "Camera")
    collect_records(host, "Camera", 3)
    host.stop_stream("Camera")


# ------------------------------------------------------------ inline devices
def test_inline_device_load_and_unload(host):
    host.load_device("Cam3", "DemoCamera", "DCam")
    assert host.has_device("Cam3")
    from pymmcore_plus import DeviceType

    assert host.device_type("Cam3") == DeviceType.Camera
    assert "Cam3" in host.labels_of_type(DeviceType.Camera)
    host.register_camera("Cam3")
    host.unload_device("Cam3")
    assert not host.has_device("Cam3")
    with pytest.raises(MMCoreHostError, match="not registered"):
        host.stream("Cam3")


def test_inline_device_failure_leaves_nothing_loaded(host):
    with pytest.raises(Exception, match="NoSuchDevice|not found|Failed|error"):
        host.load_device("Bad", "DemoCamera", "NoSuchDevice")
    assert not host.has_device("Bad")
    with pytest.raises(Exception):
        host.load_device("Bad2", "DemoCamera", "DCam", pre_init={"NoSuchProperty": "1"})
    assert not host.has_device("Bad2")


def test_missing_device_message_lists_loaded_cameras_and_a_hint(host):
    from pymmcore_plus import DeviceType

    message = host.missing_device_message("Camra", DeviceType.Camera, "camera")
    assert "Did you mean 'Camera'" in message and "Camera" in message and "Cam2" in message
    with pytest.raises(MMCoreHostError, match="Did you mean 'Camera'"):
        host.register_camera("Camra")
    with pytest.raises(MMCoreHostError, match="no camera device 'Z'"):
        host.register_camera("Z")                       # a stage is not a camera


def test_frame_format_reads_the_core_and_restores_the_camera_selection(host):
    assert host.frame_format("Camera") == ((FRAME_SIDE, FRAME_SIDE), np.dtype(np.uint16))
    host.call("setProperty", "Cam2", "PixelType", "8bit")
    assert host.set_roi("Cam2", 0, 0, SMALL_SIDE, 128) == (0, 0, SMALL_SIDE, 128)
    assert host.frame_format("Cam2") == ((128, SMALL_SIDE), np.dtype(np.uint8))
    assert host.call("getCameraDevice") == "Camera"          # selection put back


def test_frame_format_of_a_non_current_camera_needs_explicit_values_while_streaming(host):
    start(host, "Camera")
    with pytest.raises(MMCoreHostError, match="cannot switch it during a sequence"):
        host.frame_format("Cam2")
    host.stop_stream("Camera")


def test_sensor_geometry_of_an_untouched_camera_is_the_full_frame(host):
    assert host.sensor_geometry("Camera") == (FRAME_SIDE, FRAME_SIDE, (0, 0, FRAME_SIDE, FRAME_SIDE))
    assert host.call("getROI", "Camera") == [0, 0, FRAME_SIDE, FRAME_SIDE]


def test_sensor_geometry_keeps_a_roi_that_is_already_set_and_the_camera_selection(host):
    host.call("setROI", "Cam2", 16, 8, SMALL_SIDE, 128)      # not the current camera
    assert host.call("getCameraDevice") == "Camera"
    assert host.sensor_geometry("Cam2") == (FRAME_SIDE, FRAME_SIDE, (16, 8, SMALL_SIDE, 128))
    assert host.call("getROI", "Cam2") == [16, 8, SMALL_SIDE, 128]       # restored
    assert host.call("getCameraDevice") == "Camera"                       # selection restored


def test_sensor_geometry_is_the_binned_image_size(host):
    host.call("setProperty", "Camera", "Binning", 2)
    assert host.sensor_geometry("Camera") == (FRAME_SIDE // 2, FRAME_SIDE // 2,
                                              (0, 0, FRAME_SIDE // 2, FRAME_SIDE // 2))


def test_sensor_geometry_is_refused_while_any_stream_runs(host):
    start(host, "Camera")
    with pytest.raises(MMCoreHostError, match="while .*Camera.* stream"):
        host.sensor_geometry("Cam2")
    host.stop_stream("Camera")
    assert host.sensor_geometry("Cam2")[:2] == (FRAME_SIDE, FRAME_SIDE)


# ---------------------------------------------------------------- one camera
def test_single_camera_counted_equals_delivered_without_gaps(host):
    assert start(host, "Camera") is True
    assert start(host, "Camera") is False        # already running with the same format
    frames = collect_records(host, "Camera", FRAME_COUNT)
    host.stop_stream("Camera")
    frames.extend(host.pop_records("Camera"))

    snapshot = host.snapshot()["streams"]["Camera"]
    assert len(frames) >= FRAME_COUNT
    assert snapshot["frames_received"] == snapshot["frames_delivered"] == len(frames)
    assert [r.image_number for r in frames] == list(range(len(frames)))
    assert all(r.camera == "Camera" for r in frames)
    assert all(r.frame.shape == (FRAME_SIDE, FRAME_SIDE) and r.frame.dtype == np.uint16 for r in frames)
    assert snapshot["frames_lost"] == 0 and snapshot["fault"] is None
    assert host.running_streams() == []
    assert host.call("getRemainingImageCount") == 0
    assert host.snapshot()["unrouted_frames"] == {}


def test_drain_thread_stops_with_the_last_stream(host):
    start(host, "Camera")
    assert host.snapshot()["drain_thread_alive"]
    host.stop_stream("Camera")
    wait_until(lambda: not host.snapshot()["drain_thread_alive"], message="drain thread exit")


def test_pop_returns_only_that_cameras_frames_and_empties_the_queue(host):
    start(host, "Camera")
    collect_records(host, "Camera", 3)
    host.stop_stream("Camera")
    host.pop_frames("Camera")                         # whatever arrived until the stop
    assert host.pop_frames("Camera") == []
    assert host.pop_frames("Cam2") == []              # never started: nothing of Camera's leaks over


def test_pop_respects_max_frames(host):
    start(host, "Camera")
    wait_until(lambda: host.snapshot()["streams"]["Camera"]["queued_frames"] >= 5, message="5 queued frames")
    sliced = host.pop_records("Camera", max_frames=2)
    assert [r.image_number for r in sliced] == [0, 1]
    host.stop_stream("Camera")
    assert host.pop_records("Camera", max_frames=1)[0].image_number == 2


# ------------------------------------------------------------- two cameras
def test_two_cameras_every_frame_lands_in_its_own_queue_in_order(host):
    start(host, "Camera")
    start(host, "Cam2")
    both = {label: collect_records(host, label, FRAME_COUNT // 2) for label in CAMERAS}
    for label in CAMERAS:
        host.stop_stream(label)
    for label in CAMERAS:
        both[label].extend(host.pop_records(label))

    for label in CAMERAS:
        records = both[label]
        assert all(r.camera == label for r in records)            # the Camera tag matches the queue
        runs = epochs([r.image_number for r in records])
        assert 1 <= len(runs) <= 2 and all(run[0] == 0 for run in runs)   # contiguous per camera, restart at 0 on the other's start
        stats = host.snapshot()["streams"][label]
        assert stats["frames_lost"] == 0 and stats["fault"] is None
        assert stats["frames_received"] == len(records)
    # a misrouted frame would repeat an ImageNumber inside one run; none did
    assert host.snapshot()["unrouted_frames"] == {}


def test_pending_frames_survive_the_second_cameras_start(host):
    start(host, "Camera")
    first = collect_records(host, "Camera", 5)
    host.stop_stream("Camera")
    pending = host.snapshot()["streams"]["Camera"]["queued_frames"]
    start(host, "Cam2")                           # MMCore clears its buffer here
    assert host.snapshot()["streams"]["Camera"]["queued_frames"] == pending
    start(host, "Camera")
    collect_records(host, "Cam2", 3)
    host.stop_stream("Cam2")
    host.stop_stream("Camera")
    numbers = [r.image_number for r in first] + [r.image_number for r in host.pop_records("Camera")]
    assert len(numbers) >= len(first)
    assert numbers[:len(first)] == [r.image_number for r in first]
    assert host.snapshot()["streams"]["Camera"]["fault"] is None


def test_unread_frames_of_a_running_camera_survive_the_second_cameras_start(host):
    start(host, "Camera")
    collect_records(host, "Camera", 1)               # popped, so the next ones are unread
    wait_until(lambda: host.snapshot()["streams"]["Camera"]["queued_frames"] >= 5,
               message="5 unread frames")
    unread = host.snapshot()["streams"]["Camera"]["queued_frames"]
    start(host, "Cam2")
    assert host.snapshot()["streams"]["Camera"]["queued_frames"] >= unread
    host.stop_stream("Cam2")
    host.stop_stream("Camera")


@pytest.mark.parametrize("change", ["roi", "pixel_type"])
def test_second_camera_with_a_different_format_is_refused(host, change):
    """The format of Cam2 differs from the running Camera's; MMCore would drop one silently."""
    start(host, "Camera")
    collect_records(host, "Camera", 2)
    if change == "roi":
        shape, dtype = (SMALL_SIDE, SMALL_SIDE), np.uint16
    else:
        shape, dtype = (FRAME_SIDE, FRAME_SIDE), np.uint8
    with pytest.raises(FrameShapeMismatchError, match="#936"):
        host.start_stream("Cam2", shape, dtype)
    assert host.call("isSequenceRunning", "Cam2") is False
    assert host.stream("Cam2").streaming is False
    # the first camera was not disturbed
    collect_records(host, "Camera", 2)
    host.stop_stream("Camera")
    assert host.snapshot()["streams"]["Camera"]["fault"] is None


def test_a_small_camera_streaming_alone_is_read_with_its_own_size(host):
    """popNextImageAndMD shapes frames by the *current* camera; the host selects the streamer."""
    assert host.set_roi("Cam2", 0, 0, SMALL_SIDE, 128) == (0, 0, SMALL_SIDE, 128)
    assert host.call("getCameraDevice") == "Camera"
    start(host, "Cam2", (128, SMALL_SIDE))
    assert host.call("getCameraDevice") == "Cam2"
    records = collect_records(host, "Cam2", 5)
    host.stop_stream("Cam2")
    assert all(r.frame.shape == (128, SMALL_SIDE) for r in records)
    assert host.call("getCameraDevice") == "Camera"          # selection put back
    assert host.snapshot()["streams"]["Cam2"]["fault"] is None


def test_format_read_from_the_core_is_what_the_guard_compares(host):
    assert host.set_roi("Cam2", 0, 0, SMALL_SIDE, SMALL_SIDE) == (0, 0, SMALL_SIDE, SMALL_SIDE)
    shape, dtype = host.frame_format("Cam2")
    assert (shape, dtype) == ((SMALL_SIDE, SMALL_SIDE), np.dtype(np.uint16))
    host.start_stream("Cam2", shape, dtype)
    assert host.stream("Cam2").shape == (SMALL_SIDE, SMALL_SIDE)
    with pytest.raises(FrameShapeMismatchError, match="#936"):
        start(host, "Camera")
    host.stop_stream("Cam2")
    start(host, "Camera")                                               # fine once Cam2 is stopped
    host.stop_stream("Camera")


def test_explicit_format_that_contradicts_the_running_camera_is_refused(host):
    start(host, "Camera")
    with pytest.raises(FrameShapeMismatchError, match="#936"):
        host.start_stream("Cam2", (FRAME_SIDE, FRAME_SIDE), np.uint8)
    host.stop_stream("Camera")


# --------------------------------------------------------------------- ROI
def test_roi_during_streaming_keeps_pending_frames_and_changes_the_shape(host):
    start(host, "Camera")
    wait_until(lambda: host.snapshot()["streams"]["Camera"]["queued_frames"] >= 3, message="pending frames")
    pending = host.snapshot()["streams"]["Camera"]["queued_frames"]

    applied = host.set_roi("Camera", 10, 20, 100, 50)
    assert applied == (10, 20, 100, 50) and applied == tuple(host.call("getROI", "Camera"))
    assert host.stream("Camera").streaming is True        # restarted
    assert host.stream("Camera").shape == (50, 100)

    old, new = [], []

    def gathered():
        for record in host.pop_records("Camera"):
            (new if record.frame.shape == (50, 100) else old).append(record)
        return len(new) >= 3

    wait_until(gathered, message="3 frames with the new shape")
    host.stop_stream("Camera")
    assert len(old) >= pending                              # nothing pending was lost to the ROI change
    assert all(r.frame.shape == (FRAME_SIDE, FRAME_SIDE) for r in old)
    assert [r.image_number for r in old] == list(range(len(old)))     # contiguous: no gap at the restart
    assert [r.image_number for r in new[:3]] == [0, 1, 2]              # the new sequence starts at 0
    assert host.snapshot()["streams"]["Camera"]["fault"] is None


def test_pop_returns_one_shape_per_call_across_a_roi_change(host):
    start(host, "Camera")
    wait_until(lambda: host.snapshot()["streams"]["Camera"]["queued_frames"] >= 4, message="old frames")
    old_count = host.snapshot()["streams"]["Camera"]["queued_frames"]
    host.set_roi("Camera", 0, 0, 100, 50)
    collect_target = old_count + 3
    seen, shapes = 0, []

    def gathered():
        nonlocal seen
        frames = host.pop_frames("Camera")
        if frames:
            assert len({f.shape for f in frames}) == 1       # a run never mixes shapes
            shapes.append(frames[0].shape)
            seen += len(frames)
        return seen >= collect_target

    wait_until(gathered, message="old and new frames")
    host.stop_stream("Camera")
    assert shapes[0] == (FRAME_SIDE, FRAME_SIDE)             # the pending frames came first, untouched
    assert shapes[-1] == (50, 100)


def test_roi_on_an_idle_camera_is_applied_and_read_back(host):
    assert host.set_roi("Camera", 0, 0, 64, 32) == (0, 0, 64, 32)
    assert host.stream("Camera").streaming is False
    assert host.stream("Camera").shape == (32, 64)
    start(host, "Camera", (32, 64))
    frames = collect_records(host, "Camera", 2)
    host.stop_stream("Camera")
    assert frames[0].frame.shape == (32, 64)


def test_roi_of_an_idle_camera_does_not_fault_a_running_one(host):
    """MMCore clears the whole buffer on setROI, even for another camera (observed)."""
    start(host, "Camera")
    collect_records(host, "Camera", 2)
    assert host.set_roi("Cam2", 0, 0, FRAME_SIDE, FRAME_SIDE) == (0, 0, FRAME_SIDE, FRAME_SIDE)
    collect_records(host, "Camera", 2)
    host.stop_stream("Camera")                          # raises if the epoch bookkeeping was wrong
    assert host.snapshot()["streams"]["Camera"]["fault"] is None


def test_a_roi_change_behind_the_hosts_back_is_detected_not_silent(host):
    start(host, "Camera")
    collect_records(host, "Camera", 2)
    host.call("setROI", "Cam2", 0, 0, FRAME_SIDE, FRAME_SIDE)     # clears MMCore's buffer and counters
    wait_until(lambda: host.snapshot()["streams"]["Camera"]["fault"], message="counter reset fault")
    assert host.snapshot()["streams"]["Camera"]["fault"]["kind"] == StreamFaultKind.GAP.value
    with pytest.raises(FramesLostError, match="behind the host's back"):
        host.stop_stream("Camera")


def test_roi_that_would_clash_with_a_running_camera_is_rolled_back(host):
    start(host, "Camera")
    start(host, "Cam2")
    with pytest.raises(FrameShapeMismatchError, match="#936"):
        host.set_roi("Cam2", 0, 0, SMALL_SIDE, SMALL_SIDE)
    assert tuple(host.call("getROI", "Cam2")) == (0, 0, FRAME_SIDE, FRAME_SIDE)
    assert host.stream("Cam2").streaming is True and host.stream("Cam2").shape == (FRAME_SIDE, FRAME_SIDE)
    collect_records(host, "Cam2", 2)
    for label in CAMERAS:
        host.stop_stream(label)


def test_failed_roi_restores_the_old_roi_and_restarts_the_stream(host, monkeypatch):
    start(host, "Camera")
    collect_records(host, "Camera", 1)
    real_start = host._start_locked
    calls = []

    def failing_once(stream):
        calls.append(stream.shape)
        if len(calls) == 1:
            raise RuntimeError("camera refused to restart with the new ROI")
        return real_start(stream)

    monkeypatch.setattr(host, "_start_locked", failing_once)
    with pytest.raises(RuntimeError, match="refused to restart"):
        host.set_roi("Camera", 0, 0, 64, 64)
    assert tuple(host.call("getROI", "Camera")) == (0, 0, FRAME_SIDE, FRAME_SIDE)
    assert host.stream("Camera").shape == (FRAME_SIDE, FRAME_SIDE)
    assert host.stream("Camera").streaming is True               # back to what it was doing
    monkeypatch.undo()
    collect_records(host, "Camera", 2)
    host.stop_stream("Camera")


# ------------------------------------------------ no buffer clear next to a recording
def test_no_start_or_roi_change_while_another_running_stream_is_recording(host):
    """startSequenceAcquisition and setROI clear MMCore's one buffer and reset every camera's
    ImageNumber; a frame a running camera inserts after the host's drain would vanish without a
    trace. So while another running stream is being recorded, those calls are refused."""
    recording = {"Camera": False}
    host.register_camera("Camera", lambda: recording["Camera"])
    host.register_camera("Cam2", lambda: False)
    start(host, "Camera")
    collect_records(host, "Camera", 2)
    assert start(host, "Cam2") is True                            # nobody records yet: allowed
    host.stop_stream("Cam2")
    assert host.set_roi("Cam2", 0, 0, FRAME_SIDE, FRAME_SIDE) == (0, 0, FRAME_SIDE, FRAME_SIDE)

    recording["Camera"] = True
    with pytest.raises(MMCoreHostError, match=r"recording.*#936"):
        start(host, "Cam2")
    with pytest.raises(MMCoreHostError, match="recording"):
        host.set_roi("Cam2", 0, 0, FRAME_SIDE, FRAME_SIDE)
    with pytest.raises(MMCoreHostError, match="recording"):
        host.check_buffer_may_be_cleared("Cam2")
    assert not host.stream("Cam2").streaming
    host.check_buffer_may_be_cleared("Camera")                    # a camera never blocks itself
    collect_records(host, "Camera", 2)                            # the recording went on, unbroken

    recording["Camera"] = False
    assert start(host, "Cam2") is True
    host.stop_stream("Cam2")
    host.stop_stream("Camera")                                    # no fault from any of it
    host.register_camera("Camera")                                # re-registering keeps the callable
    assert host.stream("Camera").is_recording is not None


def test_a_recording_stream_that_is_not_running_does_not_block_anyone(host):
    host.register_camera("Camera", lambda: True)
    host.register_camera("Cam2", lambda: True)
    start(host, "Cam2")
    host.stop_stream("Cam2")
    start(host, "Camera")                                         # "recording" but not running: nothing to lose
    host.stop_stream("Camera")


# ---------------------------------------------------------------- overflow
def test_buffer_overflow_is_a_recorded_fault_not_silent_loss(adapter_dir):
    """1 MB buffer = 2 frames of 512 KB, 1 ms exposure, a stalled consumer (plan 4.5, P0)."""
    h = MMCoreHost("ovf", config_file=DEMO_CONFIG, adapter_paths=(adapter_dir,), circular_buffer_mb=1)
    h.connect()
    try:
        make_demo_cameras(h, exposure_ms=1)
        start(h, "Camera")
        stall_drain(h, 0.2)
        wait_until(lambda: h.snapshot()["streams"]["Camera"]["fault"], message="overflow fault")

        stats = h.snapshot()["streams"]["Camera"]
        assert stats["fault"]["kind"] == StreamFaultKind.OVERFLOW.value
        assert stats["streaming"] is False                   # MMCore ended it (stopOnOverflow)
        assert h.call("isSequenceRunning", "Camera") is False
        assert stats["frames_received"] >= 1                 # what was in the buffer was drained, not lost

        with pytest.raises(FramesLostError, match="overflow"):
            h.pop_frames("Camera")
        with pytest.raises(FramesLostError) as caught:
            h.stop_stream("Camera")
        assert caught.value.fault.kind is StreamFaultKind.OVERFLOW
        assert h.snapshot()["streams"]["Camera"]["fault"] is None     # consumed by raising it
        h.pop_frames("Camera")                                       # readable again
    finally:
        h.shutdown()


def test_a_pending_fault_blocks_a_restart_until_consumed(adapter_dir):
    h = MMCoreHost("ovf2", config_file=DEMO_CONFIG, adapter_paths=(adapter_dir,), circular_buffer_mb=1)
    h.connect()
    try:
        make_demo_cameras(h, exposure_ms=1)
        start(h, "Camera")
        stall_drain(h, 0.2)
        wait_until(lambda: h.snapshot()["streams"]["Camera"]["fault"], message="overflow fault")
        with pytest.raises(FramesLostError):
            start(h, "Camera")
        fault = h.acknowledge_fault("Camera")
        assert fault.kind is StreamFaultKind.OVERFLOW
        h.flush_stream("Camera")
        h.call("setExposure", "Camera", 100)                # slow enough not to overflow again
        assert start(h, "Camera") is True
        h.stop_stream("Camera")
    finally:
        h.shutdown()


def test_a_consumer_that_never_pops_is_bounded_and_faulted(host, monkeypatch):
    frame_cost = FRAME_SIDE * FRAME_SIDE * 2 + host._frame_overhead_bytes
    budget = 3 * frame_cost
    monkeypatch.setattr(_core, "queue_budget_bytes", lambda: budget)
    start(host, "Camera")
    wait_until(lambda: host.snapshot()["streams"]["Camera"]["frames_dropped_consumer"] >= 2,
               message="consumer-side drops")
    stats = host.snapshot()["streams"]["Camera"]
    assert stats["queued_bytes"] <= budget and stats["queued_frames"] == 3
    assert stats["fault"]["kind"] == StreamFaultKind.CONSUMER_OVERFLOW.value
    assert host.stream("Camera").streaming is True             # MMCore side is fine
    with pytest.raises(FramesLostError, match="queue"):
        host.pop_frames("Camera")
    with pytest.raises(FramesLostError):
        host.stop_stream("Camera")
    newest = [r.image_number for r in host.pop_records("Camera")]
    assert newest == sorted(newest)                            # the oldest were evicted, not the newest


def test_image_number_gap_is_detected(host):
    start(host, "Camera")
    collect_records(host, "Camera", 1)
    stream = host.stream("Camera")
    with host.lock:
        stream.expected_image_number -= 3                      # as if three frames never arrived
    wait_until(lambda: host.snapshot()["streams"]["Camera"]["fault"], message="gap fault")
    stats = host.snapshot()["streams"]["Camera"]
    assert stats["fault"]["kind"] == StreamFaultKind.GAP.value and stats["frames_lost"] == 3
    with pytest.raises(FramesLostError, match="dropped"):
        host.stop_stream("Camera")


def test_a_sequence_that_ends_unrequested_is_a_fault(host):
    start(host, "Camera")
    collect_records(host, "Camera", 1)
    with host.lock:                                            # stop it behind the host's back
        host.core.stopSequenceAcquisition("Camera")
    wait_until(lambda: host.snapshot()["streams"]["Camera"]["fault"], message="unrequested stop")
    assert host.snapshot()["streams"]["Camera"]["fault"]["kind"] == StreamFaultKind.STOPPED_UNREQUESTED.value
    with pytest.raises(FramesLostError, match="without being stopped"):
        host.stop_stream("Camera")


class _LoggedCore:
    """The host's core with a call log: everything is delegated to the real core."""

    def __init__(self, core, log, overflowed):
        self._core, self._log, self._overflowed = core, log, overflowed

    def __getattr__(self, name):
        return getattr(self._core, name)

    def isSequenceRunning(self, *args):
        self._log.append("running?")
        return self._core.isSequenceRunning(*args)

    def isBufferOverflowed(self):
        self._log.append("overflowed?")
        return self._overflowed

    def popNextImageAndMD(self):
        self._log.append("pop")
        return self._core.popNextImageAndMD()


@pytest.mark.parametrize("overflowed, kind", [(True, StreamFaultKind.OVERFLOW),
                                              (False, StreamFaultKind.STOPPED_UNREQUESTED)])
def test_drain_reads_whether_the_sequence_ended_before_it_pops(host, monkeypatch, overflowed, kind):
    """A stopped sequence cannot add frames, so what is read BEFORE the pops is complete after
    them. Read after the loop, a frame could land between the last pop and the check and the
    fault would be reported with that frame still in MMCore's buffer. Pinned by call order, and
    by every frame being queued by the time the fault is recorded."""
    start(host, "Camera")
    log, queued_at_fault = [], []
    real_core = host._core
    with host.lock:                                               # the drain thread cannot pop
        wait_until(lambda: host.core.getRemainingImageCount() >= 5, message="5 frames in the buffer")
        host.core.stopSequenceAcquisition("Camera")               # ended behind the host's back
        in_buffer = host.core.getRemainingImageCount()
        real_record = host._record_fault

        def record(stream, fault_kind, message):
            queued_at_fault.append(len(stream.queue))
            real_record(stream, fault_kind, message)

        monkeypatch.setattr(host, "_record_fault", record)
        host._core = _LoggedCore(real_core, log, overflowed)
        try:
            host.drain_now()
        finally:
            host._core = real_core
            monkeypatch.undo()
    assert log.count("pop") >= in_buffer
    first_pop = log.index("pop")
    assert "running?" in log[:first_pop] and "overflowed?" in log[:first_pop]
    assert "running?" not in log[first_pop:] and "overflowed?" not in log[first_pop:]
    stats = host.snapshot()["streams"]["Camera"]
    assert stats["frames_received"] == in_buffer and stats["queued_frames"] == in_buffer
    assert queued_at_fault == [in_buffer]                         # the fault came after the last frame
    assert stats["fault"]["kind"] == kind.value
    with pytest.raises(FramesLostError):
        host.stop_stream("Camera")


def test_frames_of_an_unregistered_camera_are_counted_not_routed(host):
    host.load_device("Cam3", "DemoCamera", "DCam")
    host.call("setProperty", "Cam3", "OnCameraCCDXSize", FRAME_SIDE)
    host.call("setProperty", "Cam3", "OnCameraCCDYSize", FRAME_SIDE)
    host.call("setProperty", "Cam3", "PixelType", "16bit")
    start(host, "Camera")
    with host.lock:
        host.core.startSequenceAcquisition("Cam3", 3, 0, True)   # someone bypassing the host
    wait_until(lambda: host.snapshot()["unrouted_frames"].get("Cam3", 0) >= 1, message="unrouted frame")
    host.stop_stream("Camera")


# -------------------------------------------------------------------- flush
def test_flush_empties_only_that_cameras_queue(host):
    start(host, "Camera")
    start(host, "Cam2")
    wait_until(lambda: all(host.snapshot()["streams"][c]["queued_frames"] >= 3 for c in CAMERAS),
               message="frames in both queues")
    host.flush_stream("Camera")
    snap = host.snapshot()["streams"]
    assert snap["Camera"]["queued_frames"] == 0 and snap["Camera"]["queued_bytes"] == 0
    assert snap["Cam2"]["queued_frames"] >= 3
    assert host.stream("Camera").streaming and host.stream("Cam2").streaming
    for label in CAMERAS:
        host.stop_stream(label)
    assert host.snapshot()["streams"]["Cam2"]["fault"] is None


def test_the_mmcore_buffer_is_cleared_only_when_no_stream_runs(host, monkeypatch):
    calls = []
    original = host.core.clearCircularBuffer
    monkeypatch.setattr(host.core, "clearCircularBuffer", lambda: (calls.append(1), original())[1],
                        raising=False)
    start(host, "Camera")
    host.flush_stream("Camera")
    assert calls == []                                        # a stream runs: the buffer is not touched
    host.stop_stream("Camera")
    host.flush_stream("Camera")
    assert calls == [1]
    assert host.snapshot()["streams"]["Camera"]["queued_frames"] == 0


def test_unknown_and_unregistered_cameras_are_reported(host):
    with pytest.raises(MMCoreHostError, match="not registered"):
        start(host, "Nope")
    with pytest.raises(MMCoreHostError, match="not registered"):
        host.pop_frames("Nope")


def test_calls_are_serialised_by_the_lock(host):
    start(host, "Camera")
    errors = []

    def hammer():
        try:
            for _ in range(50):
                host.call("getExposure", "Camera")
                host.drain_now()
        except Exception as exc:                  # noqa: BLE001 - reported through the list
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(DEADLINE_S)
    assert errors == []
    host.stop_stream("Camera")
    assert host.snapshot()["streams"]["Camera"]["fault"] is None
