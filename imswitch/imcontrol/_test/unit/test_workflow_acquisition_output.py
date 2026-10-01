"""Shared workflow image output: self-describing, atomic, never data-losing."""

import numpy as np
import pytest
import tifffile

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
)
from imswitch.imcontrol.model.workflows.acquisition_output import (
    acquisition_description,
    save_acquisition_tiff,
)


def _layout(frames=6):
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("time", "time", frames // 2),
            AcquisitionLoop("state", "condition", 2, labels=("signal", "background")),
        ),
        modality="widefield-starss",
    )


def test_saved_tiff_carries_its_acquisition_layout(tmp_path):
    stack = np.zeros((6, 4, 5), dtype=np.uint16)

    path = save_acquisition_tiff(
        tmp_path / "stack.tif",
        stack,
        layout=_layout(),
        name="Cam",
        annotations={"WidefieldStarss:polarization_role": "H"},
    )

    with tifffile.TiffFile(path) as handle:
        description = handle.pages[0].description
    assert "AcquisitionLayout:json" in description
    assert "WidefieldStarss:polarization_role" in description
    np.testing.assert_array_equal(tifffile.imread(path), stack)


def test_a_three_frame_stack_is_not_stored_as_an_rgb_image(tmp_path):
    """tifffile reads (3, H, W) as RGB unless told the data is grayscale."""
    stack = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)

    path = save_acquisition_tiff(tmp_path / "three.tif", stack)

    np.testing.assert_array_equal(tifffile.imread(path), stack)


def test_metadata_failure_never_costs_the_measurement(tmp_path):
    """A workflow that has finished measuring must still save its data."""
    stack = np.zeros((6, 4, 5), dtype=np.uint16)
    # A layout whose loops do not match the frame axis cannot be encoded.
    broken = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(AcquisitionLoop("bad", "time", -1),),
    )

    assert acquisition_description(stack, layout=broken, name="Cam") is None
    path = save_acquisition_tiff(tmp_path / "still-saved.tif", stack, layout=broken)

    np.testing.assert_array_equal(tifffile.imread(path), stack)


def test_no_layout_and_no_annotations_writes_a_plain_tiff(tmp_path):
    stack = np.zeros((2, 4, 5), dtype=np.uint16)

    assert acquisition_description(stack, layout=None, name="Cam") is None
    path = save_acquisition_tiff(tmp_path / "plain.tif", stack)

    np.testing.assert_array_equal(tifffile.imread(path), stack)


def test_the_file_appears_only_once_it_is_complete(tmp_path, monkeypatch):
    """An interrupted write must not leave a short stack that reads as valid."""
    from imswitch.imcontrol.model.workflows import acquisition_output

    target = tmp_path / "interrupted.tif"

    def explode(content, path):
        raise OSError("disk full")

    monkeypatch.setattr(acquisition_output, "atomic_write", explode)

    with pytest.raises(OSError):
        save_acquisition_tiff(target, np.zeros((6, 4, 5), dtype=np.uint16))

    assert not target.exists()
