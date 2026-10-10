"""A trajectory script runs only once approved on this computer
(docs/design/plans/scan-engines-and-expert-scan.md §7.10).

A file can carry Python. Loading it from the panel shows the code and asks;
the startup restore and API calls never ask, and hold the script; code
written in the Make… form has been seen and counts as approved. Approval is
remembered by the code's SHA-256, line endings ignored.
"""

import json

import numpy as np
import pytest

from imswitch.imcommon.model.state_contracts import ComponentStateApplyMode
from imswitch.imcontrol.model.scan_program import presets
from imswitch.imcontrol.model.scan_program.program import Trajectory
from imswitch.imcontrol.model.scan_program.script_approval import (
    FILENAME, FORMAT, ScriptApprovals, ScriptNotApproved, set_approvals,
)
from imswitch.imcontrol.model.scan_program.serialize import program_to_dict
from imswitch.imcontrol.model.scan_program.trajectory import path_positions


@pytest.fixture
def store(tmp_path):
    store = ScriptApprovals(str(tmp_path / 'config' / FILENAME))
    previous = set_approvals(store)
    yield store
    set_approvals(previous)


def _code(marker):
    """A path that leaves a mark when it runs."""
    return (f'open({str(marker)!r}, "a").write("ran\\n")\n'
            'def path(t, duration):\n'
            '    return 2 * np.sin(2 * np.pi * t / duration), 2 * np.cos(2 * np.pi * t / duration)\n')


# --- the model -------------------------------------------------------------------------

def test_an_unapproved_script_is_not_run_and_an_approved_one_is(store, tmp_path):
    marker = tmp_path / 'ran'
    trajectory = Trajectory(('X', 'Y'), 'script', {'code': _code(marker)}, 1.0, 0.0, 0.5)
    with pytest.raises(ScriptNotApproved, match='has not been approved on this computer'):
        path_positions(trajectory, np.array([0.0]))
    assert not marker.exists()
    store.approve(_code(marker))
    assert path_positions(trajectory, np.array([0.0])).ravel().tolist() == [0.0, 2.0]
    assert marker.exists()


def test_approval_is_of_the_code_kept_on_this_computer_line_endings_ignored(store, tmp_path):
    code = _code(tmp_path / 'ran')
    store.approve(code)
    assert store.approved(code.replace('\n', '\r\n'))
    assert not store.approved(code + '# changed\n')
    assert ScriptApprovals(store.path).approved(code)            # remembered in the file
    data = json.load(open(store.path))
    assert data['format'] == FORMAT and len(data['approved']) == 1


def test_an_unreadable_approvals_file_approves_nothing_and_approving_repairs_it(tmp_path):
    path = tmp_path / FILENAME
    path.write_text('{not json')
    store = ScriptApprovals(str(path))
    code = _code(tmp_path / 'ran')
    assert not store.approved(code)
    store.approve(code)
    assert json.load(open(path))['format'] == FORMAT and store.approved(code)


# --- the panel -------------------------------------------------------------------------

def _script_program(rig, marker):
    return presets.make('trajectory-script', dict(
        x_device='X', y_device='Y', code=_code(marker), duration_s=0.02, window_s=1e-4,
        approach_s=5e-3, pixel_um=0.5, laser=rig.lasers[1]))


@pytest.fixture
def rig(qtbot, store):
    from .test_scan_controller_expert import ExpertRig
    rig = ExpertRig()
    rig.asked = []
    rig.answer = False
    rig.widget.askApproveScript = lambda code: rig.asked.append(code) or rig.answer
    yield rig
    rig.close()


def test_a_restored_script_is_held_and_nothing_asks(rig, tmp_path):
    marker = tmp_path / 'ran'
    state = {'program': program_to_dict(_script_program(rig, marker))}
    assert rig.scan.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE) == []
    assert rig.asked == [] and not marker.exists()
    assert 'has not been approved on this computer' in rig.widget.reportText()


def test_loading_a_file_asks_and_a_declined_script_does_not_run(rig, tmp_path, monkeypatch):
    marker = tmp_path / 'ran'
    path = tmp_path / 'scan.json'
    path.write_text(json.dumps({'program': program_to_dict(_script_program(rig, marker))}))
    from imswitch.imcontrol.view import guitools
    monkeypatch.setattr(guitools, 'askForFilePath', lambda *a, **k: str(path))

    rig.scan.loadScan()
    assert rig.asked == [_code(marker)] and not marker.exists()
    assert 'was not approved' in rig.widget.reportText()
    rig.scan.plotProgram()                                    # asks again
    assert len(rig.asked) == 2 and not marker.exists()

    rig.answer = True
    rig.scan.plotProgram()
    assert len(rig.asked) == 3 and marker.exists()
    rig.scan.loadScan()                                       # the same code: approved
    assert len(rig.asked) == 3


def test_start_asks_first_and_a_declined_script_starts_nothing(rig, tmp_path):
    marker = tmp_path / 'ran'
    rig.scan.setProgram(_script_program(rig, marker))         # an API call: no question
    assert rig.asked == []
    rig.scan.runScan()
    assert len(rig.asked) == 1
    assert not rig.scan.isRunning and not marker.exists()


def test_code_written_in_the_form_counts_as_approved(rig, tmp_path, store):
    marker = tmp_path / 'ran'
    params = dict(x_device='X', y_device='Y', code=_code(marker), duration_s=0.02,
                  window_s=1e-4, approach_s=5e-3, pixel_um=0.5, laser=rig.lasers[1])
    rig.widget.askPresetParams = lambda preset, initial, validate: params
    rig.scan.makeFromPreset('trajectory-script')
    assert rig.asked == [] and store.approved(_code(marker))
    assert marker.exists()                                    # the report compiled it
    assert 'Trajectory script' in rig.widget.reportText()


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
