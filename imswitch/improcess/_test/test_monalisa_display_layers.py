"""Tests for MoNaLISA display layer specs."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult


def _make_scan_params():
    """Return minimal scan params for a test result."""
    return {
        'dimensions': ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints'],
        'directions': ['pos', 'pos', 'pos'],
        'steps': ['10', '10', '1', '1'],
        'step_sizes': ['35', '35', '100', '1'],
    }


def test_view_modes_display_orthogonal_planes():
    """The Bottom/Left view modes must move Z into the displayed (last two)
    positions — the viewer slices along the leading axes, so a permutation
    that only reorders slider axes or swaps the displayed pair shows a
    (transposed) XY slice instead of a true orthogonal section."""
    data = np.random.rand(1, 2, 1, 2, 4, 4).astype(np.float32)

    result = MonalisaProcessingResult(
        name="planes",
        data=data,
        scan_params=_make_scan_params(),
    )

    expected_planes = {
        "Standard": ["Y", "X"],
        "Bottom": ["Z", "X"],
        "Left": ["Z", "Y"],
    }
    modes = {mode.name: mode for mode in result.view_modes}
    assert set(modes) == set(expected_planes)
    for name, mode in modes.items():
        assert sorted(mode.transpose) == list(range(data.ndim))
        transposed_labels = [result.axis_labels[i] for i in mode.transpose]
        assert transposed_labels[-2:] == expected_planes[name]


def test_display_layers_splits_base_axis():
    """display_layers() should return one layer per base component."""
    # 6D data: (Dataset=1, Base=2, T=1, Z=1, Y=4, X=4)
    data = np.random.rand(1, 2, 1, 1, 4, 4).astype(np.float32)
    data[0, 0] = 100.0  # signal distinct from background
    data[0, 1] = 200.0
    
    result = MonalisaProcessingResult(
        name="test",
        data=data,
        scan_params=_make_scan_params(),
    )
    
    layers = result.display_layers()
    assert len(layers) == 2
    assert layers[0].name == "test_signal"
    assert layers[1].name == "test_background"


def test_display_layers_names_additional_bases():
    """Additional bases beyond signal and background get base_N names."""
    data = np.random.rand(1, 4, 1, 1, 4, 4).astype(np.float32)
    
    result = MonalisaProcessingResult(
        name="multi_base",
        data=data,
        scan_params=_make_scan_params(),
    )
    
    layers = result.display_layers()
    assert len(layers) == 4
    assert layers[0].name == "multi_base_signal"
    assert layers[1].name == "multi_base_background"
    assert layers[2].name == "multi_base_base_2"
    assert layers[3].name == "multi_base_base_3"


def test_display_layers_slices_out_base_axis():
    """Each layer should have Base axis removed and correct data slice."""
    data = np.zeros((1, 2, 1, 1, 3, 3), dtype=np.float32)
    data[0, 0, 0, 0] = np.arange(9).reshape(3, 3)  # signal
    data[0, 1, 0, 0] = np.arange(9, 18).reshape(3, 3)  # background
    
    result = MonalisaProcessingResult(
        name="slice_test",
        data=data,
        scan_params=_make_scan_params(),
    )
    
    layers = result.display_layers()
    
    # Check signal layer
    assert layers[0].data.shape == (1, 1, 1, 3, 3)  # Base axis removed
    assert layers[0].axis_labels == ["Dataset", "T", "Z", "Y", "X"]
    np.testing.assert_array_equal(layers[0].data[0, 0, 0], np.arange(9).reshape(3, 3))
    
    # Check background layer
    assert layers[1].data.shape == (1, 1, 1, 3, 3)
    np.testing.assert_array_equal(layers[1].data[0, 0, 0], np.arange(9, 18).reshape(3, 3))


def test_display_layers_preserves_axis_scales():
    """Layer axis scales should match original, minus Base axis."""
    data = np.random.rand(1, 2, 1, 1, 4, 4).astype(np.float32)
    axis_scales = [1.0, 1.0, 1.0, 100.0, 35.0, 35.0]  # Dataset, Base, T, Z, Y, X
    
    result = MonalisaProcessingResult(
        name="scales_test",
        data=data,
        scan_params=_make_scan_params(),
        axis_scales=axis_scales,
    )
    
    layers = result.display_layers()
    expected_layer_scales = [1.0, 1.0, 100.0, 35.0, 35.0]  # Base removed
    
    assert layers[0].axis_scales == expected_layer_scales
    assert layers[1].axis_scales == expected_layer_scales
    assert layers[0].scale_unit == "nm"  # Default from result


def test_update_images_refreshes_axis_scales_from_output_pixel_size():
    """Scan-param edits must refresh the napari layer scale, not only the data."""
    coeffs = np.ones((1, 1, 4, 2, 5), dtype=np.float32)
    scan_params = {
        'dimensions': ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints'],
        'directions': ['pos', 'pos', 'pos'],
        'steps': ['2', '2', '1', '1'],
        'step_sizes': ['40', '80', '120', '1'],
        'unidirectional': True,
    }
    result = MonalisaProcessingResult.from_coeffs(
        name="scale-update",
        coeffs=coeffs,
        scan_params=scan_params,
        axis_label_map={
            'r_l_text': 'Right-Left',
            'u_d_text': 'Up-Down',
            'b_f_text': 'Back-Front',
            'timepoints_text': 'Timepoints',
            'p_text': 'pos',
            'n_text': 'neg',
        },
    )

    edited_params = dict(scan_params)
    edited_params['step_sizes'] = ['100', '200', '300', '1']
    result.updateScanParams(edited_params)
    result.updateImages()

    assert result.output_pixel_size_nm == pytest.approx((100.0, 20.0))
    assert result.axis_scales == pytest.approx([1.0, 1.0, 1.0, 300.0, 100.0, 20.0])


def test_display_layers_computes_per_layer_contrast():
    """Each layer should have independent contrast limits from its own data."""
    data = np.zeros((1, 2, 1, 1, 10, 10), dtype=np.float32)
    data[0, 0] = 100.0  # signal: uniform 100
    data[0, 1] = 500.0  # background: uniform 500
    
    result = MonalisaProcessingResult(
        name="contrast_test",
        data=data,
        scan_params=_make_scan_params(),
    )
    
    layers = result.display_layers()
    
    # Uniform layers should still get strictly increasing limits that bracket
    # the true value, because napari contrast limits are safer that way.
    assert layers[0].display_levels[0] < 100.0 < layers[0].display_levels[1]
    assert layers[0].display_levels[0] < layers[0].display_levels[1]
    
    assert layers[1].display_levels[0] < 500.0 < layers[1].display_levels[1]
    assert layers[1].display_levels[0] < layers[1].display_levels[1]


def test_display_layers_includes_metadata():
    """Layer metadata should include source, component name, and base index."""
    data = np.random.rand(1, 2, 1, 1, 4, 4).astype(np.float32)
    
    result = MonalisaProcessingResult(
        name="metadata_test",
        data=data,
        scan_params=_make_scan_params(),
    )
    
    layers = result.display_layers()
    
    assert layers[0].metadata["source_result"] == "metadata_test"
    assert layers[0].metadata["component"] == "signal"
    assert layers[0].metadata["base_index"] == 0
    
    assert layers[1].metadata["source_result"] == "metadata_test"
    assert layers[1].metadata["component"] == "background"
    assert layers[1].metadata["base_index"] == 1


def test_display_layers_handles_missing_base_axis():
    """display_layers() should return empty list if no Base axis."""
    # Hypothetical 4D result without Base (shouldn't happen in practice)
    data = np.random.rand(1, 1, 4, 4).astype(np.float32)
    
    result = MonalisaProcessingResult(
        name="no_base",
        data=data,
        scan_params=_make_scan_params(),
        axis_labels=["T", "Z", "Y", "X"],
    )
    
    layers = result.display_layers()
    assert len(layers) == 0


def test_display_layers_handles_inf_and_nan():
    """Contrast calculation should handle inf/nan gracefully."""
    data = np.full((1, 2, 1, 1, 4, 4), 100.0, dtype=np.float32)
    data[0, 0, 0, 0, 0, 0] = np.inf
    data[0, 0, 0, 0, 0, 1] = np.nan
    
    result = MonalisaProcessingResult(
        name="inf_test",
        data=data,
        scan_params=_make_scan_params(),
    )
    
    layers = result.display_layers()
    
    # Should compute contrast from finite values only
    assert np.isfinite(layers[0].display_levels[0])
    assert np.isfinite(layers[0].display_levels[1])
    assert layers[0].display_levels[0] == pytest.approx(100.0, abs=1.0)


def test_result_save_unchanged_after_display_layers():
    """save() should still receive and write the original 6D data."""
    data = np.random.rand(1, 2, 1, 1, 4, 4).astype(np.float32)
    
    result = MonalisaProcessingResult(
        name="save_test",
        data=data,
        scan_params=_make_scan_params(),
    )
    
    # Call display_layers() to ensure it doesn't mutate the result
    layers = result.display_layers()
    
    # Original data should be unchanged
    assert result.data.shape == (1, 2, 1, 1, 4, 4)
    assert result.axis_labels == ["Dataset", "Base", "T", "Z", "Y", "X"]
    np.testing.assert_array_equal(result.data, data)
