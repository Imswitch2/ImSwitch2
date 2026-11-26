"""The three stores share ``<ImSwitchConfig>/calibrations/``: laser-power records
(``*.calib.json``), smart-pipeline transforms (``*.json``) and the shared
transform module's records (``transforms/*.json``). None may trip over another's
files, in either direction -- checked on one folder holding all three, as a bead
calibration and a laser calibration leave it.
"""
import pytest

from imswitch.imcontrol.model import bead_calibration as bc
from imswitch.imcontrol.model.calibrations import CalibrationStore, parse_legacy_csv
from imswitch.imcontrol.model.smartpipeline.calibrations import load_calibrations

from .test_bead_calibration_mock import files  # noqa: F401  (the fixture)
from .test_calibration_records import MEASURED_W


@pytest.fixture
def shared_folder(files, tmp_path):  # noqa: F811
    folder = tmp_path / 'calibrations'
    bc.calibrate_from_files(files['camera_beads_h5'], files['apd_hdf5'], folder=folder)
    stored = CalibrationStore(folder).add(parse_legacy_csv(MEASURED_W))
    return folder, stored


def test_the_pipeline_loader_takes_the_bead_transform_and_leaves_the_laser_record(shared_folder):
    folder, _stored = shared_folder
    registry = load_calibrations(folder)
    assert 'cam_to_scan' in registry and len(list(registry.ids())) == 1


def test_the_laser_store_lists_only_laser_records_and_reports_nothing_unreadable(shared_folder):
    folder, stored = shared_folder
    records, unreadable = CalibrationStore(folder).scan()
    assert [r.record.id for r in records] == [stored.record.id]
    assert unreadable == []


def test_the_transforms_subfolder_is_not_read_as_pipeline_transforms(shared_folder):
    folder, _stored = shared_folder
    assert (folder / 'transforms').is_dir() and list((folder / 'transforms').glob('*.json'))
    assert 'cam_to_scan' in load_calibrations(folder)     # by reference, once; the record is not a second entry
