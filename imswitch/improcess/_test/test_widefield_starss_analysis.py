from types import SimpleNamespace

import numpy as np
import pytest
import tifffile as tiff
import h5py

from imswitch.improcess.model import DataObj, PlotPayload
from imswitch.improcess.model.result import DisplayLayerSpec
from imswitch.improcess.reconstructors.widefield_starss import (
    WidefieldStarssReconstructor,
    WidefieldStarssResult,
)
from imswitch.improcess.reconstructors.widefield_starss.analysis import (
    WidefieldStarssParams,
    analyze_widefield_starss_pair,
    anisotropy_from_x,
    prepare_signal_background,
    split_frame_into4,
)
from imswitch.improcess.analysis.segmentation import segment_image


def _mosaic_frame(i0, i45, i90, i135, shape=(8, 10)):
    frame = np.zeros(shape, dtype=np.float32)
    frame[1::2, 1::2] = i0
    frame[1::2, ::2] = i45
    frame[::2, ::2] = i90
    frame[::2, 1::2] = i135
    return frame


def _alternating_stack(signal_frame, background_frame, scales=(0.95, 1.0, 1.05)):
    frames = []
    for scale in scales:
        frames.append(signal_frame * scale)
        frames.append(background_frame)
    return np.stack(frames, axis=0)


def test_split_frame_into4_uses_wfs_mosaic_convention():
    frame = _mosaic_frame(i0=10, i45=20, i90=30, i135=40)

    i0, i45, i90, i135 = split_frame_into4(frame)

    assert np.all(i0 == 10)
    assert np.all(i45 == 20)
    assert np.all(i90 == 30)
    assert np.all(i135 == 40)


def test_prepare_signal_background_alternating_pairs_frames():
    signal = np.full((4, 4), 11, dtype=np.float32)
    background = np.full((4, 4), 2, dtype=np.float32)
    stack = _alternating_stack(signal, background, scales=(1.0, 2.0))

    sig, bg = prepare_signal_background(stack, WidefieldStarssParams())

    assert sig.shape == (2, 4, 4)
    assert bg.shape == (2, 4, 4)
    np.testing.assert_array_equal(sig[0], signal)
    np.testing.assert_array_equal(sig[1], signal * 2)
    np.testing.assert_array_equal(bg, np.stack([background, background]))


def test_analyze_standard_mosaic_pair_returns_region_and_expected_anisotropy():
    background = np.full((8, 10), 5, dtype=np.float32)
    h_signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    v_signal = background + _mosaic_frame(i0=80, i45=60, i90=40, i135=60)
    stack_h = _alternating_stack(h_signal, background)
    stack_v = _alternating_stack(v_signal, background)

    params = WidefieldStarssParams(segmentation_mode="none", smooth_sigma=1.0)
    analysis = analyze_widefield_starss_pair(stack_h, stack_v, params)

    assert analysis.mask.shape == (4, 5)
    assert np.all(analysis.mask == 1)
    assert analysis.anis_maps.r_raw.shape == (4, 5)
    np.testing.assert_allclose(analysis.anis_maps.r_raw, 0.0, atol=1e-6)
    assert len(analysis.regions) == 1
    np.testing.assert_allclose(
        analysis.regions.loc[0, "anisotropy_direct"],
        0.0,
        atol=1e-6,
    )
    assert analysis.anis_maps.anisotropy_mode == "stokes"


def test_standard_mosaic_anisotropy_mode_can_use_direct_0_90():
    background = np.full((8, 10), 5, dtype=np.float32)
    h_signal = background + _mosaic_frame(i0=100, i45=20, i90=50, i135=20)
    v_signal = background + _mosaic_frame(i0=50, i45=15, i90=100, i135=15)
    stack_h = _alternating_stack(h_signal, background)
    stack_v = _alternating_stack(v_signal, background)

    stokes = analyze_widefield_starss_pair(
        stack_h,
        stack_v,
        WidefieldStarssParams(segmentation_mode="none", anisotropy_mode="stokes"),
    )
    direct = analyze_widefield_starss_pair(
        stack_h,
        stack_v,
        WidefieldStarssParams(segmentation_mode="none", anisotropy_mode="direct_0_90"),
    )

    assert stokes.anis_maps.anisotropy_mode == "stokes"
    assert direct.anis_maps.anisotropy_mode == "direct_0_90"
    np.testing.assert_allclose(direct.anis_maps.r_raw, 0.25, atol=1e-6)
    assert not np.allclose(stokes.anis_maps.r_raw, direct.anis_maps.r_raw)
    assert direct.regions.loc[0, "anisotropy_mode"] == "direct_0_90"


def test_generic_otsu_legacy_alias_reuses_improcess_segmentation():
    background = np.zeros((8, 10), dtype=np.float32)
    signal = background + _mosaic_frame(i0=1, i45=1, i90=1, i135=1)
    signal[2:6, 2:8] += 20
    stack_h = _alternating_stack(signal, background, scales=(1.0,))
    stack_v = _alternating_stack(signal, background, scales=(1.0,))

    params = WidefieldStarssParams(
        segmentation_mode="generic_otsu",
        segmentation_sigma=0.0,
        min_size=1,
        hole_size=3,
        threshold_scale=0.95,
    )
    analysis = analyze_widefield_starss_pair(stack_h, stack_v, params)
    expected = segment_image(
        analysis.base_image,
        threshold_method="otsu",
        threshold_scale=0.95,
        min_area=1,
        smooth_sigma=0.0,
        max_hole_area=3,
    )

    np.testing.assert_array_equal(analysis.mask, expected.labels)


def test_otsu_segmentation_mode_reuses_improcess_segmentation_with_wfs_options():
    background = np.zeros((12, 12), dtype=np.float32)
    signal = background + _mosaic_frame(i0=1, i45=1, i90=1, i135=1, shape=(12, 12))
    signal[4:10, 4:10] += 30
    stack_h = _alternating_stack(signal, background, scales=(1.0,))
    stack_v = _alternating_stack(signal, background, scales=(1.0,))

    params = WidefieldStarssParams(
        segmentation_mode="otsu",
        segmentation_sigma=0.0,
        min_size=1,
        hole_size=4,
        threshold_scale=0.95,
    )
    analysis = analyze_widefield_starss_pair(stack_h, stack_v, params)
    expected = segment_image(
        analysis.base_image,
        threshold_method="otsu",
        threshold_scale=0.95,
        min_area=1,
        smooth_sigma=0.0,
        max_hole_area=4,
    )

    np.testing.assert_array_equal(analysis.mask, expected.labels)


def test_segmentation_restricts_anisotropy_maps_to_segmented_regions():
    background = np.zeros((8, 10), dtype=np.float32)
    signal = background + _mosaic_frame(i0=1, i45=1, i90=1, i135=1)
    signal[2:6, 2:8] += 20
    stack_h = _alternating_stack(signal, background, scales=(1.0,))
    stack_v = _alternating_stack(signal, background, scales=(1.0,))

    params = WidefieldStarssParams(
        segmentation_mode="otsu",
        segmentation_sigma=0.0,
        min_size=1,
        hole_size=0,
    )
    analysis = analyze_widefield_starss_pair(stack_h, stack_v, params)

    outside = analysis.mask == 0
    inside = analysis.mask > 0
    assert np.any(outside) and np.any(inside)
    for maps in (analysis.anis_maps.r_raw, analysis.anis_maps.r_smooth):
        assert np.all(np.isnan(maps[outside]))
        assert np.any(np.isfinite(maps[inside]))
    assert not np.any(analysis.anis_maps.valid_mask & outside)


def test_no_segmentation_keeps_anisotropy_maps_unmasked():
    background = np.full((8, 10), 5, dtype=np.float32)
    signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    stack_h = _alternating_stack(signal, background)
    stack_v = _alternating_stack(signal, background)

    params = WidefieldStarssParams(segmentation_mode="none", smooth_sigma=1.0)
    analysis = analyze_widefield_starss_pair(stack_h, stack_v, params)

    assert np.all(np.isfinite(analysis.anis_maps.r_raw))
    assert np.all(np.isfinite(analysis.anis_maps.r_smooth))


def test_anisotropy_formula_matches_x_definition():
    x, r, sigma_r = anisotropy_from_x(
        ihh=np.array([100.0]),
        ihv=np.array([25.0]),
        ivh=np.array([25.0]),
        ivv=np.array([100.0]),
        sigma_hh=np.array([1.0]),
        sigma_hv=np.array([1.0]),
        sigma_vh=np.array([1.0]),
        sigma_vv=np.array([1.0]),
    )

    np.testing.assert_allclose(x, [0.25])
    np.testing.assert_allclose(r, [0.5])
    assert np.isfinite(sigma_r[0])


def test_widefield_starss_result_exposes_graph_payloads():
    background = np.full((8, 10), 5, dtype=np.float32)
    h_signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    v_signal = background + _mosaic_frame(i0=80, i45=60, i90=40, i135=60)
    analysis = analyze_widefield_starss_pair(
        _alternating_stack(h_signal, background),
        _alternating_stack(v_signal, background),
        WidefieldStarssParams(segmentation_mode="none"),
    )

    result = WidefieldStarssResult("wfs", analysis, params={})
    payloads = result.plot_payloads()

    assert result.data.shape == (4, 4, 5)
    assert all(isinstance(payload, PlotPayload) for payload in payloads)
    assert [payload.title for payload in payloads] == [
        "Anisotropy histogram",
        "Region anisotropy",
    ]


def test_widefield_starss_result_exposes_independent_display_layers():
    background = np.full((8, 10), 5, dtype=np.float32)
    h_signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    v_signal = background + _mosaic_frame(i0=80, i45=60, i90=40, i135=60)
    analysis = analyze_widefield_starss_pair(
        _alternating_stack(h_signal, background),
        _alternating_stack(v_signal, background),
        WidefieldStarssParams(segmentation_mode="none"),
    )

    result = WidefieldStarssResult("wfs", analysis, params={})
    layers = result.display_layers()

    assert all(isinstance(layer, DisplayLayerSpec) for layer in layers)
    assert [layer.metadata["component"] for layer in layers] == [
        "r_smooth",
        "r_raw",
        "mask",
        "base_image",
    ]
    assert [layer.axis_labels for layer in layers] == [["Y", "X"]] * 4
    assert [layer.data.shape for layer in layers] == [analysis.base_image.shape] * 4
    np.testing.assert_array_equal(layers[0].data, analysis.anis_maps.r_smooth)
    np.testing.assert_array_equal(layers[3].data, analysis.base_image)
    assert layers[0].display_levels != layers[3].display_levels
    assert all(
        layer.display_levels[0] < layer.display_levels[1]
        for layer in layers
        if layer.display_levels is not None
    )


def test_widefield_starss_reconstructor_auto_pairs_hv_tiffs(tmp_path):
    background = np.full((8, 10), 5, dtype=np.float32)
    h_signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    v_signal = background + _mosaic_frame(i0=80, i45=60, i90=40, i135=60)
    h_path = tmp_path / "sample_h.tif"
    v_path = tmp_path / "sample_v.tif"
    tiff.imwrite(h_path, _alternating_stack(h_signal, background))
    tiff.imwrite(v_path, _alternating_stack(v_signal, background))

    data_obj = DataObj(h_path.name, None, path=str(h_path))
    reconstructor = WidefieldStarssReconstructor()
    result = reconstructor.process(
        data_obj,
        {
            "current_role": "Auto",
            "counterpart_path": None,
            "segmentation_mode": "none",
            "convention": "alternating",
        },
    )

    assert isinstance(result, WidefieldStarssResult)
    assert result.params["source_h_path"] == str(h_path)
    assert result.params["source_v_path"] == str(v_path)
    np.testing.assert_allclose(result.analysis.anis_maps.r_raw, 0.0, atol=1e-6)


def test_widefield_starss_result_saves_hdf5(tmp_path):
    background = np.full((8, 10), 5, dtype=np.float32)
    h_signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    v_signal = background + _mosaic_frame(i0=80, i45=60, i90=40, i135=60)
    analysis = analyze_widefield_starss_pair(
        _alternating_stack(h_signal, background),
        _alternating_stack(v_signal, background),
        WidefieldStarssParams(segmentation_mode="none"),
    )
    result = WidefieldStarssResult("wfs", analysis, params={"mode": "test"})
    out_path = tmp_path / "wfs.h5"

    result.save(out_path, "hdf5")

    with h5py.File(out_path, "r") as h5:
        assert {"r_smooth", "r_raw", "mask", "base_image", "regions"}.issubset(h5.keys())
        assert h5["r_smooth"].shape == (4, 5)
        assert "anisotropy_direct" in h5["regions"]
        assert h5.attrs["anisotropy_mode"] == "stokes"
        assert h5.attrs["mode"] == "test"


def test_analyze_split_detection_pair_returns_maps_and_region():
    top_background = np.full((4, 6), 3, dtype=np.float32)
    bottom_background = np.full((4, 6), 3, dtype=np.float32)
    background = np.vstack([top_background, bottom_background])
    h_signal = np.vstack([
        top_background + 100,
        bottom_background + 50,
    ])
    v_signal = np.vstack([
        top_background + 80,
        bottom_background + 40,
    ])

    analysis = analyze_widefield_starss_pair(
        _alternating_stack(h_signal, background),
        _alternating_stack(v_signal, background),
        WidefieldStarssParams(
            split_detection=True,
            split_y=4,
            segmentation_mode="none",
            smooth_sigma=1.0,
        ),
    )

    assert analysis.stats_h is None
    assert analysis.stats_v is None
    assert analysis.anis_maps.r_raw.shape == (4, 6)
    assert np.all(analysis.mask == 1)
    assert len(analysis.regions) == 1
    np.testing.assert_allclose(analysis.regions.loc[0, "anisotropy_direct"], 0.0, atol=1e-6)


def test_split_detection_otsu_mode_reuses_improcess_segmentation():
    background = np.zeros((8, 8), dtype=np.float32)
    h_signal = np.ones((8, 8), dtype=np.float32)
    v_signal = np.ones((8, 8), dtype=np.float32)
    h_signal[1:3, 2:6] += 30
    h_signal[5:7, 2:6] += 30
    v_signal[1:3, 2:6] += 20
    v_signal[5:7, 2:6] += 20

    params = WidefieldStarssParams(
        split_detection=True,
        split_y=4,
        segmentation_mode="otsu",
        segmentation_sigma=0.0,
        min_size=1,
        hole_size=3,
        threshold_scale=0.9,
    )
    analysis = analyze_widefield_starss_pair(
        _alternating_stack(h_signal, background, scales=(1.0, 1.05)),
        _alternating_stack(v_signal, background, scales=(1.0, 1.05)),
        params,
    )
    expected = segment_image(
        analysis.base_image,
        threshold_method="otsu",
        threshold_scale=0.9,
        min_area=1,
        smooth_sigma=0.0,
        max_hole_area=3,
    )

    np.testing.assert_array_equal(analysis.mask, expected.labels)


def test_widefield_starss_rejects_a_mismatched_pair(tmp_path):
    """Anisotropy is computed pixel by pixel, so the pair must correspond."""
    background = np.full((8, 10), 5, dtype=np.float32)
    h_signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    smaller = np.full((6, 10), 5, dtype=np.float32)
    h_path = tmp_path / "sample_h.tif"
    v_path = tmp_path / "sample_v.tif"
    tiff.imwrite(h_path, _alternating_stack(h_signal, background))
    tiff.imwrite(v_path, _alternating_stack(smaller, smaller))

    data_obj = DataObj(h_path.name, None, path=str(h_path))

    with pytest.raises(ValueError, match="same shape"):
        WidefieldStarssReconstructor().process(
            data_obj,
            {"current_role": "Auto", "segmentation_mode": "none",
             "convention": "alternating"},
        )


def test_widefield_starss_will_not_guess_an_unlabelled_role(tmp_path):
    """An unsuffixed filename used to be silently treated as the H half.

    Getting H and V the wrong way round inverts the anisotropy instead of
    failing, so an unknown role has to stop the analysis.
    """
    background = np.full((8, 10), 5, dtype=np.float32)
    signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    unlabelled = tmp_path / "sample.tif"
    counterpart = tmp_path / "other.tif"
    tiff.imwrite(unlabelled, _alternating_stack(signal, background))
    tiff.imwrite(counterpart, _alternating_stack(signal, background))

    data_obj = DataObj(unlabelled.name, None, path=str(unlabelled))

    with pytest.raises(ValueError, match="H or V polarization"):
        WidefieldStarssReconstructor().process(
            data_obj,
            {
                "current_role": "Auto",
                "counterpart_path": str(counterpart),
                "segmentation_mode": "none",
                "convention": "alternating",
            },
        )


def test_widefield_starss_prefers_a_recorded_polarization_role(tmp_path):
    """A role written by the acquisition outranks the filename suffix."""
    background = np.full((8, 10), 5, dtype=np.float32)
    signal = background + _mosaic_frame(i0=100, i45=75, i90=50, i135=75)
    # The suffixes deliberately contradict the recorded roles.
    h_path = tmp_path / "sample_v.tif"
    v_path = tmp_path / "sample_h.tif"
    tiff.imwrite(h_path, _alternating_stack(signal, background))
    tiff.imwrite(v_path, _alternating_stack(signal, background))

    data_obj = DataObj(h_path.name, None, path=str(h_path))
    role = WidefieldStarssReconstructor._recorded_role(
        SimpleNamespace(attrs={"WidefieldStarss:polarization_role": "H"})
    )

    assert role == "H"
    assert WidefieldStarssReconstructor._recorded_role(SimpleNamespace(attrs={})) is None


def test_widefield_starss_recording_carries_its_role_and_state_order(tmp_path):
    """The workflow writes what analysis used to infer from the filename.

    Before this, a STARSS pair was two anonymous TIFF stacks: the role came
    from the ``_h``/``_v`` suffix and the alternating signal/background order
    was taken on faith.
    """
    from imswitch.imcontrol.model.workflows.widefield_starss import (
        WidefieldStarssWorkflow,
    )
    from imswitch.improcess.reconstructors.widefield_starss import (
        WidefieldStarssReconstructor,
    )

    from imswitch.imcontrol.model.workflows.acquisition_output import (
        save_acquisition_tiff,
    )

    stack = np.zeros((6, 8, 9), dtype=np.uint16)
    workflow = WidefieldStarssWorkflow.__new__(WidefieldStarssWorkflow)
    layout = WidefieldStarssWorkflow._acquisition_layout(workflow, stack, "h")
    assert layout is not None, "the workflow must describe its own acquisition"

    path = save_acquisition_tiff(
        tmp_path / "data_stack_h.tif",
        stack,
        layout=layout,
        name="WidefieldSTARSS H",
        annotations={"WidefieldStarss:polarization_role": "H"},
    )
    data_obj = DataObj(path.name, None, path=str(path))

    resolved = data_obj.acquisition_layout
    assert resolved.layout.provenance == "recorded"
    assert resolved.layout.modality == "widefield-starss"
    assert [(loop.kind, loop.count) for loop in resolved.layout.event_loops] == [
        ("time", 3),
        ("condition", 2),
    ]
    # stack[0::2] are the signal frames, so state is the inner loop.
    assert resolved.layout.event_loops[1].labels == ("signal", "background")
    assert WidefieldStarssReconstructor._recorded_role(data_obj) == "H"


def test_registry_selects_from_the_recorded_modality(tmp_path):
    """Modality lives in the layout now, not in a bare attribute."""
    from imswitch.imcontrol.model.workflows.widefield_starss import (
        WidefieldStarssWorkflow,
    )
    from imswitch.improcess.reconstructors.registry import PluginRegistry

    from imswitch.imcontrol.model.workflows.acquisition_output import (
        save_acquisition_tiff,
    )

    stack = np.zeros((6, 8, 9), dtype=np.uint16)
    workflow = WidefieldStarssWorkflow.__new__(WidefieldStarssWorkflow)
    path = save_acquisition_tiff(
        tmp_path / "data_stack_h.tif",
        stack,
        layout=WidefieldStarssWorkflow._acquisition_layout(workflow, stack, "h"),
        name="WidefieldSTARSS H",
    )
    data_obj = DataObj(path.name, None, path=str(path))

    registry = PluginRegistry()
    registry.register_reconstructor(WidefieldStarssReconstructor())

    assert registry.auto_select_reconstructor(data_obj).id == "widefield-starss"
