"""Phase 2c: selected payload assembly and detector geometry composition."""

from pathlib import Path

import numpy as np
import pytest

from imswitch.imcommon.algorithms.detector_transform import (
    parse_detector_transform,
)
from imswitch.imcommon.algorithms.tile_mosaic import (
    AlignmentArtifact,
    IndexedTile,
    ManifestPayloadRef,
    MosaicDataset,
    MosaicLayout,
    MosaicTile,
    PayloadAssemblyOptions,
    PayloadSelection,
    RefinementReport,
    TilingDatasetIndex,
    assemble,
    assemble_payload,
)


def _write_tiff(path: Path, data: np.ndarray) -> None:
    import tifffile

    tifffile.imwrite(str(path), data, photometric='minisblack')


def _ref(path, detector, data, axes, *, complete=True, transform='identity'):
    return ManifestPayloadRef(
        path=path,
        group=None,
        detector=detector,
        axes=axes,
        stored_axes=axes,
        shape=tuple(data.shape),
        stored_shape=tuple(data.shape),
        generation=7,
        complete=complete,
        transform_to_alignment=parse_detector_transform(transform),
    )


def _index(tmp_path, detector, entries):
    manifest = tmp_path / 'tiles.json'
    manifest.write_text('{}', encoding='utf-8')
    tiles = []
    for tile_id, position, ref in entries:
        alignment = AlignmentArtifact(
            path=tmp_path / f'alignment_{tile_id}.tiff',
            detector='AlignmentCamera',
            axes='YX',
            stored_axes='YX',
            shape=(8, 8),
            stored_shape=(8, 8),
        )
        tiles.append(IndexedTile(
            tile_id=tile_id,
            grid=(tile_id, 0),
            stage_um=(float(position[1]), float(position[0])),
            saved_position_yx=position,
            alignment=alignment,
            payloads={} if ref is None else {detector: ref},
        ))
    return TilingDatasetIndex(
        manifest=manifest,
        alignment_detector='AlignmentCamera',
        pixel_size_yx_um=(0.5, 0.25),
        z_step_um=0.75,
        tiles=tuple(tiles),
        detectors=(detector,),
        format='imswitch-tiling/2',
    )


def _layout(entries):
    return MosaicLayout(
        {tile_id: position for tile_id, position, _ref in entries},
        'stage',
        RefinementReport(tiles=len(entries)),
    )


def test_identity_payload_matches_existing_assembly_and_selects_named_axes(
    tmp_path,
):
    detector = 'APDred'
    first = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    second = first + 1000
    first_path, second_path = tmp_path / 'first.tiff', tmp_path / 'second.tiff'
    _write_tiff(first_path, first)
    _write_tiff(second_path, second)
    entries = [
        (11, (0.0, 0.0), _ref(first_path, detector, first, 'CZYX')),
        (29, (0.0, 5.0), _ref(second_path, detector, second, 'CZYX')),
    ]
    index, layout = _index(tmp_path, detector, entries), _layout(entries)

    result = assemble_payload(
        index, layout, PayloadSelection(detector),
        PayloadAssemblyOptions(layout_cache_key='layout-17'),
    )
    compatibility = assemble(MosaicDataset(tiles=[
        MosaicTile('first', first, (0.0, 0.0), axes='CZYX'),
        MosaicTile('second', second, (0.0, 5.0), axes='CZYX'),
    ]))

    assert result.axes == 'CZYX'
    assert result.scales == (1.0, 0.75, 0.5, 0.25)
    assert result.origin_yx == (0, 0)
    assert result.provenance.placement_path == 'identity-integer'
    assert result.provenance.layout_cache_key == 'layout-17'
    assert result.data.dtype == compatibility.dtype == np.float32
    np.testing.assert_array_equal(result.data, compatibility)

    channel = assemble_payload(
        index, layout, PayloadSelection(detector, channel=1)
    )
    projected = assemble_payload(
        index, layout, PayloadSelection(detector, z_projection='max')
    )
    both = assemble_payload(
        index, layout,
        PayloadSelection(detector, channel=0, z_projection='max'),
    )

    assert channel.axes == 'ZYX'
    np.testing.assert_array_equal(channel.data[:, :, :5], first[1])
    assert projected.axes == 'CYX'  # Z only; C survives.
    np.testing.assert_array_equal(projected.data[:, :, :5], first.max(axis=1))
    assert both.axes == 'YX'
    np.testing.assert_array_equal(both.data[:, :5], first[0].max(axis=0))


def test_only_absent_or_incomplete_payloads_are_skipped(tmp_path):
    detector = 'Camera'
    data = np.full((4, 4), 17, np.uint16)
    path = tmp_path / 'complete.tiff'
    _write_tiff(path, data)
    entries = [
        (3, (0.0, 0.0), _ref(path, detector, data, 'YX')),
        (8, (0.0, 4.0), None),
        (21, (0.0, 8.0), _ref(
            tmp_path / 'not_written.tiff', detector, data, 'YX', complete=False,
        )),
    ]
    index, layout = _index(tmp_path, detector, entries), _layout(entries)

    result = assemble_payload(index, layout, PayloadSelection(detector))

    assert result.data.shape == (4, 4)
    assert [(item.tile_id, item.reason) for item in result.provenance.skipped] == [
        (8, 'absent'), (21, 'incomplete'),
    ]


def test_all_preserves_a_length_one_channel_axis(tmp_path):
    detector = 'APDred'
    data = np.arange(1 * 2 * 4 * 4, dtype=np.uint16).reshape(1, 2, 4, 4)
    path = tmp_path / 'one_channel.tiff'
    _write_tiff(path, data)
    entries = [(1, (0.0, 0.0), _ref(path, detector, data, 'CZYX'))]
    index, layout = _index(tmp_path, detector, entries), _layout(entries)

    all_channels = assemble_payload(
        index, layout, PayloadSelection(detector)
    )
    selected = assemble_payload(
        index, layout, PayloadSelection(detector, channel=0)
    )

    assert all_channels.axes == 'CZYX'
    assert all_channels.data.shape == (1, 2, 4, 4)
    assert selected.axes == 'ZYX'
    assert selected.data.shape == (2, 4, 4)


def test_complete_missing_payload_is_a_hard_error(tmp_path):
    detector = 'Camera'
    data = np.ones((4, 4), np.uint16)
    entries = [(5, (0.0, 0.0), _ref(
        tmp_path / 'missing.tiff', detector, data, 'YX', complete=True,
    ))]
    index, layout = _index(tmp_path, detector, entries), _layout(entries)

    with pytest.raises(FileNotFoundError, match='declared file does not exist'):
        assemble_payload(index, layout, PayloadSelection(detector))


def test_no_complete_payload_names_the_detector_and_counts(tmp_path):
    detector = 'Camera'
    data = np.ones((4, 4), np.uint16)
    entries = [
        (1, (0.0, 0.0), None),
        (2, (0.0, 4.0), _ref(
            tmp_path / 'missing.tiff', detector, data, 'YX', complete=False,
        )),
    ]
    index, layout = _index(tmp_path, detector, entries), _layout(entries)

    with pytest.raises(ValueError, match="'Camera'.*1/2 declared, 0/2 complete"):
        assemble_payload(index, layout, PayloadSelection(detector))


def test_affine_uses_pixel_centres_outward_bounds_and_every_leading_plane(
    tmp_path,
):
    detector = 'Camera'
    data = np.asarray([
        [[1, 2, 3], [4, 5, 6]],
        [[11, 12, 13], [14, 15, 16]],
    ], dtype=np.float32)
    path = tmp_path / 'rotated.tiff'
    _write_tiff(path, data)
    # (row, col) -> (2 - col, row): a clockwise 90-degree rotation around
    # pixel centres. The 2x3 footprint becomes exactly 3x2 at origin (0, 0).
    transform = {
        'kind': 'affine',
        'matrix': [[0, -1, 2], [1, 0, 0], [0, 0, 1]],
        'calibration_id': 'rotate-90',
    }
    entries = [(17, (0.0, 0.0), _ref(
        path, detector, data, 'CYX', transform=transform,
    ))]
    index, layout = _index(tmp_path, detector, entries), _layout(entries)

    result = assemble_payload(index, layout, PayloadSelection(detector))

    assert result.origin_yx == (0, 0)
    assert result.data.shape == (2, 3, 2)
    assert result.axes == 'CYX'
    assert result.provenance.placement_path == 'affine'
    assert result.provenance.transform['calibration_id'] == 'rotate-90'
    np.testing.assert_allclose(result.data[0], [[3, 6], [2, 5], [1, 4]])
    np.testing.assert_allclose(result.data[1], [[13, 16], [12, 15], [11, 14]])


def test_transform_override_is_applied_and_recorded(tmp_path):
    detector = 'Camera'
    data = np.arange(6, dtype=np.float32).reshape(2, 3)
    path = tmp_path / 'override.tiff'
    _write_tiff(path, data)
    entries = [(1, (0.0, 0.0), _ref(path, detector, data, 'YX'))]
    index, layout = _index(tmp_path, detector, entries), _layout(entries)

    result = assemble_payload(
        index,
        layout,
        PayloadSelection(detector),
        PayloadAssemblyOptions(transform_resolver=lambda *_args: {
            'kind': 'affine',
            'matrix': [[0, -1, 2], [1, 0, 0], [0, 0, 1]],
            'calibration_id': 'new-calibration',
        }),
    )

    assert result.provenance.transform_source == 'override'
    assert result.provenance.transform['calibration_id'] == 'new-calibration'
    np.testing.assert_allclose(result.data, [[2, 5], [1, 4], [0, 3]])


def test_warped_empty_corners_do_not_darken_an_overlapping_tile(tmp_path):
    detector = 'Camera'
    first = np.full((8, 8), 10, np.float32)
    second = np.full((8, 8), 100, np.float32)
    first_path, second_path = tmp_path / 'first.tiff', tmp_path / 'second.tiff'
    _write_tiff(first_path, first)
    _write_tiff(second_path, second)
    angle = np.deg2rad(30.0)
    cosine, sine = float(np.cos(angle)), float(np.sin(angle))
    transform = {
        'kind': 'affine',
        'matrix': [
            [cosine, -sine, 0], [sine, cosine, 0], [0, 0, 1],
        ],
    }
    first_entry = (1, (0.0, 0.0), _ref(
        first_path, detector, first, 'YX', transform=transform,
    ))
    second_entry = (2, (0.0, 4.0), _ref(
        second_path, detector, second, 'YX', transform=transform,
    ))

    def run(entries):
        return assemble_payload(
            _index(tmp_path, detector, entries),
            _layout(entries),
            PayloadSelection(detector),
        )

    first_only = run([first_entry])
    second_only = run([second_entry])
    combined = run([first_entry, second_entry])
    checked = 0
    for row in range(combined.data.shape[0]):
        for col in range(combined.data.shape[1]):
            global_row = combined.origin_yx[0] + row
            global_col = combined.origin_yx[1] + col
            first_index = (
                global_row - first_only.origin_yx[0],
                global_col - first_only.origin_yx[1],
            )
            second_index = (
                global_row - second_only.origin_yx[0],
                global_col - second_only.origin_yx[1],
            )
            inside_first_bounds = (
                0 <= first_index[0] < first_only.data.shape[0]
                and 0 <= first_index[1] < first_only.data.shape[1]
            )
            inside_second = (
                0 <= second_index[0] < second_only.data.shape[0]
                and 0 <= second_index[1] < second_only.data.shape[1]
            )
            if not (inside_first_bounds and inside_second):
                continue
            first_value = first_only.data[first_index]
            second_value = second_only.data[second_index]
            if first_value == 0 and second_value > 0:
                # The first tile's rotated bounding box covers this coordinate,
                # but its transformed validity mask does not. It contributes
                # neither zero intensity nor weight to the second tile.
                assert combined.data[row, col] == pytest.approx(second_value)
                checked += 1
    assert checked > 0


def test_payload_axes_must_agree_before_mosaic_allocation(tmp_path):
    detector = 'Camera'
    first = np.ones((2, 4, 4), np.uint16)
    second = np.ones((2, 4, 4), np.uint16)
    first_path, second_path = tmp_path / 'cyx.tiff', tmp_path / 'tyx.tiff'
    _write_tiff(first_path, first)
    _write_tiff(second_path, second)
    entries = [
        (1, (0.0, 0.0), _ref(first_path, detector, first, 'CYX')),
        (2, (0.0, 4.0), _ref(second_path, detector, second, 'TYX')),
    ]
    index, layout = _index(tmp_path, detector, entries), _layout(entries)

    with pytest.raises(ValueError, match='axes differ between tiles'):
        assemble_payload(index, layout, PayloadSelection(detector))
