"""With the lasers at their startup state (off, power zero -- what the Laser
controller publishes in the running application) the virtual sample is dark, and
the message the scan logs names the lasers and the way out (raise one)."""
import json
from pathlib import Path

import numpy as np

from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.model.managers.detectors import _mock_optics as optics
from imswitch.imcontrol.view import ViewSetupInfo

from .test_simple_point_scan_controller import _Main

SIMPLE = (Path(__file__).resolve().parents[3] / "_data" / "user_defaults" / "imcontrol_setups"
          / "mock" / "point_scanning" / "simple_mock_setup.json")


class _StartupStates:
    """The lasers as the Laser controller publishes them at startup."""

    def get(self, name):
        return optics.LaserState(enabled=False, fraction=0.0, range=(0.0, 5.0))


def test_a_scan_with_the_lasers_at_their_startup_state_is_dark_and_says_which_to_raise(qtbot):
    setup = ViewSetupInfo.from_json(json.dumps(json.loads(SIMPLE.read_text())), infer_missing=True)
    main = _Main()
    master = MasterController(setup, CommunicationChannel(main, setup), main._moduleCommChannel)
    try:
        apd = master.detectorsManager['APD']
        lasers = apd._mock_optics.excitation
        assert lasers, 'the mock detector names no excitation lasers'
        signal = {'scanSignalsDict': {'X': np.linspace(0, 1, 16), 'Y': np.zeros(16)},
                  'TTLCycleSignalsDict': {}}
        mock = optics.PointScanSignal(apd._mock_sample, apd._mock_optics, signal, 1, 1e6, None,
                                      _StartupStates())
        assert mock.usable and mock.unlit
        message = optics.unlit_message(lasers)
        assert all(laser.name in message for laser in lasers)
        assert 'raise one in the Laser widget' in message and 'start off' in message
    finally:
        master.closeEvent()
