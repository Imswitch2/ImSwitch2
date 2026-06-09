import numpy as np
import tifffile as tiff
import h5py

from imswitch.improcess.model import DataObj, PlotPayload
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
