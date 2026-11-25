"""The device-failure policy (docs/design/plans/device-reconnect-2.0.md, section 4.0).

* startup never crashes;
* every device that did not start is reported, never silently replaced;
* a detector that fails runs on a reported mock (the application cannot work
  without one) and can be swapped for the real camera on reconnect;
* a laser or stage that fails is absent, refuses commands with a clear
  message, and comes back on reconnect -- never a mock unless the setup asks.
"""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceNotConnectedError,
    DeviceRuntimeMode,
    DeviceSupervisor,
    formatStartupReport,
    startupIssues,
    startupNoticeText,
)
from imswitch.imcontrol.model.managers.MultiManager import MultiManager
from imswitch.imcontrol.model.SetupInfo import DetectorInfo, PositionerInfo


class _Multi(MultiManager):
    def __init__(self, infos=None, package="detectors", **low):
        super().__init__(infos or {}, package, **low)

    def put(self, name, manager):
        self._subManagers[name] = manager
        return self


def _status(name, connection, mode, category="detector", summary="", details=None):
    return SimpleNamespace(name=name, category=category, connection=connection,
                           mode=mode, summary=summary, details=details)


# ------------------------------------------------------------- the report
def test_report_lists_failures_and_fallback_mocks_only():
    statuses = [
        _status("Cam", DeviceConnectionState.ERROR, DeviceRuntimeMode.MOCK, summary="camera 3 failed"),
        _status("Stage", DeviceConnectionState.ERROR, DeviceRuntimeMode.REAL, "positioner",
                summary="Stage not connected", details="USB gone"),
        _status("Laser", DeviceConnectionState.DISCONNECTED, DeviceRuntimeMode.REAL, "laser"),
        _status("Configured mock", DeviceConnectionState.NOT_APPLICABLE, DeviceRuntimeMode.MOCK),
        _status("Declared absent", DeviceConnectionState.DISCONNECTED, DeviceRuntimeMode.ABSENT),
        _status("Fine", DeviceConnectionState.CONNECTED, DeviceRuntimeMode.REAL),
    ]
    issues = startupIssues(statuses)
    assert {(i.name, i.state) for i in issues} == {
        ("Cam", "mock"), ("Stage", "not connected"), ("Laser", "not connected")}
    text = formatStartupReport(issues)
    assert "USB gone" in text and "Cam (detector): mock" in text
    notice = startupNoticeText(issues)
    assert "2 devices not connected" in notice and "MOCK" in notice
    assert startupNoticeText(()) == ""


def test_notice_is_non_blocking_and_comes_back_only_when_the_problems_change(qtbot):
    from qtpy import QtWidgets

    from imswitch.imcontrol.view.ImConMainView import ImConMainView

    class _View(QtWidgets.QMainWindow):
        setHardwareNotice = ImConMainView.setHardwareNotice
        _dismissHardwareNotice = ImConMainView._dismissHardwareNotice

        def showHardwareStatus(self):
            pass

    view = _View()
    qtbot.addWidget(view)
    view.statusBar().hide()
    view.setHardwareNotice("Hardware: 1 device not connected.", "Cam: x")
    assert not view.statusBar().isHidden()
    view._dismissHardwareNotice()
    assert view.statusBar().isHidden()
    view.setHardwareNotice("Hardware: 1 device not connected.", "Cam: x")   # same problems
    assert view.statusBar().isHidden()
    view.setHardwareNotice("Hardware: 2 devices not connected.", "Cam: x\nY: z")
    assert not view.statusBar().isHidden()
    view.setHardwareNotice("")
    assert view.statusBar().isHidden()


# ---------------------------------------- a failed detector: reported mock
def _av(monkeypatch, *, opens, useMockOnFailure=True):
    from imswitch.imcontrol.model.managers.detectors.AVManager import AVManager

    state = {"opens": opens}

    def getAVObj(self, cameraId):
        if not state["opens"]:
            raise OSError("camera 7 not found")
        return self._getMockAVObj()   # stands in for the real camera now plugged in

    monkeypatch.setattr(AVManager, "_getAVObj", getAVObj)
    info = DetectorInfo(managerName="AVManager", forAcquisition=True, managerProperties={
        "cameraListIndex": 7, "avcam": {}, "useMockOnFailure": useMockOnFailure})
    return AVManager(info, "Cam"), state


def test_failed_detector_runs_on_a_mock_that_says_so(monkeypatch):
    camera, _ = _av(monkeypatch, opens=False)
    assert camera.runtimeMode is DeviceRuntimeMode.MOCK
    assert camera.connectionState is DeviceConnectionState.ERROR
    assert camera.isUsable                              # the application can run on it
    assert camera.model.startswith("MOCK (real camera not connected)")
    multi = _Multi().put("Cam", camera)
    issues = startupIssues(DeviceSupervisor(SimpleNamespace(detectorsManager=multi)).getHardwareStatuses())
    assert [(i.name, i.state) for i in issues] == [("Cam", "mock")]
    assert "camera 7 not found" in issues[0].line


def test_mock_detector_is_replaced_by_the_real_camera_on_reconnect(monkeypatch):
    camera, state = _av(monkeypatch, opens=False)
    state["opens"] = True
    result = camera.getDeviceLifecycle().reconnect()
    assert result.success, result.summary
    assert camera.runtimeMode is DeviceRuntimeMode.REAL
    assert camera.connectionState is DeviceConnectionState.CONNECTED
    assert not camera.model.startswith("MOCK")
    assert startupIssues(DeviceSupervisor(
        SimpleNamespace(detectorsManager=_Multi().put("Cam", camera))).getHardwareStatuses()) == ()


def test_recording_marks_frames_from_a_mock_detector(monkeypatch, tmp_path):
    from imswitch.imcontrol.model.managers.RecordingManager import TiffStorer

    mocked, _ = _av(monkeypatch, opens=False)
    real, _ = _av(monkeypatch, opens=True)
    storer = TiffStorer(str(tmp_path / "x"), {"Mocked": mocked, "Real": real})
    stamped = storer._withSimulationAttrs({"Mocked": {"a": 1}, "Real": {"a": 1}})
    assert stamped["Mocked"]["recording:detector_simulated"] == "True"
    assert "camera 7 not found" in stamped["Mocked"]["recording:detector_simulation_reason"]
    assert "mock fallback" in stamped["Mocked"]["recording:detector_simulation_reason"]
    assert stamped["Mocked"]["a"] == 1
    assert "recording:detector_simulated" not in stamped["Real"]
    # OME-TIFF carries the note too (not only HDF5 / Zarr attributes).
    assert "recording:detector_simulated" in TiffStorer._ome_annotation_attrs(stamped["Mocked"])
    # A snapshot is stamped the same way.
    import numpy as np
    assert storer._snapshot_attrs("Mocked", {}, np.zeros((2, 2)))["recording:detector_simulated"] == "True"


def test_detector_without_mock_opt_in_is_reported_not_mocked(monkeypatch):
    with pytest.raises(DeviceNotConnectedError, match="useMockOnFailure"):
        _av(monkeypatch, opens=False, useMockOnFailure=False)


# --------------------------- a device on a failed transport is not usable
class _FakeTransport:
    """What RS232Manager is, to its dependents, once its port failed."""


def test_device_on_a_failed_serial_transport_does_not_look_usable():
    from imswitch.imcontrol.model.devices import DeviceManagerStatusMixin
    from imswitch.imcontrol.model.devices.status import not_connected_reason

    class Transport(DeviceManagerStatusMixin):
        def reconnectTransport(self):
            return True

    class Dependent(DeviceManagerStatusMixin):
        name = "laser"

        def __init__(self, transport):
            self._rs232Manager = transport
            self._setConnected("ok")           # it recorded nothing wrong itself

    transport = Transport()
    transport._setConnected("port open")
    laser = Dependent(transport)
    assert laser.isUsable
    transport._setConnectionError(OSError("no such port"), summary="RS232 transport COM9 failed",
                                  mock_active=True)   # the mock port keeps dependents constructible
    assert not laser.isUsable
    with pytest.raises(DeviceNotConnectedError, match="transport: RS232 transport COM9 failed"):
        laser._requireConnected("emission")
    assert "COM9" in not_connected_reason(laser)
    transport._setConnected("port open again")   # reconnect heals the dependent with no action of its own
    assert laser.isUsable


# ------------------------------------- lasers and stages: absent, not mock
def _kinesis(name="Stage", **props):
    from imswitch.imcontrol.model.managers.positioners.KinesisStageManager import KinesisStageManager

    info = PositionerInfo(managerName="KinesisStageManager", axes=["X", "Y"], forPositioning=True,
                          managerProperties={"snr": "REAL_NOT_THERE", **props})
    return KinesisStageManager(info, name)


def test_failed_stage_is_absent_refuses_commands_and_reconnects(monkeypatch):
    from imswitch.imcontrol.model.managers.positioners import KinesisStageManager as module

    calls = {"works": False}
    moved = []

    class _Stage:
        def move_by(self, distance, channel=None):
            moved.append((distance, channel))

        def get_position(self, channel=None):
            return 0.0

        def close(self):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: 0.0

    def getStage(self, snr, scale, rack):
        if not calls["works"]:
            raise OSError("USB device not found")
        return _Stage()

    monkeypatch.setattr(module.KinesisStageManager, "_getStageObj", getStage)
    stage = _kinesis()
    assert stage.runtimeMode is DeviceRuntimeMode.REAL               # not a mock
    assert stage.connectionState is DeviceConnectionState.ERROR
    assert not stage.isUsable
    with pytest.raises(DeviceNotConnectedError):
        stage.move(5, "X")
    assert moved == []                                                # nothing reached hardware
    issues = startupIssues(DeviceSupervisor(SimpleNamespace(
        positionersManager=_Multi(package="positioners").put("Stage", stage))).getHardwareStatuses())
    assert [(i.name, i.state) for i in issues] == [("Stage", "not connected")]

    calls["works"] = True
    assert stage.getDeviceLifecycle().reconnect().success
    assert stage.isUsable and stage.connectionState is DeviceConnectionState.CONNECTED
    assert moved == []                                                # reconnect moved nothing


def test_stage_on_a_missing_rs232_device_is_not_silently_mocked():
    from imswitch.imcontrol.model.managers.positioners.MHXYStageManager import MHXYStageManager

    info = PositionerInfo(managerName="MHXYStageManager", axes=["X", "Y"], forPositioning=True,
                          managerProperties={"rs232device": "nope"})
    with pytest.raises(RuntimeError, match="'nope' is not available.*useMockOnFailure"):
        MHXYStageManager(info, "xy", rs232sManager={})


def test_stage_mock_on_a_missing_rs232_device_is_an_explicit_opt_in():
    from imswitch.imcontrol.model.managers.positioners.SQUIDStageManager import SQUIDStageManager

    info = PositionerInfo(managerName="SQUIDStageManager", axes=["X", "Y", "Z"], forPositioning=True,
                          managerProperties={"rs232device": "nope", "useMockOnFailure": True})
    stage = SQUIDStageManager(info, "squid", rs232sManager={})
    assert stage.runtimeMode is DeviceRuntimeMode.MOCK and stage.connectionState is DeviceConnectionState.ERROR
    stage.move(3, "X")


def test_grbl_laser_comes_back_dark_after_a_transport_reconnect():
    from imswitch.imcontrol.model.managers.lasers.GRBLLaserManager import GRBLLaserManager
    from imswitch.imcontrol.model.SetupInfo import LaserInfo

    sent = []

    class Board:
        def set_laser_intensity(self, value):
            sent.append(value)

        def set_led(self, value):
            sent.append(value)

    board = SimpleNamespace(_board=Board())
    info = LaserInfo(analogChannel=None, digitalLine=None, managerName="GRBLLaserManager",
                     managerProperties={"rs232device": "b", "channel": "LASER"},
                     valueRangeMin=0, valueRangeMax=1023, wavelength=488)
    laser = GRBLLaserManager(info, "laser", rs232sManager={"b": board})
    laser.setValue(500)
    laser.setEnabled(True)
    assert sent[-1] == 500
    sent.clear()
    assert laser._onTransportReconnected(True) == []
    assert laser.enabled is False and sent == [0]
    laser.setValue(600)                      # a later power change must not switch it on
    assert sent[-1] == 0


# ----------------------------------------------------- whole-application start
def _probe(case):
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MPLBACKEND="Agg", PYTHONPATH=root)
    out = subprocess.run([sys.executable, os.path.join(here, "startup_failure_probe.py"), case],
                         capture_output=True, text=True, timeout=600, env=env, cwd=root)
    lines = [line for line in out.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, out.stdout[-3000:] + out.stderr[-3000:]
    return json.loads(lines[-1][len("RESULT "):])


@pytest.mark.parametrize("case", ["no_detector_manager", "detector_absent", "detector_default",
                                  "stages_absent", "everything_fails"])
def test_application_starts_when_devices_fail(case):
    result = _probe(case)
    assert result["started"], result.get("error")
    assert result["controllers"]                       # the panels were built
    if case in ("detector_default", "everything_fails"):
        # The detectors failed; what stands in for them is a reported mock.
        assert result["detectors"] and result["detector_states"]
        assert all(state == "mock" for state in result["detector_states"].values()), result
        assert "MOCK" in result["notice"]
        assert result["mock_frame_ok"]                  # a mock acquisition runs
    if case in ("stages_absent", "everything_fails"):
        assert result["stage_issues"], result
        assert all("not connected" in line for line in result["stage_issues"]), result
    if case in ("no_detector_manager", "detector_absent"):
        # Left out and reported; the application still starts without any detector.
        assert result["detectors"] == [] and result["failed"], result
