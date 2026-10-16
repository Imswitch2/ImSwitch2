"""The file says what ran (review round 1, batch 4; docs/design/plans/
scan-engines-and-expert-scan.md §2.6, §7.9).

A run publishes its realized timing beside the program as requested
(``ScanStage:scan_schedule``): the policy with its contents, the grid and
where it came from, every time that runs differently, the approach. An
Advanced scan records the distinct changes its timing note reads; a swept
one, which the program path does not describe, records none.
"""

import copy

from qtpy import QtWidgets

from imswitch.imcontrol.model.scan_program.advanced import schedule_summary

from .test_scan_controller_advanced_orders import AdvancedRig


def test_an_expert_run_records_its_realized_timing(qtbot):
    from .test_scan_controller_expert import ExpertRig, _ms_resolft
    rig = ExpertRig()
    try:
        rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
        assert rig.run(timeoutS=10.0)
        summary = rig.channel.sharedAttrs[('ScanStage', 'scan_schedule')]
        assert summary['policy']['id'] == 'strict-v1'
        assert summary['policy']['contents']['rounding']['step'] == 'exact'
        assert summary['grid_s'] == 1e-5
        assert summary['approach_s'] == 1e-3          # Z's move and settle, §7.9
        assert summary['changes'] == []
        assert {'quantity': 'timing grid', 'value': 1e-5, 'unit': 's',
                'source': 'scan.sampleRate (100000 Hz)'} in summary['sources']
    finally:
        rig.close()


def test_an_advanced_scan_records_the_times_it_rounds(qtbot):
    rig = AdvancedRig()
    try:
        rate = rig.setup.scan.sampleRate
        rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0], ttl=True)
        analog = copy.deepcopy(rig.scan._analogParameterDict)
        digital = copy.deepcopy(rig.scan._digitalParameterDict)
        laser = digital['target_device'][0]
        digital['advanced_mode'] = True
        digital['linestep_enable'] = {name: [name == laser] for name in digital['target_device']}
        digital['pulse_starts_s'] = {name: [[1.5 / rate]] for name in digital['target_device']}
        digital['pulse_ends_s'] = {name: [[50 / rate]] for name in digital['target_device']}
        rig.scan._scanParams.apply(rig.widget, analog, digital, rig.scan.positioners,
                                   rig.scan.TTLDevices)
        rig.scan._buildScanSignals()                  # as a run arms
        summary = rig.channel.sharedAttrs[('ScanStage', 'scan_schedule')]
        assert summary['policy']['id'] == 'advanced-compatible-v1'
        # the window edge the panel's note names, recorded once
        edges = [c for c in summary['changes'] if c['quantity'] == 'event_edges']
        assert edges == [{'quantity': 'event_edges', 'where': f'{laser} ttl start',
                          'requested_s': 1.5 / rate, 'realized_s': 2 / rate}]
    finally:
        rig.close()


def test_a_swept_scan_records_no_schedule_the_program_path_cannot_describe():
    from . import test_galvo_signal_goldens as galvo
    setup = galvo._setup_sted_like()
    stage = galvo.CASES['xy_galvo_galvo']           # the galvo designer's: swept
    assert schedule_summary(stage, {}, setup, setup.scan.sampleRate) is None


def test_the_shared_test_helper_makes_a_widget_ready_application():
    """A bare QCoreApplication left by one test aborted a widget test later
    in the same xdist worker."""
    from .test_mock_scan_simulation_coordinator import _ensure_qcore_app
    assert isinstance(_ensure_qcore_app(), QtWidgets.QApplication)



def test_a_galvo_scan_records_how_its_phase_delay_plays(qtbot):
    """Beside the schedule: the galvo's lag as the digital lines play it
    (whole scan-clock samples), as the detectors read it (whole timer
    samples), and the residual between them (scan timing plan, rule 7)."""
    rig = AdvancedRig(designer='GalvoScanDesigner')
    try:
        rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0])
        rig.widget.phaseDelayPar.setText('3')
        rig.scan._buildScanSignals()                  # as a run arms
        assert rig.channel.sharedAttrs[('ScanStage', 'lag')] == {
            'requested_s': 3e-6, 'gate_delay_s': 0.0, 'detector_delay_s': 3e-6,
            'residual_s': 3e-6}
    finally:
        rig.close()
    beta = AdvancedRig()                              # Beta plays no phase delay
    try:
        beta.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0])
        beta.scan._buildScanSignals()
        assert ('ScanStage', 'lag') not in beta.channel.sharedAttrs
    finally:
        beta.close()


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
