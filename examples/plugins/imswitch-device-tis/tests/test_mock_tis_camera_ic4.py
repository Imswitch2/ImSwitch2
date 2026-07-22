"""Mock-backed tests for the IC4 TIS detector manager.

These encode the contract the legacy pyicic/IC3 path violated. The two that
matter most are ``test_each_trigger_yields_a_distinct_frame`` (the original
duplicate-frame bug) and ``test_get_chunk_is_3d`` (the shape bug that corrupted
recordings on the sibling ThorCam TSI manager). Both are written to fail loudly
if a future change reintroduces either.
"""

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch_device_tis.detectors import TISCameraIC4Manager


def _makeManager(**managerProperties):
    props = {
        'cameraSerial': 'MOCK_TIS_33UX250',
        'cameraPixelSizeUm': 0.15,
        'pixelFormat': 'Mono16',
    }
    props.update(managerProperties)
    info = DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='tis.camera-ic4',
        managerProperties=props,
        forAcquisition=True,
    )
    return TISCameraIC4Manager(info, 'TISCam')


@pytest.fixture
def manager():
    mgr = _makeManager()
    yield mgr
    mgr.finalize()


def test_initializes_against_mock(manager):
    assert 'Mock' in manager.model
    assert manager.fullShape == (2448, 2048)


def test_each_trigger_yields_a_distinct_frame(manager):
    """The original bug: every triggered position returned the same image.

    Asserting the *count* alone would not catch it — duplicates still arrive.
    Distinctness is the assertion that discriminates.
    """
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(5)

    chunk = manager.getChunk()

    assert chunk.shape[0] == 5
    unique = {frame.tobytes() for frame in chunk}
    assert len(unique) == 5, 'triggered frames must all differ'


def test_get_chunk_is_3d(manager):
    """readChunk does list.extend, which iterates axis 0 — 2-D silently
    decomposes one image into H row vectors and corrupts the recording."""
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(2)

    chunk = manager.getChunk()

    assert chunk.ndim == 3
    assert chunk.shape == (2, 2048, 2448)


def test_get_chunk_is_empty_without_triggers(manager):
    """No trigger must yield an empty chunk — never fabricated black frames,
    which would be written into a recording as real data."""
    manager.startAcquisition()

    chunk = manager.getChunk()

    assert chunk.shape == (0, 2048, 2448)
    assert chunk.ndim == 3


def test_get_chunk_drains(manager):
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(3)

    assert manager.getChunk().shape[0] == 3
    assert manager.getChunk().shape[0] == 0


def test_get_latest_frame_does_not_drain(manager):
    """Live view polls this on a timer; it must not steal frames from a
    concurrent recording pulling through readChunk."""
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(2)

    latest = manager.getLatestFrame()

    assert latest.shape == (2048, 2448)
    assert manager.getChunk().shape[0] == 2, 'getLatestFrame consumed frames'


def test_live_view_survives_a_concurrent_recording_drain(manager):
    """Live view polls getLatestFrame on a timer while a recording drains
    through readChunk. Reading 'latest' off the pending queue made every poll
    that landed after a drain return black — the live view flickered to black
    for exactly as long as a recording was running."""
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(3)
    before = manager.getLatestFrame()

    assert manager.readChunk('recording') != []

    after = manager.getLatestFrame()
    assert after.max() > 0, 'live view went black after the recording drained'
    assert np.array_equal(before, after)


def test_crop_invalidates_the_retained_live_frame(manager):
    """The retained frame has the pre-crop geometry, so it must not outlive a
    crop — handing it to the viewer would report the wrong shape."""
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(1)

    manager.crop(0, 0, 512, 256)

    assert manager.getLatestFrame().shape == (256, 512)


def test_no_frames_produced_while_not_streaming(manager):
    """Triggers arriving before startAcquisition must not queue frames."""
    manager._camera.simulate_hardware_trigger(3)

    assert manager.getChunk().shape[0] == 0


def test_queue_is_bounded_and_drops_oldest():
    mgr = _makeManager(maxQueuedFrames=4)
    try:
        mgr.startAcquisition()
        mgr._camera.simulate_hardware_trigger(10)

        chunk = mgr.getChunk()

        assert chunk.shape[0] == 4, 'queue must not grow past maxQueuedFrames'
        assert mgr._camera.dropped_frame_count == 6
    finally:
        mgr.finalize()


def test_dtype_follows_pixel_format_before_any_frame_arrives():
    """dtype is the storer's source of truth for the recording dataset, and a
    recording can start before live view has ever produced a frame. The base
    implementation would report uint16 for a Mono8 camera at that moment."""
    mono8 = _makeManager(pixelFormat='Mono8')
    mono16 = _makeManager(pixelFormat='Mono16')
    try:
        assert mono8.dtype == np.uint8
        assert mono16.dtype == np.uint16
        assert mono8.getChunk().dtype == np.uint8

        mono8.startAcquisition()
        mono8._camera.simulate_hardware_trigger(1)
        assert mono8.getChunk().dtype == np.uint8, \
            'empty and non-empty chunks must agree on dtype'
    finally:
        mono8.finalize()
        mono16.finalize()


def test_flush_buffers_discards(manager):
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(3)

    manager.flushBuffers()

    assert manager.getChunk().shape[0] == 0


def test_trigger_mode_parameter_arms_hardware_trigger(manager):
    assert manager._camera.is_trigger_enabled() is False

    manager.setParameter('Trigger Mode', 'Hardware')
    assert manager._camera.is_trigger_enabled() is True

    manager.setParameter('Trigger Mode', 'Off')
    assert manager._camera.is_trigger_enabled() is False


def test_set_trigger_enabled_keeps_parameter_in_sync(manager):
    """A scan arming the trigger directly must not desync the GUI parameter."""
    manager.setTriggerEnabled(True)

    assert manager.parameters['Trigger Mode'].value == 'Hardware'


def test_exposure_and_gain_reach_the_camera(manager):
    manager.setParameter('Exposure', 1234)
    manager.setParameter('Gain', 7)

    assert manager._camera.get_exposure_us() == 1234
    assert manager._camera.get_gain() == 7


def test_blank_camera_serial_means_first_available():
    """The config editor saves a cleared text box as "", not null. Treating that
    as a literal serial would send us looking for a camera whose serial is the
    empty string instead of opening the first one."""
    mgr = _makeManager(cameraSerial='')
    try:
        # Reached the mock without treating "" as a serial to match.
        assert mgr._camera.serial is not None
    finally:
        mgr.finalize()


def test_defaults_are_applied_at_construction():
    mgr = _makeManager(defaults={'exposure_us': 2500, 'gain': 3,
                                 'trigger_mode': 'Hardware'})
    try:
        assert mgr._camera.get_exposure_us() == 2500
        assert mgr._camera.get_gain() == 3
        assert mgr._camera.is_trigger_enabled() is True
    finally:
        mgr.finalize()


def test_crop_updates_shape_and_discards_stale_frames(manager):
    """Frames queued before a crop carry the old geometry; stacking them with
    post-crop frames in getChunk would raise on mismatched shapes."""
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(2)

    manager.crop(0, 0, 512, 256)

    assert manager.shape == (512, 256)
    assert manager.getChunk().shape == (0, 256, 512)

    manager._camera.simulate_hardware_trigger(1)
    assert manager.getChunk().shape == (1, 256, 512)


def test_crop_resumes_streaming_when_it_was_active(manager):
    manager.startAcquisition()

    manager.crop(0, 0, 512, 256)

    assert manager._camera.is_streaming is True


def test_crop_leaves_stream_stopped_when_it_was_stopped(manager):
    manager.crop(0, 0, 512, 256)

    assert manager._camera.is_streaming is False


def test_readchunk_distributes_whole_frames_to_each_consumer(manager):
    """End-to-end against the real base-class fan-out: BeadRec and recording
    poll the same detector concurrently and must each get whole (H, W) frames."""
    manager.startAcquisition()
    manager._camera.simulate_hardware_trigger(3)

    recFrames = manager.readChunk('recording')
    beadFrames = manager.readChunk('beadrec')

    assert len(recFrames) == 3
    assert all(f.shape == (2048, 2448) for f in recFrames), \
        'a 2-D getChunk would decompose each frame into H row vectors here'
    # beadrec registered after the drain, so it starts empty rather than
    # stealing recording's frames.
    assert beadFrames == []

    manager._camera.simulate_hardware_trigger(2)
    assert len(manager.readChunk('recording')) == 2
    assert len(manager.readChunk('beadrec')) == 2, 'each consumer gets its own copy'


def test_finalize_disposes_camera():
    mgr = _makeManager()
    camera = mgr._camera

    mgr.finalize()

    assert camera.is_streaming is False
    assert mgr._camera is None
