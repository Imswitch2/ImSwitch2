"""Early STOP terminal is independent of the HDF5 writer/reader lifetime."""

import json

import pytest

from imswitch.imcommon.model.hdf5_lapse_terminal import (
    read_early_stop, terminal_path, write_early_stop,
)


def test_stopped_lapse_terminal_is_outside_the_finalized_hdf5(tmp_path):
    anchor = tmp_path / "scan0_CAM.hdf5"
    anchor.write_bytes(b"data stays untouched")
    out = write_early_stop(anchor, completed=2, planned=5)
    assert out == terminal_path(anchor)
    assert read_early_stop(anchor, expected_total=5) == 2
    assert read_early_stop(anchor, expected_total=6) is None
    assert anchor.read_bytes() == b"data stays untouched"
    assert not list(tmp_path.glob("*.tmp"))


def test_no_unverified_completion_is_inferred_from_a_sidecar(tmp_path):
    anchor = tmp_path / "scan0_CAM.hdf5"
    with pytest.raises(FileNotFoundError):
        write_early_stop(anchor, completed=2, planned=5)
    anchor.touch()
    for completed, planned in ((0, 5), (5, 5), (6, 5)):
        with pytest.raises(ValueError):
            write_early_stop(anchor, completed=completed, planned=planned)
    marker = terminal_path(anchor)
    marker.write_text(json.dumps({
        "version": 1, "outcome": "stopped_early",
        "anchor": "wrong.hdf5", "completed_timepoints": 2,
        "planned_timepoints": 5,
    }))
    assert read_early_stop(anchor, expected_total=5) is None
    marker.write_text("{truncated")
    assert read_early_stop(anchor, expected_total=5) is None
    marker.unlink()
    assert read_early_stop(anchor, expected_total=5) is None


def test_zarr_operation_terminal_preserves_store_and_distinguishes_abort(tmp_path):
    from imswitch.imcommon.model.hdf5_lapse_terminal import (
        read_lapse_terminal, write_lapse_terminal,
    )

    anchor = tmp_path / 'recording.zarr'
    anchor.mkdir()
    (anchor / 'sentinel').write_text('existing scientific data')
    marker = write_early_stop(anchor, completed=2, planned=5)
    assert marker == terminal_path(anchor)
    assert read_early_stop(anchor, expected_total=5) == 2
    assert read_lapse_terminal(anchor, expected_total=5)['outcome'] == 'stopped_early'

    write_lapse_terminal(anchor, completed=2, planned=5, outcome='aborted')
    assert read_early_stop(anchor, expected_total=5) is None
    assert read_lapse_terminal(anchor, expected_total=5)['completed_timepoints'] == 2
    assert read_lapse_terminal(anchor, expected_total=6) is None
    assert (anchor / 'sentinel').read_text() == 'existing scientific data'
    anchor.rename(tmp_path / 'moved.zarr')
    assert read_lapse_terminal(anchor, expected_total=5) is None


def test_empty_interrupted_zarr_has_terminal_without_completed_data(tmp_path):
    from imswitch.imcommon.model.hdf5_lapse_terminal import (
        read_lapse_terminal, write_lapse_terminal,
    )
    anchor = tmp_path / 'empty.zarr'
    anchor.mkdir()
    write_lapse_terminal(anchor, completed=0, planned=5, outcome='aborted')
    assert read_lapse_terminal(anchor)['completed_timepoints'] == 0
    with pytest.raises(ValueError):
        write_early_stop(anchor, completed=0, planned=5)
