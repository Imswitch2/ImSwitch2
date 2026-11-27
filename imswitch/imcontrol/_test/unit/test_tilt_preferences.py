"""Persistence is scoped to one hardware setup; enable state is never persisted."""

import json

from imswitch.imcontrol.model.workflows.tilt_preferences import (
    remember_calibration, selected_calibration,
)
from imswitch.imcontrol.model.workflows.stage_tilt_calibration import (
    new_tilt_calibration_folder,
)


def test_selected_calibration_is_setup_scoped(tmp_path):
    prefs = tmp_path / 'tilt_prefs.json'
    assert selected_calibration('hardware_a', prefs) == ''
    path_a = tmp_path / 'A' / 'tilt_fit.json'
    path_b = tmp_path / 'B' / 'tilt_fit.json'
    remember_calibration('hardware_a', path_a, prefs)
    remember_calibration('hardware_b', path_b, prefs)
    assert selected_calibration('hardware_a', prefs) == str(path_a.resolve())
    assert selected_calibration('hardware_b', prefs) == str(path_b.resolve())
    assert json.loads(prefs.read_text()) == {
        'hardware_a': str(path_a.resolve()), 'hardware_b': str(path_b.resolve()),
    }


def test_corrupt_preferences_are_not_silently_overwritten(tmp_path):
    import pytest
    prefs = tmp_path / 'tilt_prefs.json'
    prefs.write_text('{broken json')
    with pytest.raises(ValueError):
        # JSONDecodeError is a ValueError
        remember_calibration('hardware_a', tmp_path / 'tilt_fit.json', prefs)
    assert prefs.read_text() == '{broken json'


def test_calibration_uses_workspace_not_recording_root(monkeypatch, tmp_path):
    from imswitch.imcommon.model import dirtools
    monkeypatch.setattr(dirtools.UserFileDirs, 'Root', str(tmp_path))
    path = new_tilt_calibration_folder()
    assert path.parent == tmp_path / 'imcontrol' / 'tilt_calibrations'
    assert path.name.startswith('calibration_')


def test_named_calibration_folder_is_searchable_and_windows_safe(monkeypatch, tmp_path):
    from imswitch.imcommon.model import dirtools
    monkeypatch.setattr(dirtools.UserFileDirs, 'Root', str(tmp_path))
    folder = new_tilt_calibration_folder('  Coverslip #7 / 40× objective: test?  ')
    assert folder.parent == tmp_path / 'imcontrol' / 'tilt_calibrations'
    assert folder.name.startswith('calibration_')
    assert folder.name.endswith('_Coverslip_#7_40×_objective_test')
    assert not any(char in folder.name for char in '<>:"/\\|?*')
    unnamed = new_tilt_calibration_folder(' / : ? ')
    assert unnamed.name.startswith('calibration_')
    assert ' ?' not in unnamed.name
    # Sanitize the folder only; the separate display-name metadata can keep
    # the original spelling and remains editable without moving the folder.
    from imswitch.imcontrol.model.workflows.tilt_calibration_labels import (
        save_calibration_name, calibration_name,
    )
    folder.mkdir(parents=True)
    fit_path = folder / 'tilt_fit.json'
    fit_path.write_text('{}', encoding='utf-8')
    save_calibration_name(fit_path, 'Renamed / calibration')
    assert calibration_name(fit_path) == 'Renamed / calibration'
    assert fit_path.parent == folder
