"""A hardware device that fails at startup must not take the application down.

It ends up either as a working mock (when the setup asked for one) or as a
clearly reported absent / error device, and what consumes it tolerates that.
Each test below failed before the change it pins.
"""
import logging
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

import imswitch.imcontrol.model.managers.MultiManager  # noqa: F401  (module, not the class)
from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceId,
    DeviceManagerStatusMixin,
    DeviceNotConnectedError,
    DeviceSupervisor,
    FailedDeviceManager,
)
from imswitch.imcontrol.model.managers.MultiManager import (
    ManagerImportError,
    MultiManager,
    NoSuchSubManagerError,
)
from imswitch.imcontrol.model.plugins.manifest import DeviceManagerContribution
from imswitch.imcontrol.model.plugins.registry import DevicePluginRegistry

multimanager_module = sys.modules["imswitch.imcontrol.model.managers.MultiManager"]
THIS_MODULE = "imswitch.imcontrol._test.unit.test_startup_device_failures"
CURRENT_PACKAGE = "imswitch.imcontrol.model.managers"


# ------------------------------------------------------------ MultiManager
class _Info:
    def __init__(self, managerName):
        self.managerName = managerName


class _Good:
    def __init__(self, info, name, **_low):
        self.name = name


class _Boom:
    def __init__(self, info, name, **_low):
        raise OSError("vendor DLL exploded")


class _BaseBoom:
    """Like the SmarACT SDK's StageStatusException used to be."""

    def __init__(self, info, name, **_low):
        raise _NotAnException("stage said no")


class _NotAnException(BaseException):
    pass


class _Interrupt:
    def __init__(self, info, name, **_low):
        raise KeyboardInterrupt


class _Multi(MultiManager):
    def __init__(self, infos, package, **low):
        super().__init__(infos, package, **low)


def _registry(*classes):
    registry = DevicePluginRegistry()
    for cls in classes:
        registry.register(DeviceManagerContribution(
            id=f"t.{cls.__name__}", kind="detector", display_name=cls.__name__,
            python_name=f"{THIS_MODULE}:{cls.__name__}", plugin_name="test"))
    return registry


@pytest.fixture
def patch_registry(monkeypatch):
    def _patch(registry):
        monkeypatch.setattr(multimanager_module, "get_default_registry", lambda: registry)
    return _patch


def test_one_failing_device_does_not_abort_the_group(patch_registry, caplog):
    patch_registry(_registry(_Good, _Boom))
    with caplog.at_level(logging.ERROR):
        multi = _Multi({"ok": _Info("t._Good"), "bad": _Info("t._Boom")}, "detectors")

    assert multi.getAllDeviceNames() == ["ok"]
    failed = multi.getFailedDevices()
    assert list(failed) == ["bad"]
    status = failed["bad"]
    assert status.connectionState is DeviceConnectionState.ERROR
    assert "bad" in status.connectionStatusSummary
    assert "vendor DLL exploded" in status.connectionStatusDetails
    # Device name and the traceback are in the log.
    assert any('"bad"' in r.getMessage() and r.exc_info for r in caplog.records)


def test_base_exception_from_a_vendor_sdk_is_contained_but_interrupt_is_not(patch_registry):
    patch_registry(_registry(_BaseBoom, _Interrupt))
    multi = _Multi({"x": _Info("t._BaseBoom")}, "detectors")
    assert "x" in multi.getFailedDevices()
    with pytest.raises(KeyboardInterrupt):
        _Multi({"y": _Info("t._Interrupt")}, "detectors")


def test_failed_device_reads_as_failed_not_as_unknown(patch_registry):
    patch_registry(_registry(_Boom))
    multi = _Multi({"bad": _Info("t._Boom")}, "detectors")
    with pytest.raises(NoSuchSubManagerError, match="failed to start.*vendor DLL"):
        multi.getDevice("bad")
    with pytest.raises(NoSuchSubManagerError, match="does not exist"):
        multi.getDevice("never-configured")


def test_missing_vendor_library_is_a_missing_dependency():
    status = FailedDeviceManager("cam", ImportError("No module named 'vendor'"),
                                 managerName="VendorCam")
    assert status.connectionFailureKind is DeviceFailureKind.MISSING_DEPENDENCY
    other = FailedDeviceManager("cam", OSError("nope"))
    assert other.connectionFailureKind is DeviceFailureKind.INITIALIZATION_ERROR


def test_supervisor_reports_the_failed_device_as_an_error(patch_registry):
    patch_registry(_registry(_Good, _Boom))
    multi = _Multi({"ok": _Info("t._Good"), "bad": _Info("t._Boom")}, "detectors")
    supervisor = DeviceSupervisor(SimpleNamespace(detectorsManager=multi))
    statuses = {s.name: s for s in supervisor.getAllStatuses()}
    assert statuses["bad"].connection is DeviceConnectionState.ERROR
    assert statuses["bad"].kind == "detector"
    assert "vendor DLL exploded" in statuses["bad"].details


# ---------------------------------------------- import failure vs not found
def test_import_failure_is_not_reported_as_unknown_manager(monkeypatch):
    real_import = multimanager_module.importlib.import_module

    def fake_import(path, *a, **k):
        if path.endswith(".VendorThing"):
            raise OSError("MCSControl.dll: cannot open shared object file")
        return real_import(path, *a, **k)

    monkeypatch.setattr(multimanager_module.importlib, "import_module", fake_import)
    with pytest.raises(ManagerImportError) as excinfo:
        MultiManager._importLegacyManagerClass(CURRENT_PACKAGE, "positioners", "VendorThing")
    message = str(excinfo.value)
    assert "found" in message and "failed to import" in message
    assert "OSError" in message and "MCSControl" in message
    assert "Unknown" not in message


def test_missing_vendor_module_inside_a_manager_is_a_failure_not_absence(monkeypatch):
    # The manager module exists; the vendor module it imports does not.
    real_import = multimanager_module.importlib.import_module

    def fake_import(path, *a, **k):
        if path.endswith(".VendorThing"):
            raise ModuleNotFoundError("No module named 'vendor_sdk_nope'", name="vendor_sdk_nope")
        return real_import(path, *a, **k)

    monkeypatch.setattr(multimanager_module.importlib, "import_module", fake_import)
    with pytest.raises(ManagerImportError, match="No module named 'vendor_sdk_nope'"):
        MultiManager._importLegacyManagerClass(CURRENT_PACKAGE, "positioners", "VendorThing")


def test_no_such_manager_module_still_reads_as_not_found(patch_registry):
    patch_registry(DevicePluginRegistry())
    assert MultiManager._importLegacyManagerClass(
        CURRENT_PACKAGE, "positioners", "DefinitelyNotAManager") is None


def test_a_broken_in_tree_manager_does_not_break_registry_shadow_check(patch_registry, monkeypatch):
    patch_registry(_registry(_Good))
    real_import = multimanager_module.importlib.import_module

    def fake_import(path, *a, **k):
        if path.endswith(".t._Good") or path.endswith("._Good"):
            raise OSError("broken in-tree twin")
        return real_import(path, *a, **k)

    monkeypatch.setattr(multimanager_module.importlib, "import_module", fake_import)
    assert MultiManager._resolveManagerClass(
        CURRENT_PACKAGE, "detectors", "detector", "t._Good") is _Good


# ----------------------------------------------------------- MasterController
def _master_stub():
    from imswitch.imcontrol.controller.MasterController import MasterController

    stub = SimpleNamespace(startupFailures={})
    stub._MasterController__logger = logging.getLogger("test.master")
    return MasterController, stub


def test_master_controller_builds_a_failing_single_device_soft():
    MasterController, stub = _master_stub()

    def factory(*a, **k):
        raise RuntimeError("no such serial port")

    factory.__name__ = "TeensyPulseManager"
    assert MasterController._startDevice(stub, "pulseGeneratorManager", factory) is None
    failure = stub.startupFailures["pulseGeneratorManager"]
    assert failure.connectionState is DeviceConnectionState.ERROR
    assert "no such serial port" in failure.connectionStatusDetails
    assert "TeensyPulseManager" in failure.connectionStatusSummary
    # A device that builds is returned untouched.
    assert MasterController._startDevice(stub, "x", lambda a, b=2: (a, b), 1) == (1, 2)


def test_supervisor_reports_a_failed_single_device_even_where_a_stand_in_exists():
    MasterController, stub = _master_stub()
    MasterController._startDevice(stub, "standManager", _raise(OSError("stand port busy")))
    from imswitch.imcontrol.model.managers.StandManager import StandManager

    master = SimpleNamespace(standManager=StandManager(None), startupFailures=stub.startupFailures)
    statuses = DeviceSupervisor(master).getAllStatuses()
    stand = [s for s in statuses if s.kind == "stand"]
    assert len(stand) == 1
    assert stand[0].connection is DeviceConnectionState.ERROR
    assert "stand port busy" in stand[0].details
    # The stand-in does nothing, for every consumer.
    master.standManager.motCorrPos(1.0)
    master.standManager.setFLUO()
    assert master.standManager.isUsable is False       # present, but not a stand to command


def _raise(error):
    def factory(*a, **k):
        raise error
    return factory


class _Sig:
    def connect(self, *_a, **_k):
        pass


class _Det:
    forAcquisition = True
    isScanDriven = False

    def __init__(self, info, name, **_low):
        self.name = name
        self.sigImageUpdated = _Sig()
        self.sigNewFrame = _Sig()


def test_detectors_manager_skips_a_detector_that_failed_to_start(patch_registry):
    from imswitch.imcontrol.model import DetectorsManager

    patch_registry(_registry(_Det, _Boom))
    infos = {"good": _Info("t._Det"), "bad": _Info("t._Boom")}
    manager = DetectorsManager(infos, updatePeriod=300)
    assert manager.getAllDeviceNames() == ["good"]
    assert manager.getCurrentDetectorName() == "good"
    assert list(manager.getFailedDevices()) == ["bad"]


# ------------------------------------------------------------------- Teensy
def _absent_teensy():
    from imswitch.imcontrol.model.managers.pulsegen import TeensyPulseInfo, TeensyPulseManager

    return TeensyPulseManager(TeensyPulseInfo(
        port="/dev/definitely-not-a-port", useMockOnFailure=False))


def test_absent_teensy_reports_disconnected_instead_of_raising():
    teensy = _absent_teensy()
    assert teensy.connectionState is DeviceConnectionState.ERROR
    assert teensy.connected is False
    assert teensy.n_digital_channels == 0
    assert teensy.min_pulse_width_ns == 0
    with pytest.raises(DeviceNotConnectedError):
        teensy.setDigital(0, True)


def test_pulse_generator_laser_is_built_on_an_absent_teensy():
    from imswitch.imcontrol.model.managers.lasers.PulseGeneratorLaserManager import (
        PulseGeneratorLaserManager,
    )
    from imswitch.imcontrol.model.SetupInfo import LaserInfo

    info = LaserInfo(analogChannel=None, digitalLine=2, managerName="PulseGeneratorLaserManager",
                     managerProperties={}, valueRangeMin=0, valueRangeMax=1, wavelength=488)
    laser = PulseGeneratorLaserManager(info, name="488", pulseGeneratorManager=_absent_teensy())
    # It does not claim to be connected, nor a mock; its commands say why they fail.
    assert laser.connectionState is not DeviceConnectionState.CONNECTED
    assert laser.runtimeMode.value == "real"
    with pytest.raises(DeviceNotConnectedError):
        laser.setEnabled(True)


# --------------------------------------------------------------------- GRBL
def test_grbl_driver_raises_when_the_port_cannot_be_opened():
    serial = pytest.importorskip("serial")
    from imswitch.imcontrol.model.interfaces.grbldriver import GrblDriver

    with pytest.raises(serial.SerialException):
        GrblDriver("/dev/definitely-not-a-port")


def test_grbl_board_is_not_connected_when_its_port_fails():
    pytest.importorskip("serial")
    from imswitch.imcontrol.model.managers.rs232.GRBLManager import GRBLManager

    board = GRBLManager(SimpleNamespace(managerProperties={"port": "/dev/definitely-not-a-port"}),
                        "board")
    assert board.connectionState is DeviceConnectionState.ERROR
    assert not board.backendIsReal


def test_grbl_laser_on_an_absent_board_does_not_crash_or_claim_connected():
    pytest.importorskip("serial")
    from imswitch.imcontrol.model.managers.lasers.GRBLLaserManager import GRBLLaserManager
    from imswitch.imcontrol.model.managers.rs232.GRBLManager import GRBLManager
    from imswitch.imcontrol.model.SetupInfo import LaserInfo

    board = GRBLManager(SimpleNamespace(managerProperties={"port": "/dev/definitely-not-a-port"}),
                        "board")
    info = LaserInfo(analogChannel=None, digitalLine=None, managerName="GRBLLaserManager",
                     managerProperties={"rs232device": "board", "channel": "LASER"},
                     valueRangeMin=0, valueRangeMax=1023, wavelength=488)
    laser = GRBLLaserManager(info, "laser", rs232sManager={"board": board})
    assert laser.connectionState is not DeviceConnectionState.CONNECTED
    with pytest.raises(DeviceNotConnectedError):
        laser.setEnabled(True)


# ------------------------------------------------------------------ SmarACT
def test_smaract_stage_status_error_is_an_ordinary_exception():
    # Was a BaseException: it escaped every ``except Exception`` around device startup.
    src = open(sys.modules[MultiManager.__module__].__file__.replace(
        "MultiManager.py", "positioners/SmarACTPositionerManager.py")).read()
    assert "class StageStatusException(Exception)" in src


def test_missing_smaract_sdk_is_an_import_error_not_an_os_error():
    code = textwrap.dedent("""
        import ctypes
        real = ctypes.cdll.LoadLibrary
        def boom(name, *a, **k):
            if name == "MCSControl":
                raise OSError("MCSControl: cannot open shared object file")
            return real(name, *a, **k)
        ctypes.cdll.LoadLibrary = boom
        try:
            import imswitch.imcontrol.model.interfaces.SmarACT
        except ImportError as error:
            print("IMPORT_ERROR", error)
        except BaseException as error:
            print("OTHER", type(error).__name__)
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=300, cwd=_repo_root(), env=_env())
    assert "IMPORT_ERROR SmarACT MCSControl library could not be loaded" in out.stdout, out.stderr


def _repo_root():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", "..", ".."))


# -------------------------------------------------------------------- SQUID
def test_squid_stage_on_the_mock_path_can_move():
    from imswitch.imcontrol.model.managers.positioners.SQUIDStageManager import SQUIDStageManager

    from imswitch.imcontrol.model.SetupInfo import PositionerInfo

    info = PositionerInfo(managerName="SQUIDStageManager", axes=["X", "Y", "Z"],
                          managerProperties={"rs232device": "missing", "useMockOnFailure": True},
                          forPositioning=True)
    stage = SQUIDStageManager(info, "stage", rs232sManager={})
    for axis in "XYZ":
        stage.move(5, axis)
    assert stage.position["X"] == 5
    stage.closeEvent()


# -------------------------------------------------- vendor library guards
@pytest.mark.parametrize("module, vendor, flag", [
    ("imswitch.imcontrol.model.managers.NidaqManager", "nidaqmx", "_NIDAQMX_AVAILABLE"),
    ("imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager", "TimeTagger",
     "_TIMETAGGER_AVAILABLE"),
])
def test_vendor_library_failing_to_load_marks_it_unavailable(module, vendor, flag):
    code = textwrap.dedent(f"""
        import importlib.abc, importlib.machinery, sys

        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return None
            def exec_module(self, module):
                raise OSError("vendor shared library failed to load")

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "{vendor}":
                    return importlib.machinery.ModuleSpec(name, Loader())

        sys.meta_path.insert(0, Finder())
        import {module}
        m = sys.modules["{module}"]  # the package re-exports a class of the same name
        print("FLAG", m.{flag})
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=300, cwd=_repo_root(), env=_env())
    assert "FLAG False" in out.stdout, out.stderr[-2000:]


def _env():
    import os
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("MPLBACKEND", "Agg")
    env["PYTHONPATH"] = _repo_root()  # the checkout under test, not an installed copy
    return env


# --------------------------------------- detectors that need their backend
def test_require_backend_names_the_device_and_the_remedy():
    class _Device(DeviceManagerStatusMixin):
        name = "cam"

    device = _Device()
    device._installBackend(_raise(OSError("camera 3 not found")), label="Allied Vision camera 3")
    with pytest.raises(DeviceNotConnectedError) as excinfo:
        device._requireBackend()
    text = str(excinfo.value)
    assert "Allied Vision camera 3" in text and "camera 3 not found" in text
    assert "useMockOnFailure" in text

    mocked = _Device()
    mocked._installBackend(_raise(OSError("x")), lambda: object(), label="m",
                           use_mock_on_failure=True)
    mocked._requireBackend()  # a mock is a backend


@pytest.mark.parametrize("managerName, props", [
    ("AVManager", {"cameraListIndex": 7, "av": {}}),
    ("PiCamManager", {"cameraHost": "192.0.2.1", "cameraPort": 1, "picam": {}}),
])
def test_camera_without_mock_opt_in_fails_with_a_device_named_error(managerName, props):
    props = dict(props, useMockOnFailure=False)
    if managerName == "AVManager":
        from imswitch.imcontrol.model.managers.detectors.AVManager import AVManager as cls
    else:
        from imswitch.imcontrol.model.managers.detectors.PiCamManager import PiCamManager as cls
    info = SimpleNamespace(managerProperties=props, managerName=managerName, forAcquisition=True)
    with pytest.raises(DeviceNotConnectedError) as excinfo:
        cls(info, "cam")
    assert "could not be started" in str(excinfo.value)
    assert "useMockOnFailure" in str(excinfo.value)


# -------------------------------------------------------- Hamamatsu SLM USB
def test_hamamatsu_usb_prototype_failure_falls_to_mock_instead_of_raising(monkeypatch):
    import ctypes

    from imswitch.imcontrol.model.managers.slms import HamamatsuSLMusbManager as module

    class _Dll:
        def __getattr__(self, name):
            raise AttributeError(name)  # symbol missing from the DLL

    monkeypatch.setattr(ctypes, "CDLL", lambda path: _Dll())
    info = SimpleNamespace(serial_number="123", width=4, height=4, pixelSize=1.0,
                           managerProperties={"dll": "x.dll"})
    manager = module.HamamatsuSLMusbManager(info, "slm")
    assert manager.mockermode is True
    assert manager.dll is None
    assert manager.connectionState is DeviceConnectionState.ERROR
