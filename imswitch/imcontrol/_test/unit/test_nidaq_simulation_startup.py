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


def test_nidaq_real_channels_still_require_nidaqmx(monkeypatch):
    """Real NI-DAQ setups should still fail clearly when nidaqmx is absent."""
    monkeypatch.setattr(nidaq_module, "_NIDAQMX_AVAILABLE", False)
    setup_info = SetupInfo.from_json(REAL_NIDAQ_SETUP, infer_missing=True)

    with pytest.raises(ImportError, match="nidaqmx is required"):
        nidaq_module.NidaqManager(setup_info)


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
