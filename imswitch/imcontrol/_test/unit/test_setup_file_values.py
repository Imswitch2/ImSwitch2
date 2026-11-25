"""A blank or unreadable value in a typed setup-file field must not stop
Hardware Control: the loader warns and uses the field's default.

Background: the redSTED rig failed to start on ``float("")`` from a blank
Time Tagger value, and dataclasses_json read ``"simulation": "false"`` as
``True``. ``setup_file_values.sanitizeSetupData`` walks the setup dataclass
type hints over the parsed JSON before decoding.
"""

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pytest
from dataclasses_json import dataclass_json

from imswitch.imcontrol.model import configfiletools, setupfolder
from imswitch.imcontrol.model.Options import Options
from imswitch.imcontrol.model.SetupInfo import SetupInfo
from imswitch.imcontrol.model.setup_file_values import sanitizeSetupData
from imswitch.imcontrol.view.guitools.ViewSetupInfo import ViewSetupInfo


# The top-level Time Tagger block lives on dev, not main; this mirrors its
# field shapes so the walker is checked on exactly the block the rig uses.
@dataclass(frozen=True)
class _TimeTaggerLikeInfo:
    serial: Optional[str] = None
    simulation: bool = False
    photonsChannel: int = 1
    photonsTriggerV: float = 0.5
    lineClockTriggerV: float = 0.5
    frameClockChannel: Optional[int] = None


@dataclass_json
@dataclass
class _TopInfo:
    timeTagger: Optional[_TimeTaggerLikeInfo] = field(default_factory=lambda: None)
    channels: Dict[str, int] = field(default_factory=dict)
    gains: List[float] = field(default_factory=list)
    lineOrChannel: Optional[Union[str, int]] = None
    level: Optional[Union[int, float]] = None
    free: Dict[str, Any] = field(default_factory=dict)


def _decodeTop(data):
    cleaned, fixes = sanitizeSetupData(data, _TopInfo)
    return _TopInfo.from_dict(cleaned, infer_missing=True), fixes


def _decodeSetup(data, setupInfoType=SetupInfo):
    cleaned, fixes = sanitizeSetupData(data, setupInfoType)
    return setupInfoType.from_dict(cleaned, infer_missing=True), fixes


# --- the decoder's own failures, which this guards against ----------------

def test_the_decoder_alone_refuses_a_blank_float():
    with pytest.raises(ValueError):
        _TopInfo.from_dict({'timeTagger': {'lineClockTriggerV': ''}}, infer_missing=True)


def test_the_decoder_alone_reads_false_as_true():
    decoded = SetupInfo.from_dict({'nidaq': {'simulation': 'false'}}, infer_missing=True)
    assert decoded.nidaq.simulation is True


# --- int / float ------------------------------------------------------------

def test_blank_float_uses_the_default_and_names_path_value_and_default():
    decoded, fixes = _decodeTop({'timeTagger': {'lineClockTriggerV': ''}})

    assert decoded.timeTagger.lineClockTriggerV == 0.5
    assert fixes == ["timeTagger.lineClockTriggerV = '' is blank; using the default 0.5."]


def test_whitespace_only_counts_as_blank():
    decoded, fixes = _decodeTop({'timeTagger': {'photonsTriggerV': '   '}})

    assert decoded.timeTagger.photonsTriggerV == 0.5
    assert len(fixes) == 1


def test_word_in_an_int_field_uses_the_default():
    decoded, fixes = _decodeTop({'timeTagger': {'photonsChannel': 'one'}})

    assert decoded.timeTagger.photonsChannel == 1
    assert fixes == ["timeTagger.photonsChannel = 'one' is not a whole number; "
                     "using the default 1."]


def test_decimal_string_in_an_int_field_stays_unreadable_as_before():
    decoded, fixes = _decodeTop({'timeTagger': {'photonsChannel': '2.0'}})

    assert decoded.timeTagger.photonsChannel == 1
    assert len(fixes) == 1


def test_blank_optional_int_uses_none():
    decoded, fixes = _decodeTop({'timeTagger': {'frameClockChannel': ''}})

    assert decoded.timeTagger.frameClockChannel is None
    assert fixes == ["timeTagger.frameClockChannel = '' is blank; using the default None."]


@pytest.mark.parametrize('text, expected', [('0.3', 0.3), (' -1.25 ', -1.25), ('2', 2.0)])
def test_numeric_string_in_a_float_field_still_reads(text, expected):
    decoded, fixes = _decodeTop({'timeTagger': {'photonsTriggerV': text}})

    assert decoded.timeTagger.photonsTriggerV == expected
    assert fixes == []


def test_numeric_string_in_an_int_field_still_reads():
    decoded, fixes = _decodeTop({'timeTagger': {'photonsChannel': '-3'}})

    assert decoded.timeTagger.photonsChannel == -3
    assert fixes == []


def test_negative_channel_number_is_kept():
    # A negative Time Tagger channel selects the falling edge; never "fix" it.
    decoded, fixes = _decodeTop({'timeTagger': {'photonsChannel': -1}})

    assert decoded.timeTagger.photonsChannel == -1
    assert fixes == []


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 'nan', '-inf', 'Infinity'])
def test_non_finite_float_uses_the_default(value):
    decoded, fixes = _decodeTop({'timeTagger': {'lineClockTriggerV': value}})

    assert decoded.timeTagger.lineClockTriggerV == 0.5
    assert len(fixes) == 1
    assert 'is not a finite number; using the default 0.5.' in fixes[0]


def test_non_finite_number_in_an_int_field_uses_the_default():
    decoded, fixes = _decodeTop({'timeTagger': {'photonsChannel': float('nan')}})

    assert decoded.timeTagger.photonsChannel == 1
    assert len(fixes) == 1


def test_object_in_a_number_field_uses_the_default():
    decoded, fixes = _decodeTop({'timeTagger': {'photonsTriggerV': {'v': 1}}})

    assert decoded.timeTagger.photonsTriggerV == 0.5
    assert len(fixes) == 1


def test_numeric_union_blank_is_dropped_and_numeric_string_reads():
    decoded, fixes = _decodeTop({'level': ''})
    assert decoded.level is None and len(fixes) == 1

    decoded, fixes = _decodeTop({'level': '0.3'})
    assert decoded.level == 0.3 and fixes == []


def test_blank_entry_of_a_number_mapping_is_ignored():
    decoded, fixes = _decodeTop({'channels': {'photons': '', 'sync': '2', 'line': 3}})

    assert decoded.channels == {'sync': 2, 'line': 3}
    assert fixes == ["channels.photons = '' is blank; entry ignored."]


# --- bool -------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('false', False), ('False', False), ('NO', False), ('off', False), ('0', False),
    ('true', True), ('TRUE', True), ('yes', True), ('On', True), ('1', True),
    (' false ', False),
])
def test_bool_words_become_real_bools(text, expected):
    decoded, fixes = _decodeSetup({'nidaq': {'simulation': text}})

    assert decoded.nidaq.simulation is expected
    assert len(fixes) == 1 and 'nidaq.simulation' in fixes[0]


@pytest.mark.parametrize('value, expected', [(0, False), (1, True)])
def test_zero_and_one_become_real_bools(value, expected):
    decoded, fixes = _decodeTop({'timeTagger': {'simulation': value}})

    assert decoded.timeTagger.simulation is expected
    assert fixes == []


@pytest.mark.parametrize('value', ['maybe', '', 2, 0.5, ['true']])
def test_anything_else_in_a_bool_field_uses_the_default(value):
    decoded, fixes = _decodeTop({'timeTagger': {'simulation': value}})

    assert decoded.timeTagger.simulation is False
    assert fixes == [f'timeTagger.simulation = {value!r} is not true or false; '
                     f'using the default False.']


def test_real_bools_are_untouched():
    decoded, fixes = _decodeSetup({'nidaq': {'simulation': True, 'startTrigger': False}})

    assert decoded.nidaq.simulation is True
    assert decoded.nidaq.startTrigger is False
    assert fixes == []


# --- sections and nesting ---------------------------------------------------

def test_blank_section_uses_the_default():
    decoded, fixes = _decodeSetup({'focusLock': '', 'nidaq': ''})

    assert decoded.focusLock is None
    assert decoded.nidaq.simulation is False
    assert len(fixes) == 2


def test_devices_in_a_mapping_are_walked():
    decoded, fixes = _decodeSetup({'lasers': {'488': {
        'managerName': 'NidaqLaserManager',
        'valueRangeMin': 0, 'valueRangeMax': 100, 'wavelength': 488,
        'valueRangeStep': '', 'transient': 'false',
    }}})

    laser = decoded.lasers['488']
    assert laser.valueRangeStep == 1.0
    assert laser.transient is False
    assert any(fix.startswith('lasers.488.valueRangeStep') for fix in fixes)


def test_view_setup_info_fields_are_walked():
    decoded, fixes = _decodeSetup({'rois': {'full': {'x': '0', 'y': 0, 'w': 'wide', 'h': 64}}},
                                  ViewSetupInfo)

    assert decoded.rois['full'].x == 0
    assert decoded.rois['full'].w is None  # required: there is no default to use
    assert fixes == ["rois.full.w = 'wide' is not a whole number; "
                     "left unset (the field has no default)."]


# --- left alone --------------------------------------------------------------

def test_manager_properties_are_left_exactly_as_written():
    properties = {'lineClockTriggerV': '', 'simulation': 'false', 'gain': 'nan'}
    decoded, fixes = _decodeSetup({'detectors': {'APD': {
        'managerName': 'SwabianTimeTaggerManager',
        'managerProperties': copy.deepcopy(properties),
    }}})

    assert decoded.detectors['APD'].managerProperties == properties
    assert fixes == []


def test_free_form_fields_lists_and_string_unions_are_left_alone():
    data = {
        'gains': ['', 1.0],
        'lineOrChannel': '',
        'free': {'x': '', 'flag': 'false'},
    }
    cleaned, fixes = sanitizeSetupData(data, _TopInfo)

    assert cleaned == data
    assert fixes == []


def test_unknown_keys_are_left_alone():
    data = {'availableWidgets': 'false', 'notAField': '', 'scan': None}
    cleaned, fixes = sanitizeSetupData(data, ViewSetupInfo)

    assert cleaned == data
    assert fixes == []


def test_input_is_not_modified():
    data = {'timeTagger': {'lineClockTriggerV': '', 'simulation': 'false'}}
    original = copy.deepcopy(data)

    sanitizeSetupData(data, _TopInfo)

    assert data == original


def test_non_object_top_level_is_passed_through():
    assert sanitizeSetupData(['x'], SetupInfo) == (['x'], [])


def _shippedSetups():
    root = Path(configfiletools.__file__).resolve().parents[2] / '_data' / 'user_defaults' / 'imcontrol_setups'
    return sorted(root.rglob('*.json'))


@pytest.mark.parametrize('path', _shippedSetups(), ids=lambda path: path.name)
def test_shipped_setups_need_no_fixes(path):
    data = json.loads(path.read_text(encoding='utf-8'))

    cleaned, fixes = sanitizeSetupData(data, ViewSetupInfo)

    assert fixes == []
    assert cleaned == data


def test_shipped_setups_are_found():
    assert _shippedSetups()


# --- through loadSetupInfo ---------------------------------------------------

class _RecordingLogger:
    def __init__(self):
        self.warnings = []
        self.exceptions = []

    def warning(self, message):
        self.warnings.append(message)

    def exception(self, message):
        self.exceptions.append(message)


@pytest.fixture
def setupDir(tmp_path, monkeypatch):
    monkeypatch.setattr(setupfolder, 'defaultSetupFolder', lambda: str(tmp_path))
    logger = _RecordingLogger()
    monkeypatch.setattr(configfiletools, '_logger', logger)
    return tmp_path, logger


def test_load_setup_info_warns_and_starts_with_defaults(setupDir):
    tmp_path, logger = setupDir
    text = json.dumps({
        'nidaq': {'simulation': 'false'},
        'teensyPulse': {'port': 'COM7', 'baud': ''},
        'detectors': {'APD': {
            'managerName': 'SwabianTimeTaggerManager',
            'managerProperties': {'lineClockTriggerV': ''},
        }},
    }, indent=4)
    (tmp_path / 'rig.json').write_text(text)

    setupInfo = configfiletools.loadSetupInfo(Options(setupFileName='rig.json'), ViewSetupInfo)

    assert setupInfo.nidaq.simulation is False
    assert setupInfo.teensyPulse.baud == 115200
    assert setupInfo.detectors['APD'].managerProperties == {'lineClockTriggerV': ''}
    assert logger.warnings == [
        "Setup file rig.json: nidaq.simulation = 'false' is a string, not true/false; "
        "read as false.",
        "Setup file rig.json: teensyPulse.baud = '' is blank; using the default 115200.",
    ]
    # Never rewritten on disk.
    assert (tmp_path / 'rig.json').read_text() == text


def test_load_setup_info_of_a_clean_file_logs_nothing(setupDir):
    tmp_path, logger = setupDir
    (tmp_path / 'clean.json').write_text(json.dumps({'nidaq': {'simulation': True}}))

    setupInfo = configfiletools.loadSetupInfo(Options(setupFileName='clean.json'), ViewSetupInfo)

    assert setupInfo.nidaq.simulation is True
    assert logger.warnings == []


def test_a_failing_tolerance_pass_falls_back_to_the_file_as_written(setupDir, monkeypatch):
    tmp_path, logger = setupDir
    (tmp_path / 'rig.json').write_text(json.dumps({'nidaq': {'simulation': True}}))

    def broken(*_args, **_kwargs):
        raise RuntimeError('walker bug')

    monkeypatch.setattr(configfiletools, 'sanitizeSetupData', broken)

    setupInfo = configfiletools.loadSetupInfo(Options(setupFileName='rig.json'), ViewSetupInfo)

    assert setupInfo.nidaq.simulation is True
    assert len(logger.exceptions) == 1


def test_save_round_trip_after_a_tolerated_value(setupDir):
    tmp_path, logger = setupDir
    (tmp_path / 'rig.json').write_text(json.dumps({
        'teensyPulse': {'port': 'COM7', 'baud': '', 'mockNChannels': 8},
    }))
    options = Options(setupFileName='rig.json')
    first = configfiletools.loadSetupInfo(options, ViewSetupInfo)

    configfiletools.saveSetupInfo(options, first)
    logger.warnings.clear()
    second = configfiletools.loadSetupInfo(options, ViewSetupInfo)

    assert second == first
    assert logger.warnings == []
    saved = json.loads((tmp_path / 'rig.json').read_text())
    assert saved['teensyPulse']['baud'] == 115200
    assert saved['teensyPulse']['mockNChannels'] == 8
    assert 'nidaq' not in saved  # pruning still drops all-default sections


def test_nan_float_value_reads_as_finite_default():
    decoded, _ = _decodeSetup({'focusLock': {
        'camera': 'c', 'positioner': 'p', 'updateFreq': 10,
        'frameCropx': 0, 'frameCropy': 0, 'frameCropw': 8, 'frameCroph': 8,
        'piKp': 1.0, 'piKi': 0.1, 'reacquireTolerancePx': float('nan'),
    }})

    assert math.isfinite(decoded.focusLock.reacquireTolerancePx)
    assert decoded.focusLock.reacquireTolerancePx == 0.5


# ---------------------------------------------------------------------------
# The same findings, as records, for the config editor's validation
# ---------------------------------------------------------------------------

def test_find_reports_each_unreadable_value_with_its_path_and_whether_it_is_dropped():
    from imswitch.imcontrol.model.setup_file_values import findUnreadableSetupValues

    data = {'tiling': {'xyPositioner': 'XY', 'defaultTileStepUm': 'None'},
            'nidaq': {'simulation': 'false'}}
    fixes = findUnreadableSetupValues(data, SetupInfo)
    byPath = {fix.path: fix for fix in fixes}
    assert set(byPath) == {'tiling.defaultTileStepUm', 'nidaq.simulation'}
    assert byPath['tiling.defaultTileStepUm'].dropped is True
    assert "'None'" in byPath['tiling.defaultTileStepUm'].message
    assert byPath['nidaq.simulation'].dropped is False
    # the loader's messages are the same sentences
    _cleaned, messages = sanitizeSetupData(data, SetupInfo)
    assert sorted(messages) == sorted(fix.message for fix in fixes)
    assert data['tiling']['defaultTileStepUm'] == 'None', 'input untouched'


def test_find_reports_nothing_for_a_clean_file():
    from imswitch.imcontrol.model.setup_file_values import findUnreadableSetupValues

    assert findUnreadableSetupValues({'tiling': {'xyPositioner': 'XY'}}, SetupInfo) == []
