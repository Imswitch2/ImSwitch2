"""Start the whole ImControl UI offscreen with devices made to fail.

Run as a subprocess by test_device_failure_policy.py: it needs the napari /
vispy stubs of the no-hardware smoke test, which must not leak into the test
process. Prints one ``RESULT {json}`` line.

usage: python startup_failure_probe.py <case>
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

case = sys.argv[1]
result = {"case": case, "started": False}
home = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(home)

try:
    from imswitch.test_no_hardware_ui_smoke import (
        PROFILE_PATH, _install_gui_dependency_stubs,
    )

    _install_gui_dependency_stubs()

    from imswitch import imcontrol
    from imswitch.imcommon import prepareApp
    from imswitch.imcommon.controller import ModuleCommunicationChannel
    from imswitch.imcommon.model import dirtools
    from imswitch.imcontrol.controller.ImConMainController import ImConMainController
    from imswitch.imcontrol.model import Options
    from imswitch.imcontrol.view import ViewSetupInfo

    user_root = home / "ImSwitchConfig"
    dirtools._baseUserFilesDir = user_root
    dirtools.UserFileDirs.Root = str(user_root)
    dirtools.UserFileDirs.Config = str(user_root / "config")
    ImConMainController._shouldSaveWidgetStateOnClose = lambda self: False

    data = json.loads(Path(PROFILE_PATH).read_text())
    if case == "no_detector_manager":
        for detector in data["detectors"].values():
            detector["managerName"] = "NoSuchDetectorManager"
    if case == "detector_absent":
        for detector in data["detectors"].values():
            detector["managerName"] = "AVManager"
            detector["managerProperties"] = {
                "cameraListIndex": 99, "avcam": {}, "useMockOnFailure": False}
    if case in ("detector_default", "everything_fails"):
        for detector in data["detectors"].values():
            detector["managerName"] = "AVManager"
            detector["managerProperties"] = {"cameraListIndex": 99, "avcam": {}}
    if case in ("stages_absent", "everything_fails"):
        for positioner in data["positioners"].values():
            positioner["managerName"] = "KinesisStageManager"
            positioner["managerProperties"] = {"snr": "REAL_DOES_NOT_EXIST"}

    setup = ViewSetupInfo.from_json(json.dumps(data), infer_missing=True)
    options = Options(setupFileName=Path(PROFILE_PATH).name)
    channel = ModuleCommunicationChannel()
    channel.register(imcontrol)
    app = prepareApp()
    view = None
    try:
        view, controller = imcontrol.getMainViewAndController(
            channel, overrideSetupInfo=setup, overrideOptions=options)
        app.processEvents()
        master = controller._ImConMainController__masterController
        from imswitch.imcontrol.model.devices import startupIssues

        issues = startupIssues(master.deviceSupervisor.getHardwareStatuses())
        detectors = master.detectorsManager
        result.update(
            started=True,
            issues=[i.line for i in issues],
            controllers=sorted(controller.controllers),
            detectors=detectors.getAllDeviceNames(),
            failed=sorted(detectors.getFailedDevices()),
            detector_states={i.name: i.state for i in issues if i.category == "detector"},
            stage_issues=[i.line for i in issues if i.category == "positioner"],
            notice=(view._hardwareNoticeLabel.text()
                    if view.__dict__.get("_hardwareNoticeLabel") is not None
                    and not view.statusBar().isHidden() else ""),
            mock_frame_ok=False,
        )
        if not detectors.hasDevices():
            # What the user can still do, and what each action does about it.
            actions = {}
            for label, call in (
                ("liveview", lambda: controller.controllers["View"].liveview(True)),
                ("snap", lambda: controller.controllers["Recording"].snap()),
                ("record", lambda: controller.controllers["Recording"].toggleREC(True)),
            ):
                try:
                    call()
                    app.processEvents()
                    actions[label] = "ok"
                except Exception as error:
                    actions[label] = f"{type(error).__name__}: {error}"[:200]
            result["no_detector_actions"] = actions
        if detectors.hasDevices():
            from imswitch.imcontrol.model.managers import LeasePurpose

            detector = detectors.getCurrentDetector()
            handle = detectors.acquire([detector.name], LeasePurpose.LIVE_VIEW)
            try:
                import time
                for _ in range(50):
                    frame = detector.getLatestFrame()
                    if frame is not None and getattr(frame, "size", 0):
                        result["mock_frame_ok"] = True
                        break
                    time.sleep(0.1)
            finally:
                detectors.release(handle)
    finally:
        if view is not None:
            try:
                view.close()
                app.processEvents()
            except Exception:
                pass
except Exception:
    result["error"] = traceback.format_exc()

print("RESULT " + json.dumps(result))
sys.stdout.flush()
os._exit(0)
