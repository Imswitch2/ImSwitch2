"""The DeformableMirror panel and the mirror scripting functions, on a mock
mirror."""

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import DeformableMirrorInfo
from imswitch.imcontrol.model.managers.DeformableMirrorsManager import DeformableMirrorsManager
from imswitch.imcontrol.model.managers.deformableMirrors import mirror_files


def _controller(tmp_path, qtbot=None, **properties):
    from imswitch.imcontrol.controller.controllers.DeformableMirrorController import (
        DeformableMirrorController,
    )
    modes = np.zeros((3, 5))
    modes[0, 0], modes[1, 1], modes[2, 2] = 0.1, 0.2, 0.3
    mirror_files.write_matrix(tmp_path / 'modes.csv', modes)
    properties = {**dict(serialNumber='MOCK1', actuators=5, firstMode=4,
                         modesFile=str(tmp_path / 'modes.csv')), **properties}
    mirrors = DeformableMirrorsManager({'DM': DeformableMirrorInfo(
        managerName='AlpaoDMManager', managerProperties=properties)})
    master = SimpleNamespace(deformableMirrorsManager=mirrors)
    widget = None
    if qtbot is not None:
        from imswitch.imcontrol.view.widgets.DeformableMirrorWidget import DeformableMirrorWidget
        widget = DeformableMirrorWidget(None)
        qtbot.addWidget(widget)
    comm = SimpleNamespace(sharedAttrs={})
    controller = DeformableMirrorController(None, comm, master, widget=widget,
                                            factory=None, moduleCommChannel=None)
    return controller, mirrors['DM'], comm.sharedAttrs, widget


def test_scripts_send_modes_and_the_recording_gets_them(tmp_path):
    controller, mirror, attrs, _ = _controller(tmp_path)
    assert controller.getDeformableMirrorNames() == ['DM']
    assert controller.getMirrorInfo('DM') == dict(actuators=5, maxCommand=1.0, modes=[4, 5, 6],
                                                  simulated=True, usable=True)
    sent = controller.setMirrorModes('DM', {5: 2.0, 6: -1.0})
    assert sent == pytest.approx([0, 0.4, -0.3, 0, 0])
    assert attrs[('DeformableMirror', 'DM', 'Command')] == pytest.approx(sent)
    assert attrs[('DeformableMirror', 'DM', 'Modes')] == {'5': 2.0, '6': -1.0}

    controller.pokeMirrorActuator('DM', 4, 0.05)              # not a sum of modes
    assert attrs[('DeformableMirror', 'DM', 'Modes')] == {}
    assert attrs[('DeformableMirror', 'DM', 'Command')] == pytest.approx([0, 0, 0, 0, 0.05])


def test_a_flat_saved_by_script_is_sent_again_after_a_reset(tmp_path):
    controller, mirror, attrs, _ = _controller(tmp_path)
    controller.setMirrorCommand('DM', [0.1, -0.1, 0.2, 0.0, 0.05])
    controller.setMirrorFlat('DM')
    path = controller.saveMirrorCommand('DM', str(tmp_path / 'flat.csv'))
    controller.resetMirror('DM')
    assert controller.getMirrorCommand('DM') == [0.0] * 5
    assert controller.loadMirrorCommand('DM', path) == pytest.approx([0.1, -0.1, 0.2, 0.0, 0.05])
    controller.zeroMirror('DM')
    assert controller.flattenMirror('DM') == pytest.approx([0.1, -0.1, 0.2, 0.0, 0.05])


def test_an_unknown_mirror_names_the_ones_there_are(tmp_path):
    controller, *_ = _controller(tmp_path)
    with pytest.raises(KeyError, match='the setup has: DM'):
        controller.flattenMirror('DM2')


def test_the_panel_shows_the_command_and_a_refused_one(tmp_path, qtbot):
    controller, mirror, attrs, widget = _controller(tmp_path, qtbot, maxCommand=0.5)
    panel = widget._mirrors['DM']
    assert 'Mock mirror; 5 actuators' in panel['status'].text()
    assert 'modes 4-6' in panel['status'].text()

    widget.sigPoke.emit('DM', 2, 0.3)
    assert mirror.command.tolist() == [0, 0, 0.3, 0, 0]
    assert 'Largest |value| 0.300 of 0.5' in panel['summary'].text()

    widget.sigPoke.emit('DM', 2, 0.6)                  # beyond maxCommand
    assert 'beyond' in panel['message'].text()
    assert mirror.command.tolist() == [0, 0, 0.3, 0, 0]

    panel['modes'][6].setValue(1.0)
    panel['buttons']['modes'].click()
    assert mirror.command == pytest.approx([0, 0, 0.3, 0, 0])
    assert attrs[('DeformableMirror', 'DM', 'Modes')] == {'4': 0.0, '5': 0.0, '6': 1.0}
    assert panel['message'].text() == ''

    controller.setMirrorModes('DM', {4: 2.0})          # a script's modes show in the panel
    assert panel['modes'][4].value() == 2.0 and panel['modes'][6].value() == 0.0


def test_a_mirror_that_is_not_connected_greys_its_panel(tmp_path, qtbot):
    controller, mirror, _, widget = _controller(tmp_path, qtbot, serialNumber='BAX1',
                                                sdkPath=str(tmp_path / 'none'))
    panel = widget._mirrors['DM']
    assert 'Not connected' in panel['status'].text()
    assert not panel['buttons']['flat'].isEnabled()
    assert 'ALPAO SDK not found' in panel['buttons']['flat'].toolTip()
