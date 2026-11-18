"""The config editor's validation finds what ImControl's loader cannot read.

The loader leaves such a value out, warns and uses the default, so a slip no
longer stops ImControl -- but the file still says something that will not
happen. On the rig (2026-11-18) a new Timelapse section held
``"illuminationSettleMs": "None"`` and ImControl refused to start; the editor
had shown no issue. Validation now reports it with the loader's own rules.
"""

import json
from pathlib import Path

import pytest

from imswitch.imcontrol.model.plugins.registry import build_default_registry
from imswitch.imcontrol.model.plugins.validation import validate_setup_data

SETUPS = Path(__file__).resolve().parents[3] / '_data' / 'user_defaults' / 'imcontrol_setups'


def _values(data):
    report = validate_setup_data(data, build_default_registry(discover=False))
    return [d for d in report.diagnostics if d.code.startswith('value.')], report


def test_a_number_written_as_none_is_an_error_naming_path_value_and_default():
    found, report = _values({'tiling': {'xyPositioner': 'XY', 'defaultTileStepUm': 'None'}})
    assert len(found) == 1
    diag = found[0]
    assert diag.severity == 'error' and diag.code == 'value.unreadable'
    assert diag.path == ('tiling', 'defaultTileStepUm')
    assert "'None'" in diag.message and '100.0' in diag.message
    assert report.has_errors


@pytest.mark.parametrize('value', ['', '   ', 'fast', [1], {'a': 1}])
def test_blank_words_and_containers_in_a_number_field_are_errors(value):
    found, _ = _values({'tiling': {'xyPositioner': 'XY', 'defaultTileStepUm': value}})
    assert [d.code for d in found] == ['value.unreadable']


def test_a_quoted_boolean_is_a_warning_because_it_is_read_as_meant():
    found, report = _values({'nidaq': {'simulation': 'false'}})
    assert [(d.severity, d.code) for d in found] == [('warning', 'value.quoted_bool')]
    assert 'read as false' in found[0].message


def test_a_device_value_is_found_too():
    found, _ = _values({'lasers': {'L488': {
        'managerName': 'NidaqLaserManager', 'valueRangeMin': '', 'valueRangeMax': 100,
        'wavelength': 488, 'managerProperties': {}}}})
    assert ('lasers', 'L488', 'valueRangeMin') in [d.path for d in found]


def test_numeric_strings_and_nulls_are_not_reported():
    found, _ = _values({'tiling': {'xyPositioner': 'XY', 'defaultTileStepUm': '50',
                                   'zPositioner': None}})
    assert found == []


@pytest.mark.parametrize('path', sorted(SETUPS.glob('*.json')), ids=lambda p: p.name)
def test_shipped_setups_have_no_unreadable_values(path):
    found, _ = _values(json.loads(path.read_text(encoding='utf-8')))
    assert found == [], [d.message for d in found]


def test_the_editor_wrapper_passes_value_findings_through():
    pytest.importorskip('PyQt5')
    from imswitch.imcontrol.view.configeditor import editor

    issues = editor._collect_xref_issues(
        {'tiling': {'xyPositioner': 'XY', 'defaultTileStepUm': 'None'}})
    assert any(severity == 'error' and 'Unreadable value' in html
               for severity, html in issues)
