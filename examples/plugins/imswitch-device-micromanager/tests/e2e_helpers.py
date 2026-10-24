"""Boot ImControl headlessly on the plugin's demo setup template, and drive it.

Shared by ``test_e2e_app.py`` (in this pytest process) and ``e2e_child.py`` (a
child interpreter, for the exit test). Nothing here is a mock: the app is the
real ``ImConMainController`` / ``MasterController``, the hub, camera and stages
are the plugin's managers on the real MMCore with the ``mm-test-adapters`` demo
devices, and a recording goes through the real ``RecordingManager``.

Only the napari/vispy canvas is replaced (as in ``imswitch/test_no_hardware_ui_smoke.py``
and the scripting tutorial runner): it cannot get an OpenGL context offscreen
and takes the interpreter down trying.

**User files.** Importing ``imswitch`` syncs ``~/ImSwitchConfig`` and the app
reads and writes widget state, recordings' defaults and the options there. The
child process gets a scratch ``HOME`` before it imports anything; in the pytest
process ``imswitch`` may already be imported, so :func:`boot` moves the
user-files folder under a scratch directory instead (and ``conftest.py`` sets
``HOME`` before any import when it can). On Windows ``HOME`` cannot move the
Documents folder: run these tests on POSIX.
"""
import json
import threading
import time
from importlib import resources
from pathlib import Path

TEMPLATE = "setup_templates/demo_scope.json"
SETUP_FILE_NAME = "demo_scope.json"
DEADLINE_S = 30.0


def load_template_text() -> str:
    return (resources.files("imswitch_device_micromanager") / TEMPLATE).read_text(encoding="utf-8")


class BootedApp:
    """The running app: ``app`` (QApplication), ``view`` (ImConMainView), ``controller``."""

    def __init__(self, app, view, controller):
        self.app = app
        self.view = view
        self.controller = controller

    @property
    def api(self):
        return self.controller.api

    @property
    def master(self):
        return self.controller.controllers["Recording"]._master

    @property
    def hub(self):
        return self.master.rs232sManager["MMCore"]

    def pump(self, seconds: float = 0.0) -> None:
        """Process Qt events for ``seconds`` (once if 0)."""
        end = time.monotonic() + seconds
        while True:
            self.app.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.005)

    def wait_until(self, predicate, message: str, timeout: float = DEADLINE_S):
        """Poll ``predicate`` while the Qt event loop runs; a timeout is a failure."""
        end = time.monotonic() + timeout
        while True:
            self.app.processEvents()
            value = predicate()
            if value:
                return value
            if time.monotonic() > end:
                raise AssertionError(f"timed out after {timeout} s waiting for {message}")
            time.sleep(0.005)

    def close(self) -> None:
        self.view.close()
        self.pump()


def install_gui_stubs() -> None:
    from imswitch.test_no_hardware_ui_smoke import _install_gui_dependency_stubs

    _install_gui_dependency_stubs(matplotlib=False)


def boot(scratch: Path, *, move_user_files: bool) -> BootedApp:
    """Build ImControl on the demo template. ``scratch`` holds the (moved) user files."""
    install_gui_stubs()

    from imswitch import imcontrol
    from imswitch.imcommon import prepareApp
    from imswitch.imcommon.controller import ModuleCommunicationChannel
    from imswitch.imcommon.model import dirtools
    from imswitch.imcontrol.controller.ImConMainController import ImConMainController
    from imswitch.imcontrol.model import Options
    from imswitch.imcontrol.view import ViewSetupInfo

    if move_user_files:
        user_root = scratch / "ImSwitchConfig"
        dirtools._baseUserFilesDir = str(user_root)
        dirtools.UserFileDirs.Root = str(user_root)
        dirtools.UserFileDirs.Config = str(user_root / "config")

    # Modal boxes nobody answers offscreen: "Save the current widget state?" on
    # close and restore warnings on startup.
    ImConMainController._shouldSaveWidgetStateOnClose = lambda self: False
    ImConMainController._showWidgetStateRestoreWarnings = lambda self, *args, **kwargs: None

    setup_info = ViewSetupInfo.from_json(load_template_text(), infer_missing=True)
    app = prepareApp()
    channel = ModuleCommunicationChannel()
    channel.register(imcontrol)
    view, controller = imcontrol.getMainViewAndController(
        channel, overrideSetupInfo=setup_info, overrideOptions=Options(setupFileName=SETUP_FILE_NAME))
    booted = BootedApp(app, view, controller)
    booted.pump()
    return booted


def record_frames(booted: BootedApp, folder: Path, name: str, count: int) -> Path:
    """Record ``count`` frames of ``Camera`` to HDF5 through the Recording controller; return the file."""
    api = booted.api
    api.setRecFolder(str(folder))
    api.setSpecifyFileName(True)
    api.setRecFilename(name)
    api.setRecFileFormat("HDF5")
    api.setDetectorToRecord("Camera")
    api.setRecModeSpecFrames(count)
    api.startRecording()
    booted.wait_until(lambda: not api.isRecording(), f"the {count}-frame recording to end")
    booted.pump(0.3)                                     # the writer thread's terminal signals land
    files = sorted(Path(folder).glob(f"{name}*.hdf5"))
    if len(files) != 1:
        raise AssertionError(f"expected one recording file {name}*.hdf5 in {folder}, found {files}")
    return files[0]


def read_recording(path: Path):
    """``(data, committed, complete)`` of the Camera dataset in a recorded HDF5 file."""
    import h5py
    import numpy as np

    with h5py.File(path, "r") as h5:
        group = h5["Camera"]
        return np.asarray(group["data"]), int(group["frames_committed"][0]), int(group["stream_complete"][0])


def drain_threads():
    return [t for t in threading.enumerate() if t.name.startswith("mmcore-drain-") and t.is_alive()]


def template_json() -> dict:
    return json.loads(load_template_text())
