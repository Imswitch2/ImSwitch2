"""MicroManagerCameraManager against the real MMCore and the DemoCamera adapters (no mocks)."""
import threading

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo, RS232Info
from imswitch.imcontrol.model.devices.graph import DeviceRelationKind, DeviceRole
from imswitch.imcontrol.model.devices.status import (
    DeviceConnectionState, DeviceFailureKind, DeviceNotConnectedError, DeviceRuntimeMode,
)
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    CAMERA_PIXEL_SIZE_PARAM, CAMERA_PIXEL_SIZE_SOURCE_PARAM, ChunkKind, PIXEL_SIZE_SOURCE_DEFAULT,
    PIXEL_SIZE_SOURCE_SETUP, PIXEL_SIZE_SOURCE_USER, RawFrameUnavailableError,
)
from imswitch_device_micromanager import _core as _core_module
from imswitch_device_micromanager import camera as camera_module
from imswitch_device_micromanager._core import (
    FormatNotVerifiableError, FramesLostError, FrameShapeMismatchError, MMCoreHostError, StreamFaultKind,
)
from imswitch_device_micromanager._frames import UnsupportedPixelTypeError
from imswitch_device_micromanager.camera import (
    ABSENT_FULL_SHAPE, PIXEL_SIZE_SOURCE_MICROMANAGER, MicroManagerCameraManager,
)
from imswitch_device_micromanager.core import MicroManagerCoreManager
from mm_helpers import DEADLINE_S, DEMO_CONFIG, FRAME_SIDE, TEST_EXPOSURE_MS, wait_until

LABEL = "Camera"


# ------------------------------------------------------------------- helpers
def make_hub(adapter_dir, **properties):
    properties.setdefault("adapterPaths", [adapter_dir])
    properties.setdefault("configFile", DEMO_CONFIG)
    return MicroManagerCoreManager(
        RS232Info(managerName="micromanager.core", managerProperties=properties), "MMCore")


def detector_info(**properties):
    properties.setdefault("rs232device", "MMCore")
    properties.setdefault("label", LABEL)
    return DetectorInfo(managerName="micromanager.camera", managerProperties=properties, forAcquisition=True)


def make_camera(hub, name="Cam", **properties):
    return MicroManagerCameraManager(detector_info(**properties), name, rs232sManager={"MMCore": hub})


@pytest.fixture
def hub(adapter_dir):
    h = make_hub(adapter_dir)
    yield h
    h.finalize()


@pytest.fixture
def camera(hub):
    cam = make_camera(hub)
    cam.setParameter("Exposure", TEST_EXPOSURE_MS)
    yield cam
    cam.finalize()


def frames_within_deadline(read, count=1, deadline=DEADLINE_S):
    """Poll ``read()`` (returns a list of frames) until ``count`` frames arrived."""
    frames = []

    def enough():
        frames.extend(read())
        return len(frames) >= count

    wait_until(enough, deadline, f"{count} frame(s)")
    return frames


# ------------------------------------------------------------------------ init
def test_init_from_a_cfg_label(camera):
    assert camera.model == "DemoCamera/DCam"
    assert camera.fullShape == (FRAME_SIDE, FRAME_SIDE)
    assert camera.shape == (FRAME_SIDE, FRAME_SIDE) and camera.frameStart == (0, 0)
    assert camera.supportedBinnings == [1, 2, 4, 8]
    assert camera.binning == 1
    assert camera.croppable and camera.forAcquisition and camera.isScanDriven is False
    assert camera.connectionState is DeviceConnectionState.CONNECTED
    assert camera.backendIsReal
    assert camera.label == LABEL


def test_init_parameters_have_exposure_properties_and_pixel_size(camera):
    parameters = camera.parameters
    assert parameters["Exposure"].valueUnits == "ms" and parameters["Exposure"].group == "Timings"
    assert parameters["Exposure"].value == TEST_EXPOSURE_MS
    assert parameters["Gain"].group == "Camera properties"
    assert parameters["PixelType"].value == "16bit" and "8bit" in parameters["PixelType"].options
    assert "Binning" not in parameters                     # first-class: binning / supportedBinnings / setBinning
    assert CAMERA_PIXEL_SIZE_PARAM in parameters and CAMERA_PIXEL_SIZE_SOURCE_PARAM in parameters


def test_pixel_size_comes_from_micromanagers_pixel_size_config_when_the_setup_omits_it(camera):
    assert camera.pixelSizeUm == [1.0, 1.0, 1.0]           # MMConfig_demo's Res10x
    source = camera.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM]
    assert source.value == PIXEL_SIZE_SOURCE_MICROMANAGER and source.value in source.options
    assert CAMERA_PIXEL_SIZE_PARAM in camera.configOwnedParameters   # MM's config wins over a saved state


def test_pixel_size_is_divided_by_the_binning(hub):
    hub.host.call("setProperty", LABEL, "Binning", 2)      # MMCore reports 2.0 um for the binned pixel
    assert hub.host.call("getPixelSizeUm") == 2.0
    cam = make_camera(hub)
    try:
        assert cam.parameters[CAMERA_PIXEL_SIZE_PARAM].value == 1.0     # the parameter is the unbinned value
        assert cam.pixelSizeUm[1] == 2.0                                # a recorded pixel is two unbinned ones
        assert cam.binning == 2 and cam.shape == (FRAME_SIDE // 2, FRAME_SIDE // 2)
        assert cam.fullShape == (FRAME_SIDE // 2, FRAME_SIDE // 2)
    finally:
        cam.finalize()


@pytest.mark.parametrize("setup_value", [None, 0.25])
def test_pixel_size_um_is_the_size_of_a_recorded_pixel_so_it_follows_the_binning(hub, setup_value):
    """The ``Camera pixel size`` parameter stays the UNBINNED sample-plane value (setup file, user or
    Micro-Manager); ``pixelSizeUm`` / ``scale`` -- what recording metadata and napari use -- are
    multiplied by the binning because one recorded pixel covers b x b sensor pixels."""
    cam = make_camera(hub, **({} if setup_value is None else {"cameraPixelSizeUm": setup_value}))
    try:
        unbinned = cam.parameters[CAMERA_PIXEL_SIZE_PARAM].value
        assert unbinned == (1.0 if setup_value is None else setup_value)
        assert cam.pixelSizeUm == [1.0, unbinned, unbinned]
        for binning in (2, 4, 1):
            cam.setBinning(binning)
            assert cam.parameters[CAMERA_PIXEL_SIZE_PARAM].value == unbinned
            assert cam.pixelSizeUm == [1.0, binning * unbinned, binning * unbinned]
            assert cam.scale == [binning * unbinned, binning * unbinned]
        cam.setParameter(CAMERA_PIXEL_SIZE_PARAM, 0.5)                        # the user states the unbinned size
        cam.setBinning(2)
        assert cam.pixelSizeUm == [1.0, 1.0, 1.0]
    finally:
        cam.finalize()


def test_a_setup_pixel_size_wins(hub):
    cam = make_camera(hub, cameraPixelSizeUm=0.25)
    try:
        assert cam.pixelSizeUm[1] == 0.25
        assert cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_SETUP
        assert PIXEL_SIZE_SOURCE_MICROMANAGER not in cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].options
        assert CAMERA_PIXEL_SIZE_PARAM in cam.configOwnedParameters
    finally:
        cam.finalize()


def test_no_pixel_size_anywhere_falls_back_to_the_placeholder(hub):
    hub.host.call("setState", "Objective", 5)              # a state without a pixel-size config: 0.0
    assert hub.host.call("getPixelSizeUm") == 0.0
    cam = make_camera(hub)
    try:
        assert cam.parameters[CAMERA_PIXEL_SIZE_PARAM].value == 0.15      # makeCameraPixelSizeParameter's default
        assert cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_DEFAULT
        assert CAMERA_PIXEL_SIZE_PARAM not in cam.configOwnedParameters
    finally:
        cam.finalize()


def test_editing_the_micromanager_pixel_size_marks_it_as_the_users(camera):
    camera.setParameter(CAMERA_PIXEL_SIZE_PARAM, 0.5)
    assert camera.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_USER
    assert camera.pixelSizeUm[1] == 0.5


def test_inline_mode_loads_the_device_and_finalize_unloads_it(hub):
    cam = make_camera(hub, label="Inline", adapter="DemoCamera", device="DCam",
                      preInitProperties={"MaximumExposureMs": 5000})
    try:
        assert "Inline" in hub.host.call("getLoadedDevices")
        assert hub.host.call("getProperty", "Inline", "MaximumExposureMs") == "5000.0000"
        assert cam.model == "DemoCamera/DCam"
    finally:
        cam.finalize()
    assert "Inline" not in hub.host.call("getLoadedDevices")
    assert cam.connectionState is DeviceConnectionState.DISCONNECTED


def test_inline_device_that_the_cfg_already_loaded_is_refused(hub):
    with pytest.raises(ValueError, match="already loaded"):
        make_camera(hub, adapter="DemoCamera", device="DCam")      # label 'Camera' is in the cfg


def test_inline_needs_adapter_and_device_together(hub):
    with pytest.raises(ValueError, match="both"):
        make_camera(hub, label="Inline", adapter="DemoCamera")


def test_missing_label_error_lists_the_camera_labels(hub):
    hub.host.load_device("Cam2", "DemoCamera", "DCam")
    with pytest.raises(ValueError) as caught:
        make_camera(hub, label="Camra")
    message = str(caught.value)
    assert "'Camra'" in message and "Did you mean 'Camera'" in message
    assert "Camera" in message and "Cam2" in message and "Z" not in message.split("Loaded camera devices:")[1]


def test_a_label_that_is_not_a_camera_is_refused_naming_its_type(hub):
    with pytest.raises(ValueError, match=r"'Z' is a Stage, not a Camera"):
        make_camera(hub, label="Z")


def test_a_device_that_fails_to_initialise_inline_leaves_nothing_loaded(hub):
    with pytest.raises(Exception):
        make_camera(hub, label="Bad", adapter="DemoCamera", device="DCam",
                    preInitProperties={"NoSuchProperty": 1})
    assert "Bad" not in hub.host.call("getLoadedDevices")


def test_an_inline_device_that_is_not_a_camera_is_refused_and_unloaded(hub):
    with pytest.raises(ValueError, match=r"'Stage' is a Stage, not a Camera"):
        make_camera(hub, label="Stage", adapter="DemoCamera", device="DStage")
    assert "Stage" not in hub.host.call("getLoadedDevices")


def test_a_failure_after_the_inline_load_unloads_the_device(hub):
    info = DetectorInfo(managerName="micromanager.camera", forAcquisition=False,        # DetectorManager refuses this
                        managerProperties={"rs232device": "MMCore", "label": "Inline",
                                           "adapter": "DemoCamera", "device": "DCam"})
    with pytest.raises(ValueError, match="forAcquisition"):
        MicroManagerCameraManager(info, "Cam", rs232sManager={"MMCore": hub})
    assert "Inline" not in hub.host.call("getLoadedDevices")


def test_colour_camera_from_the_cfg_is_refused(hub):
    hub.host.load_device("Cam2", "DemoCamera", "DCam")
    hub.host.call("setProperty", "Cam2", "PixelType", "32bitRGB")
    with pytest.raises(UnsupportedPixelTypeError, match="mono cameras only"):
        make_camera(hub, label="Cam2")


def test_hub_lookup_errors_name_the_setup_fields(hub):
    with pytest.raises(ValueError, match=r"rs232device.*'Nope'.*rs232devices"):
        MicroManagerCameraManager(detector_info(rs232device="Nope"), "Cam", rs232sManager={"MMCore": hub})
    with pytest.raises(ValueError, match=r"rs232device.*not a Micro-Manager hub"):
        MicroManagerCameraManager(detector_info(), "Cam", rs232sManager={"MMCore": object()})
    with pytest.raises(ValueError, match="rs232device"):
        MicroManagerCameraManager(detector_info(), "Cam")
    with pytest.raises(ValueError, match="managerProperties.label"):
        MicroManagerCameraManager(
            DetectorInfo(managerName="micromanager.camera", managerProperties={"rs232device": "MMCore"},
                         forAcquisition=True), "Cam", rs232sManager={"MMCore": hub})


# --------------------------------------------------------------- hub is down
def test_a_camera_is_built_absent_when_the_hub_is_not_connected(adapter_dir, tmp_path):
    hub = make_hub(adapter_dir, configFile=str(tmp_path / "nope.cfg"))
    try:
        cam = make_camera(hub)
        assert cam.connectionState is DeviceConnectionState.ERROR
        assert cam.connectionFailureKind is DeviceFailureKind.DEVICE_NOT_FOUND     # the hub's kind
        assert "nope.cfg" in cam.connectionStatusDetails
        assert not cam.backendIsReal and not cam.isUsable
        assert cam.fullShape == ABSENT_FULL_SHAPE and cam.supportedBinnings == [1]
        # The Settings tree is built from these at start-up: the groups the camera will have are declared,
        # the exposure as a read-only placeholder (changed from "no Exposure parameter at all").
        assert CAMERA_PIXEL_SIZE_PARAM in cam.parameters
        assert cam.parameters["Exposure"].group == "Timings" and cam.parameters["Exposure"].editable is False
        assert "Gain" not in cam.parameters
        with pytest.raises(DeviceNotConnectedError):
            cam.setParameter("Exposure", 5)
        with pytest.raises(DeviceNotConnectedError):
            cam.startAcquisition()
        assert cam.getChunk() == []
        cam.stopAcquisition()                                  # nothing runs: not an error
        cam.finalize()
    finally:
        hub.finalize()


def test_a_camera_built_while_the_hub_was_down_adopts_the_device_on_reconnect(adapter_dir, tmp_path):
    cfg = tmp_path / "late.cfg"
    hub = make_hub(adapter_dir, configFile=str(cfg))
    try:
        cam = make_camera(hub)
        assert cam.connectionState is DeviceConnectionState.ERROR
        placeholder_exposure = cam.parameters["Exposure"]
        cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
        assert hub.reconnectTransport() is True
        assert cam._onTransportReconnected(True) == []
        assert cam.connectionState is DeviceConnectionState.CONNECTED and cam.backendIsReal
        assert cam.model == "DemoCamera/DCam" and cam.fullShape == (FRAME_SIDE, FRAME_SIDE)
        assert cam.supportedBinnings == [1, 2, 4, 8]
        assert "Exposure" in cam.parameters and "Gain" in cam.parameters
        assert cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_MICROMANAGER
        assert cam.pixelSizeUm[1] == 1.0
        assert cam.parameters["Exposure"] is placeholder_exposure       # updated in place: the tree row stays valid
        assert placeholder_exposure.editable is True and placeholder_exposure.value == 10.0   # the .cfg's exposure
        assert cam.parameters["Exposure"].group == "Timings"
        cam.setParameter("Exposure", TEST_EXPOSURE_MS)
        assert hub.host.call("getExposure", LABEL) == TEST_EXPOSURE_MS
        cam.startAcquisition()
        assert frames_within_deadline(cam.getChunk)[0].shape == (FRAME_SIDE, FRAME_SIDE)
        cam.stopAcquisition()
        cam.finalize()
    finally:
        hub.finalize()


def test_a_transient_camera_stays_absent_until_connected(hub):
    info = DetectorInfo(managerName="micromanager.camera", forAcquisition=True, transient=True,
                        managerProperties={"rs232device": "MMCore", "label": LABEL})
    cam = MicroManagerCameraManager(info, "Cam", rs232sManager={"MMCore": hub})
    try:
        assert cam.runtimeMode is DeviceRuntimeMode.ABSENT and not cam.backendIsReal
        result = cam.getDeviceLifecycle().connect()
        assert result.success, result
        assert cam.backendIsReal and cam.model == "DemoCamera/DCam" and "Exposure" in cam.parameters
    finally:
        cam.finalize()


# ----------------------------------------------------------------- parameters
def test_exposure_is_applied_and_read_back(camera, hub):
    camera.setParameter("Exposure", 20)
    assert hub.host.call("getExposure", LABEL) == 20.0
    assert camera.parameters["Exposure"].value == 20.0


def test_exposure_stores_what_the_camera_took_not_what_was_asked(camera, caplog):
    camera.setParameter("Exposure", 1e9)                   # beyond MaximumExposureMs: the demo camera ignores it
    assert camera.parameters["Exposure"].value == TEST_EXPOSURE_MS
    assert any("the camera took" in r.getMessage() for r in caplog.records)


def test_list_property_round_trips(camera, hub):
    camera.setParameter("Mode", "Noise")
    assert hub.host.call("getProperty", LABEL, "Mode") == "Noise"
    assert camera.parameters["Mode"].value == "Noise"


def test_number_property_round_trips_typed(camera, hub):
    camera.setParameter("Gain", 3)
    camera.setParameter("BeadSize", 4.5)
    assert hub.host.call("getProperty", LABEL, "Gain") == "3"
    assert camera.parameters["Gain"].value == 3 and isinstance(camera.parameters["Gain"].value, int)
    assert camera.parameters["BeadSize"].value == 4.5


def test_value_micromanager_refuses_raises_the_micromanager_error_and_keeps_the_cache(camera):
    with pytest.raises(RuntimeError, match='Cannot set property "Gain"'):
        camera.setParameter("Gain", 100)
    assert camera.parameters["Gain"].value == 0
    with pytest.raises(ValueError, match="not one of the allowed values"):
        camera.setParameter("Mode", "Sparkles")
    with pytest.raises(ValueError, match="read-only"):
        camera.setParameter("CameraName", "x")


def test_unknown_parameter_raises_attribute_error(camera):
    with pytest.raises(AttributeError):
        camera.setParameter("NoSuchParameter", 1)


def test_dependent_parameters_are_refreshed_after_a_write(camera):
    assert camera.parameters["BitDepth"].value == "16"
    camera.setParameter("BitDepth", "12")
    assert camera.parameters["BitDepth"].value == "12"


# ------------------------------------------------------------ dtype and binning
def test_dtype_is_known_before_any_frame_and_follows_pixel_type(camera):
    assert camera.dtype == np.uint16 and camera.bitDepth == 16
    camera.setParameter("PixelType", "8bit")
    assert camera.dtype == np.uint8 and camera.bitDepth == 8
    camera.startAcquisition()
    frames = frames_within_deadline(camera.getChunk)
    camera.stopAcquisition()
    assert frames[0].dtype == np.uint8 and frames[0].shape == (FRAME_SIDE, FRAME_SIDE)


def test_a_pixel_type_change_while_streaming_restarts_the_stream_with_the_new_format(camera):
    camera.startAcquisition()
    frames_within_deadline(camera.getChunk)
    camera.setParameter("PixelType", "8bit")
    assert camera._host.stream(LABEL).streaming
    wait_until(lambda: any(f.dtype == np.uint8 for f in camera.getChunk()), message="8 bit frames after the restart")
    camera.stopAcquisition()                               # no fault from the restart


def test_switching_to_a_colour_pixel_type_is_refused_and_undone(camera, hub):
    camera.startAcquisition()
    with pytest.raises(UnsupportedPixelTypeError, match="mono cameras only"):
        camera.setParameter("PixelType", "32bitRGB")
    assert hub.host.call("getProperty", LABEL, "PixelType") == "16bit"
    assert camera.parameters["PixelType"].value == "16bit" and camera.dtype == np.uint16
    assert camera._host.stream(LABEL).streaming            # restarted with the old format
    frames_within_deadline(camera.getChunk)
    camera.stopAcquisition()


def test_set_binning_halves_shape_and_frames(camera):
    camera.setBinning(2)
    assert camera.binning == 2
    assert camera.shape == (FRAME_SIDE // 2, FRAME_SIDE // 2) and camera.fullShape == camera.shape
    camera.startAcquisition()
    frames = frames_within_deadline(camera.getChunk)
    camera.stopAcquisition()
    assert frames[0].shape == (FRAME_SIDE // 2, FRAME_SIDE // 2)
    camera.setBinning(1)
    assert camera.shape == (FRAME_SIDE, FRAME_SIDE) and camera.fullShape == (FRAME_SIDE, FRAME_SIDE)


def test_set_binning_while_streaming_restarts_the_stream(camera):
    camera.startAcquisition()
    frames_within_deadline(camera.getChunk)
    camera.setBinning(4)
    side = FRAME_SIDE // 4
    wait_until(lambda: any(f.shape == (side, side) for f in camera.getChunk()), message="binned frames")
    camera.stopAcquisition()


def test_set_binning_rescales_a_crop_the_way_the_adapter_does(camera):
    camera.crop(16, 8, 128, 64)
    camera.setBinning(2)
    x, y = camera.frameStart
    w, h = camera.shape
    assert (w, h) == (64, 32) and (x, y) == (8, 4)         # DemoCamera rescales the ROI with the binning
    assert camera._host.call("getROI", LABEL) == [8, 4, 64, 32]


def test_unsupported_binning_is_refused(camera):
    with pytest.raises(ValueError, match="not supported"):
        camera.setBinning(3)


# ----------------------------------------------------------------- acquisition
def test_start_getchunk_stop(camera):
    camera.startAcquisition()
    frames = frames_within_deadline(camera.getChunk, 20)
    camera.stopAcquisition()
    assert isinstance(frames, list)
    assert all(f.shape == (FRAME_SIDE, FRAME_SIDE) and f.dtype == np.uint16 for f in frames)
    assert camera.getChunk() == [] or all(isinstance(f, np.ndarray) for f in camera.getChunk())


def test_frames_stay_readable_after_stop(camera):
    camera.startAcquisition()
    frames_within_deadline(camera.getChunk)
    camera.stopAcquisition()
    camera.flushBuffers()
    assert camera.getChunk() == []


def test_get_latest_frame_without_consumers_is_the_newest_frame(camera):
    black = camera.getLatestFrame()
    assert black.shape == (FRAME_SIDE, FRAME_SIDE) and black.dtype == np.uint16 and not black.any()
    with pytest.raises(RawFrameUnavailableError):
        camera.getLatestFrame(is_save=True)               # nothing real to save yet
    camera.startAcquisition()
    wait_until(lambda: camera.getLatestFrame().any(), message="a live frame")
    assert camera.getLatestFrame(is_save=True).any()
    camera.stopAcquisition()
    assert camera.getLatestFrame().any()                  # the last frame is kept for the viewer


def test_live_view_survives_a_recording_that_drains_the_chunk_path(camera):
    """The TIS pitfall: live view read the deque a recording had just emptied and went black."""
    camera.startChunkConsumer("recording")
    camera.startAcquisition()
    received, stop = [], threading.Event()
    problems = []

    def recorder():
        try:
            while not stop.is_set():
                received.extend(camera.readChunk("recording"))
                stop.wait(0.001)
        except Exception as exc:                          # noqa: BLE001 - surfaced by the assertion below
            problems.append(exc)

    thread = threading.Thread(target=recorder)
    thread.start()
    try:
        direct, shared = [], []
        for _ in range(60):
            direct.append(camera.getLatestFrame())        # direct call, as EtSnouty/Tiling make it
            shared.append(camera.getLatestFrameShared())  # the broker's path, as live view makes it
            stop.wait(0.005)
        assert len(received) > 0
    finally:
        stop.set()
        thread.join(DEADLINE_S)
    camera.stopAcquisition()
    received.extend(camera.readChunk("recording"))
    camera.releaseChunkConsumer("recording")
    assert not problems
    assert all(frame.shape == (FRAME_SIDE, FRAME_SIDE) for frame in direct + shared if frame.size)
    assert direct[-1].any() and shared[-1].any()
    assert sum(1 for frame in direct if frame.any()) > len(direct) // 2      # never went black while recording


def test_a_direct_get_latest_frame_does_not_steal_frames_from_the_recording(camera):
    camera.startChunkConsumer("recording")
    camera.startAcquisition()
    received = []
    deadline = threading.Event()
    wait_until(lambda: camera.getLatestFrame().any(), message="a frame")
    for _ in range(100):                                  # hammer the direct path ...
        camera.getLatestFrame()
        deadline.wait(0.002)
    camera.stopAcquisition()
    received.extend(camera.readChunk("recording"))        # ... then read what the recording owns
    received.extend(camera.readChunk("recording"))
    delivered = camera._host.snapshot()["streams"][LABEL]["frames_received"]
    camera.releaseChunkConsumer("recording")
    assert len(received) == delivered                     # every frame the camera made reached the recording


def test_two_consumers_each_see_every_frame(camera):
    camera.startChunkConsumer("a")
    camera.startChunkConsumer("b")
    camera.startAcquisition()
    got_a = frames_within_deadline(lambda: camera.readChunk("a"), 10)
    camera.stopAcquisition()
    got_a.extend(camera.readChunk("a"))
    got_b = camera.readChunk("b")
    assert len(got_b) == len(got_a)


def test_live_view_follows_a_crop(camera):
    camera.crop(0, 0, 128, 64)
    camera.startAcquisition()
    wait_until(lambda: camera.getLatestFrame().shape == (64, 128) and camera.getLatestFrame().any(),
               message="a cropped live frame")
    camera.stopAcquisition()


# ------------------------------------------------------------------------ crop
def test_crop_sets_origin_shape_and_the_next_frames(camera):
    camera.crop(0, 0, 256, 128)
    assert camera.frameStart == (0, 0) and camera.shape == (256, 128)
    assert camera.fullShape == (FRAME_SIDE, FRAME_SIDE)
    camera.startAcquisition()
    frames = frames_within_deadline(camera.getChunk)
    camera.stopAcquisition()
    assert frames[0].shape == (128, 256)                  # numpy (height, width) vs ImSwitch (width, height)


def test_crop_with_an_offset(camera):
    camera.crop(16, 24, 100, 50)
    assert camera.frameStart == (16, 24) and camera.shape == (100, 50)
    camera.startAcquisition()
    assert frames_within_deadline(camera.getChunk)[0].shape == (50, 100)
    camera.stopAcquisition()


def test_crop_while_streaming_keeps_streaming_with_the_new_shape(camera):
    camera.startAcquisition()
    frames_within_deadline(camera.getChunk)
    camera.crop(0, 0, 128, 128)
    assert camera._host.stream(LABEL).streaming
    wait_until(lambda: any(f.shape == (128, 128) for f in camera.getChunk()), message="cropped frames")
    camera.stopAcquisition()


def test_crop_reports_the_applied_roi_and_warns_when_the_adapter_snapped(camera, caplog):
    applied = (4, 4, 100, 50)
    camera._host.set_roi = lambda label, *roi: applied   # an adapter that snaps every request
    camera.crop(5, 5, 101, 51)
    assert camera.frameStart == (4, 4) and camera.shape == (100, 50)
    assert any("snapped" in record.getMessage() for record in caplog.records)


def test_crop_back_to_the_full_frame(camera):
    camera.crop(10, 10, 64, 64)
    camera.crop(0, 0, FRAME_SIDE, FRAME_SIDE)
    assert camera.frameStart == (0, 0) and camera.shape == (FRAME_SIDE, FRAME_SIDE)


def test_start_acquisition_picks_up_changes_made_behind_the_managers_back(camera):
    """The manager caches ROI, shape and dtype; whoever changed them directly through the core
    while the camera was idle must not leave the stream started with the stale format."""
    host = camera._host
    with host.lock:
        host.core.setROI(LABEL, 10, 6, 64, 32)
        host.core.setProperty(LABEL, "PixelType", "8bit")
    assert camera.shape == (FRAME_SIDE, FRAME_SIDE) and camera.dtype == np.uint16     # not yet known
    camera.startAcquisition()
    assert camera.shape == (64, 32) and camera.frameStart == (10, 6)
    assert camera.dtype == np.uint8
    stream = host.stream(LABEL)
    assert stream.shape == (32, 64) and stream.dtype == np.uint8
    frames = frames_within_deadline(camera.getChunk)
    camera.stopAcquisition()                              # a stale format would be a frame-format fault
    assert frames[0].shape == (32, 64) and frames[0].dtype == np.uint8


def test_a_second_camera_with_a_different_size_cannot_start_next_to_a_running_one(hub):
    hub.host.load_device("Cam2", "DemoCamera", "DCam")
    first, second = make_camera(hub, name="A"), make_camera(hub, name="B", label="Cam2")
    try:
        second.crop(0, 0, 128, 128)
        first.startAcquisition()
        with pytest.raises(FrameShapeMismatchError, match="PR #936"):
            second.startAcquisition()
        with pytest.raises(Exception, match="stream"):
            second.setBinning(2)                           # would also change the shared buffer's frame size
        first.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


# --------------------------------------------------------------------- overflow
def test_forced_overflow_makes_stop_acquisition_raise(adapter_dir):
    hub = make_hub(adapter_dir, circularBufferMB=1)       # 1 MB = two 512 KB frames
    cam = make_camera(hub)
    try:
        cam.setParameter("Exposure", 1)
        cam.startAcquisition()
        with cam._host.lock:                              # a consumer that stalls: the drain thread cannot pop
            threading.Event().wait(0.2)
        wait_until(lambda: cam._host.snapshot()["streams"][LABEL]["fault"], message="overflow fault")
        with pytest.raises(FramesLostError, match="overflow"):
            cam.getChunk()                                # a recording's next read fails loudly ...
        with pytest.raises(FramesLostError, match="overflow"):
            cam.stopAcquisition()                         # ... and so does the stop (the lease table's fault)
        assert cam.getChunk() is not None                 # consumed: readable again
        cam.getLatestFrame()                              # live view never raises the stream fault
    finally:
        cam.finalize()
        hub.finalize()


def test_live_view_logs_a_stream_fault_once_and_keeps_the_last_frame(adapter_dir, caplog):
    hub = make_hub(adapter_dir, circularBufferMB=1)
    cam = make_camera(hub)
    try:
        cam.setParameter("Exposure", 1)
        cam.startAcquisition()
        wait_until(lambda: cam.getLatestFrame().any(), message="a frame")
        with cam._host.lock:
            threading.Event().wait(0.2)
        wait_until(lambda: cam._host.snapshot()["streams"][LABEL]["fault"], message="overflow fault")
        caplog.clear()
        for _ in range(5):
            assert cam.getLatestFrame().shape == (FRAME_SIDE, FRAME_SIDE)
        assert sum("Live view" in r.getMessage() for r in caplog.records) == 1
        with pytest.raises(FramesLostError):
            cam.stopAcquisition()
    finally:
        cam.finalize()
        hub.finalize()


# ------------------------------------------------- one buffer: no change while another camera records
def _two_cameras(hub):
    """Two cameras of one format (the buffer needs it; PR #936): the .cfg's and an inline one."""
    hub.host.load_device("Cam2", "DemoCamera", "DCam")
    hub.host.call("setProperty", "Cam2", "PixelType", "16bit")
    return make_camera(hub, name="A"), make_camera(hub, name="B", label="Cam2")


def _record(cam):
    """What the recording does to a camera: a lease, then a RAW chunk consumer."""
    cam._acquisitionLeased = True
    cam.startChunkConsumer("recording", kind=ChunkKind.RAW)


def _stop_recording(cam):
    cam.releaseChunkConsumer("recording")
    cam._acquisitionLeased = False


def test_a_fault_found_while_changing_a_setting_neither_loses_the_change_nor_stalls_the_recording(adapter_dir):
    """The sequence overflowed and the drain thread had not noticed yet when a property was
    written: the stop finds the fault. The change must still be applied, the stream restarted,
    the caller told, and the recording's next getChunk / stopAcquisition must raise it too
    (the lease table faults the detector from stopAcquisition) instead of reading [] forever."""
    hub = make_hub(adapter_dir, circularBufferMB=1)
    cam = make_camera(hub)
    try:
        cam.setParameter("Exposure", 1)
        cam.startAcquisition()
        with cam._host.lock:                              # the drain thread cannot notice the overflow ...
            threading.Event().wait(0.2)
            assert cam._host.stream(LABEL).streaming and cam._host.stream(LABEL).fault is None
            with pytest.raises(FramesLostError, match="overflow"):
                cam.setParameter("Mode", "Noise")         # ... so the settings change's stop finds it
        assert hub.host.call("getProperty", LABEL, "Mode") == "Noise"     # the change was applied
        assert cam.parameters["Mode"].value == "Noise" and cam._edited["Mode"] == "Noise"
        assert cam._host.stream(LABEL).streaming          # restarted: not left stopped and silent
        with pytest.raises(FramesLostError, match="overflow"):
            cam.getChunk()                                # the recording's next read fails ...
        with pytest.raises(FramesLostError) as info:
            cam.stopAcquisition()                         # ... and so does its stop (the lease table's fault)
        assert info.value.fault.kind is StreamFaultKind.OVERFLOW
        assert not cam._host.stream(LABEL).streaming
    finally:
        cam.finalize()
        hub.finalize()


def test_a_fault_recorded_before_a_settings_change_stays_pending(adapter_dir):
    hub = make_hub(adapter_dir, circularBufferMB=1)
    cam = make_camera(hub)
    try:
        cam.setParameter("Exposure", 1)
        cam.startAcquisition()
        with cam._host.lock:
            threading.Event().wait(0.2)
        wait_until(lambda: cam._host.snapshot()["streams"][LABEL]["fault"], message="overflow fault")
        cam.setParameter("Mode", "Noise")                 # nothing runs: applied, no restart, no second raise
        assert hub.host.call("getProperty", LABEL, "Mode") == "Noise"
        assert not cam._host.stream(LABEL).streaming
        with pytest.raises(FramesLostError, match="overflow"):
            cam.getChunk()
        with pytest.raises(FramesLostError, match="overflow"):
            cam.stopAcquisition()
    finally:
        cam.finalize()
        hub.finalize()


def test_a_settings_change_is_refused_before_it_stops_a_stream_that_could_not_restart(hub):
    first, second = _two_cameras(hub)
    try:
        first.startAcquisition()
        second.startAcquisition()                         # both stream (equal format) before anyone records
        first._acquisitionLeased = True
        first.startChunkConsumer("recording", kind=ChunkKind.RAW)
        with pytest.raises(MMCoreHostError, match="recording"):
            second.setParameter("PixelType", "8bit")      # would stop B and could not restart it
        assert hub.host.stream("Cam2").streaming
        assert hub.host.call("getProperty", "Cam2", "PixelType") == "16bit"
        first.releaseChunkConsumer("recording")
        first._acquisitionLeased = False
        first.stopAcquisition()
        second.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_camera_cannot_start_or_crop_while_another_records(hub):
    first, second = _two_cameras(hub)
    try:
        first.startAcquisition()
        _record(first)
        with pytest.raises(MMCoreHostError, match=r"recording.*#936"):
            second.startAcquisition()
        with pytest.raises(MMCoreHostError, match="recording"):
            second.crop(0, 0, FRAME_SIDE, FRAME_SIDE)     # setROI would clear the recording's buffer too
        assert not hub.host.stream("Cam2").streaming
        frames_within_deadline(first.getChunk, count=3)   # the recording went on untouched
        _stop_recording(first)
        second.startAcquisition()                         # the recording is over: allowed again
        second.stopAcquisition()
        first.stopAcquisition()                           # no fault: nothing was lost
    finally:
        first.finalize()
        second.finalize()


def test_live_view_and_the_start_of_a_recording_do_not_count_as_recording(hub):
    """A recording leases all its cameras in one acquire before it registers its consumers, and
    live view consumes DISPLAY chunks: neither may block the other camera."""
    first, second = _two_cameras(hub)
    try:
        first._acquisitionLeased = True                   # leased, no consumer yet: the acquire window
        first.startAcquisition()
        second.startAcquisition()
        second.stopAcquisition()
        first.startChunkConsumer("viewer", kind=ChunkKind.DISPLAY)
        second.startAcquisition()                         # a viewer is not a recording
        second.stopAcquisition()
        first.releaseChunkConsumer("viewer")
        first._acquisitionLeased = False
        first.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_live_view_says_in_the_status_that_frames_were_lost(adapter_dir):
    """A stale live image looks alive. The status stays CONNECTED (commands are not refused) but its
    summary and details name the lost frames, until the stream is stopped (the fault is raised) or restarted."""
    hub = make_hub(adapter_dir, circularBufferMB=1)
    cam = make_camera(hub)
    try:
        cam.setParameter("Exposure", 1)
        cam.startAcquisition()
        wait_until(lambda: cam.getLatestFrame().any(), message="a frame")
        assert cam.statusSnapshot()[3] is None                        # healthy: no details
        with cam._host.lock:
            threading.Event().wait(0.2)
        wait_until(lambda: cam._host.snapshot()["streams"][LABEL]["fault"], message="overflow fault")
        assert cam.getLatestFrame().shape == (FRAME_SIDE, FRAME_SIDE)  # still returns the last frame ...
        state, _mode, summary, details, failure_kind = cam.statusSnapshot()
        assert state is DeviceConnectionState.CONNECTED and failure_kind is None
        assert "lost frames" in summary and "overflow" in details      # ... and says so
        assert cam.isUsable
        with pytest.raises(FramesLostError):
            cam.stopAcquisition()
        state, _mode, summary, details, _kind = cam.statusSnapshot()
        assert state is DeviceConnectionState.CONNECTED and details is None and "lost" not in summary
    finally:
        cam.finalize()
        hub.finalize()


# ------------------------------------------------------------- lifecycle / graph
def test_descriptor_depends_on_the_hub_transport(camera):
    spec = camera.getDeviceDescriptorSpec()
    assert spec.role is DeviceRole.PRIMARY and spec.category == "detector"
    (dependency,) = spec.dependencies
    assert dependency.kind is DeviceRelationKind.USES_TRANSPORT
    assert (dependency.target.kind, dependency.target.name) == ("rs232", "MMCore")


def test_safe_state_stops_a_running_stream(camera):
    camera.startAcquisition()
    assert camera._lifecycleSafeState(verified=False) == []
    assert not camera._host.stream(LABEL).streaming
    assert camera._lifecycleSafeState(verified=True) == []          # idempotent


def test_reconnect_reapplies_exposure_roi_binning_and_properties(camera, hub):
    camera.setParameter("Exposure", 30)
    camera.setParameter("Mode", "Noise")
    camera.setParameter("PixelType", "8bit")
    camera.setBinning(2)
    camera.crop(8, 8, 64, 32)
    expected = (camera.frameStart, camera.shape, camera.fullShape, camera.dtype, camera.binning)

    assert camera._lifecycleSafeState(verified=False) == []
    assert hub.reconnectTransport() is True
    assert hub.host.call("getExposure", LABEL) == 10.0                    # the .cfg value: a fresh core
    assert camera._onTransportReconnected(True) == []

    assert (camera.frameStart, camera.shape, camera.fullShape, camera.dtype, camera.binning) == expected
    assert hub.host.call("getExposure", LABEL) == 30.0
    assert hub.host.call("getProperty", LABEL, "Mode") == "Noise"
    assert hub.host.call("getROI", LABEL) == [8, 8, 64, 32]
    assert camera.connectionState is DeviceConnectionState.CONNECTED
    camera.startAcquisition()
    frames = frames_within_deadline(camera.getChunk)
    camera.stopAcquisition()
    assert frames[0].shape == (32, 64) and frames[0].dtype == np.uint8


def test_reconnect_restarts_a_stream_that_was_running(camera, hub):
    camera.startAcquisition()
    camera._lifecycleSafeState(verified=False)
    assert hub.reconnectTransport() is True
    assert camera._onTransportReconnected(True) == []
    assert camera._host.stream(LABEL).streaming
    frames_within_deadline(camera.getChunk)
    camera.stopAcquisition()


def test_reconnect_reloads_an_inline_device(hub):
    cam = make_camera(hub, label="Inline", adapter="DemoCamera", device="DCam")
    try:
        cam.setParameter("Exposure", 25)
        assert hub.reconnectTransport() is True
        assert "Inline" not in hub.host.call("getLoadedDevices")
        assert cam._onTransportReconnected(True) == []
        assert "Inline" in hub.host.call("getLoadedDevices")
        assert hub.host.call("getExposure", "Inline") == 25.0
    finally:
        cam.finalize()


def test_reconnect_without_a_real_transport_returns_an_error(camera):
    errors = camera._onTransportReconnected(False)
    assert errors and isinstance(errors[0], str)
    assert camera.connectionState is DeviceConnectionState.ERROR


def test_reconnect_reports_a_label_the_reloaded_core_no_longer_has(camera, hub, tmp_path):
    cfg = tmp_path / "other.cfg"
    cfg.write_text("Property,Core,Initialize,0\nDevice,Other,DemoCamera,DCam\nProperty,Core,Initialize,1\n")
    hub.host.config_file = str(cfg)
    assert hub.reconnectTransport() is True
    errors = camera._onTransportReconnected(True)
    assert errors and "no camera device 'Camera'" in errors[0] and "Other" in errors[0]
    assert camera.connectionState is DeviceConnectionState.ERROR


def test_finalize_stops_the_stream_and_is_safe_twice(hub):
    cam = make_camera(hub)
    cam.startAcquisition()
    cam.finalize()
    assert not hub.host.running_streams()
    assert cam.connectionState is DeviceConnectionState.DISCONNECTED
    cam.finalize()


def test_camera_module_has_no_forbidden_core_calls():
    source = open(camera_module.__file__, encoding="utf-8").read()
    for forbidden in ("popNextTaggedImage", "clearCircularBuffer", "startSequenceAcquisition",
                      "stopSequenceAcquisition", "setROI(", "time.sleep", "_hardwareFaulted", "_acquisitionLeased",
                      ".instance()", "home("):
        assert forbidden not in source, forbidden


def test_evicted_staged_frames_are_a_fault_not_a_hole(camera, monkeypatch):
    """Plan section 4.5, "loss is an error": frames a registered consumer never read are evicted
    from the staging list when it exceeds the per-detector queue budget, and that eviction must
    fail the recording (getChunk / stopAcquisition raise once) instead of leaving a hole."""
    from imswitch_device_micromanager import camera as camera_module
    one_frame = FRAME_SIDE * FRAME_SIDE * 2 + camera_module.QUEUED_FRAME_OVERHEAD_BYTES
    monkeypatch.setattr(camera_module, "queue_budget_bytes", lambda: 3 * one_frame)
    camera.startChunkConsumer("recording")
    camera.startAcquisition()
    try:
        # Live view keeps pulling while the recording never calls getChunk: staging fills, evicts.
        wait_until(lambda: (camera.getLatestFrame().any() and camera._staged_dropped > 0),
                   message="staged frames evicted")
        with pytest.raises(FramesLostError) as info:
            camera.getChunk()
        assert info.value.fault.kind is StreamFaultKind.CONSUMER_OVERFLOW
        assert camera.getChunk() is not None                # raised once; the next call works again
        camera.flushBuffers()
        assert camera._staged_fault is None
    finally:
        try:
            camera.stopAcquisition()
        except FramesLostError:
            pass


# ------------------------------------------------- review round 3: two-camera reconnect
def _two_inline_cameras(hub):
    """Two 16-bit cameras whose devices the managers load themselves (a reload brings them back)."""
    first = make_camera(hub, name="A", label="CamA", adapter="DemoCamera", device="DCam")
    second = make_camera(hub, name="B", label="CamB", adapter="DemoCamera", device="DCam")
    for cam in (first, second):
        cam.setParameter("PixelType", "16bit")
        cam.setParameter("Exposure", TEST_EXPOSURE_MS)
    return first, second


def test_a_hub_reconnect_with_two_streaming_cameras_resumes_both_in_any_order(hub):
    """The first camera resumes its stream at once; the second one's re-initialisation must not
    need the idle-only ``sensor_geometry`` (it would clear the buffer and select the camera)."""
    for order in (("A", "B"), ("B", "A")):
        first, second = _two_inline_cameras(hub)
        cams = {"A": first, "B": second}
        try:
            first.crop(0, 0, 128, 64)
            second.crop(0, 0, 128, 64)
            first.startAcquisition()
            second.startAcquisition()
            for cam in cams.values():
                frames_within_deadline(cam.getChunk)
            for cam in cams.values():
                assert cam._lifecycleSafeState(verified=False) == []
            assert hub.reconnectTransport() is True
            for key in order:
                assert cams[key]._onTransportReconnected(True) == [], key
            assert sorted(hub.host.running_streams()) == ["CamA", "CamB"]
            for cam in cams.values():
                assert cam.connectionState is DeviceConnectionState.CONNECTED
                assert cam.shape == (128, 64) and cam.fullShape == (FRAME_SIDE, FRAME_SIDE)
                frames = frames_within_deadline(cam.getChunk, count=3)
                assert frames[-1].shape == (64, 128) and frames[-1].dtype == np.uint16
            first.stopAcquisition()
            second.stopAcquisition()
        finally:
            first.finalize()
            second.finalize()


def test_a_reconnect_reports_a_sensor_that_changed_and_the_camera_that_was_fine_still_resumes(hub):
    """A reloaded device whose sensor no longer matches what the manager reports is found. (Review round 4:
    the resume barrier keeps every stream stopped until all cameras have re-verified, so the sensor is
    re-measured on the idle core -- before, the camera beside a running stream could only be checked by
    reads and reported "ROI is now".)"""
    first, second = _two_inline_cameras(hub)
    try:
        first.startAcquisition()
        frames_within_deadline(first.getChunk)
        for cam in (first, second):
            cam._lifecycleSafeState(verified=False)
        assert hub.reconnectTransport() is True
        second._edited["OnCameraCCDXSize"] = str(FRAME_SIDE // 2)   # re-applied on the reloaded device: it comes back narrower
        assert first._onTransportReconnected(True) == []
        assert hub.host.running_streams() == []             # the barrier: nothing resumes before every camera reported
        errors = second._onTransportReconnected(True)
        assert errors and any("sensor is now" in e for e in errors), errors
        assert second.connectionState is DeviceConnectionState.ERROR
        assert hub.host.running_streams() == ["CamA"]       # the one that was fine resumed, the failed one did not
        frames_within_deadline(first.getChunk)
        first.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


# ------------------------------------------------- review round 3: format change beside a stream
def _both_stream_equal_frames(first, second):
    """Both cameras deliver 16-bit frames of the demo size; a chunk from each proves it."""
    for cam in (first, second):
        frame = frames_within_deadline(cam.getChunk)[-1]
        assert frame.shape == (FRAME_SIDE, FRAME_SIDE) and frame.dtype == np.uint16


@pytest.mark.parametrize("changing", ["first", "second"])
def test_a_pixel_type_change_beside_a_streaming_camera_is_refused_and_leaves_both_streams_alone(hub, changing):
    """With two 16-bit cameras streaming, writing 8bit to either one used to stop it (the restart was
    refused by the format guard) and, when it was MMCore's current camera, made the OTHER camera's
    frames decode wrongly. Now nothing is written."""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.startAcquisition()
        assert host.call("getCameraDevice") == LABEL           # the first one started is MMCore's current camera
        victim, other_label = (first, "Cam2") if changing == "first" else (second, LABEL)
        with pytest.raises(MMCoreHostError, match=rf"{other_label}.*#936"):
            victim.setParameter("PixelType", "8bit")
        assert host.call("getProperty", victim.label, "PixelType") == "16bit"
        assert victim.parameters["PixelType"].value == "16bit" and victim.dtype == np.uint16
        assert "PixelType" not in victim._edited
        assert sorted(host.running_streams()) == sorted([LABEL, "Cam2"])
        assert host.call("getCameraDevice") == LABEL and host.call("getBytesPerPixel") == 2
        _both_stream_equal_frames(first, second)
        first.stopAcquisition()
        second.stopAcquisition()                               # no stream fault from any of it
    finally:
        first.finalize()
        second.finalize()


def test_a_pixel_type_change_of_the_current_camera_is_refused_even_when_it_is_idle(hub):
    """The current camera that stopped while the other one streams still decides how the buffer is
    decoded: its pixel type must not change until the other stream is gone."""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.startAcquisition()
        first.stopAcquisition()                                # idle now, still MMCore's current camera
        assert host.call("getCameraDevice") == LABEL and host.running_streams() == ["Cam2"]
        with pytest.raises(MMCoreHostError, match=r"Cam2.*#936"):
            first.setParameter("PixelType", "8bit")
        assert host.call("getProperty", LABEL, "PixelType") == "16bit"
        frame = frames_within_deadline(second.getChunk)[-1]
        assert frame.dtype == np.uint16 and frame.shape == (FRAME_SIDE, FRAME_SIDE)
        second.stopAcquisition()
        first.setParameter("PixelType", "8bit")                # nothing streams any more: allowed
        assert first.dtype == np.uint8
    finally:
        first.finalize()
        second.finalize()


def test_a_pixel_type_change_of_an_idle_camera_that_is_not_current_is_allowed_beside_a_stream(hub):
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.setParameter("PixelType", "8bit")               # idle and not current: the buffer is not decoded with it
        assert host.call("getProperty", "Cam2", "PixelType") == "8bit"
        assert host.call("getBytesPerPixel") == 2
        frame = frames_within_deadline(first.getChunk)[-1]
        assert frame.dtype == np.uint16
        with pytest.raises(MMCoreHostError, match="#936"):
            second.startAcquisition()                          # it may not start with the other format
        first.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_change_that_cannot_be_predicted_is_rolled_back_when_the_format_clashes(hub):
    """``OnCameraCCDXSize`` changes the frame width and is not a pixel type: it is written, the
    format read back, and when the restart would clash the old value is put back and the stream
    restarted. (A camera that is not MMCore's current one cannot be read during a sequence, so
    for it the host's per-frame check is the guard; this is the current camera's case.)"""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.startAcquisition()
        assert "OnCameraCCDXSize" in first.parameters
        with pytest.raises(FrameShapeMismatchError, match=r"Cam2.*#936"):
            first.setParameter("OnCameraCCDXSize", FRAME_SIDE // 2)
        assert host.call("getProperty", LABEL, "OnCameraCCDXSize") == str(FRAME_SIDE)
        assert first.parameters["OnCameraCCDXSize"].value == FRAME_SIDE and "OnCameraCCDXSize" not in first._edited
        assert first.shape == (FRAME_SIDE, FRAME_SIDE)
        assert sorted(host.running_streams()) == sorted([LABEL, "Cam2"])      # restarted with the old format
        assert host.call("getCameraDevice") == LABEL and host.call("getImageWidth") == FRAME_SIDE
        _both_stream_equal_frames(first, second)
        first.stopAcquisition()
        second.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_pixel_type_change_of_a_single_streaming_camera_still_works(camera):
    camera.startAcquisition()
    frames_within_deadline(camera.getChunk)
    camera.setParameter("PixelType", "8bit")
    assert camera._host.stream(LABEL).streaming and camera.dtype == np.uint8
    wait_until(lambda: any(f.dtype == np.uint8 for f in camera.getChunk()), message="8 bit frames")
    camera.setParameter("PixelType", "16bit")
    wait_until(lambda: any(f.dtype == np.uint16 for f in camera.getChunk()), message="16 bit frames")
    camera.stopAcquisition()


# ------------------------------------------------- review round 3: live pixel-size calibration
OBJECTIVE_40X = "Nikon 40X Plan Fluor ELWD"


def _px(cam):
    return cam.parameters[CAMERA_PIXEL_SIZE_PARAM].value


def test_an_objective_change_reaches_the_micromanager_supplied_pixel_size(hub):
    cam = make_camera(hub)
    try:
        assert _px(cam) == 1.0
        hub.host.call("setConfig", "Objective", "20X")               # Res20x: 0.5 um
        assert wait_until(lambda: _px(cam) == 0.5, message="the new calibration")
        assert cam.pixelSizeUm == [1.0, 0.5, 0.5] and cam.scale == [0.5, 0.5]
        assert cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_MICROMANAGER
        assert CAMERA_PIXEL_SIZE_PARAM in cam.configOwnedParameters
        hub.host.call("setProperty", "Objective", "Label", OBJECTIVE_40X)           # a property write, no config
        assert cam.pixelSizeUm[1] == 0.25
        hub.host.call("setState", "Objective", 1)                                    # a State write: back to 10X
        assert cam.pixelSizeUm[1] == 1.0
        assert cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_MICROMANAGER
    finally:
        cam.finalize()


def test_the_live_pixel_size_stays_unbinned_and_the_recorded_pixel_follows_the_binning(hub):
    cam = make_camera(hub)
    try:
        cam.setBinning(2)
        hub.host.call("setConfig", "Objective", "20X")                # MMCore now says 1.0 um for a binned pixel
        assert hub.host.call("getPixelSizeUm") == 1.0
        assert _px(cam) == 0.5 and cam.pixelSizeUm[1] == 1.0
    finally:
        cam.finalize()


def test_refresh_parameters_rereads_the_pixel_size_even_without_the_event(hub):
    cam = make_camera(hub)
    try:
        assert _px(cam) == 1.0
        cam._disconnect_pixel_events()                                # as if the callback never came
        hub.host.call("setConfig", "Objective", "40X")
        assert cam._pixel_dirty is False and _px(cam) == 1.0
        cam.refreshParameters()
        assert _px(cam) == 0.25
    finally:
        cam.finalize()


def test_a_setup_pixel_size_wins_over_a_changed_objective_and_says_so_once(hub, caplog):
    cam = make_camera(hub, cameraPixelSizeUm=0.3)
    try:
        with caplog.at_level("WARNING"):
            hub.host.call("setConfig", "Objective", "20X")
            assert cam.pixelSizeUm[1] == 0.3 and _px(cam) == 0.3
            cam.refreshParameters()
            assert cam.pixelSizeUm[1] == 0.3
        warnings = [r.getMessage() for r in caplog.records if "pixel size changed" in r.getMessage()]
        assert len(warnings) == 1 and "setup file" in warnings[0] and "0.5" in warnings[0] and "0.3" in warnings[0]
        assert cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_SETUP
    finally:
        cam.finalize()


def test_a_user_pixel_size_wins_over_a_changed_objective(camera, hub, caplog):
    camera.setParameter(CAMERA_PIXEL_SIZE_PARAM, 0.33)
    assert camera.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_USER
    with caplog.at_level("WARNING"):
        hub.host.call("setConfig", "Objective", "20X")
        assert camera.pixelSizeUm[1] == 0.33
    assert any("you set" in r.getMessage() for r in caplog.records)
    assert camera.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_USER


def test_a_placeholder_pixel_size_gives_way_when_micromanager_gets_a_calibration(hub):
    hub.host.call("setState", "Objective", 5)                         # no pixel-size config for this state
    cam = make_camera(hub)
    try:
        assert cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_DEFAULT
        hub.host.call("setConfig", "Objective", "20X")
        assert cam.pixelSizeUm[1] == 0.5
        assert cam.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM].value == PIXEL_SIZE_SOURCE_MICROMANAGER
        assert CAMERA_PIXEL_SIZE_PARAM in cam.configOwnedParameters
    finally:
        cam.finalize()


def test_a_state_without_a_pixel_size_config_keeps_the_last_calibration_and_warns(camera, hub, caplog):
    with caplog.at_level("WARNING"):
        hub.host.call("setState", "Objective", 5)
        assert camera.pixelSizeUm[1] == 1.0
    assert any("no pixel size" in r.getMessage() for r in caplog.records)


def test_the_live_pixel_size_follows_the_objective_after_a_hub_reconnect(camera, hub):
    assert camera._lifecycleSafeState(verified=False) == []
    assert hub.reconnectTransport() is True
    assert camera._onTransportReconnected(True) == []
    hub.host.call("setConfig", "Objective", "20X")
    assert camera.pixelSizeUm[1] == 0.5


def test_a_late_adopted_camera_follows_the_objective_too(adapter_dir, tmp_path):
    cfg = tmp_path / "late.cfg"
    hub = make_hub(adapter_dir, configFile=str(cfg))
    try:
        cam = make_camera(hub)
        cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
        assert hub.reconnectTransport() is True and cam._onTransportReconnected(True) == []
        hub.host.call("setConfig", "Objective", "40X")
        assert cam.pixelSizeUm[1] == 0.25
        cam.finalize()
    finally:
        hub.finalize()


# ------------------------------------------------- review round 4: formats that cannot be verified
@pytest.mark.parametrize("value", ["32bitRGB", "64bitRGB"])
def test_a_colour_pixel_type_on_the_camera_that_is_not_current_is_refused_before_the_write(hub, value):
    """Two 16-bit cameras stream; the second is not MMCore's current camera, so its format cannot be read
    back. Colour is predictably unsupported: refused with the mono-only message, nothing written, nothing
    stopped (it used to be written, and the second camera's frames then faulted)."""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.startAcquisition()
        assert host.call("getCameraDevice") == LABEL
        with pytest.raises(UnsupportedPixelTypeError, match="mono cameras only"):
            second.setParameter("PixelType", value)
        assert host.call("getProperty", "Cam2", "PixelType") == "16bit"
        assert second.parameters["PixelType"].value == "16bit" and "PixelType" not in second._edited
        assert second.dtype == np.uint16
        assert sorted(host.running_streams()) == sorted([LABEL, "Cam2"])
        _both_stream_equal_frames(first, second)
        first.stopAcquisition()
        second.stopAcquisition()                               # no fault from any of it
    finally:
        first.finalize()
        second.finalize()


def test_an_ordinary_property_of_the_camera_that_is_not_current_is_written_beside_a_stream(hub):
    """``Gain`` cannot change the frame format; its format is verified per label (``getROI(label)`` and
    ``PixelType``) after the write, so it is allowed on the second camera while the first streams, and
    both keep streaming correct frames. (The broader round-4 rule refused it.)"""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.startAcquisition()
        assert host.call("getCameraDevice") == LABEL
        gain = float(second.parameters["Gain"].value) + 1
        second.setParameter("Gain", gain)
        assert float(host.call("getProperty", "Cam2", "Gain")) == gain
        assert sorted(host.running_streams()) == sorted([LABEL, "Cam2"])
        _both_stream_equal_frames(first, second)
        first.stopAcquisition()
        second.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_property_that_changes_the_roi_of_the_camera_that_is_not_current_is_detected_and_rolled_back(hub):
    """``OnCameraCCDXSize`` changes the frame width. For the camera that is not MMCore's current one the
    new size is read with ``getROI(label)``; it clashes with the running stream, so the old value is put
    back and both streams restart with the old format."""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.startAcquisition()
        with pytest.raises(FrameShapeMismatchError, match=r"Camera.*#936"):
            second.setParameter("OnCameraCCDXSize", FRAME_SIDE // 2)
        assert host.call("getProperty", "Cam2", "OnCameraCCDXSize") == str(FRAME_SIDE)
        assert "OnCameraCCDXSize" not in second._edited and second.shape == (FRAME_SIDE, FRAME_SIDE)
        assert second.parameters["OnCameraCCDXSize"].value == FRAME_SIDE
        assert sorted(host.running_streams()) == sorted([LABEL, "Cam2"])
        _both_stream_equal_frames(first, second)
        first.stopAcquisition()
        second.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_roi_changing_property_of_an_idle_camera_that_is_not_current_updates_its_format_and_its_start_is_refused(hub):
    """The idle camera's cached format follows the write (read per label), so the guard that runs at its
    start sees the real format instead of a stale one."""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.setParameter("OnCameraCCDXSize", FRAME_SIDE // 2)
        assert host.call("getProperty", "Cam2", "OnCameraCCDXSize") == str(FRAME_SIDE // 2)
        assert second.shape == (FRAME_SIDE // 2, FRAME_SIDE)
        with pytest.raises(FrameShapeMismatchError, match="#936"):
            second.startAcquisition()
        first.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_write_is_refused_before_it_happens_only_when_the_per_label_read_cannot_interpret_the_pixel_type(hub, monkeypatch):
    """With a PixelType wording the bridge cannot interpret (``12bit``-like), the format of a camera that is
    not MMCore's current one cannot be verified beside a stream: both a PixelType write and an ordinary
    property are refused before anything is written."""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.startAcquisition()
        monkeypatch.setattr(camera_module, "predict_pixel_type_property", lambda text: None)
        monkeypatch.setattr(_core_module, "predict_pixel_type_property", lambda text: None)
        with pytest.raises(FormatNotVerifiableError, match="cannot set PixelType"):
            second.setParameter("PixelType", "8bit")
        assert host.call("getProperty", "Cam2", "PixelType") == "16bit"
        with pytest.raises(FormatNotVerifiableError, match=r"cannot change Gain.*cannot be interpreted"):
            second.setParameter("Gain", float(second.parameters["Gain"].value) + 1)
        first.setParameter("Gain", float(first.parameters["Gain"].value) + 1)     # the current camera is read directly
        assert sorted(host.running_streams()) == sorted([LABEL, "Cam2"])
        monkeypatch.undo()
        _both_stream_equal_frames(first, second)
        first.stopAcquisition()
        second.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_an_unpredictable_property_is_written_when_nothing_else_streams(hub):
    """With the other camera stopped (second streams alone) the format is read from the core directly and the
    write stands -- as it does for the current camera beside a stream
    (see ``test_a_change_that_cannot_be_predicted_is_rolled_back_when_the_format_clashes``)."""
    first, second = _two_cameras(hub)
    host = hub.host
    try:
        first.startAcquisition()
        second.startAcquisition()
        first.stopAcquisition()
        assert host.running_streams() == ["Cam2"]
        second.setParameter("OnCameraCCDXSize", FRAME_SIDE // 2)
        assert host.call("getProperty", "Cam2", "OnCameraCCDXSize") == str(FRAME_SIDE // 2)
        assert second.shape == (FRAME_SIDE // 2, FRAME_SIDE) and host.stream("Cam2").shape == (FRAME_SIDE, FRAME_SIDE // 2)
        wait_until(lambda: any(f.shape == (FRAME_SIDE, FRAME_SIDE // 2) for f in second.getChunk()),
                   message="frames of the new width")
        second.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_colour_pixel_type_is_refused_for_a_single_camera_without_stopping_it(camera, hub):
    camera.startAcquisition()
    frames_within_deadline(camera.getChunk)
    with pytest.raises(UnsupportedPixelTypeError, match="mono cameras only"):
        camera.setParameter("PixelType", "32bitRGB")
    assert hub.host.call("getProperty", LABEL, "PixelType") == "16bit"
    assert camera._host.stream(LABEL).streaming
    camera.stopAcquisition()


# ------------------------------------------------- review round 4: the reconnect resume barrier
def _streaming_inline_pair(hub):
    first, second = _two_inline_cameras(hub)
    for cam in (first, second):
        cam.crop(0, 0, 128, 64)
        cam.startAcquisition()
    for cam in (first, second):
        frames_within_deadline(cam.getChunk)
    for cam in (first, second):
        assert cam._lifecycleSafeState(verified=False) == []
    return first, second


def test_a_camera_whose_hardware_changed_during_the_reconnect_is_refused_and_never_streams_a_wrong_frame(hub):
    """B's configuration came back 8-bit (re-applied on the reloaded device). A's hook runs first: before the
    barrier A resumed at once and B, unable to read its format beside A's stream, trusted its cached 16-bit and
    resumed too -- then its frames faulted. Now nothing resumes until B has re-verified: B's hook names the
    mismatch and B stays stopped, A streams."""
    first, second = _streaming_inline_pair(hub)
    try:
        assert hub.reconnectTransport() is True
        second._edited["PixelType"] = "8bit"
        assert first._onTransportReconnected(True) == []
        assert hub.host.running_streams() == []                      # A waits for B
        errors = second._onTransportReconnected(True)
        assert errors and any("frame format is now" in e and "uint8" in e for e in errors), errors
        assert second.connectionState is DeviceConnectionState.ERROR
        assert hub.host.running_streams() == ["CamA"]
        frames = frames_within_deadline(first.getChunk, count=3)
        assert all(f.shape == (64, 128) and f.dtype == np.uint16 for f in frames)
        assert hub.host.stream("CamB").fault is None and not hub.host.stream("CamB").streaming
        first.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_the_plain_two_camera_reconnect_resumes_both_when_the_last_one_reports_in_either_order(hub):
    for order in (("A", "B"), ("B", "A")):
        first, second = _streaming_inline_pair(hub)
        cams = {"A": first, "B": second}
        try:
            assert hub.reconnectTransport() is True
            assert hub.host.reconnect_round_open
            for position, key in enumerate(order):
                assert cams[key]._onTransportReconnected(True) == [], key
                expected = [] if position == 0 else ["CamA", "CamB"]
                assert sorted(hub.host.running_streams()) == expected
            assert not hub.host.reconnect_round_open
            for cam in cams.values():
                frames = frames_within_deadline(cam.getChunk, count=3)
                assert frames[-1].shape == (64, 128) and frames[-1].dtype == np.uint16
            first.stopAcquisition()
            second.stopAcquisition()
        finally:
            first.finalize()
            second.finalize()


def test_a_camera_that_never_reports_does_not_keep_the_others_stopped_for_good(hub):
    """Rule: a round ends when every expected camera reported, or when a camera is started or stopped
    explicitly (the ones that already reported then resume; the missing one is not waited for), and it is
    dropped without starting anything by the hub's next reconnect or by shutdown."""
    first, second = _streaming_inline_pair(hub)
    try:
        assert hub.reconnectTransport() is True
        assert first._onTransportReconnected(True) == []             # B's hook never runs
        assert hub.host.running_streams() == [] and hub.host.reconnect_round_open
        first.startAcquisition()                                     # explicit use ends the round
        assert not hub.host.reconnect_round_open
        assert hub.host.running_streams() == ["CamA"]                # A resumed; B (no device back yet) was not waited for
        frames_within_deadline(first.getChunk)
        first.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_the_next_reconnect_drops_a_round_that_never_completed_and_starts_nothing_from_it(hub):
    first, second = _streaming_inline_pair(hub)
    try:
        assert hub.reconnectTransport() is True
        assert first._onTransportReconnected(True) == []             # only A reports
        assert hub.reconnectTransport() is True                       # the next reconnect: a fresh round
        assert hub.host.running_streams() == []                      # A's old report did not start anything
        first._resume = second._resume = True                        # (the safe-state hook records this on a real reconnect)
        assert first._onTransportReconnected(True) == []
        assert second._onTransportReconnected(True) == []
        assert sorted(hub.host.running_streams()) == ["CamA", "CamB"]
        first.stopAcquisition()
        second.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_hook_that_raises_still_reports_so_the_others_resume(hub, monkeypatch):
    first, second = _streaming_inline_pair(hub)
    try:
        assert hub.reconnectTransport() is True
        monkeypatch.setattr(second, "_reinitialise", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert first._onTransportReconnected(True) == []
        errors = second._onTransportReconnected(True)
        assert errors and "boom" in errors[0]
        assert hub.host.running_streams() == ["CamA"]
        first.stopAcquisition()
    finally:
        first.finalize()
        second.finalize()


def test_a_finalized_camera_is_no_longer_awaited_by_a_round(hub):
    first, second = _streaming_inline_pair(hub)
    try:
        assert hub.reconnectTransport() is True
        second.finalize()                                            # it will never report
        assert first._onTransportReconnected(True) == []
        assert hub.host.running_streams() == ["CamA"]
        first.stopAcquisition()
    finally:
        first.finalize()
