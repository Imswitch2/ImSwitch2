"""What a CONFIGURED panel's section is proposed, checked and built from.

On the real managers of shipped mock setups: ``example_no_hardware.json``
names a single-axis stage as its tiling stage (the case validation exists
for), ``example_mock.json`` has one XY stage and one camera.
"""

import json
from pathlib import Path

import pytest

from imswitch import imcontrol
from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.model import panel_config, setupfolder
from imswitch.imcontrol.model.SetupInfo import AutofocusInfo, TilingInfo
from imswitch.imcontrol.view import ViewSetupInfo
from imswitch.imcontrol.view.configeditor.section_form import (
    _build_default_section, load_section_schema,
)


pytestmark = pytest.mark.nohardware


class _Main:
    def __init__(self):
        self.controllers = {}
        self._moduleCommChannel = ModuleCommunicationChannel()
        self._moduleCommChannel.register(imcontrol)


@pytest.fixture
def rig(qapp):
    made = []

    def make(name, edit=None):
        data = json.loads(Path(setupfolder.shippedSetupPath(name)).read_text(encoding='utf-8'))
        if edit is not None:
            edit(data)
        setup = ViewSetupInfo.from_dict(data, infer_missing=True)
        main = _Main()
        master = MasterController(setup, CommunicationChannel(main, setup), main._moduleCommChannel)
        made.append(master)
        return setup, master

    yield make
    for master in made:
        master.closeEvent()


def _tilingDefaults():
    return _build_default_section(load_section_schema('tiling'))


def test_the_proposal_picks_the_only_two_axis_stage_and_derives_the_step(rig):
    setup, master = rig('example_no_hardware.json')

    values, notes = panel_config.proposeSection('Tiling', setup, master, _tilingDefaults())

    # Mock X and Mock Y have one axis each; the Kinesis stage has two.
    assert values['xyPositioner'] == 'Mock Kinesis XY'
    camera = master.detectorsManager['Mock Camera']
    width, height = camera.shape[:2]
    size = camera.pixelSizeUm[-1]
    assert values['defaultTileStepUm'] == pytest.approx(
        round(min(width, height) * size * 0.9, 1))
    assert any('field of view of Mock Camera' in note for note in notes)
    # Two detectors acquire there, so the form leaves "align on" to the
    # controller's own default rather than guessing.
    assert values['camera'] == ''
    assert panel_config.validateSection('Tiling', values, setup, master, ViewSetupInfo) == []


def test_the_shipped_single_axis_tiling_stage_is_refused(rig):
    setup, master = rig('example_no_hardware.json')

    errors = panel_config.validateSection(
        'Tiling', {**_tilingDefaults(), 'xyPositioner': 'Mock X'}, setup, master, ViewSetupInfo)

    assert errors == ['XY positioner "Mock X" has 1 axis (X); tiling moves two.']


@pytest.mark.parametrize('edit, message', [
    ({'xyPositioner': ''}, 'Choose the XY positioner.'),
    ({'xyPositioner': 'Nope'}, 'XY positioner: there is no positioner "Nope" in this setup.'),
    ({'camera': 'Ghost'}, 'Detector to align on: there is no detector "Ghost" in this setup.'),
    ({'zPositioner': 'Ghost'}, 'Z positioner: there is no positioner "Ghost" in this setup.'),
    ({'defaultTileStepUm': 0}, 'Default tile step must be more than 0.'),
    ({'defaultTileStepUm': float('nan')}, 'Default tile step must be a number.'),
    ({'defaultTilesX': 0}, 'Default tiles X must be at least 1.'),
    ({'defaultPattern': 'zigzag'}, 'Default pattern must be one of spiral, serpentine.'),
    ({'mode': 'burst'}, 'Mode must be one of free-running, triggered.'),
    ({'detectorTransforms': []}, 'Detector transforms must be a JSON object, e.g. {"APD1": "identity"}.'),
])
def test_validation_names_what_is_wrong(rig, edit, message):
    setup, master = rig('example_mock.json')
    values, _ = panel_config.proposeSection('Tiling', setup, master, _tilingDefaults())

    errors = panel_config.validateSection('Tiling', {**values, **edit}, setup, master, ViewSetupInfo)

    assert message in errors


def test_a_detector_that_does_not_acquire_is_refused(rig):
    def noAcquisition(data):
        data['detectors']['Camera']['forAcquisition'] = False
        data['detectors']['Camera']['forFocusLock'] = True

    setup, master = rig('example_mock.json', noAcquisition)

    assert panel_config.prerequisiteProblem('Tiling', setup, master) == (
        'Needs a detector that acquires; this setup has none.')
    errors = panel_config.validateSection(
        'Tiling', {**_tilingDefaults(), 'xyPositioner': 'Mock XY Stage', 'camera': 'Camera'},
        setup, master, ViewSetupInfo)
    assert 'Detector to align on "Camera" is not an acquisition detector (forAcquisition is off).' in errors


def test_the_section_is_built_the_way_the_loader_builds_it(rig):
    setup, master = rig('example_mock.json')
    values, _ = panel_config.proposeSection('Tiling', setup, master, _tilingDefaults())
    values['flipTileAxisX'] = True

    typed = panel_config.buildSection('Tiling', values, ViewSetupInfo)

    assert isinstance(typed, TilingInfo)
    assert typed.xyPositioner == 'Mock XY Stage'
    assert typed.camera == 'Camera'
    assert typed.flipTileAxisX is True
    # Exactly what loading a file holding this section yields.
    loaded = ViewSetupInfo.from_dict({'tiling': values}, infer_missing=True).tiling
    assert typed == loaded


def test_a_blank_value_gets_the_loader_s_default(rig):
    """The loader's blank-value tolerance applies here too."""
    typed = panel_config.buildSection(
        'Tiling', {'xyPositioner': 'S', 'settleTimeMs': ''}, ViewSetupInfo)
    assert typed.settleTimeMs == TilingInfo('S').settleTimeMs


def test_autofocus_needs_a_z_stage(rig):
    setup, master = rig('example_mock.json')
    assert panel_config.prerequisiteProblem('Autofocus', setup, master) == (
        'Needs a stage with a Z axis; this setup has none.')


def test_autofocus_proposal_and_validation_with_a_z_stage(rig):
    def addZ(data):
        data['positioners']['Mock Z'] = {
            'analogChannel': None, 'digitalLine': None,
            'managerName': 'MockPositionerManager', 'managerProperties': {},
            'axes': ['Z'], 'forPositioning': True, 'resetOnClose': False,
        }

    setup, master = rig('example_mock.json', addZ)
    schema = load_section_schema('autofocus')

    values, notes = panel_config.proposeSection(
        'Autofocus', setup, master, _build_default_section(schema))

    assert values['positioner'] == 'Mock Z'
    assert values['camera'] == 'Camera'
    width, height = master.detectorsManager['Camera'].shape[:2]
    assert (values['frameCropw'], values['frameCroph']) == (width, height)
    assert panel_config.validateSection('Autofocus', values, setup, master, ViewSetupInfo) == []
    assert isinstance(panel_config.buildSection('Autofocus', values, ViewSetupInfo), AutofocusInfo)



def test_autofocus_refuses_a_stage_without_z(rig):
    setup, master = rig('example_mock.json')
    values = {**_build_default_section(load_section_schema('autofocus')),
              'positioner': 'Mock XY Stage', 'camera': 'Camera'}

    assert 'Z positioner "Mock XY Stage" has no Z axis; the focus search moves Z.' in (
        panel_config.validateSection('Autofocus', values, setup, master, ViewSetupInfo))


def test_the_tiling_form_offers_every_tiling_field():
    """The form covers TilingInfo, orientation flips and swap included."""
    import dataclasses

    schema = load_section_schema('tiling')
    assert {f['key'] for f in schema['fields']} == {f.name for f in dataclasses.fields(TilingInfo)}
