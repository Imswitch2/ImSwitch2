"""Spot counting: SMLM's net-gradient detection per frame, and tiles -> stage targets."""

import numpy as np
import pytest

from imswitch.improcess.analysis.spot_count import (
    SpotParams,
    TileOrientation,
    find_spots,
    find_spots_in_stack,
    spot_stage_positions,
    tile_spot_targets,
)
from imswitch.improcess.model import ArrayProcessingResult
from imswitch.improcess.model.result import result_kind
from imswitch.improcess.processors.spot_count import SpotCountProcessor, spot_default_params
from imswitch.improcess.reconstructors.smlm.detection import detect_spots

SHAPE = (128, 128)


def _frame(positions, *, amplitude=400.0, sigma=1.4, background=True, square=False, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[: SHAPE[0], : SHAPE[1]]
    image = np.full(SHAPE, 100.0)
    if background:
        # A broad hump and a ramp: the inhomogeneous background spots sit on.
        image += 600.0 * np.exp(-((yy - 40) ** 2 + (xx - 90) ** 2) / (2 * 25.0 ** 2)) + 1.5 * xx
    if square:
        image[70:110, 15:55] += 800.0
    for y, x in positions:
        image += amplitude * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * sigma ** 2))
    return rng.poisson(image).astype(np.float32)


def _spread(count, seed):
    """``count`` spot positions at least 12 px apart and away from the edges."""
    rng = np.random.default_rng(seed)
    chosen = []
    while len(chosen) < count:
        candidate = rng.uniform(10, SHAPE[0] - 10, 2)
        if all(np.hypot(*(candidate - other)) > 12 for other in chosen):
            chosen.append(candidate)
    return np.asarray(chosen).reshape(-1, 2)


def test_counts_spots_on_an_inhomogeneous_background():
    truth = [_spread(n, seed=n) for n in (0, 3, 8, 15)]
    stack = np.stack([_frame(p, seed=i) for i, p in enumerate(truth)])
    spots, counts = find_spots_in_stack(stack, SpotParams())
    assert counts.tolist() == [0, 3, 8, 15]
    for index, positions in enumerate(truth):
        mine = spots["frame"] == index
        found = np.column_stack([spots["y"][mine], spots["x"][mine]])
        for y, x in positions:
            assert np.hypot(found[:, 0] - y, found[:, 1] - x).min() <= 1.5


def test_relative_threshold_ignores_overall_brightness():
    positions = _spread(6, seed=3)
    dim = _frame(positions, seed=1)
    bright = dim * 4.0  # same scene, four times the counts
    params = SpotParams(threshold=6.0, threshold_mode="relative")
    assert len(find_spots(dim, params)["y"]) == len(find_spots(bright, params)["y"]) == 6


def test_edge_rejection_drops_the_rim_of_a_bright_structure():
    frame = _frame([], square=True)
    with_rim = find_spots(frame, SpotParams(reject_edges=False))
    without = find_spots(frame, SpotParams(reject_edges=True))
    assert len(with_rim["y"]) > 8
    # Only the square's four convex corners look like spots.
    assert len(without["y"]) <= 4


def test_matches_the_smlm_detector_without_the_additions():
    frame = _frame(_spread(10, seed=5))
    ours = find_spots(frame, SpotParams(threshold=500.0, reject_edges=False))
    smlm = detect_spots(frame, threshold=500.0, roi=7, sigma=1.0)
    assert np.array_equal(np.column_stack([ours["y"], ours["x"]]).astype(int), smlm)


def test_spot_count_gives_one_result_with_points_overlay_and_counts():
    truth = [_spread(n, seed=n) for n in (2, 5)]
    stack = np.stack([_frame(p, seed=i) for i, p in enumerate(truth)])
    source = ArrayProcessingResult(
        "movie", stack, ["T", "Y", "X"], axis_scales=[0.5, 0.1, 0.1], scale_unit="um"
    )
    result = SpotCountProcessor().apply(source, spot_default_params())
    assert result_kind(result) == "points"
    assert result.count == 7 and sorted(np.asarray(result.properties["t"]).tolist()) == [0, 0, 1, 1, 1, 1, 1]
    assert result.counts.tolist() == [2.0, 5.0]
    (plot,) = result.plot_payloads()
    assert plot.series[0].x.tolist() == [0.0, 0.5] and plot.x_label == "T (s)"
    image, spots = result.display_layers()
    assert image.role == "context" and np.shares_memory(image.data, stack) is False
    assert spots.kind == "points" and spots.data.shape == (7, 3)  # (t, y, x): each on its frame


def test_spot_count_takes_a_single_frame_and_survives_finding_nothing():
    source = ArrayProcessingResult("frame", _frame(_spread(4, seed=9)), ["Y", "X"])
    assert SpotCountProcessor().apply(source, spot_default_params()).counts.tolist() == [4.0]

    empty = SpotCountProcessor().apply(
        ArrayProcessingResult("blank", _frame([], background=False), ["Y", "X"]), spot_default_params())
    assert empty.count == 0 and empty.counts.tolist() == [0.0]
    # The image alone: an empty points layer is never handed to the viewer.
    assert [layer.kind for layer in empty.display_layers()] == ["image"]
    assert empty.table_records() == []


@pytest.fixture(scope="module")
def qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_widget_defaults_match_default_params(qapp):
    from qtpy import QtWidgets

    parent = QtWidgets.QWidget()
    widget = SpotCountProcessor().make_param_widget(parent)
    assert widget.get_values() == SpotCountProcessor.default_params()


# -- tiles -> stage targets ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "orientation, expected",
    [
        (TileOrientation(), (1000.0 + 2.0, 500.0 + 1.0)),
        (TileOrientation(flip_x=True), (1000.0 - 2.0, 500.0 + 1.0)),
        (TileOrientation(flip_y=True), (1000.0 + 2.0, 500.0 - 1.0)),
        (TileOrientation(swap_axes=True), (1000.0 + 1.0, 500.0 + 2.0)),
    ],
)
def test_a_spot_maps_to_the_stage_position_that_centres_it(orientation, expected):
    # Tile of 11 x 11 px at 0.5 um/px: centre pixel (5, 5). A spot 2 rows and
    # 4 columns from it is 1 um and 2 um away in the image.
    spots = {"frame": np.array([0]), "y": np.array([7.0]), "x": np.array([9.0])}
    x, y = spot_stage_positions(spots, np.array([[1000.0, 500.0]]), (11, 11), (0.5, 0.5), orientation)
    assert (x[0], y[0]) == pytest.approx(expected)


def test_mapping_inverts_the_tiling_layout_convention():
    """A spot at mosaic position m, seen in two overlapping tiles, gives one stage target."""
    from imswitch.imcommon.algorithms.tile_mosaic import _positions_from_stage

    orientation = {"flip_x": True, "flip_y": False, "swap_axes": True}
    stage = np.array([[0.0, 0.0], [30.0, 0.0], [0.0, 20.0]])
    pixel = (0.5, 0.5)
    entries = [{"stage_um": list(s)} for s in stage]
    tile_positions = _positions_from_stage({"orientation": orientation}, entries, pixel)
    mosaic_spot = np.array([70.0, 15.0])  # (row, col) in mosaic pixels
    spots = {"frame": [], "y": [], "x": []}
    for index, (row, col) in enumerate(tile_positions):
        local = mosaic_spot - (row, col)
        if (0 <= local).all() and (local < 200).all():
            spots["frame"].append(index)
            spots["y"].append(local[0])
            spots["x"].append(local[1])
    assert len(spots["frame"]) >= 2
    spots = {key: np.asarray(value) for key, value in spots.items()}
    x, y = spot_stage_positions(spots, stage, (200, 200), pixel, TileOrientation.from_mapping(orientation))
    assert np.ptp(x) == pytest.approx(0.0, abs=1e-9) and np.ptp(y) == pytest.approx(0.0, abs=1e-9)


def test_tile_targets_rank_by_count_and_select_above_the_minimum():
    counts = np.array([0, 3, 1, 3])
    stage = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [10.0, 10.0]])
    spots = {"frame": np.array([1, 1, 1, 2, 3, 3, 3])}
    spot_x = np.array([11.0, 12.0, 13.0, 1.0, 9.0, 10.0, 11.0])
    spot_y = np.array([1.0, 1.0, 1.0, 9.0, 9.0, 10.0, 11.0])
    table = tile_spot_targets(counts, spots, spot_x, spot_y, stage, min_spots=1)
    assert table["tile"].tolist() == [1, 3, 2, 0]  # ties keep acquisition order
    assert table["selected"].tolist() == [True, True, True, False]
    assert table["target_x_um"].tolist() == [12.0, 10.0, 1.0, 0.0]
    centre = tile_spot_targets(counts, spots, spot_x, spot_y, stage, target="tile centre")
    assert centre["target_x_um"].tolist() == [10.0, 10.0, 0.0, 0.0]


# -- a saved tiling run: per-tile counts, stage targets, the processor -------------------------

TILE = 96
PIXEL_UM = 0.5
ORIENTATION = {"flip_x": True, "flip_y": False, "swap_axes": True}


def _write_tiling_run(folder, spot_count=14, seed=4):
    """A 3 x 3 run cut from one synthetic field, laid out as the tiling controller would.

    Returns ``(manifest path, tile origins in field pixels, spot positions in field pixels)``.
    """
    import json

    import tifffile

    from imswitch.imcommon.algorithms.tile_mosaic import _positions_from_stage

    step_um = 40.0  # 80 px: 16 px of overlap
    stage = [(1000.0 + gx * step_um, 2000.0 + gy * step_um) for gy in range(3) for gx in range(3)]
    entries = [{"stage_um": list(s)} for s in stage]
    layout = np.asarray(_positions_from_stage({"orientation": ORIENTATION}, entries, (PIXEL_UM, PIXEL_UM)))
    layout -= layout.min(axis=0)
    size = int(layout.max() + TILE)

    rng = np.random.default_rng(seed)
    spots = []
    while len(spots) < spot_count:
        candidate = rng.uniform(6, size - 6, 2)
        # Clear of every tile border, so which tiles see it is not in doubt.
        near_border = any(
            (np.abs(candidate - origin) < 6).any() or (np.abs(candidate - origin - TILE) < 6).any()
            for origin in layout
        )
        if not near_border and all(np.hypot(*(candidate - s)) > 10 for s in spots):
            spots.append(candidate)
    yy, xx = np.mgrid[:size, :size]
    field = np.full((size, size), 100.0) + 0.5 * yy
    for y, x in spots:
        field += 400.0 * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * 1.4 ** 2))
    field = rng.poisson(field).astype(np.uint16)

    folder.mkdir(parents=True, exist_ok=True)
    tiles = []
    for index, ((row, col), s) in enumerate(zip(layout.astype(int), stage)):
        name = f"tile_{index:02d}.tiff"
        tifffile.imwrite(str(folder / name), field[row:row + TILE, col:col + TILE])
        tiles.append({"filename": name, "grid": [index % 3, index // 3], "stage_um": list(s),
                      "pixel_xy": [float(col), float(row)], "correction_px": [0.0, 0.0]})
    manifest = folder / "tiles.json"
    manifest.write_text(json.dumps({
        "format": "imswitch-tiling/1",
        "pixel_size_um": {"y": PIXEL_UM, "x": PIXEL_UM},
        "tile_step_um": step_um,
        "tile_shape_px": {"height": TILE, "width": TILE, "depth": 1},
        "orientation": ORIENTATION,
        "xy_positioner": "XY Stage",
        "xy_axes": ["X", "Y"],
        "tiles": tiles,
    }), encoding="utf-8")
    return manifest, layout, np.asarray(spots)


def test_tiling_run_counts_per_tile_and_maps_each_spot_to_one_stage_position(tmp_path):
    from imswitch.improcess.analysis.spot_count import count_spots_in_tiling

    manifest, layout, spots = _write_tiling_run(tmp_path / "run")
    found = count_spots_in_tiling(manifest, SpotParams())

    expected = [
        int(sum(((s >= origin + 3) & (s < origin + TILE - 3)).all() for s in spots))
        for origin in layout
    ]
    counts = dict(zip(found.tiles["tile"].tolist(), found.tiles["spots"].tolist()))
    assert [counts[i] for i in range(9)] == expected
    assert found.tiles["spots"].tolist() == sorted(expected, reverse=True)
    assert found.positioner == "XY Stage" and found.axes == ("X", "Y")

    # Every sample spot, however many tiles saw it, has one stage position.
    for y, x in spots:
        near = np.hypot(found.spots["mosaic_y"] - y, found.spots["mosaic_x"] - x) <= 1.5
        assert near.any()
        assert np.ptp(found.spots["stage_x_um"][near]) <= PIXEL_UM
        assert np.ptp(found.spots["stage_y_um"][near]) <= PIXEL_UM


def test_tile_spots_processor_on_a_tiling_mosaic(tmp_path):
    from imswitch.improcess.processors.tile_spots import TileSpotsProcessor
    from imswitch.improcess.reconstructors.tiling.reconstructor import TilingMosaicResult

    manifest, _layout, spots = _write_tiling_run(tmp_path / "run")
    mosaic = TilingMosaicResult(
        name="run mosaic", data=np.zeros((10, 10)), axis_labels=["Y", "X"],
        axis_scales=[PIXEL_UM, PIXEL_UM], scale_unit="µm", manifest=manifest,
    )
    processor = TileSpotsProcessor()
    assert processor.accepts(mosaic)
    assert not processor.accepts(ArrayProcessingResult("plain", np.zeros((10, 10)), ["Y", "X"]))

    tiles = processor.apply(mosaic, TileSpotsProcessor.default_params())
    assert tiles.frame == "stage:XY Stage" and tiles.count == 9
    selected = np.asarray(tiles.properties["selected"], dtype=bool)
    counts = np.asarray(tiles.properties["spots"])
    assert (selected == (counts >= 1)).all() and (np.diff(counts) <= 0).all()
    assert len(tiles.spots["y"]) >= len(spots)  # overlap spots are seen twice
    kinds = [(layer.kind, layer.component) for layer in tiles.display_layers()]
    assert kinds == [("image", "image"), ("points", "spots"), ("points", "targets")]
    assert sorted(tiles.counts.tolist(), reverse=True) == counts.tolist()
