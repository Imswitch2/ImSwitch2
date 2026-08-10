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
    detectors_in,
    find_manifest,
    load_dataset,
    refine_layout,
    refine_positions,
)


def _texture(shape, seed=0):
    from scipy.ndimage import gaussian_filter
    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.random(shape).astype(np.float32), 2.0)


def _write_dataset(folder, tiles, extra=None, depth=1, z_step=0.0,
                   descriptor=None):
    """Write a minimal tiling dataset: tiles plus a manifest.

    ``descriptor`` adds ``axes``/``stored_axes``/``shape`` to every tile entry,
    for the descriptor-aware read path; omitting it exercises the older
    manifests that carry no such thing.
    """
    import tifffile

    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, data, (row, col), grid in tiles:
        tifffile.imwrite(str(folder / name), data)
        entry = {
            'filename': name,
            'grid': list(grid),
            'stage_um': [0.0, 0.0],
            'pixel_xy': [float(col), float(row)],
            'correction_px': [0.0, 0.0],
        }
        if descriptor:
            entry.update(descriptor)
        entries.append(entry)

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
    assert 'groups nothing could be measured across' in report.summary()


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


def _write_staged_dataset(folder, tiles, orientation, pixel_um=0.5):
    """A dataset whose saved pixel positions disagree with its stage positions."""
    import tifffile

    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, data, stage_um, pixel_xy in tiles:
        tifffile.imwrite(str(folder / name), data)
        entries.append({
            'filename': name,
            'grid': [0, 0],
            'stage_um': list(stage_um),
            'pixel_xy': list(pixel_xy),
            'correction_px': [0.0, 0.0],
        })
    (folder / MANIFEST_NAME).write_text(json.dumps({
        'format': 'imswitch-tiling/1',
        'pixel_size_um': {'y': pixel_um, 'x': pixel_um},
        'tile_step_um': 10.0,
        'tile_shape_px': {'height': 64, 'width': 64, 'depth': 1},
        'z_step_um': 0.0,
        'orientation': orientation,
        'tiles': entries,
    }), encoding='utf-8')
    return folder


@pytest.mark.parametrize('orientation,expected', [
    ({'flip_x': False, 'flip_y': False, 'swap_axes': False}, (0.0, 64.0)),
    ({'flip_x': True, 'flip_y': False, 'swap_axes': False}, (0.0, -64.0)),
    ({'flip_x': False, 'flip_y': True, 'swap_axes': False}, (0.0, 64.0)),
    ({'flip_x': False, 'flip_y': False, 'swap_axes': True}, (64.0, 0.0)),
    ({'flip_x': False, 'flip_y': True, 'swap_axes': True}, (-64.0, 0.0)),
])
def test_stage_layout_follows_the_configured_orientation(tmp_path, orientation,
                                                         expected):
    """The same transform the acquisition side applies: swap, then flip."""
    tile = np.zeros((64, 64), np.uint16)
    folder = _write_staged_dataset(tmp_path / str(id(orientation)), [
        ('a.tiff', tile, (100.0, 200.0), (0, 0)),
        # +32 um along stage X, at 0.5 um/px, is 64 px somewhere.
        ('b.tiff', tile, (132.0, 200.0), (0, 0)),
    ], orientation)

    dataset = load_dataset(folder, progress=lambda _m: None)
    moved = (dataset.tiles[1].position[0] - dataset.tiles[0].position[0],
             dataset.tiles[1].position[1] - dataset.tiles[0].position[1])

    assert moved == pytest.approx(expected)


def test_stage_layout_overrides_a_bad_saved_position(tmp_path):
    """Live registration's corrections must not survive into the offline pass.

    A live correction larger than the overlap leaves tiles that no longer meet,
    and refinement crops its correlation windows from those same positions, so
    it cannot recover on its own. The commanded stage coordinates carry no such
    history.
    """
    tile = np.zeros((64, 64), np.uint16)
    folder = _write_staged_dataset(tmp_path / 'run', [
        ('a.tiff', tile, (0.0, 0.0), (0, 0)),
        ('b.tiff', tile, (16.0, 0.0), (999, 999)),   # saved position is junk
    ], {'flip_x': False, 'flip_y': False, 'swap_axes': False})

    from_stage = load_dataset(folder, progress=lambda _m: None)
    assert from_stage.tiles[1].position == pytest.approx((0.0, 32.0))

    as_saved = load_dataset(folder, progress=lambda _m: None,
                            prefer_stage_positions=False)
    assert as_saved.tiles[1].position == pytest.approx((999.0, 999.0))


def test_layout_falls_back_when_stage_positions_say_nothing(tmp_path):
    """Older manifests put the same coordinate on every tile."""
    tile = np.zeros((64, 64), np.uint16)
    folder = _write_staged_dataset(tmp_path / 'run', [
        ('a.tiff', tile, (0.0, 0.0), (0, 0)),
        ('b.tiff', tile, (0.0, 0.0), (48, 0)),
    ], {'flip_x': False, 'flip_y': False, 'swap_axes': False})

    dataset = load_dataset(folder, progress=lambda _m: None)

    assert dataset.tiles[1].position == pytest.approx((0.0, 48.0))


# ----------------------------------------------------------------------
# N-dimensional tiles (Phase 0 acceptance)
# ----------------------------------------------------------------------


def test_assemble_places_tiles_of_any_leading_rank():
    """CZYX assembles; the mosaic keeps both leading axes."""
    left = np.arange(2 * 3 * 8 * 8, dtype=np.float32).reshape(2, 3, 8, 8)
    right = left + 1000.0

    mosaic = assemble(_dataset([
        ('a', left, (0.0, 0.0)), ('b', right, (0.0, 8.0)),
    ]))

    assert mosaic.shape == (2, 3, 8, 16)
    assert mosaic[..., :8] == pytest.approx(left)
    assert mosaic[..., 8:] == pytest.approx(right)


def test_a_length_one_channel_axis_survives_a_round_trip(tmp_path):
    """The old loader squeezed leading singletons, destroying a 1-channel axis."""
    tile = np.arange(1 * 4 * 8 * 8, dtype=np.uint16).reshape(1, 4, 8, 8)
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', tile, (0, 0), (0, 0))],
        descriptor={'axes': 'CZYX', 'stored_axes': 'CZYX',
                    'shape': [1, 4, 8, 8]},
    )

    dataset = load_dataset(folder, progress=lambda _m: None)

    assert dataset.tiles[0].data.shape == (1, 4, 8, 8)
    assert dataset.axes == 'CZYX'
    assert assemble(dataset, progress=lambda _m: None).shape == (1, 4, 8, 8)


def test_the_mosaic_is_labelled_from_the_manifest(tmp_path):
    """A line-step run's leading axis is C, and must not be called Z."""
    tile = np.zeros((3, 8, 8), np.uint16)
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', tile, (0, 0), (0, 0))],
        descriptor={'axes': 'CYX', 'stored_axes': 'CYX', 'shape': [3, 8, 8]},
    )

    dataset = load_dataset(folder, progress=lambda _m: None)

    assert dataset.axes == 'CYX'


def test_stored_axes_equal_to_axes_are_read_unchanged(tmp_path):
    """The camera case: a scan with Nz>1 is stored ZYX, with no leading T."""
    tile = np.zeros((5, 8, 8), np.uint16)
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', tile, (0, 0), (0, 0))],
        descriptor={'axes': 'ZYX', 'stored_axes': 'ZYX', 'shape': [5, 8, 8]},
    )

    dataset = load_dataset(folder, progress=lambda _m: None)

    assert dataset.tiles[0].data.shape == (5, 8, 8)
    assert dataset.axes == 'ZYX'


def test_a_stored_axis_the_logical_view_drops_is_squeezed(tmp_path):
    """The APD case: stored TCZYX, read as CZYX."""
    tile = np.zeros((1, 2, 3, 8, 8), np.uint16)
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', tile, (0, 0), (0, 0))],
        descriptor={'axes': 'CZYX', 'stored_axes': 'TCZYX',
                    'shape': [2, 3, 8, 8]},
    )

    dataset = load_dataset(folder, progress=lambda _m: None)

    assert dataset.tiles[0].data.shape == (2, 3, 8, 8)
    assert dataset.axes == 'CZYX'


def test_squeezing_an_axis_that_holds_data_is_refused(tmp_path):
    """Dropping a length-4 axis would discard three quarters of the tile."""
    tile = np.zeros((4, 3, 8, 8), np.uint16)
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', tile, (0, 0), (0, 0))],
        descriptor={'axes': 'ZYX', 'stored_axes': 'TZYX', 'shape': [3, 8, 8]},
    )

    # Loud, not skipped: skipping would assemble a mosaic that looks fine and
    # is quietly missing this tile's data.
    with pytest.raises(ValueError, match='would discard data'):
        load_dataset(folder, progress=lambda _m: None)


def test_a_manifest_without_descriptors_reads_exactly_as_before(tmp_path):
    """Back-compat: the old shape, the old squeeze, the old axis names."""
    scene = _texture((128, 128), seed=21)
    folder = _write_dataset(tmp_path / 'run', [
        ('a.tiff', (scene[0:64, 0:64] * 65535).astype(np.uint16), (0, 0), (0, 0)),
        ('b.tiff', (scene[0:64, 32:96] * 65535).astype(np.uint16), (0, 32), (1, 0)),
    ])

    dataset = load_dataset(folder, progress=lambda _m: None)
    mosaic = assemble(dataset, progress=lambda _m: None)

    assert dataset.axes == 'YX'
    assert mosaic.shape == (64, 96)
    assert dataset.tiles[0].data.ndim == 2


def test_a_flat_tile_among_stacks_contributes_to_every_plane():
    """Rank need not be uniform; a short tile is broadcast, not dropped."""
    stack = np.full((3, 8, 8), 4.0, np.float32)
    flat = np.full((8, 8), 10.0, np.float32)

    mosaic = assemble(_dataset([
        ('a', stack, (0.0, 0.0)), ('b', flat, (0.0, 8.0)),
    ]), blend=False)

    assert mosaic.shape == (3, 8, 16)
    assert mosaic[:, :, 8:] == pytest.approx(10.0)


# ----------------------------------------------------------------------
# Multi-detector datasets (Phase 1)
# ----------------------------------------------------------------------


def _write_multidetector(folder, detectors, positions):
    """A run that saved several detectors at each stage position."""
    import tifffile

    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, (row, col) in enumerate(positions):
        files = {}
        for name, (data, axes) in detectors.items():
            filename = f'tile{index}_{name}.tiff'
            tifffile.imwrite(str(folder / filename), data)
            files[name] = {
                'filename': filename, 'axes': axes, 'stored_axes': axes,
                'shape': list(data.shape),
                'transform_to_alignment': (
                    'reference' if name == next(iter(detectors)) else 'identity'
                ),
            }
        reference = next(iter(detectors))
        entries.append({
            'filename': files[reference]['filename'],
            'grid': [index, 0],
            'stage_um': [float(col), float(row)],
            'pixel_xy': [float(col), float(row)],
            'correction_px': [0.0, 0.0],
            'alignment': dict(files[reference], detector=reference),
            'payloads': {
                name: dict(descriptor, path=descriptor['filename'],
                           group=None, complete=True)
                for name, descriptor in files.items()
            },
        })

    (folder / MANIFEST_NAME).write_text(json.dumps({
        'format': 'imswitch-tiling/2',
        'pixel_size_um': {'y': 1.0, 'x': 1.0},
        'tile_step_um': 8.0,
        'tile_shape_px': {'height': 16, 'width': 16, 'depth': 1},
        'z_step_um': 0.0,
        'orientation': {'flip_x': False, 'flip_y': False, 'swap_axes': False},
        'tiles': entries,
    }), encoding='utf-8')
    return folder


def test_a_run_lists_the_detectors_it_saved(tmp_path):
    folder = _write_multidetector(tmp_path / 'run', {
        'APDred': (np.zeros((2, 16, 16), np.uint16), 'CYX'),
        'Camera': (np.zeros((16, 16), np.uint16), 'YX'),
    }, [(0, 0), (0, 8)])

    payload = json.loads((folder / MANIFEST_NAME).read_text())

    assert detectors_in(payload) == ['APDred', 'Camera']


def test_each_detector_loads_its_own_files_and_axes(tmp_path):
    folder = _write_multidetector(tmp_path / 'run', {
        'APDred': (np.full((2, 16, 16), 7, np.uint16), 'CYX'),
        'Camera': (np.full((16, 16), 9, np.uint16), 'YX'),
    }, [(0, 0), (0, 8)])

    apd = load_dataset(folder, progress=lambda _m: None, detector='APDred')
    camera = load_dataset(folder, progress=lambda _m: None, detector='Camera')

    assert apd.axes == 'CYX'
    assert apd.tiles[0].data.shape == (2, 16, 16)
    assert camera.axes == 'YX'
    assert camera.tiles[0].data.shape == (16, 16)
    # One run, one geometry: the layouts must agree.
    assert [t.position for t in apd.tiles] == [t.position for t in camera.tiles]


def test_detectors_share_one_layout_so_mosaics_line_up(tmp_path):
    folder = _write_multidetector(tmp_path / 'run', {
        'APDred': (np.full((16, 16), 3, np.uint16), 'YX'),
        'Camera': (np.full((16, 16), 5, np.uint16), 'YX'),
    }, [(0, 0), (0, 8)])

    shapes = {
        name: assemble(
            load_dataset(folder, progress=lambda _m: None, detector=name),
            progress=lambda _m: None,
        ).shape
        for name in ('APDred', 'Camera')
    }

    assert shapes['APDred'] == shapes['Camera'] == (16, 24)


def test_the_aligned_on_detector_loads_without_naming_it(tmp_path):
    """A reader that knows nothing of save sets still finds the tile."""
    folder = _write_multidetector(tmp_path / 'run', {
        'APDred': (np.full((16, 16), 3, np.uint16), 'YX'),
        'Camera': (np.full((16, 16), 5, np.uint16), 'YX'),
    }, [(0, 0), (0, 8)])

    dataset = load_dataset(folder, progress=lambda _m: None)

    assert dataset.tiles[0].data[0, 0] == 3      # the reference detector


def test_asking_for_a_detector_the_run_did_not_save_is_not_substituted(tmp_path):
    """Better to fail than to quietly assemble a different detector."""
    folder = _write_multidetector(tmp_path / 'run', {
        'APDred': (np.zeros((16, 16), np.uint16), 'YX'),
    }, [(0, 0), (0, 8)])

    with pytest.raises(ValueError, match='No readable tiles'):
        load_dataset(folder, progress=lambda _m: None, detector='Nonexistent')


def test_blending_credits_only_the_planes_a_tile_reached():
    """A short tile must not darken the planes it never contributed to.

    The weights count contributors. Counting them per Y/X pixel only, while
    summing pixels per plane, lets a one-plane tile claim credit on every
    plane of the stack it overlaps — and the division then halves the planes
    it never touched.
    """
    stack = np.full((3, 8, 8), 10.0, np.float32)
    short = np.full((1, 8, 8), 10.0, np.float32)

    mosaic = assemble(_dataset([
        ('a', stack, (0.0, 0.0)), ('b', short, (0.0, 4.0)),
    ]), blend=True)

    assert mosaic.shape == (3, 8, 12)
    assert mosaic[0, :, 4:8] == pytest.approx(10.0)   # both contributed
    assert mosaic[1, :, 4:8] == pytest.approx(10.0)   # only the stack did
    assert mosaic[2, :, 4:8] == pytest.approx(10.0)


def test_blending_is_unchanged_when_every_tile_covers_the_same_planes():
    """The ordinary case keeps the cheap plane-blind weights."""
    left = np.full((2, 8, 8), 4.0, np.float32)
    right = np.full((2, 8, 8), 8.0, np.float32)

    mosaic = assemble(_dataset([
        ('a', left, (0.0, 0.0)), ('b', right, (0.0, 4.0)),
    ]), blend=True)

    assert mosaic[:, :, 4:8] == pytest.approx(6.0)    # (4 + 8) / 2


def test_a_descriptor_contradicting_its_file_fails_the_load(tmp_path):
    """Skipping it would assemble a plausible mosaic that is missing data."""
    good = np.zeros((8, 8), np.uint16)
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', good, (0, 0), (0, 0)), ('b.tiff', good, (0, 4), (1, 0))],
        descriptor={'axes': 'YX', 'stored_axes': 'YX', 'shape': [16, 16]},
    )

    with pytest.raises(ValueError, match='declares shape'):
        load_dataset(folder, progress=lambda _m: None)


def test_a_container_that_adds_a_leading_axis_is_reconciled(tmp_path):
    """HDF5 gives every 2-D image a frame axis; the manifest cannot know that.

    So the stored rank comes from the file in hand, not from the manifest, and
    a declared ``YX`` still loads from a ``(1, Y, X)`` array.
    """
    stored = np.zeros((1, 8, 8), np.uint16)          # as a container wrote it
    folder = _write_dataset(
        tmp_path / 'run',
        [('a.tiff', stored, (0, 0), (0, 0))],
        descriptor={'axes': 'YX', 'shape': [8, 8]},
    )

    dataset = load_dataset(folder, progress=lambda _m: None)

    assert dataset.tiles[0].data.shape == (8, 8)
    assert dataset.axes == 'YX'


def _vignette(shape, strength=0.6):
    """Radial falloff fixed to the camera, so identical in every tile."""
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]].astype(np.float32)
    cy, cx = (shape[0] - 1) / 2, (shape[1] - 1) / 2
    radius = ((yy - cy) / cy) ** 2 + ((xx - cx) / cx) ** 2
    return (1.0 - strength * radius / 2).clip(0.05, 1.0)


def test_shading_does_not_cost_the_link():
    """Vignetting anti-correlates two tiles' shared edges; ignore it.

    Where tiles meet, one shows the falloff at its right edge and the other the
    rise toward its centre. Those ramp opposite ways and correlate at about -1,
    which scored as a hopeless match and threw away links whose sample
    structure had in fact aligned. Losing links fragments the mosaic.
    """
    scene = _texture((128, 128), seed=16)
    shading = _vignette((64, 64))
    left = (scene[0:64, 0:64] + 0.15) * shading
    right = (scene[0:64, 32:96] + 0.15) * shading

    report = refine_layout(_dataset([
        ('a', left, (0.0, 0.0)), ('b', right, (0.0, 37.0)),
    ]))

    assert len(report.links) == 1, 'shading must not cost the link'
    assert report.links[0].confidence > 0.5


def test_every_stage_reports_progress(tmp_path):
    """A long run must be distinguishable from a hung one."""
    scene = _texture((128, 128), seed=15)
    folder = _write_dataset(tmp_path / 'run', [
        ('a.tiff', (scene[0:64, 0:64] * 65535).astype(np.uint16), (0, 0), (0, 0)),
        ('b.tiff', (scene[0:64, 32:96] * 65535).astype(np.uint16), (0, 32), (1, 0)),
    ])

    said = []
    dataset = load_dataset(folder, progress=said.append)
    refine_layout(dataset, progress=said.append)
    assemble(dataset, progress=said.append)

    joined = '\n'.join(said)
    assert 'Reading 2 tiles' in joined
    assert 'Correlating 1 overlapping tile pairs' in joined
    assert 'Tiling refinement:' in joined
    assert 'Assembling 2 tiles into a 64x96 mosaic' in joined


def test_assembly_reports_its_memory_before_allocating_it():
    """The operator sees what a large mosaic will cost before it is asked for."""
    said = []
    assemble(_dataset([
        ('a', np.zeros((1000, 1000), np.float32), (0.0, 0.0)),
        ('b', np.zeros((1000, 1000), np.float32), (0.0, 900.0)),
    ]), progress=said.append)

    # 1000 x 1900 float32 mosaic + uint16 weights = 7.6 MB + 3.8 MB.
    assert any('1000x1900 mosaic (0.01 GB)' in message for message in said)


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
