"""Tests for pruneDefaultSetupInfoFields — setup-file saves must not pollute
hand-maintained JSON with machine-added default/null sections.

Background: saveSetupInfo() runs on every laser-preset or camera-ROI save and
used to rewrite the whole file from the in-memory dataclass, adding
``"rotators": null``, an all-default ``"nidaq"`` section, etc. Loading uses
``infer_missing=True``, so omitting fields that equal their defaults is a
lossless round-trip.
"""

import json

from imswitch.imcontrol.model.configfiletools import pruneDefaultSetupInfoFields
from imswitch.imcontrol.model.SetupInfo import SetupInfo


MINIMAL_TRIGGERSCOPE_SETUP = json.dumps({
    'lasers': {
        '640': {
            'analogChannel': None,
            'digitalLine': 'Triggerscope/TTL2',
            'managerName': 'Cobolt0601NewLaserManager',
            'managerProperties': {'digitalPorts': ['COM23']},
            'wavelength': 640, 'valueRangeMin': 0, 'valueRangeMax': 100,
        },
    },
    'scan': {
        'scanWidgetType': 'TriggerScope',
        'scanDesigner': 'BetaScanDesigner',
        'scanDesignerParams': {'return_time': 0.5},
        'TTLCycleDesigner': 'BetaTTLCycleDesigner',
        'TTLCycleDesignerParams': {},
        'sampleRate': 100000,
    },
    'triggerScope': {'rs232device': 'triggerscope'},
    'availableWidgets': ['Laser', 'TriggerScopeRaster'],
})


def test_default_and_null_fields_are_pruned():
    setupInfo = SetupInfo.from_json(MINIMAL_TRIGGERSCOPE_SETUP, infer_missing=True)
    data = pruneDefaultSetupInfoFields(setupInfo)

    # Fields the setup does not define must not appear at all
    for absent in ('rotators', 'microscopeStand', 'etSTED', 'teensyPulse',
                   'nidaq', 'pulseStreamer', 'pyroServerInfo',
                   'detectors', 'positioners', 'rs232devices',
                   'slm', 'focusLock', 'autofocus', 'tiling'):
        assert absent not in data, f'machine-added default section: {absent}'


def test_non_default_content_is_preserved():
    setupInfo = SetupInfo.from_json(MINIMAL_TRIGGERSCOPE_SETUP, infer_missing=True)
    data = pruneDefaultSetupInfoFields(setupInfo)

    assert data['scan']['scanWidgetType'] == 'TriggerScope'
    assert data['triggerScope'] == {'rs232device': 'triggerscope'}
    assert data['lasers']['640']['digitalLine'] == 'Triggerscope/TTL2'
    # Unknown keys (dataclasses_json catch-all) survive the round-trip
    assert data['availableWidgets'] == ['Laser', 'TriggerScopeRaster']


def test_round_trip_is_lossless():
    setupInfo = SetupInfo.from_json(MINIMAL_TRIGGERSCOPE_SETUP, infer_missing=True)
    pruned = json.dumps(pruneDefaultSetupInfoFields(setupInfo))
    reloaded = SetupInfo.from_json(pruned, infer_missing=True)

    assert reloaded == setupInfo


def test_smart_microscopy_mode_switching_flag_round_trips():
    setup = json.loads(MINIMAL_TRIGGERSCOPE_SETUP)
    setup['smartMicroscopyModes'] = {
        'EtSnouty': {
            'scouting': 'Scout mode',
            'event': 'Event mode',
        },
    }
    setup['smartMicroscopyModeSwitchingEnabled'] = {'EtSnouty': True}

    setupInfo = SetupInfo.from_json(json.dumps(setup), infer_missing=True)
    pruned = pruneDefaultSetupInfoFields(setupInfo)
    reloaded = SetupInfo.from_json(json.dumps(pruned), infer_missing=True)

    assert setupInfo.smartMicroscopyModeSwitchingEnabled == {'EtSnouty': True}
    assert pruned['smartMicroscopyModeSwitchingEnabled'] == {'EtSnouty': True}
    assert reloaded.smartMicroscopyModeSwitchingEnabled == {'EtSnouty': True}
