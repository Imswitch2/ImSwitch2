"""Tests for reassembling a saved tiling dataset."""

import json

import numpy as np
import pytest

from imswitch.imcommon.algorithms.tile_mosaic import (
    MANIFEST_NAME,
    MosaicDataset,
    MosaicTile,
    assemble,
    assemble_dataset,
    TileLink,
    find_manifest,
    load_dataset,
    refine_layout,
    refine_positions,
)


def _texture(shape, seed=0):
    from scipy.ndimage import gaussian_filter
    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.random(shape).astype(np.float32), 2.0)


def _write_dataset(folder, tiles, extra=None, depth=1, z_step=0.0):
    """Write a minimal tiling dataset: tiles plus a manifest."""
    import tifffile

    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, data, (row, col), grid in tiles:
        tifffile.imwrite(str(folder / name), data)
        entries.append({
            'filename': name,
            'grid': list(grid),
            'stage_um': [0.0, 0.0],
            'pixel_xy': [float(col), float(row)],
            'correction_px': [0.0, 0.0],
        })

    payload = {
        'format': 'imswitch-tiling/1',
        'pixel_size_um': {'y': 0.5, 'x': 0.5},
        'tile_step_um': 10.0,
        'tile_shape_px': {
            'height': tiles[0][1].shape[-2],
            'width': tiles[0][1].shape[-1],
            'depth': depth,
        },
        'z_step_um': z_step,
        'orientation': {'flip_x': False, 'flip_y': False, 'swap_axes': False},
        'tiles': entries,
    }
    payload.update(extra or {})
    (folder / MANIFEST_NAME).write_text(json.dumps(payload), encoding='utf-8')
    return folder


# ----------------------------------------------------------------------
# Finding and loading
# ----------------------------------------------------------------------


def test_manifest_is_found_from_a_tile_the_folder_or_itself(tmp_path):
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', np.ones((8, 8), np.uint16), (0, 0), (0, 0))],
    )

    assert find_manifest(folder) == folder / MANIFEST_NAME
    assert find_manifest(folder / 'a.tiff') == folder / MANIFEST_NAME
    assert find_manifest(folder / MANIFEST_NAME) == folder / MANIFEST_NAME


def test_missing_manifest_is_reported_clearly(tmp_path):
    (tmp_path / 'stray.tiff').write_bytes(b'')

    with pytest.raises(FileNotFoundError, match='tiling dataset'):
        load_dataset(tmp_path / 'stray.tiff')


def test_load_reads_tiles_and_calibration(tmp_path):
    folder = _write_dataset(tmp_path / 'run', [
        ('a.tiff', np.full((8, 8), 3, np.uint16), (0, 0), (0, 0)),
        ('b.tiff', np.full((8, 8), 7, np.uint16), (0, 4), (1, 0)),
    ])

    dataset = load_dataset(folder)

    assert len(dataset.tiles) == 2
    assert dataset.pixel_size_um == (0.5, 0.5)
    # pixel_xy is (x, y); positions are (row, col).
    assert dataset.tiles[1].position == (0.0, 4.0)
    assert dataset.is_volumetric is False


def test_load_skips_a_tile_whose_file_is_gone(tmp_path):
    folder = _write_dataset(tmp_path / 'run', [
        ('a.tiff', np.ones((8, 8), np.uint16), (0, 0), (0, 0)),
        ('b.tiff', np.ones((8, 8), np.uint16), (0, 4), (1, 0)),
    ])
    (folder / 'b.tiff').unlink()

    dataset = load_dataset(folder)

    assert [tile.name for tile in dataset.tiles] == ['a.tiff']


def test_load_raises_when_nothing_is_readable(tmp_path):
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', np.ones((8, 8), np.uint16), (0, 0), (0, 0))],
    )
    (folder / 'a.tiff').unlink()

    with pytest.raises(ValueError, match='No readable tiles'):
        load_dataset(folder)


# ----------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------


def _dataset(tiles, depth=1):
    return MosaicDataset(tiles=[
        MosaicTile(name=name, data=data, position=pos)
        for name, data, pos in tiles
    ])


def test_assemble_places_tiles_by_position():
    left = np.full((4, 4), 2.0, np.float32)
    right = np.full((4, 4), 6.0, np.float32)

    mosaic = assemble(_dataset([
        ('a', left, (0.0, 0.0)), ('b', right, (0.0, 4.0)),
    ]), blend=False)

    assert mosaic.shape == (4, 8)
    assert mosaic[0, 0] == pytest.approx(2.0)
    assert mosaic[0, 7] == pytest.approx(6.0)


def test_assemble_averages_an_overlap_when_blending():
    a = np.full((4, 4), 2.0, np.float32)
    b = np.full((4, 4), 6.0, np.float32)

    mosaic = assemble(_dataset([
        ('a', a, (0.0, 0.0)), ('b', b, (0.0, 2.0)),
    ]), blend=True)

    assert mosaic.shape == (4, 6)
    assert mosaic[0, 0] == pytest.approx(2.0)   # a only
    assert mosaic[0, 3] == pytest.approx(4.0)   # both
    assert mosaic[0, 5] == pytest.approx(6.0)   # b only


def test_assemble_handles_negative_positions():
    a = np.ones((4, 4), np.float32)

    mosaic = assemble(_dataset([
        ('a', a, (0.0, 0.0)), ('b', a * 2, (-2.0, -3.0)),
    ]), blend=False)

    assert mosaic.shape == (6, 7)


def test_assemble_builds_a_volume_from_3d_tiles():
    a = np.ones((3, 4, 4), np.float32)
    b = np.full((3, 4, 4), 5.0, np.float32)

    mosaic = assemble(_dataset([
        ('a', a, (0.0, 0.0)), ('b', b, (0.0, 4.0)),
    ]), blend=False)

    assert mosaic.shape == (3, 4, 8)
    assert mosaic[2, 0, 7] == pytest.approx(5.0)


def test_assemble_rejects_an_empty_dataset():
    with pytest.raises(ValueError, match='empty dataset'):
        assemble(MosaicDataset())


# ----------------------------------------------------------------------
# Refinement
# ----------------------------------------------------------------------


def test_refine_recovers_a_misrecorded_position():
    """A tile whose stored position is wrong gets pulled back into place."""
    scene = _texture((128, 128), seed=3)
    a = scene[0:64, 0:64]
    b = scene[0:64, 32:96]          # truly 32 px right of a

    dataset = _dataset([('a', a, (0.0, 0.0)), ('b', b, (0.0, 37.0))])

    moved = refine_positions(dataset)

    assert moved == 1
    assert dataset.tiles[1].position[1] == pytest.approx(32.0, abs=1.0)
    assert dataset.tiles[1].position[0] == pytest.approx(0.0, abs=1.0)


def test_refine_leaves_correct_positions_alone():
    scene = _texture((128, 128), seed=4)
    dataset = _dataset([
        ('a', scene[0:64, 0:64], (0.0, 0.0)),
        ('b', scene[0:64, 32:96], (0.0, 32.0)),
    ])

    refine_positions(dataset)

    assert dataset.tiles[1].position[1] == pytest.approx(32.0, abs=1.0)


def test_refine_rejects_an_implausible_correction():
    scene = _texture((128, 128), seed=5)
    dataset = _dataset([
        ('a', scene[0:64, 0:64], (0.0, 0.0)),
        ('b', scene[0:64, 32:96], (0.0, 50.0)),
    ])

    moved = refine_positions(dataset, max_shift_px=2.0)

    assert moved == 0
    assert dataset.tiles[1].position[1] == pytest.approx(50.0)


def test_refine_aligns_volumes_on_their_projection():
    """3D tiles move as a whole; planes are not aligned independently."""
    scene = _texture((128, 128), seed=6)
    a = np.stack([scene[0:64, 0:64]] * 3)
    b = np.stack([scene[0:64, 32:96]] * 3)

    dataset = _dataset([('a', a, (0.0, 0.0)), ('b', b, (0.0, 37.0))])
    moved = refine_positions(dataset)

    assert moved == 1
    assert dataset.tiles[1].position[1] == pytest.approx(32.0, abs=1.0)


def test_refine_is_a_no_op_for_a_single_tile():
    assert refine_positions(_dataset([('a', np.ones((8, 8)), (0.0, 0.0))])) == 0


# ----------------------------------------------------------------------
# Global refinement
# ----------------------------------------------------------------------


def _grid_2x2(seed, positions=None):
    """Four 64x64 tiles overlapping by 32 px, cut from one 128x128 scene."""
    scene = _texture((128, 128), seed=seed)
    truth = [(0.0, 0.0), (0.0, 32.0), (32.0, 0.0), (32.0, 32.0)]
    return _dataset([
        (name, scene[int(row):int(row) + 64, int(col):int(col) + 64], stored)
        for name, (row, col), stored
        in zip('abcd', truth, positions or truth)
    ])


def test_refine_measures_every_overlapping_pair_not_just_consecutive():
    """A 2x2 grid offers six overlaps; a chain would only ever use three."""
    report = refine_layout(_grid_2x2(seed=11))

    assert len(report.links) == 6
    assert {(link.i, link.j) for link in report.links} == {
        (0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3),
    }
    assert all(link.accepted for link in report.links)
    assert report.moved == 0
    assert report.residual_rms() < 0.5   # sub-pixel disagreement only


def test_refine_places_a_tile_by_all_of_its_neighbours():
    """Three neighbours agree where the last tile belongs; it goes there."""
    dataset = _grid_2x2(seed=12, positions=[
        (0.0, 0.0), (0.0, 32.0), (32.0, 0.0), (32.0, 40.0),   # 8 px off
    ])

    report = refine_layout(dataset)

    assert report.moved == 1
    assert dataset.tiles[3].position[0] == pytest.approx(32.0, abs=0.5)
    assert dataset.tiles[3].position[1] == pytest.approx(32.0, abs=0.5)
    assert len(report.components) == 1


def test_refine_leaves_an_unreachable_tile_where_the_stage_said():
    """A tile nothing overlaps is not guessed at — it keeps its position."""
    scene = _texture((128, 128), seed=13)
    dataset = _dataset([
        ('a', scene[0:64, 0:64], (0.0, 0.0)),
        ('b', scene[0:64, 32:96], (0.0, 32.0)),
        ('far', _texture((64, 64), seed=14), (900.0, 900.0)),
    ])

    report = refine_layout(dataset)

    assert dataset.tiles[2].position == (900.0, 900.0)
    assert report.components == [2, 1]
    assert 'disconnected groups' in report.summary()


def test_solve_spreads_a_loop_closure_error_over_the_whole_loop():
    """The point of solving globally: no tile carries the whole discrepancy.

    Four tiles round a square, where the link closing the loop disagrees with
    the three chained ones by 2 px. Aligning each tile to its predecessor would
    dump all 2 px on the last tile; least squares shares it out.
    """
    from imswitch.imcommon.algorithms.tile_mosaic import _solve

    nominal = [(0.0, 0.0), (0.0, 100.0), (100.0, 100.0), (100.0, 0.0)]
    links = [
        TileLink(0, 1, (0.0, 100.0), 1.0, (0.0, 0.0)),
        TileLink(1, 2, (100.0, 0.0), 1.0, (0.0, 0.0)),
        TileLink(2, 3, (0.0, -100.0), 1.0, (0.0, 0.0)),
        TileLink(0, 3, (100.0, 2.0), 1.0, (0.0, 2.0)),   # closure disagrees
    ]

    solved = _solve(nominal, links)

    assert solved[0] == (0.0, 0.0)                                # anchor
    assert solved[1][1] == pytest.approx(100.5, abs=1e-6)
    assert solved[2][1] == pytest.approx(101.0, abs=1e-6)
    assert solved[3][1] == pytest.approx(1.5, abs=1e-6)
    # The consistent axis is reproduced exactly.
    assert [round(pos[0], 6) for pos in solved] == [0.0, 0.0, 100.0, 100.0]


def test_a_link_that_contradicts_the_consensus_is_dropped():
    from imswitch.imcommon.algorithms.tile_mosaic import _drop_outliers

    links = [TileLink(i, i + 1, (0.0, 0.0), 1.0, (0.0, 0.0))
             for i in range(5)]
    for link, residual in zip(links, [0.2, 0.3, 0.25, 0.4, 12.0]):
        link.residual = residual

    kept = _drop_outliers(links)

    assert len(kept) == 4
    assert links[4] not in kept
    assert links[4].accepted is False
    assert all(link.accepted for link in links[:4])


def test_ordinary_residual_scatter_is_not_mistaken_for_outliers():
    from imswitch.imcommon.algorithms.tile_mosaic import _drop_outliers

    links = [TileLink(i, i + 1, (0.0, 0.0), 1.0, (0.0, 0.0))
             for i in range(5)]
    for link, residual in zip(links, [0.2, 0.3, 0.25, 0.4, 0.5]):
        link.residual = residual

    assert _drop_outliers(links) == links
    assert all(link.accepted for link in links)


# ----------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------


def test_assemble_dataset_round_trips_a_saved_run(tmp_path):
    scene = _texture((128, 128), seed=7)
    folder = _write_dataset(tmp_path / 'run', [
        ('a.tiff', (scene[0:64, 0:64] * 65535).astype(np.uint16), (0, 0), (0, 0)),
        ('b.tiff', (scene[0:64, 32:96] * 65535).astype(np.uint16), (0, 32), (1, 0)),
    ])

    mosaic, dataset, moved = assemble_dataset(folder, refine=True)

    assert mosaic.shape == (64, 96)
    assert dataset.pixel_size_um == (0.5, 0.5)
    assert moved == 0  # positions were already right


def test_volumetric_dataset_round_trip(tmp_path):
    stack = np.ones((3, 16, 16), np.uint16)
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', stack, (0, 0), (0, 0)),
         ('b.tiff', stack * 2, (0, 16), (1, 0))],
        depth=3, z_step=0.4,
    )

    mosaic, dataset, _moved = assemble_dataset(folder, refine=False)

    assert dataset.is_volumetric is True
    assert dataset.z_step_um == 0.4
    assert mosaic.shape == (3, 16, 32)
