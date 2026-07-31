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
    find_manifest,
    load_dataset,
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
