"""The retired MoNaLISA scan panel: clear error, shipped mocks on Base, no axial plumbing."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.model.SetupInfo import retiredScanWidgetTypeError
from imswitch.imcontrol.view.ImConMainView import ImConMainView

ROOT = Path(__file__).resolve().parents[3]
SETUPS = ROOT / '_data' / 'user_defaults' / 'imcontrol_setups'


def test_monalisa_error_names_the_replacement_panels():
    error = retiredScanWidgetTypeError('MoNaLISA')
    assert isinstance(error, ValueError)
    assert '"Base"' in str(error)
    assert '"Advanced"' in str(error)
    assert 'axial' in str(error)


def test_main_view_refuses_a_monalisa_setup_before_building_widgets():
    view = ImConMainView.__new__(ImConMainView)
    view.viewSetupInfo = SimpleNamespace(scan=SimpleNamespace(scanWidgetType='MoNaLISA'))
    view.widgets = {}
    with pytest.raises(ValueError, match='Base'):
        view._addDocks({'Scan': SimpleNamespace(name='Scan', yPosition=0)}, None, 'left')
    assert view.widgets == {}


@pytest.mark.parametrize('name', ['mock_scan_setup.json', 'mock_scan_monalisa_live.json'])
def test_shipped_mock_scan_setups_use_the_base_panel(name):
    scan = json.loads((SETUPS / name).read_text())['scan']
    assert scan['scanWidgetType'] == 'Base'
    assert scan['scanDesigner'] == 'BetaScanDesigner'
    assert scan['TTLCycleDesigner'] == 'BetaTTLCycleDesigner'


@pytest.mark.parametrize('name', sorted(p.name for p in SETUPS.glob('*.json')))
def test_no_shipped_setup_names_a_retired_scan_panel(name):
    setup = json.loads((SETUPS / name).read_text())
    scanType = (setup.get('scan') or {}).get('scanWidgetType')
    assert retiredScanWidgetTypeError(scanType) is None


@pytest.mark.parametrize('attr', [
    'sigQueryCenterCoord', 'sigCenterCoordPipelineFinished',
    'sigUpdateBeadRecCenter', 'sigShowBeadRecCenterCross',
    'sigAutoAxialToggled', 'sigNewAxialListBuffer', 'getNextAxial',
    'beadRecWorkflow',
])
def test_communication_channel_has_no_axial_or_bead_center_plumbing(attr):
    assert not hasattr(CommunicationChannel, attr)
