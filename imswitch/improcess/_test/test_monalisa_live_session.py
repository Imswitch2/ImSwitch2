"""Tests for MoNaLISA live streaming reconstruction."""

import numpy as np
import pytest
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.live import InMemoryStackWrapper, ZarrLiveSource
from imswitch.improcess.reconstructors.base import StackInfo, StreamInit, StreamingSession
from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor
from imswitch.improcess.reconstructors.monalisa.gauss_processor import (
    DEFAULT_FOOTPRINT_NUM_RECTS,
    DEFAULT_GAUSSIAN_SIGMA_PX,
    make_gauss_processor,
)
from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
    get_interp_coords,
    get_pinhole_footprint,
    get_rectangles_coords,
)


@pytest.fixture
def synthetic_stack():
    """Create a synthetic MoNaLISA-like stack with scan geometry."""
    nx_s, ny_s = 10, 10
    num_rows, num_cols = 100, 100
    num_frames = nx_s * ny_s

    dx, dy = 50.0, 50.0
    x0, y0, z0 = 0.0, 0.0, 0.0
    x1, y1 = (nx_s - 1) * dx, (ny_s - 1) * dy

    rng = np.random.default_rng(12345)
    stack = rng.integers(50, 150, size=(num_frames, num_rows, num_cols), dtype=np.uint16)

    xp, yp = 10.0, 10.0
    xo, yo = 5.0, 5.0
    nx_c = int(np.ceil((num_cols - xo) / xp))
    ny_c = int(np.ceil((num_rows - yo) / yp))

    for i in range(num_frames):
        x_offset = (i % nx_s) * 0.5
        y_offset = (i // nx_s) * 0.5
        for ix in range(nx_c):
            for iy in range(ny_c):
                xc = int(xo + ix * xp + x_offset)
                yc = int(yo + iy * yp + y_offset)
                if 0 <= xc < num_cols and 0 <= yc < num_rows:
                    stack[i, yc, xc] += 100

    attrs = {
        "ImswitchData": {
            "ScanStage:axis_startpos": [x0, y0, z0],
            "ScanStage:axis_length": [x1, y1, 1.0],
            "ScanStage:axis_step_size": [dx, dy, 1.0],
            "ScanStage:axis_step_size_unit": "nm",
            "Rec:LapseTime": 2,
        }
    }

    return stack, attrs, nx_s, ny_s, nx_c, ny_c


def _write_monalisa_zarr(path, stack: np.ndarray, attrs: dict, detector_name: str = "CAM"):
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    det_group = root.create_group(detector_name)
    array = ZarrStorer._create_array(
        det_group,
        "data",
        data=stack,
        chunks=(25, *stack.shape[-2:]),
    )
    array.attrs["detector_name"] = detector_name
    array.attrs["writing"] = False
    array.attrs["axes"] = ["T", "Y", "X"]
    array.attrs["recording:detector_name"] = detector_name
    array.attrs["recording:dataset_path"] = f"/{detector_name}/data"
    array.attrs["recording:source_format"] = "ZARR"
    array.attrs["recording:expected_frames"] = int(stack.shape[0])
    array.attrs["recording:frames_per_stack"] = int(stack.shape[0])

    metadata = det_group.create_group("metadata")
    imswitch_data = attrs["ImswitchData"]
    scan_stage = metadata.create_group("ScanStage")
    scan_stage.attrs["axis_startpos"] = imswitch_data["ScanStage:axis_startpos"]
    scan_stage.attrs["axis_length"] = imswitch_data["ScanStage:axis_length"]
    scan_stage.attrs["axis_step_size"] = imswitch_data["ScanStage:axis_step_size"]
    if "ScanStage:axis_step_size_unit" in imswitch_data:
        scan_stage.attrs["axis_step_size_unit"] = imswitch_data[
            "ScanStage:axis_step_size_unit"
        ]
    rec = metadata.create_group("Rec")
    rec.attrs["LapseTime"] = imswitch_data["Rec:LapseTime"]
    return root


def test_monalisa_reconstructor_supports_streaming():
    """Test that MonalisaReconstructor declares streaming support."""
    reconstructor = MonalisaReconstructor()
    assert hasattr(reconstructor, "supports_streaming")
    assert reconstructor.supports_streaming is True
    assert hasattr(reconstructor, "make_session")


def test_make_session_returns_streaming_session():
    """Test that make_session returns a StreamingSession instance."""
    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()
    assert isinstance(session, StreamingSession)


def test_live_session_begin(synthetic_stack):
    """Test StreamingSession.begin with synthetic data."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    plan = session.begin(init_obj, params={"use_gpu": False, "num_rects": 3})

    assert plan.out_shape[0] == 1
    assert plan.out_shape[1] == 1
    assert plan.out_shape[2] == 2
    assert plan.out_shape[3] == 1
    assert plan.out_shape[4] > 0
    assert plan.out_shape[5] > 0
    assert plan.dtype == np.dtype(np.float32)
    assert "Dataset" in plan.axis_labels
    assert "Base" in plan.axis_labels
    assert "T" in plan.axis_labels


def test_live_session_treats_null_lapse_metadata_as_missing(synthetic_stack):
    """Legacy `"null"` timepoint metadata must not reach the output shape."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    attrs = {"ImswitchData": dict(attrs["ImswitchData"])}
    attrs["ImswitchData"]["Rec:LapseTime"] = "null"
    attrs["ImswitchData"]["recording:num_timepoints"] = "null"

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    session = MonalisaReconstructor().make_session()
    plan = session.begin(init_obj, params={"use_gpu": False})

    assert plan.out_shape[2] == 1


def test_live_session_uses_expected_frames_when_timepoints_are_missing(synthetic_stack):
    """Expected frame count is a useful fallback when explicit timepoints are absent."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    attrs = {"ImswitchData": dict(attrs["ImswitchData"])}
    attrs["ImswitchData"]["Rec:LapseTime"] = "null"
    stack_info = StackInfo(
        frame_shape=stack.shape[-2:],
        dtype=stack.dtype,
        attrs=attrs,
        expected_frames=stack.shape[0] * 3,
    )

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
        stack_info=stack_info,
    )

    session = MonalisaReconstructor().make_session()
    plan = session.begin(init_obj, params={"use_gpu": False})

    assert plan.out_shape[2] == 3


def test_live_session_converts_scan_stage_micrometer_steps_to_result_scale(synthetic_stack):
    """ImControl ScanStage step sizes are micrometers, but results expose nm."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    attrs = {"ImswitchData": dict(attrs["ImswitchData"])}
    attrs["ImswitchData"].pop("ScanStage:axis_step_size_unit", None)
    attrs["ImswitchData"]["ScanStage:axis_length"] = [0.45, 0.45, 1.0]
    attrs["ImswitchData"]["ScanStage:axis_step_size"] = [0.05, 0.05, 1.0]

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    session = MonalisaReconstructor().make_session()
    plan = session.begin(init_obj, params={"use_gpu": False})
    result = session.result()

    expected_px = (50.0 / session.ny_c, 50.0 / session.nx_c)
    assert plan.axis_scales[-2:] == pytest.approx(expected_px)
    assert result.output_pixel_size_nm == pytest.approx(expected_px)
    assert result.axis_scales[-2:] == pytest.approx(expected_px)
    assert result.scan_params["step_sizes"][:2] == pytest.approx([50.0, 50.0])


def test_live_session_prefers_scan_ttl_grid_counts(synthetic_stack):
    """Older scan files can report the real grid only via ScanTTL:Nx/Ny."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    attrs = {"ImswitchData": dict(attrs["ImswitchData"])}
    attrs["ImswitchData"]["ScanStage:axis_length"] = [550.0, 550.0, 1.0]
    attrs["ImswitchData"]["ScanTTL:Nx"] = nx_s
    attrs["ImswitchData"]["ScanTTL:Ny"] = ny_s

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    session = MonalisaReconstructor().make_session()
    session.begin(init_obj, params={"use_gpu": False})

    assert session.nx_s == nx_s
    assert session.ny_s == ny_s
    assert session.num_frames_in_stack == stack.shape[0]


def test_live_session_accepts_scan_size_geometry_with_stack_frame_count(synthetic_stack):
    """Live ImControl metadata uses axis_length as size, not endpoint."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    attrs = {"ImswitchData": dict(attrs["ImswitchData"])}
    attrs["ImswitchData"]["ScanStage:axis_length"] = [500.0, 500.0, 1.0]
    stack_info = StackInfo(
        frame_shape=stack.shape[-2:],
        dtype=stack.dtype,
        attrs=attrs,
        frames_per_stack=stack.shape[0],
    )

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
        stack_info=stack_info,
    )

    session = MonalisaReconstructor().make_session()
    session.begin(init_obj, params={"use_gpu": False})

    assert session.nx_s == nx_s
    assert session.ny_s == ny_s
    assert session.num_frames_in_stack == stack.shape[0]


def test_live_session_from_zarr_live_source(tmp_path, synthetic_stack):
    """Drive a real MoNaLISA live session from a structured ZarrLiveSource."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    zarr_path = tmp_path / "monalisa-live.zarr"
    _write_monalisa_zarr(zarr_path, stack, attrs, detector_name="CAM")

    source = ZarrLiveSource(detector_name="CAM", chunk_size=25)
    stack_info = source.open(zarr_path)

    chunks = []
    total_frames = 0
    while total_frames < stack_info.frames_per_stack:
        new_chunks = source.poll()
        assert new_chunks
        chunks.extend(new_chunks)
        total_frames += sum(chunk.data.shape[0] for chunk in new_chunks)

    init_data = np.concatenate([chunk.data for chunk in chunks], axis=0)
    assert init_data.shape == stack.shape
    assert stack_info.attrs["ScanStage:axis_startpos"] == attrs["ImswitchData"]["ScanStage:axis_startpos"]
    assert stack_info.attrs["Rec:LapseTime"] == attrs["ImswitchData"]["Rec:LapseTime"]

    init_obj = StreamInit(
        name="monalisa-live",
        dataset_name=stack_info.detector_name,
        data=init_data,
        attrs=stack_info.attrs,
        stack_info=stack_info,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()
    plan = session.begin(init_obj, params={"use_gpu": False, "num_rects": 3})
    result = session.result()

    assert result.data.shape == plan.out_shape
    assert result.data.dtype == np.dtype(np.float32)
    assert np.all(np.isfinite(result.data))
    assert not np.all(result.data == 0)
    source.close()
    session.close()


def test_live_session_push_and_result(synthetic_stack):
    """Test StreamingSession push and result with chunked data."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack

    first_chunk = stack[:50]
    second_chunk = stack[50:]

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    plan = session.begin(init_obj, params={"use_gpu": False})

    session.push(first_chunk, 0, 50)

    session.push(second_chunk, 50, 100)

    result = session.result()

    assert result is not None
    assert result.data.shape == plan.out_shape
    assert result.data.dtype == plan.dtype
    assert np.all(np.isfinite(result.data))
    assert not np.all(result.data == 0)


def test_live_session_multi_timepoint_global_indices(synthetic_stack):
    """A second timepoint pushed with GLOBAL frame indices (as the single-file
    lapse source streams them) must scatter into time_index 1 using LOCAL
    frame_inds positions — not index the per-stack frame_inds out of range."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    fps = nx_s * ny_s  # frames per stack; fixture Rec:LapseTime == 2

    init_obj = StreamInit(
        name="lapse_stack", dataset_name="detector_0", data=stack, attrs=attrs,
    )
    session = MonalisaReconstructor().make_session()
    plan = session.begin(init_obj, params={"use_gpu": False})

    # begin() already scattered timepoint 0. Push the same stack as timepoint 1
    # using global indices [fps : 2*fps] — this is what ZarrLapseSource emits.
    assert plan.out_shape[2] == 2  # two timepoints allocated
    session.push(stack, fps, 2 * fps)

    result = session.result()
    tp0 = result.data[0, 0, 0, 0]
    tp1 = result.data[0, 0, 1, 0]
    # Both timepoints filled, and identical input -> identical reconstruction.
    assert not np.all(tp1 == 0)
    np.testing.assert_array_equal(tp0, tp1)


def test_live_session_begin_splits_oversized_initial_chunk(synthetic_stack):
    """If startup hands two stacks at once, scatter them as two timepoints."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    two_stacks = np.concatenate([stack, stack], axis=0)
    attrs = {"ImswitchData": dict(attrs["ImswitchData"])}
    attrs["ImswitchData"]["Rec:LapseTime"] = 2
    stack_info = StackInfo(
        frame_shape=stack.shape[-2:],
        dtype=stack.dtype,
        attrs=attrs,
        frames_per_stack=stack.shape[0],
        expected_frames=two_stacks.shape[0],
    )

    init_obj = StreamInit(
        name="two-stack-init",
        dataset_name="detector_0",
        data=two_stacks,
        attrs=attrs,
        stack_info=stack_info,
    )

    session = MonalisaReconstructor().make_session()
    plan = session.begin(init_obj, params={"use_gpu": False})

    assert plan.out_shape[2] == 2
    result = session.result()
    tp0 = result.data[0, 0, 0, 0]
    tp1 = result.data[0, 0, 1, 0]
    assert not np.all(tp0 == 0)
    assert not np.all(tp1 == 0)
    np.testing.assert_array_equal(tp0, tp1)


def test_live_session_bleaching_correction_uses_first_frame_energy(synthetic_stack):
    """Fast-Gauss live reconstruction uses the same 4th-power energy correction."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    chunk = stack[:3].astype(np.float32)
    chunk *= np.array([1.0, 0.8, 0.6], dtype=np.float32)[:, np.newaxis, np.newaxis]

    session = MonalisaReconstructor().make_session()
    corrected = session._apply_bleaching_correction(chunk)

    energies = np.sum(chunk, axis=(1, 2), dtype=np.float64)
    expected_scale = ((energies[0] / energies) ** 4).astype(np.float32)
    expected = chunk * expected_scale[:, np.newaxis, np.newaxis]

    assert session._bleach_reference_energy == pytest.approx(float(energies[0]))
    np.testing.assert_allclose(corrected, expected, rtol=1e-6, atol=1e-4)


def test_live_session_begin_honors_bleaching_correction_param(synthetic_stack):
    """The live fast-Gauss path honors the MoNaLISA parameter widget checkbox."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    session = MonalisaReconstructor().make_session()
    session.begin(init_obj, params={"use_gpu": False, "bleaching_correction": True})

    expected_reference = np.sum(stack[0].astype(np.float32), dtype=np.float64)
    assert session.bleaching_correction is True
    assert session._bleach_reference_energy == pytest.approx(float(expected_reference))


def test_live_session_begin_honors_fast_gauss_fit_options(synthetic_stack):
    """Fast-Gauss live/offline params control the processor footprint fit."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    session = MonalisaReconstructor().make_session()
    session.begin(
        init_obj,
        params={
            "use_gpu": False,
            "fast_gauss_footprint_num_rects": 2,
            "fast_gauss_gaussian_sigma_px": 1.25,
        },
    )

    assert session.processor.num_rects == 2
    assert session.processor.gaussian_sigma_px == pytest.approx(1.25)
    assert session.processor.pts_per_focus == len(get_rectangles_coords(2)[0])


def test_monalisa_process_can_run_fast_gauss_offline(synthetic_stack):
    """Offline MoNaLISA process() can use the live fast-Gauss algorithm."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    data_obj = InMemoryStackWrapper(
        name="offline-fast-gauss",
        dataset_name="detector_0",
        data=stack,
        attrs={},
    )
    params = {
        "reconstruction_method": "Fast Gauss MoNaLISA",
        "device": "CPU",
        "fast_gauss_footprint_num_rects": DEFAULT_FOOTPRINT_NUM_RECTS,
        "fast_gauss_gaussian_sigma_px": DEFAULT_GAUSSIAN_SIGMA_PX,
        "bleaching_correction": False,
        "scan_params": {
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["pos", "pos", "pos"],
            "steps": [str(nx_s), str(ny_s), "1", "1"],
            "step_sizes": ["50", "50", "1", "1"],
            "unidirectional": False,
        },
    }

    result = MonalisaReconstructor().process(data_obj, params)

    assert result.name == "offline-fast-gauss"
    assert result.data.ndim == 6
    assert result.data.shape[:4] == (1, 1, 1, 1)
    assert result.output_pixel_size_nm is not None
    assert result.coeffs is None
    assert np.all(np.isfinite(result.data))
    assert not np.all(result.data == 0)


def test_fast_gauss_offline_uses_file_scan_metadata_when_params_mismatch(synthetic_stack):
    """Old files can carry the correct grid in ScanTTL even when the UI params differ."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    two_stacks = np.concatenate([stack, stack], axis=0)
    imswitch_data = attrs["ImswitchData"]
    data_attrs = {
        "ScanStage:axis_startpos": imswitch_data["ScanStage:axis_startpos"],
        # Would imply 12x12 via endpoint-style inference; ScanTTL is correct.
        "ScanStage:axis_length": [0.55, 0.55, 1.0],
        "ScanStage:axis_step_size": [0.05, 0.05, 1.0],
        "ScanTTL:Nx": nx_s,
        "ScanTTL:Ny": ny_s,
    }
    data_obj = InMemoryStackWrapper(
        name="offline-fast-gauss-file-metadata",
        dataset_name="detector_0",
        data=two_stacks,
        attrs=data_attrs,
    )
    params = {
        "reconstruction_method": "Fast Gauss MoNaLISA",
        "device": "CPU",
        "fast_gauss_footprint_num_rects": DEFAULT_FOOTPRINT_NUM_RECTS,
        "fast_gauss_gaussian_sigma_px": DEFAULT_GAUSSIAN_SIGMA_PX,
        "bleaching_correction": False,
        "scan_params": {
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["pos", "pos", "pos"],
            "steps": ["25", "25", "1", "1"],
            "step_sizes": ["50", "50", "1", "1"],
            "unidirectional": False,
        },
    }

    result = MonalisaReconstructor().process(data_obj, params)

    assert result.data.shape[2] == 2
    assert result.scan_params["steps"] == [nx_s, ny_s, 1, 2]
    assert result.scan_params["step_sizes"][:2] == pytest.approx([50.0, 50.0])
    assert result.output_pixel_size_nm == pytest.approx(
        (50.0 / (result.data.shape[-2] // ny_s), 50.0 / (result.data.shape[-1] // nx_s))
    )
    assert np.all(np.isfinite(result.data))
    assert not np.all(result.data == 0)


def test_fast_gauss_offline_accepts_legacy_scan_dimension_labels(synthetic_stack):
    """The visible legacy MoNaLISA scan dialog uses slash-style dimension labels."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    data_obj = InMemoryStackWrapper(
        name="offline-fast-gauss-legacy-labels",
        dataset_name="detector_0",
        data=stack,
        attrs={},
    )
    params = {
        "reconstruction_method": "Fast Gauss MoNaLISA",
        "device": "CPU",
        "fast_gauss_footprint_num_rects": DEFAULT_FOOTPRINT_NUM_RECTS,
        "fast_gauss_gaussian_sigma_px": DEFAULT_GAUSSIAN_SIGMA_PX,
        "bleaching_correction": False,
        "scan_params": {
            "dimensions": ["Up/Down", "Right/Left", "Back/Forth", "Timepoints"],
            "directions": ["pos", "pos", "pos"],
            "steps": [str(ny_s), str(nx_s), "1", "1"],
            "step_sizes": ["50", "50", "1", "1"],
            "unidirectional": False,
        },
    }

    result = MonalisaReconstructor().process(data_obj, params)

    assert result.data.shape[:4] == (1, 1, 1, 1)
    assert result.scan_params["dimensions"] == [
        "Up-Down", "Right-Left", "Back-Front", "Timepoints",
    ]
    assert np.all(np.isfinite(result.data))


def test_fast_gauss_uses_mini_recon_shell_footprint():
    """The footprint is concentric rectangular shells, not a filled square.

    The central pixel (innermost shell) appears exactly once — it is not
    duplicated by the perimeter construction.
    """
    x_offsets, y_offsets = get_rectangles_coords(2)

    expected = np.array([
        [0.0, 0.0],
        [-1.0, 1.0],
        [0.0, 1.0],
        [1.0, 1.0],
        [-1.0, -1.0],
        [0.0, -1.0],
        [1.0, -1.0],
        [-1.0, 0.0],
        [1.0, 0.0],
    ])
    actual = np.column_stack((x_offsets, y_offsets))
    np.testing.assert_array_equal(actual, expected)


def test_get_pinhole_footprint_is_a_centered_disc():
    """The pinhole footprint is a filled disc with the center included once."""
    x, y = get_pinhole_footprint(1.0)
    offsets = {(int(a), int(b)) for a, b in zip(x, y)}
    # radius 1 keeps the center and the 4-neighbours, excludes the diagonals.
    assert offsets == {(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)}
    assert len(x) == 5  # no duplicated center

    # radius scales the disc; center still appears exactly once.
    x2, y2 = get_pinhole_footprint(2.5)
    assert sum((a == 0 and b == 0) for a, b in zip(x2, y2)) == 1
    assert np.all(x2 ** 2 + y2 ** 2 <= 2.5 ** 2)


def test_fast_gauss_pinhole_footprint_overrides_shells(synthetic_stack):
    """A pinhole radius (k×σ) replaces the shell footprint for both the interp
    coords and the fit weights."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    init_obj = StreamInit(name="s", dataset_name="d", data=stack, attrs=attrs)

    session = MonalisaReconstructor().make_session()
    session.begin(init_obj, params={
        "use_gpu": False,
        "fast_gauss_footprint_mode": "Circular pinhole",
        "fast_gauss_gaussian_sigma_px": 2.0,
        "fast_gauss_pinhole_radius_sigma": 1.5,  # radius = 3.0 px
    })

    assert session.processor.pinhole_radius_px == pytest.approx(3.0)
    assert session.processor.pts_per_focus == len(get_pinhole_footprint(3.0)[0])


def test_fast_gauss_default_footprint_mode_keeps_shells(synthetic_stack):
    """A nonzero pinhole-radius default is ignored unless pinhole mode is selected."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    init_obj = StreamInit(name="s", dataset_name="d", data=stack, attrs=attrs)

    session = MonalisaReconstructor().make_session()
    session.begin(init_obj, params={
        "use_gpu": False,
        "fast_gauss_footprint_mode": "Rectangular shells",
        "fast_gauss_gaussian_sigma_px": 2.0,
        "fast_gauss_pinhole_radius_sigma": 1.5,
    })

    assert session.processor.pinhole_radius_px is None
    assert session.processor.pts_per_focus == len(
        get_rectangles_coords(DEFAULT_FOOTPRINT_NUM_RECTS)[0]
    )


def test_fast_gauss_sigma_derived_from_psf(synthetic_stack):
    """With no explicit sigma, sigma is derived from PSF FWHM and pixel size."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    init_obj = StreamInit(name="s", dataset_name="d", data=stack, attrs=attrs)

    session = MonalisaReconstructor().make_session()
    session.begin(init_obj, params={
        "use_gpu": False,
        "fast_gauss_gaussian_sigma_px": 0.0,  # 0 = auto
        "psf_fwhm_nm": 300.0,
        "pixel_size_nm": 100.0,
    })

    assert session.processor.gaussian_sigma_px == pytest.approx(300.0 / (2.355 * 100.0))


def test_fast_gauss_no_background_uses_matched_filter(synthetic_stack):
    """'No background' drops the constant-background term (pure matched filter)."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    init_obj = StreamInit(name="s", dataset_name="d", data=stack, attrs=attrs)

    session = MonalisaReconstructor().make_session()
    session.begin(init_obj, params={"use_gpu": False, "bg_modelling": "No background"})

    assert session.processor.fit_background is False


def test_fast_gauss_interp_coords_clip_to_frame_edge():
    """Footprint samples beyond the high edge stay on the edge, not column/row zero."""
    x_interp, y_interp = get_interp_coords(
        xp=10.0,
        xo=9.0,
        yp=10.0,
        yo=9.0,
        nx_c=1,
        ny_c=1,
        num_rows=10,
        num_cols=10,
        num_rects=3,
    )

    assert np.min(x_interp) >= 0
    assert np.min(y_interp) >= 0
    assert np.max(x_interp) == 9
    assert np.max(y_interp) == 9
    assert not np.any(x_interp == 0)
    assert not np.any(y_interp == 0)


def test_live_session_finish(synthetic_stack):
    """Test StreamingSession finish returns final result."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    plan = session.begin(init_obj, params={"use_gpu": False})

    session.push(stack, 0, 100)

    final_result = session.finish()

    assert final_result is not None
    assert final_result.data.shape == plan.out_shape
    assert np.all(np.isfinite(final_result.data))


def test_live_session_close(synthetic_stack):
    """Test StreamingSession close cleans up resources."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    session.begin(init_obj, params={"use_gpu": False})

    session.close()


def test_gauss_processor_gpu_fallback():
    """Test that make_gauss_processor falls back to CPU when GPU unavailable."""
    processor = make_gauss_processor(
        xp=10.0,
        xo=5.0,
        yp=10.0,
        yo=5.0,
        nx_c=10,
        ny_c=10,
        nx_s=10,
        ny_s=10,
        num_rows=100,
        num_cols=100,
        num_rects=2,
        gaussian_sigma_px=1.25,
        use_gpu=True,
    )

    assert processor is not None
    assert hasattr(processor, "process_chunk")
    assert processor.num_rects == 2
    assert processor.gaussian_sigma_px == pytest.approx(1.25)
    assert processor.pts_per_focus == len(get_rectangles_coords(2)[0])


def test_make_gauss_processor_rejects_invalid_fast_gauss_options():
    """Fast-Gauss magic numbers are validated when supplied as parameters."""
    with pytest.raises(ValueError, match="num_rects"):
        make_gauss_processor(
            xp=10.0,
            xo=5.0,
            yp=10.0,
            yo=5.0,
            nx_c=10,
            ny_c=10,
            nx_s=10,
            ny_s=10,
            num_rows=100,
            num_cols=100,
            num_rects=0,
            use_gpu=False,
        )

    with pytest.raises(ValueError, match="gaussian_sigma_px"):
        make_gauss_processor(
            xp=10.0,
            xo=5.0,
            yp=10.0,
            yo=5.0,
            nx_c=10,
            ny_c=10,
            nx_s=10,
            ny_s=10,
            num_rows=100,
            num_cols=100,
            gaussian_sigma_px=0,
            use_gpu=False,
        )


def test_live_session_missing_scan_geometry():
    """Test that begin raises clear error when scan geometry is missing."""
    stack = np.random.randint(50, 150, size=(100, 100, 100), dtype=np.uint16)

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs={},
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    with pytest.raises(ValueError, match="Missing ScanStage"):
        session.begin(init_obj, params={})


def test_live_session_invalid_data_shape():
    """Test that begin raises error for non-3D data."""
    stack = np.random.randint(50, 150, size=(100, 100), dtype=np.uint16)

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs={
            "ImswitchData": {
                "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
                "ScanStage:axis_length": [450.0, 450.0, 1.0],
                "ScanStage:axis_step_size": [50.0, 50.0, 1.0],
                "Rec:LapseTime": 1,
            }
        },
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    with pytest.raises(ValueError, match="Expected 3D data"):
        session.begin(init_obj, params={})


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
