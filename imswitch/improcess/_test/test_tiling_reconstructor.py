"""Tests for the ImProcess tiling-mosaic reconstructor."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.algorithms.tile_mosaic import (
    MANIFEST_NAME,
    AlignmentArtifact,
    IndexedTile,
    MosaicDataset,
    MosaicLayout,
    MosaicTile,
    RefinementReport,
    TilingDatasetIndex,
)
from imswitch.improcess.reconstructors import (
    _AVAILABLE_RECONSTRUCTOR_CLASSES,
    available_reconstructor_ids,
)
from imswitch.improcess.reconstructors.tiling import TilingReconstructor


def _write_run(folder, tiles, depth=1, z_step=0.0):
    import tifffile

    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, data, (row, col) in tiles:
        tifffile.imwrite(str(folder / name), data)
        entries.append({
            'filename': name,
            'grid': [0, 0],
            'stage_um': [0.0, 0.0],
            'pixel_xy': [float(col), float(row)],
            'correction_px': [0.0, 0.0],
        })
    (folder / MANIFEST_NAME).write_text(json.dumps({
        'format': 'imswitch-tiling/1',
        'pixel_size_um': {'y': 0.25, 'x': 0.25},
        'tile_step_um': 4.0,
        'tile_shape_px': {
            'height': tiles[0][1].shape[-2],
            'width': tiles[0][1].shape[-1],
            'depth': depth,
        },
        'z_step_um': z_step,
        'tiles': entries,
    }), encoding='utf-8')
    return folder


def _params(**overrides):
    values = {
        'refine': False, 'blend': True, 'max_shift_px': None, 'project': False,
    }
    values.update(overrides)
    return values


def test_reconstructor_is_registered():
    assert 'tiling-mosaic' in available_reconstructor_ids()
    assert _AVAILABLE_RECONSTRUCTOR_CLASSES['tiling-mosaic'] is TilingReconstructor


def test_reconstructor_needs_no_metadata_dialog():
    """The geometry is in the dataset's own manifest."""
    assert TilingReconstructor().make_metadata_dialog(None) is None


def test_process_stitches_a_saved_run(tmp_path):
    folder = _write_run(tmp_path / 'tiling_1', [
        ('a.tiff', np.full((16, 16), 100, np.uint16), (0, 0)),
        ('b.tiff', np.full((16, 16), 200, np.uint16), (0, 16)),
    ])
    data_obj = SimpleNamespace(dataPath=str(folder / 'a.tiff'), name='a.tiff')

    result = TilingReconstructor().process(data_obj, _params())

    assert result.data.shape == (16, 32)
    assert result.axis_labels == ['Y', 'X']
    assert result.axis_scales == [0.25, 0.25]
    assert result.scale_unit == 'µm'
    assert result.data[0, 0] == pytest.approx(100.0)
    assert result.data[0, 31] == pytest.approx(200.0)


def test_process_can_be_pointed_at_the_manifest(tmp_path):
    folder = _write_run(
        tmp_path / 'tiling_1',
        [('a.tiff', np.ones((8, 8), np.uint16), (0, 0))],
    )
    data_obj = SimpleNamespace(dataPath=str(folder / MANIFEST_NAME))

    result = TilingReconstructor().process(data_obj, _params())

    assert result.data.shape == (8, 8)


def test_process_keeps_planes_of_a_volumetric_run(tmp_path):
    stack = np.ones((4, 8, 8), np.uint16)
    folder = _write_run(
        tmp_path / 'tiling_3d',
        [('a.tiff', stack, (0, 0)), ('b.tiff', stack * 3, (0, 8))],
        depth=4, z_step=0.6,
    )
    data_obj = SimpleNamespace(dataPath=str(folder / 'a.tiff'))

    result = TilingReconstructor().process(data_obj, _params())

    assert result.data.shape == (4, 8, 16)
    assert result.axis_labels == ['Z', 'Y', 'X']
    assert result.axis_scales == [0.6, 0.25, 0.25]


def test_process_can_project_a_volume(tmp_path):
    stack = np.zeros((4, 8, 8), np.uint16)
    stack[2] = 500
    folder = _write_run(
        tmp_path / 'tiling_3d', [('a.tiff', stack, (0, 0))],
        depth=4, z_step=0.6,
    )
    data_obj = SimpleNamespace(dataPath=str(folder / 'a.tiff'))

    result = TilingReconstructor().process(data_obj, _params(project=True))

    assert result.data.shape == (8, 8)
    assert result.axis_labels == ['Y', 'X']
    assert result.data[0, 0] == pytest.approx(500.0)


def test_process_reports_refinement_in_the_result_name(tmp_path):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(2)
    scene = gaussian_filter(rng.random((128, 128)).astype(np.float32), 2.0)
    scene = (scene * 65535).astype(np.uint16)

    folder = _write_run(tmp_path / 'tiling_1', [
        ('a.tiff', scene[0:64, 0:64], (0, 0)),
        ('b.tiff', scene[0:64, 32:96], (0, 38)),   # 6 px off
    ])
    data_obj = SimpleNamespace(dataPath=str(folder / 'a.tiff'))

    result = TilingReconstructor().process(data_obj, _params(refine=True))

    assert 'refined 1/2' in result.name


def test_process_without_a_path_explains_itself():
    data_obj = SimpleNamespace(dataPath=None, name=None)

    with pytest.raises(ValueError, match='needs a file on disk'):
        TilingReconstructor().process(data_obj, _params())


def test_process_outside_a_dataset_explains_itself(tmp_path):
    stray = tmp_path / 'lonely.tiff'
    stray.write_bytes(b'')
    data_obj = SimpleNamespace(dataPath=str(stray))

    with pytest.raises(FileNotFoundError, match='tiling dataset'):
        TilingReconstructor().process(data_obj, _params())


def test_result_saves_as_ome_tiff_with_its_calibration(tmp_path):
    import tifffile

    folder = _write_run(
        tmp_path / 'tiling_1',
        [('a.tiff', np.ones((8, 8), np.uint16), (0, 0))],
    )
    data_obj = SimpleNamespace(dataPath=str(folder / 'a.tiff'))
    result = TilingReconstructor().process(data_obj, _params())

    out = tmp_path / 'mosaic.ome.tiff'
    result.save(out)

    assert out.exists()
    xml = tifffile.tiffcomment(str(out))
    assert 'PhysicalSizeX="0.25"' in xml


def test_v2_detector_changes_reuse_alignment_only_layout(monkeypatch, tmp_path):
    """Payload intensity/selection cannot trigger a second geometry solve."""
    import importlib

    module = importlib.import_module(
        'imswitch.improcess.reconstructors.tiling.reconstructor'
    )
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text('{}', encoding='utf-8')
    alignment = tmp_path / 'alignment.tiff'
    alignment.write_bytes(b'alignment fingerprint')
    artifact = AlignmentArtifact(
        alignment, 'AlignmentCamera', 'YX', 'YX', (4, 4), (4, 4)
    )
    indexed_tile = IndexedTile(
        tile_id=0,
        grid=(0, 0),
        stage_um=(0.0, 0.0),
        saved_position_yx=(0.0, 0.0),
        alignment=artifact,
        payloads={},
    )
    index = TilingDatasetIndex(
        manifest=manifest,
        alignment_detector='AlignmentCamera',
        pixel_size_yx_um=(1.0, 1.0),
        z_step_um=0.0,
        tiles=(indexed_tile,),
        detectors=('Red', 'Green'),
        format='imswitch-tiling/2',
    )
    solves = []

    monkeypatch.setattr(module, 'inspect_dataset', lambda _path: (index, None))

    def fake_solve(
        _index,
        options,
        progress=None,
        check_cancelled=None,
        phase_progress=None,
    ):
        solves.append(options)
        return MosaicLayout(
            {0: (0.0, 0.0)}, 'stage', RefinementReport(tiles=1)
        )

    def fake_assemble(_index, _layout, selection, options):
        value = 10 if selection.detector == 'Red' else 20
        return SimpleNamespace(
            data=np.full((4, 4), value, np.float32),
            axes='YX',
            scales=(1.0, 1.0),
            origin_yx=(0, 0),
            provenance=SimpleNamespace(detector=selection.detector),
        )

    monkeypatch.setattr(module, 'solve_layout', fake_solve)
    monkeypatch.setattr(module, 'assemble_payload', fake_assemble)
    reconstructor = TilingReconstructor()
    data_obj = SimpleNamespace(dataPath=str(manifest))

    red = reconstructor.process(data_obj, _params(detector='Red'))
    green = reconstructor.process(data_obj, _params(detector='Green'))

    assert len(solves) == 1
    assert np.all(red.data == 10)
    assert np.all(green.data == 20)


def test_v2_defaults_to_full_alignment_detector_payload(tmp_path):
    import tifffile

    folder = tmp_path / 'tiling_v2'
    folder.mkdir()
    alignment = np.ones((8, 8), np.uint16)
    payload = np.full((8, 8), 19, np.uint16)
    tifffile.imwrite(str(folder / 'alignment.tiff'), alignment)
    tifffile.imwrite(str(folder / 'payload.tiff'), payload)
    manifest = folder / MANIFEST_NAME
    manifest.write_text(json.dumps({
        'format': 'imswitch-tiling/2',
        'pixel_size_um': {'y': 0.5, 'x': 0.25},
        'z_step_um': 0.0,
        'orientation': {
            'flip_x': False, 'flip_y': False, 'swap_axes': False,
        },
        'tiles': [{
            'grid': [0, 0],
            'stage_um': [0.0, 0.0],
            'pixel_xy': [0.0, 0.0],
            'alignment': {
                'detector': 'Camera',
                'filename': 'alignment.tiff',
                'axes': 'YX',
                'stored_axes': 'YX',
                'shape': [8, 8],
                'stored_shape': [8, 8],
            },
            'payloads': {
                'Camera': {
                    'path': 'payload.tiff',
                    'group': None,
                    'detector': 'Camera',
                    'axes': 'YX',
                    'stored_axes': 'YX',
                    'shape': [8, 8],
                    'stored_shape': [8, 8],
                    'generation': 1,
                    'complete': True,
                    'transform_to_alignment': 'identity',
                },
            },
        }],
    }), encoding='utf-8')
    data_obj = SimpleNamespace(dataPath=str(manifest))
    reconstructor = TilingReconstructor()

    result = reconstructor.process(data_obj, _params(refine=False))
    diagnostic = reconstructor.process(
        data_obj, _params(refine=False, alignment_diagnostic=True)
    )

    assert np.all(result.data == 19)
    assert result.provenance.detector == 'Camera'
    assert result.axis_labels == ['Y', 'X']
    assert result.axis_scales == [0.5, 0.25]
    assert np.all(diagnostic.data == 1)
    assert diagnostic.provenance.detector == '__alignment__'
    assert diagnostic.provenance.placement_path == 'alignment-diagnostic'


def test_layout_cache_key_tracks_geometry_inputs_only(tmp_path):
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text('{}', encoding='utf-8')
    alignment = tmp_path / 'alignment.tiff'
    alignment.write_bytes(b'first')
    artifact = AlignmentArtifact(
        alignment, 'Camera', 'YX', 'YX', (4, 4), (4, 4)
    )
    index = TilingDatasetIndex(
        manifest, 'Camera', (1.0, 1.0), 0.0,
        (IndexedTile(
            0, (0, 0), (0.0, 0.0), (0.0, 0.0), artifact, {},
        ),),
        ('Red', 'Green'),
        format='imswitch-tiling/2',
    )
    first = TilingReconstructor._layout_cache_key(index, {
        'stage_positions': True,
        'refine': True,
        'max_shift_px': 12,
        'detector': 'Red',
        'blend': True,
    })
    selection_changed = TilingReconstructor._layout_cache_key(index, {
        'stage_positions': True,
        'refine': True,
        'max_shift_px': 12,
        'detector': 'Green',
        'blend': False,
        'project': True,
    })
    option_changed = TilingReconstructor._layout_cache_key(index, {
        'stage_positions': False,
        'refine': True,
        'max_shift_px': 12,
    })
    alignment.write_bytes(b'a different alignment artifact')
    artifact_changed = TilingReconstructor._layout_cache_key(index, {
        'stage_positions': True,
        'refine': True,
        'max_shift_px': 12,
    })

    assert selection_changed == first
    assert option_changed != first
    assert artifact_changed != first
