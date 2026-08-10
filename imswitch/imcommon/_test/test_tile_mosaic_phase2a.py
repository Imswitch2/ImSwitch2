"""Phase 2a contracts for tiling manifest indexing and exact payload reads."""

import json
from pathlib import Path

import numpy as np
import pytest

from imswitch.imcommon.algorithms.detector_transform import (
    parse_detector_transform,
)
from imswitch.imcommon.algorithms.tile_mosaic import (
    MANIFEST_NAME,
    LocatorReadError,
    ManifestValidationError,
    inspect_dataset,
    inspect_manifest_payload,
    load_dataset,
    read_manifest_payload,
)


def _write_alignment(folder: Path, index: int) -> str:
    import tifffile

    name = f'alignment_{index}.tiff'
    tifffile.imwrite(str(folder / name), np.full((4, 4), index + 1, np.uint16))
    return name


def _payload_ref(path, group, shape, *, stored_axes='YX', axes='YX',
                 complete=True, transform='identity'):
    return {
        'path': str(path),
        'group': group,
        'detector': 'Camera',
        'axes': axes,
        'stored_axes': stored_axes,
        'shape': list(shape),
        'generation': 7,
        'complete': complete,
        'transform_to_alignment': transform,
    }


def _write_v2(folder: Path, refs) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, ref in enumerate(refs):
        alignment = _write_alignment(folder, index)
        entries.append({
            'filename': alignment,
            'grid': [index, 0],
            'stage_um': [float(index * 4), 0.0],
            'pixel_xy': [float(index * 4), 0.0],
            'alignment': {
                'detector': 'Camera',
                'filename': alignment,
                'axes': 'YX',
                'stored_axes': 'YX',
                'shape': [4, 4],
                'stored_shape': [4, 4],
            },
            'payloads': {'Camera': ref},
        })
    manifest = folder / MANIFEST_NAME
    manifest.write_text(json.dumps({
        'format': 'imswitch-tiling/2',
        'pixel_size_um': {'y': 1.0, 'x': 1.0},
        'tile_step_um': 4.0,
        'z_step_um': 0.0,
        'orientation': {
            'flip_x': False, 'flip_y': False, 'swap_axes': False,
        },
        'tiles': entries,
    }), encoding='utf-8')
    return manifest


@pytest.mark.parametrize('kind', ['hdf5', 'zarr'])
def test_exact_group_selects_each_array_in_one_container(tmp_path, kind):
    first = np.full((4, 4), 11, np.uint16)
    second = np.full((4, 4), 22, np.uint16)
    if kind == 'hdf5':
        import h5py

        payload_path = tmp_path / 'run' / 'payloads.h5'
        payload_path.parent.mkdir(parents=True)
        with h5py.File(payload_path, 'w') as handle:
            handle.create_dataset('scan0/Camera/data', data=first)
            handle.create_dataset('scan1/Camera/data', data=second)
    else:
        import zarr

        payload_path = tmp_path / 'run' / 'payloads.zarr'
        root = zarr.open_group(str(payload_path), mode='w')
        for scan, data in (('scan0', first), ('scan1', second)):
            group = root.require_group(scan).require_group('Camera')
            if hasattr(group, 'create_array'):
                group.create_array('data', data=data)
            else:
                group.create_dataset('data', data=data, shape=data.shape)

    manifest = _write_v2(payload_path.parent, [
        _payload_ref(payload_path.name, 'scan0/Camera', first.shape),
        _payload_ref(payload_path.name, 'scan1/Camera', second.shape),
    ])
    index, _ = inspect_dataset(manifest)

    np.testing.assert_array_equal(
        read_manifest_payload(index.tiles[0].payloads['Camera']), first
    )
    np.testing.assert_array_equal(
        read_manifest_payload(index.tiles[1].payloads['Camera']), second
    )

    compatibility = load_dataset(
        manifest, detector='Camera', progress=lambda _message: None,
    )
    assert [int(tile.data[0, 0]) for tile in compatibility.tiles] == [11, 22]


def test_index_keeps_manifest_ordinals_when_a_file_is_missing(tmp_path):
    folder = tmp_path / 'run'
    manifest = _write_v2(folder, [
        _payload_ref('payload_0.tiff', None, (4, 4)),
        _payload_ref('payload_1.tiff', None, (4, 4)),
    ])
    # Only the second payload exists; identity still comes from manifest order.
    import tifffile
    tifffile.imwrite(str(folder / 'payload_1.tiff'), np.ones((4, 4), np.uint16))

    index, summary = inspect_dataset(manifest)

    assert [tile.tile_id for tile in index.tiles] == [0, 1]
    assert summary.total_tiles == 2
    assert summary.payloads_declared == {'Camera': 2}
    assert folder / 'payload_0.tiff' in summary.missing_paths


def test_tiff_payload_rejects_a_non_null_group(tmp_path):
    import tifffile

    folder = tmp_path / 'run'
    folder.mkdir()
    tifffile.imwrite(str(folder / 'payload.tiff'), np.ones((4, 4), np.uint16))
    manifest = _write_v2(folder, [
        _payload_ref('payload.tiff', 'Camera', (4, 4)),
    ])
    index, _ = inspect_dataset(manifest)

    with pytest.raises(LocatorReadError, match='requires group=null'):
        read_manifest_payload(index.tiles[0].payloads['Camera'])


@pytest.mark.parametrize('path', ['../outside.h5', '/tmp/outside.h5'])
def test_v2_payload_path_cannot_escape_the_run(tmp_path, path):
    manifest = _write_v2(tmp_path / 'run', [
        _payload_ref(path, 'Camera', (4, 4)),
    ])

    with pytest.raises(ManifestValidationError, match='run folder|traversal'):
        inspect_dataset(manifest)


def test_declared_axes_remove_only_the_named_singleton_wrapper(tmp_path):
    import h5py

    folder = tmp_path / 'run'
    folder.mkdir()
    payload_path = folder / 'payload.h5'
    stored = np.arange(20, dtype=np.uint16).reshape(1, 1, 1, 4, 5)
    with h5py.File(payload_path, 'w') as handle:
        handle.create_dataset('scan0/Camera/data', data=stored)

    ref = _payload_ref(
        payload_path.name,
        'scan0/Camera',
        (1, 1, 4, 5),
        stored_axes='TCZYX',
        axes='CZYX',
    )
    ref['stored_shape'] = list(stored.shape)
    manifest = _write_v2(folder, [ref])
    index, _ = inspect_dataset(manifest)
    parsed = index.tiles[0].payloads['Camera']

    header = inspect_manifest_payload(parsed)
    data = read_manifest_payload(parsed)

    assert header.squeeze_axes == (0,)
    assert data.shape == (1, 1, 4, 5)  # length-one C and Z both survive
    np.testing.assert_array_equal(data, stored[0])


def test_complete_missing_group_is_corruption_not_discovery(tmp_path):
    import h5py

    folder = tmp_path / 'run'
    folder.mkdir()
    payload_path = folder / 'payload.h5'
    with h5py.File(payload_path, 'w') as handle:
        handle.create_dataset('some_other_group/data', data=np.ones((4, 4)))
    manifest = _write_v2(folder, [
        _payload_ref(payload_path.name, 'scan0/Camera', (4, 4)),
    ])
    index, _ = inspect_dataset(manifest)

    with pytest.raises(LocatorReadError, match='does not exist'):
        read_manifest_payload(index.tiles[0].payloads['Camera'])
    with pytest.raises(LocatorReadError, match='does not exist'):
        load_dataset(manifest, detector='Camera', progress=lambda _message: None)


def test_transform_schema_is_normalized_and_keeps_provenance(tmp_path):
    transform = {
        'kind': 'affine',
        'matrix': [[1, 0, 2.5], [0, 1, -1.0], [0, 0, 1]],
        'source': 'transform-module',
        'calibration_id': 'cal-17',
        'measured': '2026-08-04T11:03:00Z',
    }
    manifest = _write_v2(tmp_path / 'run', [
        _payload_ref('payload.tiff', None, (4, 4), transform=transform),
    ])

    index, _ = inspect_dataset(manifest)
    parsed = index.tiles[0].payloads['Camera'].transform_to_alignment

    assert parsed.kind == 'affine'
    assert parsed.calibration_id == 'cal-17'
    np.testing.assert_array_equal(
        parsed.as_array(), np.asarray(transform['matrix'], dtype=np.float64)
    )


@pytest.mark.parametrize('value, message', [
    ('reference', 'alignment detector'),
    ({'kind': 'affine', 'matrix': [[1, 0, 0], [0, 0, 0], [0, 0, 1]]},
     'singular'),
    ({'kind': 'warp'}, 'unknown transform kind'),
])
def test_transform_parser_rejects_ambiguous_or_invalid_values(value, message):
    with pytest.raises(ValueError, match=message):
        parse_detector_transform(value)
