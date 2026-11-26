"""Tests for the ImProcess BeadRec reconstructor (raster reconstruct + fit)."""
import types

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    RecordedEventSpan,
    TraversalRule,
    encode_acquisition_layout,
)
from imswitch.improcess.live.sources import InMemoryStackWrapper
from imswitch.improcess.reconstructors.beadrec import (
    BeadRecReconstructor,
    infer_scan_dims,
    reconstruct_bead_image,
)
from imswitch.improcess.reconstructors.beadrec.reconstructor import _FIT_CHOICES


def _uniform_frames(values, frame=5):
    """One (frame x frame) frame per value, each frame filled with that value."""
    values = np.asarray(values, dtype=float)
    return values[:, None, None] * np.ones((values.size, frame, frame), dtype=float)


def _line_step_layout(
    *, rows=18, cols=18, conditions=2, spans=None, traversal=()
) -> AcquisitionLayout:
    """The motivating Advanced Scan geometry: rows -> condition -> cols."""
    loops = [AcquisitionLoop("scan_y", "scan_y", rows, step=0.05, unit="um")]
    if conditions > 1:
        loops.append(
            AcquisitionLoop("linestep", "condition", conditions, labels=("A", "B"))
        )
    loops.append(AcquisitionLoop("scan_x", "scan_x", cols, step=0.05, unit="um"))
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=tuple(loops),
        traversal=traversal,
        recorded_event_spans=spans,
        scan_source="ScanControllerAdvanced",
    )


def _recorded_source(frames, layout):
    """A DataObj-like source carrying its recorded acquisition layout."""
    return InMemoryStackWrapper(
        "scan",
        "CAM",
        frames,
        {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": encode_acquisition_layout(layout),
            "writing": False,
            "recording:completion_outcome": "complete",
        },
    )


def test_648_frame_line_step_scan_is_neither_squared_nor_truncated():
    """The motivating regression: 648 frames are 18x18x2, not a 25x25 raster.

    sqrt(648) = 25.4, so the old auto-square inferred 25x25 = 625 pixels and
    the reconstruction buffer silently discarded the last 23 frames.
    """
    rows = cols = 18
    frames = _uniform_frames(np.arange(rows * cols * 2, dtype=float))

    result = BeadRecReconstructor().process(
        _recorded_source(frames, _line_step_layout()), {"fit_model": "none"}
    )

    assert result.data.shape == (2, rows, cols)
    assert result.axis_labels == ["Condition", "Y", "X"]
    assert result.metadata["scan_dims"] == (cols, rows)
    assert result.metadata["geometry_source"] == "layout"
    assert result.metadata["condition_labels"] == ("A", "B")
    # frame = ((scan_y * 2 + condition) * 18) + scan_x, so every frame lands
    # in exactly one pixel and none is dropped.
    y, x = np.mgrid[0:rows, 0:cols]
    for condition in (0, 1):
        expected = (y * 2 + condition) * cols + x
        np.testing.assert_allclose(result.data[condition], expected)


def test_detector_gated_to_one_condition_reconstructs_all_its_frames():
    """A detector enabled only for B has 324 frames and one condition."""
    rows = cols = 18
    frames = _uniform_frames(np.arange(rows * cols, dtype=float))
    layout = _line_step_layout(
        spans=(RecordedEventSpan(cols, cols, stride=1, period=cols * 2, repeats=rows),)
    )

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    assert result.data.shape == (rows, cols)
    assert result.axis_labels == ["Y", "X"]
    assert result.metadata["condition_labels"] == ("B",)
    y, x = np.mgrid[0:rows, 0:cols]
    np.testing.assert_allclose(result.data, y * cols + x)


def test_serpentine_rows_are_placed_by_logical_coordinate():
    """Reversed lines must not mirror every other row of the raster."""
    rows, cols = 4, 3
    frames = _uniform_frames(np.arange(rows * cols, dtype=float))
    layout = _line_step_layout(
        rows=rows,
        cols=cols,
        conditions=1,
        traversal=(TraversalRule("scan_x", "serpentine", ("scan_y",)),),
    )

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    expected = np.arange(rows * cols, dtype=float).reshape(rows, cols)
    expected[1::2] = expected[1::2, ::-1]
    np.testing.assert_allclose(result.data, expected)


def test_frame_count_that_disagrees_with_the_layout_is_rejected():
    """A short stack never reconstructs: resolution refuses it outright.

    The explicit layout claims 648 frames, so a 647-frame array is a conflict
    rather than a smaller scan, and falling back to inference would be exactly
    the silent reshape this contract exists to prevent.
    """
    rows = cols = 18
    frames = _uniform_frames(np.arange(rows * cols * 2 - 1, dtype=float))
    source = _recorded_source(frames, _line_step_layout())

    with pytest.raises(ValueError) as error:
        BeadRecReconstructor().process(source, {"fit_model": "none"})

    assert "FRAME_COUNT_MISMATCH" in {
        issue.code for issue in getattr(error.value, "issues", ())
    }


def test_layout_count_check_guards_a_source_whose_layout_resolves():
    """BeadRec re-checks the count for layouts resolution accepted.

    A stopped-early or still-writing source resolves with its mismatch
    downgraded to a warning, so the plugin is the last line of defence
    against padding or discarding frames.
    """
    rows = cols = 4
    layout = _line_step_layout(rows=rows, cols=cols, conditions=1)
    frames = _uniform_frames(np.arange(rows * cols - 1, dtype=float))
    source = InMemoryStackWrapper(
        "scan",
        "CAM",
        frames,
        {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": encode_acquisition_layout(layout),
            "recording:completion_outcome": "stopped_early",
            "recording:planned_frames": rows * cols,
            "recording:actual_frames": rows * cols - 1,
            "writing": False,
        },
    )

    with pytest.raises(ValueError) as error:
        BeadRecReconstructor().process(source, {"fit_model": "none"})

    message = str(error.value)
    assert "complete declared acquisition layout" in message or "needs exactly" in message


def test_manual_entries_that_contradict_the_recording_are_a_conflict():
    """A stale spinbox must not quietly reshape a recorded scan."""
    rows = cols = 18
    frames = _uniform_frames(np.arange(rows * cols * 2, dtype=float))

    with pytest.raises(ValueError, match="Clear the manual entries"):
        BeadRecReconstructor().process(
            _recorded_source(frames, _line_step_layout()),
            {"scan_x": 25, "scan_y": 25, "fit_model": "none"},
        )


def test_layout_step_sizes_calibrate_the_raster():
    """Recorded steps replace the manual defaults, so pixels stay square."""
    rows, cols = 4, 2
    frames = _uniform_frames(np.arange(rows * cols, dtype=float))
    loops = (
        AcquisitionLoop("scan_y", "scan_y", rows, step=0.2, unit="um"),
        AcquisitionLoop("scan_x", "scan_x", cols, step=0.1, unit="um"),
    )
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=loops,
    )

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    # 0.1 um in x against 0.2 um in y: y is resampled to the finer pitch.
    assert result.data.shape[0] > rows
    assert result.metadata["geometry_source"] == "layout"


def _scan_layout(*loops) -> AcquisitionLayout:
    """A frame-stream layout with ``loops`` outermost first."""
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=tuple(loops),
    )


def _axis(kind, count, step=0.1):
    return AcquisitionLoop(kind, kind, count, step=step, unit="um")


@pytest.mark.parametrize(
    ("slow_kind", "fast_kind", "labels"),
    [
        ("scan_z", "scan_x", ["Z", "X"]),
        ("scan_z", "scan_y", ["Z", "Y"]),
        ("scan_y", "scan_x", ["Y", "X"]),
    ],
)
def test_any_two_scan_axes_make_an_image(slow_kind, fast_kind, labels):
    """An XZ or YZ scan is as much a raster as XY, with its own axis labels."""
    rows, cols = 3, 5
    frames = _uniform_frames(np.arange(rows * cols, dtype=float))
    layout = _scan_layout(_axis(slow_kind, rows), _axis(fast_kind, cols))

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    assert result.axis_labels == labels
    assert result.metadata["scan_axes"] == tuple(labels)
    assert result.metadata["scan_dims"] == (cols, rows)
    np.testing.assert_allclose(
        result.data, np.arange(rows * cols, dtype=float).reshape(rows, cols)
    )


def test_a_z_fast_scan_is_placed_by_coordinate_not_by_loop_order():
    """Z stepped inside X still shows Z down the rows and X along the columns."""
    xs, zs = 4, 3
    frames = _uniform_frames(np.arange(xs * zs, dtype=float))
    layout = _scan_layout(_axis("scan_x", xs), _axis("scan_z", zs))

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    assert result.axis_labels == ["Z", "X"]
    # frame = x * zs + z, so pixel (z, x) holds that frame's value.
    z, x = np.mgrid[0:zs, 0:xs]
    np.testing.assert_allclose(result.data, x * zs + z)


def test_an_xyz_scan_reconstructs_a_volume():
    zs, ys, xs = 3, 4, 5
    frames = _uniform_frames(np.arange(zs * ys * xs, dtype=float))
    layout = _scan_layout(
        _axis("scan_z", zs), _axis("scan_y", ys), _axis("scan_x", xs)
    )

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    assert result.axis_labels == ["Z", "Y", "X"]
    assert result.metadata["scan_dims"] == (xs, ys, zs)
    np.testing.assert_allclose(
        result.data, np.arange(zs * ys * xs, dtype=float).reshape(zs, ys, xs)
    )


def test_an_xyz_scan_with_conditions_keeps_one_volume_per_condition():
    zs, ys, xs = 2, 3, 4
    frames = _uniform_frames(np.arange(zs * ys * 2 * xs, dtype=float))
    layout = _scan_layout(
        _axis("scan_z", zs),
        _axis("scan_y", ys),
        AcquisitionLoop("linestep", "condition", 2, labels=("A", "B")),
        _axis("scan_x", xs),
    )

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    assert result.data.shape == (2, zs, ys, xs)
    assert result.axis_labels == ["Condition", "Z", "Y", "X"]
    assert result.metadata["condition_labels"] == ("A", "B")
    z, y, x = np.mgrid[0:zs, 0:ys, 0:xs]
    for condition in (0, 1):
        expected = ((z * ys + y) * 2 + condition) * xs + x
        np.testing.assert_allclose(result.data[condition], expected)


def test_a_single_plane_z_loop_leaves_an_xy_image():
    """A one-step Z is a placeholder, not a third dimension."""
    ys, xs = 3, 4
    frames = _uniform_frames(np.arange(ys * xs, dtype=float))
    layout = _scan_layout(
        _axis("scan_z", 1), _axis("scan_y", ys), _axis("scan_x", xs)
    )

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    assert result.axis_labels == ["Y", "X"]
    assert result.data.shape == (ys, xs)


def test_a_coarser_z_step_is_resampled_to_square_pixels():
    zs, xs = 4, 6
    frames = _uniform_frames(np.arange(zs * xs, dtype=float))
    layout = _scan_layout(_axis("scan_z", zs, step=0.2), _axis("scan_x", xs, step=0.1))

    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    assert result.data.shape == (2 * zs, xs)


def test_an_uncalibrated_z_loop_is_refused():
    """Z steps are the fit's axial pixel size, as X and Y steps are laterally."""
    frames = _uniform_frames(np.arange(12, dtype=float))
    layout = _scan_layout(
        AcquisitionLoop("scan_z", "scan_z", 3), _axis("scan_x", 4)
    )

    with pytest.raises(ValueError) as error:
        BeadRecReconstructor().process(
            _recorded_source(frames, layout), {"fit_model": "none"}
        )

    assert "UNCALIBRATED_ACQUISITION_LOOP" in {
        issue.code for issue in getattr(error.value, "issues", ())
    }


def test_a_volume_is_fitted_through_the_bead_centre_in_three_sections():
    zs = ys = xs = 15
    z, y, x = np.mgrid[0:zs, 0:ys, 0:xs]
    # Elongated along Z, as a real PSF is.
    bead = 1000.0 * np.exp(
        -((x - 7) ** 2 / (2 * 1.5 ** 2) + (y - 6) ** 2 / (2 * 1.5 ** 2)
          + (z - 8) ** 2 / (2 * 3.0 ** 2))
    ) + 10.0
    layout = _scan_layout(
        _axis("scan_z", zs), _axis("scan_y", ys), _axis("scan_x", xs)
    )

    result = BeadRecReconstructor().process(
        _recorded_source(_uniform_frames(bead.ravel()), layout),
        {"fit_model": "gaussian2d"},
    )

    sections = result.metadata["fit_sections"]
    assert set(sections) == {"XY", "XZ", "YZ"}
    assert sections["XY"]["section"] == {"Z": 8}
    assert sections["XZ"]["section"] == {"Y": 6}
    assert sections["YZ"]["section"] == {"X": 7}
    assert sections["XZ"]["axes"] == ("Z", "X")
    for entry in sections.values():
        assert entry["r_squared"] > 0.99

    def widest(entry):
        return max(entry["sigma_x"], entry["sigma_y"])

    assert widest(sections["XY"]) == pytest.approx(1.5, rel=0.05)
    assert widest(sections["XZ"]) == pytest.approx(3.0, rel=0.05)
    assert widest(sections["YZ"]) == pytest.approx(3.0, rel=0.05)


@pytest.mark.parametrize("fmt", ["tiff", "zarr"])
@pytest.mark.parametrize("kinds", [("scan_z", "scan_x"), ("scan_z", "scan_y", "scan_x")])
def test_xz_and_xyz_results_save(tmp_path, fmt, kinds):
    from imswitch.improcess.model.result_io import save_image_result

    layout = _scan_layout(*(_axis(kind, 3) for kind in kinds))
    frames = _uniform_frames(np.arange(3 ** len(kinds), dtype=float))
    result = BeadRecReconstructor().process(
        _recorded_source(frames, layout), {"fit_model": "none"}
    )

    path = tmp_path / f"bead.{'ome.tif' if fmt == 'tiff' else 'zarr'}"
    save_image_result(result, path, fmt)

    assert path.exists()


def test_infer_scan_dims():
    assert infer_scan_dims(12, 4, 3) == (4, 3)     # explicit
    assert infer_scan_dims(12, scan_x=4) == (4, 3)  # infer the other axis
    assert infer_scan_dims(12, scan_y=3) == (4, 3)
    # Neither entry set: the raster is unknown. Guessing sqrt(n) turned a
    # 648-frame 18x18x2 line-step scan into 25x25 and dropped 23 frames.
    assert infer_scan_dims(16) is None


def test_reconstruct_bead_image_raster_order():
    # frame i is uniform == i, so pixel (y, x) == frame index y*sx + x
    sx, sy = 4, 3
    frames = _uniform_frames(np.arange(sx * sy))
    image = reconstruct_bead_image(frames, (sx, sy))
    assert image.shape == (sy, sx)
    assert np.allclose(image, np.arange(sx * sy).reshape(sy, sx))


def test_reconstruct_bead_image_roi_mean():
    # ROI selects a sub-region; pixel value = mean intensity there
    frames = np.zeros((4, 6, 6), dtype=float)
    for i in range(4):
        frames[i, 1:3, 1:3] = 10.0 * (i + 1)   # ROI region
    image = reconstruct_bead_image(frames, (2, 2), roi_bounds=(1, 1, 3, 3))
    assert np.allclose(image.ravel(), [10, 20, 30, 40])


def test_process_reconstructs_and_fits_gaussian():
    sx = sy = 15
    yy, xx = np.mgrid[0:sy, 0:sx]
    bead = 1000.0 * np.exp(-(((xx - 7) ** 2 + (yy - 7) ** 2) / (2 * 2.0 ** 2))) + 10.0
    frames = _uniform_frames(bead.ravel())         # scan index i -> bead[y, x]

    data_obj = types.SimpleNamespace(name="scan", data=frames)
    result = BeadRecReconstructor().process(
        data_obj, {"scan_x": sx, "scan_y": sy, "fit_model": "gaussian2d"}
    )

    assert result.name == "scan (beadrec)"
    assert result.data.shape == (sy, sx)
    assert np.allclose(result.data, bead, atol=1e-3)      # reconstruction == bead
    assert result.metadata["scan_dims"] == (sx, sy)
    fit = result.metadata["fit"]
    assert fit["model"] == "gaussian2d"
    assert fit["r_squared"] > 0.9
    cx, cy = fit["center_px"]
    assert cx == pytest.approx(7.0, abs=1.5) and cy == pytest.approx(7.0, abs=1.5)


def test_process_without_fit_and_bad_fit_is_tolerant():
    frames = _uniform_frames(np.arange(16))
    data_obj = types.SimpleNamespace(name="s", data=frames)
    square = {"scan_x": 4, "scan_y": 4}
    # no fit requested
    r1 = BeadRecReconstructor().process(data_obj, {**square, "fit_model": "none"})
    assert "fit" not in r1.metadata and r1.metadata["scan_dims"] == (4, 4)
    # a flat image can't be fit -> recorded as fit_error, not a crash
    flat = _uniform_frames(np.full(16, 5.0))
    r2 = BeadRecReconstructor().process(
        types.SimpleNamespace(name="s", data=flat),
        {**square, "fit_model": "gaussian2d"},
    )
    assert "fit_error" in r2.metadata


def test_process_refuses_to_guess_an_unknown_raster():
    """Without geometry the scan shape is unknown, and a guess corrupts it."""
    frames = _uniform_frames(np.arange(16))

    with pytest.raises(ValueError, match="cannot determine the raster"):
        BeadRecReconstructor().process(
            types.SimpleNamespace(name="s", data=frames), {"fit_model": "none"}
        )


@pytest.mark.parametrize("model_name", ["exponential2d", "sine1d"])
def test_process_exposes_and_runs_new_shared_fits(model_name):
    size = 32
    yy, xx = np.indices((size, size), dtype=float)
    if model_name == "exponential2d":
        image = 80.0 * np.exp(-np.hypot(xx - 15.2, yy - 16.1) / 4.5) + 6.0
    else:
        theta = np.deg2rad(24.0)
        u = xx * np.cos(theta) + yy * np.sin(theta)
        image = 20.0 * np.sin(2 * np.pi * u / 10.5 + 0.3) + 50.0

    result = BeadRecReconstructor().process(
        types.SimpleNamespace(name="scan", data=_uniform_frames(image.ravel())),
        {"scan_x": size, "scan_y": size, "fit_model": model_name},
    )

    assert model_name in _FIT_CHOICES
    assert result.metadata["fit"]["model"] == model_name
    assert result.metadata["fit"]["r_squared"] > 0.99


def test_beadrec_registered_as_builtin():
    from imswitch.improcess.reconstructors import available_reconstructor_ids
    assert "beadrec" in available_reconstructor_ids()
