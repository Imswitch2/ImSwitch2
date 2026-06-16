import numpy as np
import pytest

from imswitch.imcontrol.model.bead_recognition import (
    BeadAcquisitionConfig,
    BeadAnalysisParameters,
    BeadRecResultRecord,
    BeadWorkerUpdate,
    RoiBounds,
    analyze_donut,
    append_roi_means,
    create_reconstruction_buffer,
    find_bead_center,
    find_center_donut,
    find_center_foci,
    mean_intensity_in_roi,
    normalize_roi_bounds,
    reconstruction_image,
    rescale_reconstruction_to_pixel_size,
)


def test_analysis_parameters_keep_legacy_defaults_and_mapping_round_trip():
    params = BeadAnalysisParameters()

    assert params.as_dict() == {
        "min_area": 50,
        "max_area": 1000,
        "tol_peaks_pos": 10,
        "thresh_coeff": 0.2,
        "erosion_coeff": 0.2,
    }
    assert BeadAnalysisParameters.from_mapping(params.as_dict()) == params


def test_result_record_metadata_excludes_image_pixels():
    image = np.ones((2, 3))
    record = BeadRecResultRecord(
        name="run_1",
        image=image,
        axial_name="XY",
        source_path="/tmp/run_1.tif",
        timestamp=123.0,
        scaled=True,
    )

    assert record.metadata() == {
        "name": "run_1",
        "axial_name": "XY",
        "source_path": "/tmp/run_1.tif",
        "timestamp": 123.0,
        "scaled": True,
    }
    assert "image" not in record.metadata()


def test_acquisition_config_validates_scan_dims_and_poll_interval():
    config = BeadAcquisitionConfig.from_scan_dims((4, 3, 1), poll_interval_s=0.01)

    assert config.scan_dims == (4, 3)
    assert config.total_pixels == 12
    assert config.wrap is True
    assert config.poll_interval_s == pytest.approx(0.01)

    with pytest.raises(ValueError, match="positive"):
        BeadAcquisitionConfig.from_scan_dims((4, 3), poll_interval_s=0)


def test_worker_update_carries_progress_without_mutating_buffer():
    buffer = np.arange(6, dtype=float)
    update = BeadWorkerUpdate(
        buffer=buffer,
        filled_pixels=3,
        total_pixels=6,
        frames_written=2,
        wrapped=False,
    )

    assert update.buffer is buffer
    assert update.filled_pixels == 3
    assert update.total_pixels == 6
    assert update.frames_written == 2
    assert update.wrapped is False


def test_normalize_roi_bounds_rounds_outward_and_clips_to_image():
    roi = normalize_roi_bounds((-2.2, 1.2, 4.1, 6.8), (5, 4))

    assert roi == RoiBounds(x0=0, y0=1, x1=4, y1=5)
    assert roi.width == 4
    assert roi.height == 4
    assert roi.as_slices() == (slice(1, 5), slice(0, 4))


def test_normalize_roi_bounds_accepts_reversed_visual_coordinates():
    roi = normalize_roi_bounds((4.0, 3.0, 1.0, 0.0), (5, 6))

    assert roi == RoiBounds(x0=1, y0=0, x1=4, y1=3)


def test_normalize_roi_bounds_rejects_empty_or_outside_roi():
    with pytest.raises(ValueError, match="does not overlap"):
        normalize_roi_bounds((10, 10, 12, 12), (5, 5))

    with pytest.raises(ValueError, match="exactly four"):
        normalize_roi_bounds((0, 1, 2), (5, 5))


def test_mean_intensity_in_roi_uses_row_column_slicing():
    frame = np.arange(25, dtype=float).reshape(5, 5)
    roi = RoiBounds(x0=1, y0=2, x1=4, y1=5)

    assert mean_intensity_in_roi(frame, roi) == pytest.approx(np.mean(frame[2:5, 1:4]))


def test_mean_intensity_in_roi_keeps_fractional_mean_for_integer_frames():
    """Integer detector frames must not truncate the ROI mean to an int.

    The recording dtype contract now hands BeadRec native integer frames
    (e.g. uint16) instead of float64. A 2x2 ROI of [1, 2, 1, 2] averages to
    1.5 — regression-guard that this stays 1.5, not 1, and that the result is
    a Python float.
    """
    frame = np.array([[1, 2], [1, 2]], dtype=np.uint16)
    roi = RoiBounds(x0=0, y0=0, x1=2, y1=2)

    result = mean_intensity_in_roi(frame, roi)
    assert result == pytest.approx(1.5)
    assert isinstance(result, float)


def test_append_roi_means_is_float_for_integer_frames():
    """Reconstruction buffer stays float (no integer truncation) for int frames."""
    buffer = create_reconstruction_buffer((2, 1))
    frame = np.array([[1, 2], [1, 2]], dtype=np.uint16)  # ROI mean 1.5

    update = append_roi_means(buffer, 0, [frame], RoiBounds(0, 0, 2, 2))

    assert np.issubdtype(update.buffer.dtype, np.floating)
    assert update.buffer[0] == pytest.approx(1.5)


def test_reconstruction_buffer_and_display_shape_use_existing_orientation():
    buffer = create_reconstruction_buffer((3, 2))
    buffer[:] = np.arange(6)

    image = reconstruction_image(buffer, (3, 2))

    assert image.shape == (2, 3)
    np.testing.assert_array_equal(image, np.array([[0, 1, 2], [3, 4, 5]]))


def test_append_roi_means_returns_copy_and_next_index():
    frames = [
        np.full((4, 4), 2.0),
        np.full((4, 4), 5.0),
    ]
    buffer = create_reconstruction_buffer((2, 2))

    update = append_roi_means(buffer, 1, frames, RoiBounds(0, 0, 2, 2))

    np.testing.assert_array_equal(buffer, np.zeros(4))
    np.testing.assert_array_equal(update.buffer, np.array([0.0, 2.0, 5.0, 0.0]))
    assert update.next_index == 3
    assert update.frames_written == 2
    assert update.wrapped is False


def test_append_roi_means_can_wrap_or_stop_at_buffer_end():
    frames = [
        np.full((2, 2), 1.0),
        np.full((2, 2), 2.0),
        np.full((2, 2), 3.0),
    ]
    buffer = np.zeros(2)

    wrapped = append_roi_means(buffer, 1, frames, RoiBounds(0, 0, 2, 2))
    stopped = append_roi_means(buffer, 1, frames, RoiBounds(0, 0, 2, 2), wrap=False)

    np.testing.assert_array_equal(wrapped.buffer, np.array([2.0, 3.0]))
    assert wrapped.next_index == 0
    assert wrapped.frames_written == 3
    assert wrapped.wrapped is True

    np.testing.assert_array_equal(stopped.buffer, np.array([0.0, 1.0]))
    assert stopped.next_index == 2
    assert stopped.frames_written == 1
    assert stopped.wrapped is False


def test_rescale_reconstruction_uses_physical_step_sizes():
    image = np.arange(6, dtype=float).reshape(2, 3)

    same = rescale_reconstruction_to_pixel_size(image, (0.1, 0.1))
    stretched_y = rescale_reconstruction_to_pixel_size(image, (0.1, 0.2))
    stretched_x = rescale_reconstruction_to_pixel_size(image, (0.2, 0.1))

    assert same is image
    assert stretched_y.shape == (4, 3)
    assert stretched_x.shape == (2, 6)


def test_rescale_reconstruction_rejects_invalid_step_sizes():
    with pytest.raises(ValueError, match="positive"):
        rescale_reconstruction_to_pixel_size(np.zeros((2, 2)), (0.0, 1.0))


def test_find_center_foci_returns_maximum_inside_valid_component():
    image = np.zeros((30, 30), dtype=float)
    image[8:18, 9:19] = 100
    image[12, 14] = 120

    result = find_center_foci(image)

    assert result.found is True
    assert result.coord == (12, 14)
    assert result.reason is None


def test_find_center_foci_returns_failure_reason_without_plotting_side_effects():
    result = find_center_foci(np.zeros((12, 12), dtype=float))

    assert result.found is False
    assert result.coord is None
    assert result.reason == "no connected components after thresholding"


def test_analyze_donut_returns_structured_metrics_and_intermediates():
    y, x = np.ogrid[:41, :41]
    radius = np.sqrt((y - 20) ** 2 + (x - 20) ** 2)
    image = np.full((41, 41), 10.0)
    image[(radius >= 4) & (radius <= 10)] = 100
    image[20, 20] = 0

    result = analyze_donut(image)

    assert result.accepted is True
    assert result.reason is None
    assert result.coord == (20, 20)
    assert result.background == pytest.approx(10.0)
    assert result.fill_x == pytest.approx(-1 / 9)
    assert result.fill_y == pytest.approx(-1 / 9)
    assert result.binarized.shape == (47, 47)
    assert result.selected_mask.shape == image.shape
    assert result.eroded_mask.shape == image.shape
    assert result.background_mask.shape == image.shape
    assert result.peak_x_positions is not None
    assert result.peak_y_positions is not None


def test_find_bead_center_dispatches_legacy_modes():
    y, x = np.ogrid[:41, :41]
    radius = np.sqrt((y - 20) ** 2 + (x - 20) ** 2)
    donut = np.full((41, 41), 10.0)
    donut[(radius >= 4) & (radius <= 10)] = 100
    donut[20, 20] = 0

    foci = np.zeros((30, 30), dtype=float)
    foci[8:18, 9:19] = 100
    foci[12, 14] = 120

    assert find_bead_center(foci, "Maxima").coord == (12, 14)
    assert find_bead_center(donut, "Minima").coord == (20, 20)
    assert find_center_donut(donut).coord == (20, 20)
    with pytest.raises(ValueError, match="Center search mode unknown"):
        find_bead_center(donut, "Other")
