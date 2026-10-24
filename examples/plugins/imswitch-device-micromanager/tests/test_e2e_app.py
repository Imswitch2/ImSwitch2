"""End to end: the real ImControl app on the plugin's demo setup template, headless.

Phase 1 closes with this module (plan ``docs/design/plans/micromanager-bridge.md``
section 5, P1). The app is the real ``ImConMainController`` / ``MasterController``
built from ``setup_templates/demo_scope.json`` exactly as shipped: the hub, the
camera, the two stages, the LED light and the flip mirror come out of ``RS232sManager``, ``DetectorsManager`` and
``PositionersManager`` on the real MMCore with the ``mm-test-adapters`` demo
devices (``MMConfig_demo.cfg``), and a recording goes through the real
``RecordingManager`` into a real HDF5 file that is read back. Only the napari
canvas is replaced (no OpenGL offscreen); see ``e2e_helpers``.

The tests share ONE app, built once, and run in file order: boot, live view,
recording, stages, light, flip mirror, close. The user-files folder is moved to a scratch directory
(``HOME`` is also isolated by ``conftest.py`` when imswitch is not yet imported),
so nothing here reads or writes the real ``~/ImSwitchConfig``.

``test_interpreter_exit_after_a_full_session_is_clean`` runs the same session in
a child interpreter and lets it fall off the end: the "full MasterController
close with the real widget tree" that plan section 3 (P0 exit row) left to this
end-to-end.

Because the tests depend on each other (one shared app, file order), the module
SKIPS ITSELF, with a message, when pytest would run it out of order or in a
worker: under pytest-xdist (``PYTEST_XDIST_WORKER`` is set; ``-n`` spreads the
tests of one file over processes, each booting its own app and finding it in the
wrong state) and under pytest-randomly (it reorders the tests of a module unless
``-p no:randomly`` or ``--randomly-dont-reorganize`` is given). Run the module
serially, e.g. ``pytest -p no:randomly examples/plugins/imswitch-device-micromanager/tests/test_e2e_app.py``.
"""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import e2e_helpers as helpers

TESTS_DIR = Path(__file__).resolve().parent
FRAMES = 30
SIDE = 512
EXPOSURE_MS = 5


def _plugin_installed() -> bool:
    try:
        from imswitch.imcontrol.model.plugins.registry import get_default_registry

        return get_default_registry().resolve("rs232", "micromanager.core") is not None
    except Exception:                                   # noqa: BLE001 - the reason is reported by the fixture
        return False


@pytest.fixture(scope="module", autouse=True)
def _ordered_serial_run_only(request):
    """Skip the module when the tests would not run in file order in this process."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.skip("test_e2e_app.py shares one app between tests that run in file order; it does "
                    "not run under pytest-xdist workers (PYTEST_XDIST_WORKER is set). Run it serially.")
    config = request.config
    if config.pluginmanager.hasplugin("randomly") and config.getoption("randomly_reorganize", True):
        pytest.skip("test_e2e_app.py shares one app between tests that run in file order; "
                    "pytest-randomly would reorder them. Run it with -p no:randomly or "
                    "--randomly-dont-reorganize.")


@pytest.fixture(scope="module")
def booted(tmp_path_factory):
    """The app, booted once on the template. A missing install fails here with the reason."""
    if not _plugin_installed():
        pytest.fail("the plugin is not discoverable (its entry point is missing): run "
                    "`pip install -e examples/plugins/imswitch-device-micromanager` in this environment")
    scratch = tmp_path_factory.mktemp("e2e-app")
    app = helpers.boot(scratch, move_user_files=True)
    app.scratch = scratch
    app.closed = False
    try:
        yield app
    finally:
        if not app.closed:
            app.close()


# ------------------------------------------------------------------------ boot
def test_boot_builds_the_hub_camera_and_stages_from_the_template(booted):
    from imswitch_device_micromanager.camera import MicroManagerCameraManager
    from imswitch_device_micromanager.core import MicroManagerCoreManager
    from imswitch_device_micromanager.stage import MicroManagerStageManager

    assert {"Settings", "View", "Recording", "Image", "Positioner"} <= set(booted.view.widgets)
    assert {"Settings", "View", "Recording", "Image", "Positioner"} <= set(booted.controller.controllers)

    master = booted.master
    hub = master.rs232sManager["MMCore"]
    assert type(hub) is MicroManagerCoreManager
    assert hub.isConnected and hub.host.is_connected
    assert hub.host.has_device("Camera")

    camera = master.detectorsManager["Camera"]
    assert type(camera) is MicroManagerCameraManager
    assert camera.model == "DemoCamera/DCam" and camera.fullShape == (SIDE, SIDE)
    assert camera.pixelSizeUm[1] == 1.0                  # the template omits it: MM's Res10x supplies it

    names = sorted(master.positionersManager.getAllDeviceNames())
    assert names == ["XY", "Z"]
    for name in names:
        assert type(master.positionersManager[name]) is MicroManagerStageManager
    assert master.positionersManager["Z"].axes == ["Z"]
    assert master.positionersManager["XY"].axes == ["X", "Y"]
    assert not master.detectorsManager.isDetectorFaulted("Camera")
    assert not hub.host.running_streams()                # nothing streams before the first lease


# ------------------------------------------------------------------- live view
def test_live_view_lease_streams_frames_into_the_image_and_stops(booted):
    master, host = booted.master, booted.hub.host
    camera = master.detectorsManager["Camera"]
    camera.setParameter("Exposure", EXPOSURE_MS)

    booted.api.setLiveViewActive(True)
    booted.wait_until(lambda: host.running_streams() == ["Camera"], "the live view lease to start the stream")
    first = booted.wait_until(lambda: camera.getLatestFrame().any(), "a non-zero live frame")
    assert first
    frame = camera.getLatestFrame()
    assert frame.shape == (SIDE, SIDE) and frame.dtype == np.uint16

    # The controller's own live view path (not only this test's polling) reaches the image widget.
    layers = booted.view.widgets["Image"].imgLayers
    booted.wait_until(lambda: "Camera" in layers and np.asarray(layers["Camera"].data).any(),
                      "the live view to draw the camera frame in the Image widget")

    received = host.snapshot()["streams"]["Camera"]["frames_received"]
    booted.wait_until(lambda: host.snapshot()["streams"]["Camera"]["frames_received"] > received + 5,
                      "the stream to keep delivering")

    booted.api.setLiveViewActive(False)
    booted.wait_until(lambda: not host.running_streams(), "releasing the live view lease to stop the stream")
    assert not master.detectorsManager.isDetectorFaulted("Camera")


# ------------------------------------------------------------------- recording
def test_recording_a_fixed_frame_count_while_live_view_runs_saves_exactly_that_many(booted):
    master, host = booted.master, booted.hub.host
    camera = master.detectorsManager["Camera"]
    camera.setParameter("Exposure", EXPOSURE_MS)
    booted.api.setLiveViewActive(True)                   # a live view lease beside the recording lease
    booted.wait_until(lambda: camera.getLatestFrame().any(), "live frames before recording")

    path = helpers.record_frames(booted, booted.scratch / "recordings", "e2e", FRAMES)

    data, committed, complete = helpers.read_recording(path)
    assert data.shape == (FRAMES, SIDE, SIDE)
    assert data.dtype == np.uint16
    assert committed == FRAMES and complete == 1
    assert all(data[i].any() for i in range(FRAMES)), "a recorded frame is all zero"
    stream = host.snapshot()["streams"]["Camera"]
    assert stream["fault"] is None and stream["frames_lost"] == 0

    # Recording shares the stream with live view: the stream is still up and still delivering.
    assert host.running_streams() == ["Camera"]
    received = stream["frames_received"]
    booted.wait_until(lambda: host.snapshot()["streams"]["Camera"]["frames_received"] > received + 5,
                      "live view to keep streaming after the recording ended")

    booted.api.setLiveViewActive(False)
    booted.wait_until(lambda: not host.running_streams(), "the stream to stop with the last lease")
    assert not master.detectorsManager.isDetectorFaulted("Camera")


# ------------------------------------------------------------------- positioners
def test_stage_positions_read_back_through_the_positioners_manager(booted):
    positioners = booted.master.positionersManager
    z, xy = positioners["Z"], positioners["XY"]

    z.setPosition(12.5, "Z")
    assert z.position["Z"] == 12.5
    xy.setPosition(30.0, "X")
    assert xy.position["X"] == 30.0
    xy.setPosition(-7.5, "Y")
    assert xy.position == {"X": 30.0, "Y": -7.5}
    z.move(1.5, "Z")
    assert z.position["Z"] == 14.0

    # What the controller API reports is what the managers track, and it is the hardware's value.
    reported = booted.api.getPositionerPositions()
    assert reported["Z"]["Z"] == 14.0 and reported["XY"] == {"X": 30.0, "Y": -7.5}
    with booted.hub.host.lock:
        core = booted.hub.host.core
        assert core.getPosition("Z") == 14.0
        assert tuple(core.getXYPosition("XY")) == (30.0, -7.5)


def test_xy_is_homeable_and_references_while_the_template_z_has_nothing_to_reference(booted):
    positioners = booted.master.positionersManager
    z, xy = positioners["Z"], positioners["XY"]

    assert xy.homeable and xy.requiresReference
    assert not z.homeable and not z.requiresReference and z.isReferenced

    # The template marks XY homeable, so it starts unreferenced and the reference action homes it.
    xy.markUnreferenced()
    assert not xy.isReferenced
    xy.reference()
    assert xy.isReferenced
    with booted.hub.host.lock:
        assert tuple(xy.position[a] for a in ("X", "Y")) == tuple(booted.hub.host.core.getXYPosition("XY"))

    # Z is not marked homeable: referencing it does nothing (MM's own "Unsupported device command"
    # for a stage without a home command is raised only for a Z declared homeable; see test_stage_manager).
    before = dict(z.position)
    z.reference()
    assert z.position == before and z.isReferenced


# ------------------------------------------------------------------------ light
def test_the_template_light_is_a_binary_laser_that_opens_the_led_shutter_in_mmcore(booted):
    from imswitch_device_micromanager.light import MicroManagerLightManager

    assert "Laser" in booted.view.widgets and "Laser" in booted.controller.controllers
    led = booted.master.lasersManager["LED"]
    assert type(led) is MicroManagerLightManager
    assert led.isBinary and led.wavelength == 470 and led.hub is booted.hub
    core = booted.hub.host.core
    assert not core.getShutterOpen("LED Shutter")        # the laser controller switched it off at startup
    assert led.setEnabled(True) is True
    assert core.getShutterOpen("LED Shutter")
    led.setEnabled(False)
    assert not core.getShutterOpen("LED Shutter")


# ------------------------------------------------------------------- flip mirror
def test_the_template_flip_mirror_moves_the_white_light_shutter_in_mmcore(booted):
    from imswitch_device_micromanager.flipmirror import MicroManagerFlipMirrorManager

    assert "FlipMirror" in booted.view.widgets and "FlipMirror" in booted.controller.controllers
    mirror = booted.master.flipMirrorsManager["WhiteLight"]
    assert type(mirror) is MicroManagerFlipMirrorManager and mirror.hub is booted.hub
    assert mirror.is_connected() and mirror.get_state_names() == {0: "Closed", 1: "Open"}
    core = booted.hub.host.core
    shutter = "White Light Shutter"
    booted.api.move_flip("WhiteLight", 0)               # through the controller, as the widget's button does
    assert not core.getShutterOpen(shutter) and mirror.get_state() == 0
    mirror.move_to(1)
    assert core.getShutterOpen(shutter) and mirror.get_state() == 1 and mirror.get_cached_state() == 1
    assert booted.api.get_flip_state("WhiteLight") == 1
    booted.api.move_flip("WhiteLight", 0)
    assert not core.getShutterOpen(shutter)


def test_the_template_filter_wheel_is_a_ten_position_flip_mirror_row(booted):
    from imswitch_device_micromanager.flipmirror import MicroManagerFlipMirrorManager

    wheel = booted.master.flipMirrorsManager["Dichroic"]
    assert type(wheel) is MicroManagerFlipMirrorManager and wheel.is_connected()
    names = wheel.get_state_names()
    assert len(names) == 10 and names[0] == "400DCLP" and names[1] == "Q505LP" and names[9] == "State-9"
    core = booted.hub.host.core
    assert list(names.values()) == list(core.getStateLabels("Dichroic"))
    row = booted.view.widgets["FlipMirror"].rows["Dichroic"]
    assert row["stateCombo"].count() == 10 and row["stateCombo"].itemText(0) == "400DCLP"    # one row, ten positions
    booted.api.move_flip("Dichroic", 3)                 # through the controller, as the widget's drop-down does
    assert core.getState("Dichroic") == 3 and wheel.get_state() == 3
    assert booted.api.get_flip_state("Dichroic") == 3
    assert row["stateCombo"].currentIndex() == 3        # the widget follows the device
    booted.api.move_flip("Dichroic", 0)
    assert core.getState("Dichroic") == 0


# ------------------------------------------------------------------------ close
def test_close_finalizes_devices_first_and_the_hub_last_and_leaves_no_thread(booted):
    master, hub = booted.master, booted.hub
    host = hub.host
    camera = master.detectorsManager["Camera"]
    stages = [master.positionersManager["Z"], master.positionersManager["XY"]]
    mirror = master.flipMirrorsManager["WhiteLight"]

    order = []

    def spy(name, obj, method):
        original = getattr(obj, method)

        def wrapper(*args, **kwargs):
            order.append(name)
            return original(*args, **kwargs)

        setattr(obj, method, wrapper)

    spy("camera", camera, "finalize")
    spy("Z", stages[0], "finalize")
    spy("XY", stages[1], "finalize")
    spy("flip", mirror, "finalize")
    spy("hub", hub, "finalize")
    spy("host.shutdown", host, "shutdown")

    booted.api.setLiveViewActive(True)                   # close with a live stream and its drain thread running
    booted.wait_until(lambda: host.running_streams() == ["Camera"], "a stream to close under")
    assert helpers.drain_threads(), "no drain thread while streaming"

    booted.app.processEvents()
    booted.close()
    booted.closed = True

    # MasterController closes detectors, positioners ... then rs232sManager: the hub is last, and its
    # finalize ends in the host shutdown (drain thread stopped, devices unloaded).
    assert sorted(order[:4]) == ["XY", "Z", "camera", "flip"], order
    assert order[4:] == ["hub", "host.shutdown"], order

    assert hub.host.is_connected is False
    assert not helpers.drain_threads()
    assert host.snapshot()["drain_thread_alive"] is False
    assert master._managerFinalizationCompleted(master.rs232sManager)


# --------------------------------------------------------------- interpreter exit
def test_interpreter_exit_after_a_full_session_is_clean(tmp_path):
    """Boot, live view, record 10 frames, close, then let the interpreter exit: return code 0."""
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.update(HOME=str(home), QT_QPA_PLATFORM="offscreen", MPLBACKEND="Agg",
               PYTHONPATH=os.pathsep.join(filter(None, [str(TESTS_DIR), env.get("PYTHONPATH", "")])))
    env.pop("PYTEST_CURRENT_TEST", None)
    result = subprocess.run([sys.executable, str(TESTS_DIR / "e2e_child.py"), str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=180)
    tail = f"\n--- stdout ---\n{result.stdout[-2000:]}\n--- stderr ---\n{result.stderr[-3000:]}"
    assert result.returncode == 0, f"the child exited with {result.returncode}{tail}"
    assert "E2E-CHILD-OK 10 committed=10 complete=1" in result.stdout, tail
