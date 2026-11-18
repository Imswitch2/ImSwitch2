"""The polarization rig in the Expert panel: its mock setup (a simulated
Nucleo, the Cobolts, the Hamamatsu, the BPC303, the Elliptec waveplates) under
the real controllers, its scans loaded from files as a script loads them
(``loadScanParamsFromFile``), and run on the Nucleo."""

import json

import pytest

from imswitch.imcontrol.model.scan_program import legacy_microscope_gui as lm

from .test_scan_controller_expert import ExpertRig

SETUP = 'imswitch/_data/user_defaults/imcontrol_setups/nucleo_polarization_mock_setup.json'


@pytest.fixture
def rig(qtbot):
    rig = ExpertRig(setup=json.load(open(SETUP)))
    yield rig
    rig.close()


@pytest.fixture
def scans(tmp_path):
    wiring = lm.Wiring.from_setup(json.load(open(SETUP)))
    paths = {}
    for name, state in lm.rig_scans(wiring, pairs=1).items():
        path = tmp_path / f'{name}.json'
        path.write_text(json.dumps(state))
        paths[name] = path
    return paths, wiring


def test_every_scan_opens_in_the_expert_panel_with_its_repeat(rig, scans):
    paths, wiring = scans
    for name, path in paths.items():
        rig.scan.loadScanParamsFromFile(str(path))
        assert rig.scan.program() is not None, name
        assert rig.widget.repeatEnabled() == name.endswith('_live'), name
    rig.scan.loadScanParamsFromFile(str(paths['galvo_scan_live']))
    assert rig.scan.program() == lm.galvo_scan(lm.LegacyParams(), wiring)


@pytest.mark.parametrize('name', ['dark_frames', 'wide_field_2d', 'line_scan_2d'])
def test_the_acquisitions_run_on_the_nucleo(rig, scans, name):
    paths, _ = scans
    rig.scan.loadScanParamsFromFile(str(paths[name]))
    assert rig.run(timeoutS=60.0), (name, rig.rejected)
    assert not rig.rejected
