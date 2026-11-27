"""Calibration labels are separate from hashed stage-tilt measurement data."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


# Pure file operations; avoid pulling in Qt and hardware managers for these tests.
module_path = Path(__file__).resolve().parents[2] / 'model' / 'workflows' / 'tilt_calibration_labels.py'
spec = importlib.util.spec_from_file_location('tilt_calibration_labels', module_path)
labels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(labels)


def _fit_file(folder: Path):
    folder.mkdir(parents=True)
    fit_path = folder / 'tilt_fit.json'
    fit_path.write_text(json.dumps({
        'version': 1, 'created_utc': '2026-11-23T14:12:00+00:00',
        'fit': {'slope_x_um_per_um': 0.01},
    }), encoding='utf-8')
    return fit_path


def test_name_does_not_change_hashed_fit(tmp_path):
    path = _fit_file(tmp_path / 'calibration_001')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    labels.save_calibration_name(path, '  My coverslip #7  ')
    assert labels.calibration_display_name(path) == 'My coverslip #7'
    assert json.loads((path.parent / 'tilt_metadata.json').read_text()) == {
        'name': 'My coverslip #7'
    }
    labels.save_calibration_name(path, 'Re-calibrated')
    assert labels.calibration_name(path) == 'Re-calibrated'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_existing_calibration_falls_back_to_recorded_timestamp(tmp_path):
    path = _fit_file(tmp_path / 'calibration_002')
    assert labels.calibration_name(path) == ''
    assert labels.calibration_display_name(path) == 'Calibration 2026-11-23 14:12'
    labels.save_calibration_name(path, 'Custom')
    labels.save_calibration_name(path, '')
    assert labels.calibration_display_name(path) == 'Calibration 2026-11-23 14:12'
    assert not (path.parent / 'tilt_metadata.json').exists()


def test_missing_timestamp_falls_back_to_folder(tmp_path):
    path = tmp_path / 'Imported' / 'tilt_fit.json'
    assert labels.calibration_display_name(path) == 'Imported'


def test_reject_overlong_name_without_editing_fit(tmp_path):
    path = _fit_file(tmp_path / 'calibration_003')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='at most 160'):
        labels.save_calibration_name(path, 'X' * 161)
    assert path.read_bytes() == before
    assert not (path.parent / 'tilt_metadata.json').exists()


def test_malformed_name_metadata_does_not_hide_valid_fit(tmp_path):
    path = _fit_file(tmp_path / 'calibration_004')
    (path.parent / 'tilt_metadata.json').write_text('{invalid', encoding='utf-8')
    assert labels.calibration_display_name(path) == 'Calibration 2026-11-23 14:12'
