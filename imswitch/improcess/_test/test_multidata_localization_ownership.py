from types import SimpleNamespace

from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController,
)


def _source(path, dataset="Camera", *, kind="image"):
    return SimpleNamespace(
        dataPath=str(path),
        datasetName=dataset,
        sourceKind=kind,
        _file=None,
    )


def test_same_data_source_accepts_distinct_objects_for_same_dataset(tmp_path):
    path = tmp_path / "scan0.h5"
    left = _source(path)
    right = _source(path)

    assert ReconstructorManagerController._same_data_source(left, right)


def test_same_data_source_rejects_different_acquisition_same_detector(tmp_path):
    # Multi-data localization reuse must not be keyed by detector identity alone.
    current = _source(tmp_path / "other_scan.h5")
    first_batch_item = _source(tmp_path / "scan0.h5")

    assert not ReconstructorManagerController._same_data_source(
        current, first_batch_item
    )


def test_same_data_source_rejects_different_dataset_in_same_file(tmp_path):
    path = tmp_path / "recording.h5"

    assert not ReconstructorManagerController._same_data_source(
        _source(path, "scan0/Camera"),
        _source(path, "scan1/Camera"),
    )
