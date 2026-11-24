import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from imswitch.imcontrol.controller.ImConMainController import ImConMainController
from imswitch.imcontrol.model import SetupInfo

nidaq_module = importlib.import_module(
    "imswitch.imcontrol.model.managers.NidaqManager"
)


SIMULATED_NIDAQ_SETUP = """
{
    "detectors": {},
    "lasers": {
        "Laser": {
            "analogChannel": null,
            "digitalLine": "Dev1/port0/line1",
            "managerName": "NidaqLaserManager",
            "managerProperties": {},
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 100
        }
    },
    "positioners": {
        "X": {
            "analogChannel": "Dev1/ao0",
            "digitalLine": null,
            "managerName": "NidaqPositionerManager",
            "managerProperties": {},
            "axes": ["X"],
            "forScanning": true,
            "forPositioning": true
        }
    },
    "nidaq": {
        "simulation": true
    }
}
"""


REAL_NIDAQ_SETUP = """
{
    "detectors": {},
    "lasers": {
        "Laser": {
            "analogChannel": null,
            "digitalLine": "Dev1/port0/line1",
            "managerName": "NidaqLaserManager",
            "managerProperties": {},
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 100
        }
    },
    "positioners": {},
    "nidaq": {
        "simulation": false
    }
}
"""


def test_nidaq_simulation_allows_channels_without_nidaqmx(monkeypatch):
    """Mock scan setups can keep Dev1 channels without installing nidaqmx."""
    monkeypatch.setattr(nidaq_module, "_NIDAQMX_AVAILABLE", False)
    setup_info = SetupInfo.from_json(SIMULATED_NIDAQ_SETUP, infer_missing=True)

    manager = nidaq_module.NidaqManager(setup_info)
    manager.setDigital("Laser", True)
    manager.setAnalog("X", 1.0)

    assert manager.tasks == {}
    assert manager.isSimulated is True
    assert manager.backendAvailable is False
    assert manager._NidaqManager__scanSimulator is not None


def test_nidaq_runtime_errors_warn_once_then_debug(monkeypatch):
    """Repeated identical NI-DAQ write failures must not spam warnings."""
    monkeypatch.setattr(nidaq_module, "_NIDAQMX_AVAILABLE", False)
    setup_info = SetupInfo.from_json(SIMULATED_NIDAQ_SETUP, infer_missing=True)
    manager = nidaq_module.NidaqManager(setup_info)
    logger = SimpleNamespace(warning=Mock(), debug=Mock())
    manager._NidaqManager__logger = logger

    manager._warnRuntimeErrorOnce(('setDigital', 'Laser', 'DaqError', 'boom'), 'msg 1')
    manager._warnRuntimeErrorOnce(('setDigital', 'Laser', 'DaqError', 'boom'), 'msg 1')
    manager._warnRuntimeErrorOnce(('setAnalog', 'X', 'DaqError', 'boom'), 'msg 2')

    assert logger.warning.call_count == 2
    assert logger.debug.call_count == 1


def test_nidaq_real_channels_start_degraded_without_nidaqmx(monkeypatch):
    """Real NI-DAQ setups stay real but no longer abort ImControl startup."""
    monkeypatch.setattr(nidaq_module, "_NIDAQMX_AVAILABLE", False)
    setup_info = SetupInfo.from_json(REAL_NIDAQ_SETUP, infer_missing=True)

    manager = nidaq_module.NidaqManager(setup_info)

    assert manager.isSimulated is False
    assert manager.backendAvailable is False
    assert isinstance(manager.backendError, ImportError)
    assert manager._NidaqManager__scanSimulator is None
    assert manager.tasks == {}


def test_nidaq_unavailable_real_backend_fails_outputs_closed(monkeypatch):
    """Unavailable real NI-DAQ writes fail without pretending to succeed."""
    monkeypatch.setattr(nidaq_module, "_NIDAQMX_AVAILABLE", False)
    setup_info = SetupInfo.from_json(REAL_NIDAQ_SETUP, infer_missing=True)
    manager = nidaq_module.NidaqManager(setup_info)

    assert manager.setDigital("Laser", True) is False
    assert manager.tasks == {}


def test_nidaq_unavailable_real_backend_refuses_scan_before_arm(monkeypatch):
    """A missing backend must not enter scan state or fall back to simulation."""
    monkeypatch.setattr(nidaq_module, "_NIDAQMX_AVAILABLE", False)
    setup_info = SetupInfo.from_json(REAL_NIDAQ_SETUP, infer_missing=True)
    manager = nidaq_module.NidaqManager(setup_info)

    with pytest.raises(
        nidaq_module.NidaqBackendUnavailableError,
        match="NI-DAQmx is unavailable.*simulation=false",
    ):
        manager.runScan({}, {})

    assert manager.busy is False
    assert manager.tasks == {}


def test_nidaq_unavailable_strict_analog_write_raises(monkeypatch):
    monkeypatch.setattr(nidaq_module, "_NIDAQMX_AVAILABLE", False)
    setup_info = SetupInfo.from_json(SIMULATED_NIDAQ_SETUP, infer_missing=True)
    setup_info = SetupInfo.from_dict(
        {
            **setup_info.to_dict(),
            "nidaq": {"simulation": False},
        },
        infer_missing=True,
    )
    manager = nidaq_module.NidaqManager(setup_info)

    assert manager.setAnalog("X", 1.0) is False
    with pytest.raises(nidaq_module.NidaqBackendUnavailableError):
        manager.setAnalog("X", 1.0, raise_on_error=True)


def test_configured_nidaq_names_include_scan_and_point_detector_channels():
    setup_info = SetupInfo.from_dict(
        {
            "detectors": {
                "APD": {
                    "managerName": "APDManager",
                    "managerProperties": {
                        "ctrInputLine": "Dev2/ctr0",
                        "terminal": "PFI0",
                    },
                    "forAcquisition": True,
                }
            },
            "scan": {
                "scanWidgetType": "Base",
                "scanDesigner": "GalvoScanDesigner",
                "scanDesignerParams": {},
                "TTLCycleDesigner": "PointScanTTLCycleDesigner",
                "TTLCycleDesignerParams": {},
                "sampleRate": 100000,
                "lineClockLine": "Dev3/port0/line6",
            },
            "nidaq": {
                "simulation": False,
                "timerCounterChannel": "Dev1/ctr2",
            },
        },
        infer_missing=True,
    )

    assert nidaq_module.NidaqManager._extractConfiguredDeviceNames(setup_info) == (
        "Dev1",
        "Dev2",
        "Dev3",
    )


def test_imcon_close_event_handles_partial_initialization(monkeypatch):
    """If startup fails before the controller factory exists, close must be safe."""
    controller = ImConMainController.__new__(ImConMainController)
    controller._ImConMainController__logger = SimpleNamespace(
        info=Mock(), warning=Mock(), debug=Mock()
    )
    controller._ImConMainController__factory = None
    controller._ImConMainController__masterController = None
    monkeypatch.setattr(
        controller, "_shouldSaveWidgetStateOnClose", lambda: False
    )

    controller.closeEvent()
