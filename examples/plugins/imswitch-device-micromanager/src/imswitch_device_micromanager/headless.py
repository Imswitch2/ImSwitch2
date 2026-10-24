"""Boot ImControl headlessly on any setup file, and drive it: live view, recording, read-back.

This is the plugin's tests' end-to-end harness (``tests/e2e_helpers.py`` re-exports it)
and the engine of ``tools/compare_cameras.py``, which runs it against a rig. Nothing here
is a mock: the app is the real ``ImConMainController`` / ``MasterController``, the
devices are whatever managers the setup file names, and a recording goes through the real
``RecordingManager``.

Only the napari/vispy canvas is replaced (as in ``imswitch/test_no_hardware_ui_smoke.py``
and the scripting tutorial runner): it cannot get an OpenGL context offscreen and takes
the interpreter down trying.

The module imports nothing heavy at import time (no Qt, no ``imswitch``, no numpy):
every function imports what it needs when it runs, so ``import imswitch_device_micromanager.headless``
is as cheap as importing the package.

**User files.** Importing ``imswitch`` syncs ``~/ImSwitchConfig`` (the same as starting
ImSwitch2 does), and the app reads the user's options and, at startup, the saved widget
state. :func:`boot` has three switches for that:

* ``move_user_files=True`` points the user-files folder at a scratch directory (tests; in a
  child process the caller sets ``HOME`` before importing anything instead). On Windows
  ``HOME`` cannot move the Documents folder: run the tests on POSIX.
* ``move_user_files=False`` leaves ``~/ImSwitchConfig`` exactly where it is, which is what a
  rig run wants: the app still logs there like any start, but this module never saves the
  widget state on close (the "Save the current widget state?" question is answered No) and
  never writes a setup file.
* ``restore_widget_state=False`` skips the startup restore of the saved widget state, so a
  run starts from the setup file's own values and not from whatever the last session left
  (the state is keyed by detector name, so two setups that name their camera differently
  would otherwise start differently).
"""
from __future__ import annotations

import json
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Callable, Optional, Union

#: Demo setup template shipped inside the package, used when :func:`boot` is given no setup.
TEMPLATE = "setup_templates/demo_scope.json"
#: ``Options.setupFileName`` of the demo template.
SETUP_FILE_NAME = "demo_scope.json"
#: Default timeout of :meth:`BootedApp.wait_until`, seconds: generous against a loaded CI machine,
#: short against a hung test.
DEADLINE_S = 30.0
#: How long :meth:`BootedApp.wait_until` and :meth:`BootedApp.pump` let the Qt event loop run between
#: two looks at the world, seconds. Short against the fastest thing waited for (a 1 ms exposure), long
#: enough that waiting does not burn a core.
POLL_S = 0.005
#: ``manager_name`` of the hub (``rs232devices``) that :attr:`BootedApp.hub` looks for.
HUB_MANAGER_NAME = "micromanager.core"
#: The detector the e2e tests record, and the default of the functions below.
DEFAULT_DETECTOR = "Camera"
#: Settle time after a recording ends, seconds: the writer thread's terminal signals (commit counters,
#: ``stream_complete``) land on the Qt thread after ``isRecording`` turns False.
RECORDING_SETTLE_S = 0.3


def load_template_text() -> str:
    """The text of the packaged demo template."""
    return (resources.files("imswitch_device_micromanager") / TEMPLATE).read_text(encoding="utf-8")


def _spin(seconds: float) -> None:
    """Run a nested Qt event loop for ``seconds`` (no ``time.sleep``: events are processed while waiting)."""
    from qtpy import QtCore

    loop = QtCore.QEventLoop()
    timer = QtCore.QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(QtCore.Qt.PreciseTimer)        # the coarse type is only accurate to 5 %, i.e. 0.25 ms of 5 ms
    timer.timeout.connect(loop.quit)
    timer.start(max(1, int(round(seconds * 1000.0))))
    loop.exec_()
    timer.stop()


class BootedApp:
    """The running app: ``app`` (QApplication), ``view`` (ImConMainView), ``controller``, ``setup_info``."""

    def __init__(self, app, view, controller, setup_info=None, hub_name: Optional[str] = None,
                 _restore_override=None):
        self.app = app
        self.view = view
        self.controller = controller
        self.setup_info = setup_info
        self._hub_name = hub_name
        self._restore_override = _restore_override

    @property
    def api(self):
        return self.controller.api

    @property
    def master(self):
        """The ``MasterController`` (found through the Recording controller, else any controller that has one)."""
        controllers = self.controller.controllers
        if "Recording" in controllers:
            return controllers["Recording"]._master
        for controller in controllers.values():
            master = getattr(controller, "_master", None)
            if master is not None:
                return master
        raise LookupError("no controller of this app carries the MasterController")

    @property
    def hub(self):
        """The ``micromanager.core`` hub manager (``rs232sManager``).

        The name comes from ``boot(hub=...)``, else from the setup's only ``micromanager.core`` entry.
        Raises ``LookupError`` when the setup has none (a native-only setup) or has several and was not told.
        """
        name = self._hub_name or self._find_hub_name()
        return self.master.rs232sManager[name]

    def _find_hub_name(self) -> str:
        entries = getattr(self.setup_info, "rs232devices", None) or {}
        names = [n for n, info in entries.items() if getattr(info, "managerName", None) == HUB_MANAGER_NAME]
        if len(names) != 1:
            raise LookupError(
                f"the setup has {len(names)} {HUB_MANAGER_NAME!r} hubs ({names}); "
                f"name the one to use with boot(hub=...)" if names else
                f"the setup has no {HUB_MANAGER_NAME!r} hub in rs232devices")
        self._hub_name = names[0]
        return names[0]

    def has_hub(self) -> bool:
        """Whether the setup carries a ``micromanager.core`` hub that was built."""
        try:
            self.hub
        except (LookupError, KeyError):
            return False
        return True

    def detector(self, name: str = DEFAULT_DETECTOR):
        """The detector manager called ``name``."""
        return self.master.detectorsManager[name]

    def pump(self, seconds: float = 0.0) -> None:
        """Process Qt events for ``seconds`` (once if 0)."""
        end = time.monotonic() + seconds
        while True:
            self.app.processEvents()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            _spin(min(POLL_S, remaining))

    def wait_until(self, predicate: Callable[[], object], message: str, timeout: float = DEADLINE_S):
        """Poll ``predicate`` while the Qt event loop runs; a timeout is a failure."""
        end = time.monotonic() + timeout
        while True:
            self.app.processEvents()
            value = predicate()
            if value:
                return value
            if time.monotonic() > end:
                raise AssertionError(f"timed out after {timeout} s waiting for {message}")
            _spin(POLL_S)

    def close(self) -> None:
        try:
            self.view.close()
            self.pump()
        finally:
            if self._restore_override is not None:
                persistence, original = self._restore_override
                if original is None:
                    persistence.__dict__.pop("loadAllWidgetStates", None)
                else:
                    persistence.loadAllWidgetStates = original
                self._restore_override = None


def install_gui_stubs() -> None:
    """Replace napari and vispy (not matplotlib) by minimal stand-ins: they cannot get an OpenGL context offscreen."""
    from imswitch.test_no_hardware_ui_smoke import _install_gui_dependency_stubs

    _install_gui_dependency_stubs(matplotlib=False)


def boot(scratch: Optional[Path] = None, *, move_user_files: bool, setup: Union[str, Path, None] = None,
         restore_widget_state: bool = True, hub: Optional[str] = None) -> BootedApp:
    """Build ImControl on a setup file.

    ``setup`` is the path of a setup JSON file; ``None`` boots the packaged demo template. The
    file is read as given (it need not sit in ``imcontrol_setups``) and handed to the app as an
    override, so nothing is looked up or written in the user's setup folder.

    ``scratch`` holds the moved user files when ``move_user_files`` is True (see the module
    docstring for what the three switches do); it may be None otherwise. ``hub`` names the
    ``micromanager.core`` entry for :attr:`BootedApp.hub` when the setup has more than one.
    """
    install_gui_stubs()

    from imswitch import imcontrol
    from imswitch.imcommon import prepareApp
    from imswitch.imcommon.controller import ModuleCommunicationChannel
    from imswitch.imcommon.model import dirtools, getWidgetStatePersistence
    from imswitch.imcontrol.controller.ImConMainController import ImConMainController
    from imswitch.imcontrol.model import Options
    from imswitch.imcontrol.view import ViewSetupInfo

    if move_user_files:
        if scratch is None:
            raise ValueError("boot(move_user_files=True) needs a scratch directory")
        user_root = Path(scratch) / "ImSwitchConfig"
        dirtools._baseUserFilesDir = str(user_root)
        dirtools.UserFileDirs.Root = str(user_root)
        dirtools.UserFileDirs.Config = str(user_root / "config")

    # Modal boxes nobody answers offscreen: "Save the current widget state?" on
    # close and restore warnings on startup.
    ImConMainController._shouldSaveWidgetStateOnClose = lambda self: False
    ImConMainController._showWidgetStateRestoreWarnings = lambda self, *args, **kwargs: None

    restore_override = None
    if not restore_widget_state:
        persistence = getWidgetStatePersistence()
        restore_override = (persistence, persistence.__dict__.get("loadAllWidgetStates"))
        persistence.loadAllWidgetStates = lambda *args, **kwargs: 0

    if setup is None:
        text, file_name = load_template_text(), SETUP_FILE_NAME
    else:
        path = Path(setup)
        text, file_name = path.read_text(encoding="utf-8"), path.name
    setup_info = ViewSetupInfo.from_json(text, infer_missing=True)
    app = prepareApp()
    channel = ModuleCommunicationChannel()
    channel.register(imcontrol)
    view, controller = imcontrol.getMainViewAndController(
        channel, overrideSetupInfo=setup_info, overrideOptions=Options(setupFileName=file_name))
    booted = BootedApp(app, view, controller, setup_info=setup_info, hub_name=hub,
                       _restore_override=restore_override)
    booted.pump()
    return booted


def record_frames(booted: BootedApp, folder: Path, name: str, count: int, *,
                  detector: str = DEFAULT_DETECTOR, timings: Optional[dict] = None) -> Path:
    """Record ``count`` frames of ``detector`` to HDF5 through the Recording controller; return the file.

    ``timings``, if given, receives ``wall_s``: seconds from the start request to the recording
    having ended (start-up and shut-down latency of the recording included, the settle time
    after it not).
    """
    api = booted.api
    api.setRecFolder(str(folder))
    api.setSpecifyFileName(True)
    api.setRecFilename(name)
    api.setRecFileFormat("HDF5")
    api.setDetectorToRecord(detector)
    api.setRecModeSpecFrames(count)
    started = time.perf_counter()
    api.startRecording()
    booted.wait_until(lambda: not api.isRecording(), f"the {count}-frame recording to end")
    if timings is not None:
        timings["wall_s"] = time.perf_counter() - started
    booted.pump(RECORDING_SETTLE_S)
    files = sorted(Path(folder).glob(f"{name}*.hdf5"))
    if len(files) != 1:
        raise AssertionError(f"expected one recording file {name}*.hdf5 in {folder}, found {files}")
    return files[0]


def read_recording(path: Path, detector: str = DEFAULT_DETECTOR):
    """``(data, committed, complete)`` of the ``detector`` dataset in a recorded HDF5 file."""
    import h5py
    import numpy as np

    with h5py.File(path, "r") as h5:
        group = h5[detector]
        return np.asarray(group["data"]), int(group["frames_committed"][0]), int(group["stream_complete"][0])


def drain_threads():
    """Live frame-drain threads of the hub (``mmcore-drain-*``)."""
    return [t for t in threading.enumerate() if t.name.startswith("mmcore-drain-") and t.is_alive()]


def template_json() -> dict:
    """The packaged demo template as a dict."""
    return json.loads(load_template_text())
