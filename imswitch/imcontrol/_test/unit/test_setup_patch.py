"""Saving a panel added at run time patches the setup file, nothing more.

The maintainer's rule: the file is read as it is on disk; only
``availableWidgets`` and the panel's own section change; unknown keys (top
level and nested), key order and everything unrelated survive; the write is
atomic; and "save as new" never overwrites.
"""

import json
import os

import pytest

from imswitch.imcontrol.model import setup_patch


pytestmark = pytest.mark.nohardware


HAND_EDITED = """{
  "zzzFirstUnknown": {"keep": [1, 2, 3]},
  "detectors": {
    "Camera": {"managerName": "MockCameraManager", "vendorNote": "unknown nested key",
               "managerProperties": {"cameraListIndex": "mock", "customThing": 7}}
  },
  "laserPresets": {"Preset A": {"488": {"value": 3}}},
  "availableWidgets": ["Settings", "View", "Image"],
  "tiling": {"xyPositioner": "Old", "futureKey": true},
  "aaaLastUnknown": "µm stays µm"
}
"""


@pytest.fixture
def setupFile(tmp_path):
    path = tmp_path / 'scope.json'
    path.write_text(HAND_EDITED, encoding='utf-8')
    return path


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_only_the_panel_and_its_section_change(setupFile):
    before = _load(setupFile)

    result = setup_patch.patchSetupFile(
        str(setupFile), addWidgets=['Tiling'],
        sections={'tiling': {'xyPositioner': 'Stage', 'futureKey': True}},
    )

    after = _load(setupFile)
    assert after['availableWidgets'] == ['Settings', 'View', 'Image', 'Tiling']
    assert after['tiling'] == {'xyPositioner': 'Stage', 'futureKey': True}
    for key in before:
        if key not in ('availableWidgets', 'tiling'):
            assert after[key] == before[key], key
    # Key order as the file had it, unknown keys at both ends included.
    assert list(after) == list(before)
    assert result.changes == ['added Tiling to availableWidgets', 'replaced the tiling section']
    assert result.path == str(setupFile)


def test_unrelated_in_memory_changes_are_not_written(setupFile):
    """Nothing but the file and the two arguments goes in: a laser preset
    changed in the running session stays out of it."""
    setup_patch.patchSetupFile(str(setupFile), addWidgets=['Console'])

    assert _load(setupFile)['laserPresets'] == {'Preset A': {'488': {'value': 3}}}
    assert 'tiling' in _load(setupFile)  # untouched, not dropped


def test_the_file_keeps_its_indentation_and_text(setupFile):
    setup_patch.patchSetupFile(str(setupFile), addWidgets=['Console'])

    text = setupFile.read_text(encoding='utf-8')
    assert '\n  "zzzFirstUnknown"' in text  # two spaces, as written
    assert 'µm stays µm' in text            # not escaped
    assert text.endswith('\n')


def test_nothing_is_written_when_nothing_changes(setupFile):
    stamp = os.stat(setupFile).st_mtime_ns
    result = setup_patch.patchSetupFile(str(setupFile), addWidgets=['Image'])

    assert result.changes == []
    assert os.stat(setupFile).st_mtime_ns == stamp
    assert setupFile.read_text(encoding='utf-8') == HAND_EDITED


def test_available_widgets_true_is_left_alone(tmp_path):
    path = tmp_path / 'all.json'
    path.write_text('{"availableWidgets": true}\n')

    setup_patch.patchSetupFile(str(path), addWidgets=['Tiling'], sections={'tiling': {'xyPositioner': 'S'}})

    assert _load(path) == {'availableWidgets': True, 'tiling': {'xyPositioner': 'S'}}


def test_the_write_is_atomic(setupFile, monkeypatch):
    """A failure while writing leaves the old file whole and no temp file."""
    real = os.replace

    def failingReplace(src, dst):
        raise OSError('disk full')

    monkeypatch.setattr(setup_patch.os, 'replace', failingReplace)
    with pytest.raises(OSError):
        setup_patch.patchSetupFile(str(setupFile), addWidgets=['Console'])
    monkeypatch.setattr(setup_patch.os, 'replace', real)

    assert setupFile.read_text(encoding='utf-8') == HAND_EDITED
    assert sorted(p.name for p in setupFile.parent.iterdir()) == ['scope.json']


def test_the_write_goes_through_replace(setupFile, monkeypatch):
    calls = []
    real = os.replace
    monkeypatch.setattr(setup_patch.os, 'replace',
                        lambda src, dst: (calls.append((src, dst)), real(src, dst)))

    setup_patch.patchSetupFile(str(setupFile), addWidgets=['Console'])

    [(source, target)] = calls
    assert target == str(setupFile)
    assert os.path.dirname(source) == str(setupFile.parent)


def test_save_as_new_copies_the_file_into_a_subfolder(setupFile, tmp_path):
    folder = tmp_path / 'setups'
    folder.mkdir()

    result = setup_patch.saveAsNewSetup(
        str(setupFile), str(folder), 'lab/with_tiling', addWidgets=['Tiling'],
        sections={'tiling': {'xyPositioner': 'Stage'}},
    )

    assert result.path == str(folder / 'lab' / 'with_tiling.json')
    new = _load(folder / 'lab' / 'with_tiling.json')
    assert new['tiling'] == {'xyPositioner': 'Stage'}
    assert new['zzzFirstUnknown'] == {'keep': [1, 2, 3]}
    assert setupFile.read_text(encoding='utf-8') == HAND_EDITED  # source untouched


def test_save_as_new_refuses_to_overwrite(setupFile, tmp_path):
    folder = tmp_path / 'setups'
    (folder / 'lab').mkdir(parents=True)
    (folder / 'lab' / 'taken.json').write_text('{"mine": 1}')

    with pytest.raises(FileExistsError):
        setup_patch.saveAsNewSetup(str(setupFile), str(folder), 'lab/taken.json',
                                   addWidgets=['Tiling'])
    assert _load(folder / 'lab' / 'taken.json') == {'mine': 1}


@pytest.mark.parametrize('name', ['', '../outside', '/abs/path.json', 'sub/'])
def test_save_as_new_refuses_names_outside_the_folder(setupFile, tmp_path, name):
    folder = tmp_path / 'setups'
    folder.mkdir()
    with pytest.raises(ValueError):
        setup_patch.saveAsNewSetup(str(setupFile), str(folder), name)


def test_read_section_returns_the_raw_section(setupFile):
    assert setup_patch.readSection(str(setupFile), 'tiling') == {
        'xyPositioner': 'Old', 'futureKey': True}
    assert setup_patch.readSection(str(setupFile), 'autofocus') is None
    assert setup_patch.readSection(str(setupFile.parent / 'missing.json'), 'tiling') is None
