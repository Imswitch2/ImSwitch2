"""Phase 2b: alignment-only geometry as a reusable value."""

from pathlib import Path

import numpy as np
import pytest

from imswitch.imcommon.algorithms.tile_mosaic import (
    AlignmentArtifact,
    IndexedTile,
    LayoutOptions,
    MosaicDataset,
    MosaicTile,
    TilingDatasetIndex,
    apply_layout,
    solve_layout,
)


def _artifact(path: Path, shape=(64, 64)) -> AlignmentArtifact:
    return AlignmentArtifact(
        path=path,
        detector='AlignmentCamera',
        axes='YX',
        stored_axes='YX',
        shape=shape,
        stored_shape=shape,
    )


def _tile(tile_id, path, stage_x, saved_col) -> IndexedTile:
    return IndexedTile(
        tile_id=tile_id,
        grid=(tile_id, 0),
        stage_um=(float(stage_x), 0.0),
        saved_position_yx=(0.0, float(saved_col)),
        alignment=_artifact(path),
        payloads={},
    )


def _index(tmp_path, tiles) -> TilingDatasetIndex:
    manifest = tmp_path / 'tiles.json'
    manifest.write_text('{}', encoding='utf-8')
    return TilingDatasetIndex(
        manifest=manifest,
        alignment_detector='AlignmentCamera',
        pixel_size_yx_um=(1.0, 1.0),
        z_step_um=0.0,
        tiles=tuple(tiles),
        detectors=(),
        orientation=(False, False, False),
        format='imswitch-tiling/2',
    )


def test_solve_layout_refines_alignment_images_and_keeps_missing_identity(tmp_path):
    import tifffile
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(4)
    scene = gaussian_filter(rng.random((96, 128)).astype(np.float32), 2.0)
    first = tmp_path / 'alignment_0.tiff'
    second = tmp_path / 'alignment_1.tiff'
    tifffile.imwrite(str(first), scene[:64, :64])
    tifffile.imwrite(str(second), scene[:64, 32:96])
    missing = tmp_path / 'alignment_2.tiff'
    index = _index(tmp_path, [
        _tile(10, first, 0, 0),
        _tile(30, second, 38, 38),  # six pixels away from the true overlap
        _tile(90, missing, 96, 96),
    ])

    layout = solve_layout(
        index,
        LayoutOptions(refine=True, max_shift_px=10),
        progress=lambda _message: None,
    )

    assert layout.source == 'refined'
    assert layout.positions_yx[10] == pytest.approx((0.0, 0.0), abs=0.5)
    assert layout.positions_yx[30] == pytest.approx((0.0, 32.0), abs=0.5)
    # Missing tile 90 is not shifted into tile 30's list slot.
    assert layout.positions_yx[90] == pytest.approx((0.0, 96.0))
    assert layout.report.tiles == 3
    with pytest.raises(TypeError):
        layout.positions_yx[10] = (1.0, 1.0)


def test_refinement_with_no_readable_alignment_explains_the_fallback(tmp_path):
    index = _index(tmp_path, [
        _tile(4, tmp_path / 'missing_0.tiff', 0, 0),
        _tile(8, tmp_path / 'missing_1.tiff', 32, 32),
    ])

    with pytest.raises(ValueError, match='Disable refinement'):
        solve_layout(index, LayoutOptions(refine=True))

    nominal = solve_layout(index, LayoutOptions(refine=False))
    assert nominal.source == 'stage'
    assert nominal.positions_yx == {4: (0.0, 0.0), 8: (0.0, 32.0)}


def test_layout_is_applied_by_tile_id_not_loaded_order(tmp_path):
    index = _index(tmp_path, [
        _tile(2, tmp_path / 'unused_0.tiff', 0, 0),
        _tile(7, tmp_path / 'unused_1.tiff', 40, 40),
    ])
    layout = solve_layout(index, LayoutOptions(refine=False))
    dataset = MosaicDataset(tiles=[
        MosaicTile('seven', np.zeros((4, 4)), (999, 999), tile_id=7),
        MosaicTile('two', np.zeros((4, 4)), (999, 999), tile_id=2),
    ])

    apply_layout(dataset, layout)

    assert dataset.tiles[0].position == (0.0, 40.0)
    assert dataset.tiles[1].position == (0.0, 0.0)
