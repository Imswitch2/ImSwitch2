"""The Advanced panel seeds its first dwell and phase delay from the setup
(``scan.scanDesignerParams``: ``dwell_ms``, ``phase_delay``) and names the
clock the dwell is counted on. Setting a field emits the change signals the
base scan controller wired to the shared-attribute updates, which read the
whole panel; seeded before the controls and the parameter serializer
existed, every setup with a ``dwell_ms`` -- the MoNaLISA and CoolLED
templates -- logged an AttributeError at startup (the UI start-up test of
those templates, 2026-10-17). pytest-qt fails a test on any exception raised
in a Qt slot, so building the controller is the test."""
import json

from .test_scan_controller_advanced_orders import MOCK_STAGE, AdvancedRig


class _SeededRig(AdvancedRig):
    def __init__(self):
        from imswitch.imcontrol._test import optionsBasic
        from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
        from imswitch.imcontrol.controller.MasterController import MasterController
        from imswitch.imcontrol.controller.basecontrollers import ImConWidgetControllerFactory
        from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
            ScanControllerAdvanced,
        )
        from imswitch.imcontrol.view import ViewSetupInfo
        from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced
        from .test_simple_point_scan_controller import SETUP, _Main
        setup = json.load(open(SETUP))
        setup['scan'].update(scanWidgetType='Advanced', scanDesigner='BetaScanDesigner',
                             scanDesignerParams=dict(MOCK_STAGE, dwell_ms=10, phase_delay=40))
        self.setup = ViewSetupInfo.from_json(json.dumps(setup))
        self.main = _Main()
        self.channel = CommunicationChannel(self.main, self.setup)
        self.master = MasterController(self.setup, self.channel, self.main._moduleCommChannel)
        self.factory = ImConWidgetControllerFactory(
            self.setup, self.master, self.channel, self.main._moduleCommChannel)
        self.widget = ScanWidgetAdvanced(optionsBasic)
        self.scan = self.factory.createController(ScanControllerAdvanced, self.widget)
        self.main.controllers['Scan'] = self.scan
        self.ended = []


def test_the_setups_first_dwell_and_delay_are_seeded_without_an_error(qtbot):
    rig = _SeededRig()
    try:
        assert rig.widget.seqTimePar.text() == '10.0'          # dwell_ms 10
        assert rig.widget.phaseDelayPar.text() == '40'
        assert rig.widget._dwellLabel.text() == 'Dwell time (ms, 10 µs clock):'
        # and the shared attributes describe the seeded panel
        assert rig.channel.sharedAttrs[('ScanTTL', 'sequence_time')] == 0.01
    finally:
        rig.close()
