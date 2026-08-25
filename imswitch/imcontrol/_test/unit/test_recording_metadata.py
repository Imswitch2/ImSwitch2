"""Phase 1 unit tests for the shared OME recording-metadata model."""
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SCAN, MODE_SCAN_LAPSE, MODE_SNAP, MODE_TIMELAPSE,
    OmeImageMeta, axes_for_recording, build_ome_image_meta, normalize_mode,
)


# --- axis-labeling matrix (the decision the user signed off on) ---------------

@pytest.mark.parametrize('mode, n_frames, scan_dims, expected', [
    (MODE_SNAP,       1,  None,         ['y', 'x']),
    (MODE_SNAP,       4,  None,         ['t', 'y', 'x']),
    (MODE_SNAP,       1,  (64, 64, 0),  ['y', 'x']),
    (MODE_TIMELAPSE,  10, None,         ['t', 'y', 'x']),
    (MODE_TIMELAPSE,  1,  None,         ['y', 'x']),          # single frame collapses
    (MODE_SCAN,       50, (64, 64, 50), ['z', 'y', 'x']),     # Z/slow axis active
    (MODE_SCAN,       100, (100, 100, 1), ['t', 'y', 'x']),   # single plane, many exposures
    (MODE_SCAN,       1,  (100, 100, 1), ['y', 'x']),         # single reconstructed frame (APD)
    (MODE_SCAN_LAPSE, 30, (64, 64, 10), ['z', 'y', 'x']),
    (MODE_SCAN_LAPSE, 5,  (64, 64, 1),  ['t', 'y', 'x']),
])
def test_axes_for_recording(mode, n_frames, scan_dims, expected):
    assert axes_for_recording(mode, n_frames, scan_dims) == expected


def test_normalize_mode():
    assert normalize_mode('ScanOnce') == MODE_SCAN
    assert normalize_mode('ScanLapse') == MODE_SCAN_LAPSE
    assert normalize_mode('SpecFrames') == MODE_TIMELAPSE
    assert normalize_mode('SpecTime') == MODE_TIMELAPSE
    assert normalize_mode('CameraLapse') == MODE_TIMELAPSE
    assert normalize_mode('UntilStop') == MODE_TIMELAPSE
    assert normalize_mode(None, is_snap=True) == MODE_SNAP
    assert normalize_mode('Anything', is_snap=True) == MODE_SNAP


# --- builder + scale alignment ------------------------------------------------

def test_build_scan_zstack_scale_order():
    m = build_ome_image_meta(
        'APD', MODE_SCAN, n_frames=50, scan_dims=(64, 64, 50),
        pixel_size_yx_um=(0.2, 0.1), z_step_um=0.5, dtype=np.uint16)
    assert m.axes_string == 'ZYX'
    # scale follows axis order: z, y, x
    assert m.scale == [0.5, 0.2, 0.1]
    assert [a.type for a in m.axes] == ['space', 'space', 'space']


def test_build_timelapse_scale():
    m = build_ome_image_meta(
        'Cam', MODE_TIMELAPSE, n_frames=10,
        pixel_size_yx_um=(0.15, 0.15), t_interval_s=0.05)
    assert m.axes_string == 'TYX'
    assert m.scale == [0.05, 0.15, 0.15]
    assert m.axes[0].type == 'time' and m.axes[0].unit == 's'


def test_axes_scale_length_mismatch_raises():
    from imswitch.imcontrol.model.managers.recording_metadata import OmeAxis
    with pytest.raises(ValueError):
        OmeImageMeta(name='x', axes=[OmeAxis('y', 'space', 'µm')], scale=[1.0, 2.0])


# --- serializers --------------------------------------------------------------

def test_tiff_metadata_zstack():
    m = build_ome_image_meta('Cam', MODE_SCAN, 50, scan_dims=(64, 64, 50),
                             pixel_size_yx_um=(0.2, 0.1), z_step_um=0.5)
    md = m.tiff_metadata()
    assert md['axes'] == 'ZYX'
    assert md['PhysicalSizeX'] == 0.1 and md['PhysicalSizeXUnit'] == 'µm'
    assert md['PhysicalSizeY'] == 0.2
    assert md['PhysicalSizeZ'] == 0.5
    assert 'TimeIncrement' not in md
    assert md['Channel'] == {'Name': ['Cam']}


def test_ngff_ome_metadata_structure():
    m = build_ome_image_meta('Cam', MODE_TIMELAPSE, 10,
                             pixel_size_yx_um=(0.15, 0.15), t_interval_s=0.05)
    ome = m.ngff_ome_metadata(path='0')
    assert ome['version'] == '0.5'
    ms = ome['multiscales'][0]
    assert [a['name'] for a in ms['axes']] == ['t', 'y', 'x']
    assert ms['axes'][0] == {'name': 't', 'type': 'time', 'unit': 'second'}
    assert ms['axes'][1] == {'name': 'y', 'type': 'space', 'unit': 'micrometer'}
    ds = ms['datasets'][0]
    assert ds['path'] == '0'
    assert ds['coordinateTransformations'] == [
        {'type': 'scale', 'scale': [0.05, 0.15, 0.15]}]


def test_ngff_ndim_padding_for_2d_snap_stored_3d():
    # A 2D snap (YX meta) stored as (1, Y, X) must get a leading t axis so the
    # multiscales axes match the stored array rank.
    m = build_ome_image_meta('Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1))
    ome = m.ngff_ome_metadata(path='data', ndim=3)
    ms = ome['multiscales'][0]
    assert [a['name'] for a in ms['axes']] == ['t', 'y', 'x']
    assert ms['datasets'][0]['coordinateTransformations'][0]['scale'] == [1.0, 0.2, 0.1]
    assert ms['datasets'][0]['path'] == 'data'


def test_element_size_um_zyx_for_hdf5():
    m = build_ome_image_meta('Cam', MODE_SCAN, 50, scan_dims=(64, 64, 50),
                             pixel_size_yx_um=(0.2, 0.1), z_step_um=0.5)
    assert m.element_size_um() == [0.5, 0.2, 0.1]  # [z, y, x]
    # 2D snap: z defaults to 1.0
    m2 = build_ome_image_meta('Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1))
    assert m2.element_size_um() == [1.0, 0.2, 0.1]

# --- 1-axis (single-line) scans: the YX-compat + provenance contract ----------
# See docs/galvo-designer-single-axis-findings.md, phase C item 6.


def test_axes_for_single_line_scan_stay_compat_yx():
    """A Z-only profile records as compatibility YX (one line, SizeY=1);
    the scanned physical axis is preserved separately as provenance."""
    # getDimsScan for a 1-axis scan: N pixels on the scanned dim, 1 elsewhere
    assert axes_for_recording(MODE_SCAN, 1, (20, 1, 1)) == ['y', 'x']


def test_build_single_line_scan_meta_shape_and_scale():
    m = build_ome_image_meta(
        'APD', MODE_SCAN, n_frames=1, scan_dims=(20, 1, 1),
        pixel_size_yx_um=(0.5, 0.5), dtype=np.uint16)
    assert m.axes_string == 'YX'
    # the fast axis carries the scan step; the singleton y is padded with it
    assert m.scale == [0.5, 0.5]
    md = m.tiff_metadata(shape=(1, 20))
    assert md['axes'] == 'YX'
    assert md['PhysicalSizeX'] == 0.5 and md['PhysicalSizeY'] == 0.5


def test_scan_axis_provenance_is_write_only_metadata():
    """The provenance helper names the scanned devices and their physical
    axes; a Z-only scan yields (['ND-PiezoZ'], ['Z']). Deliberately no
    ImProcess reader exists for these fields."""
    from types import SimpleNamespace
    from imswitch.imcontrol.model.scan_parameters import scan_axis_provenance

    positioners = {
        'ND-GalvoX': SimpleNamespace(axes=['X']),
        'ND-PiezoZ': SimpleNamespace(axes=['Z']),
        'Weird': SimpleNamespace(axes=[]),
    }
    devices, physical = scan_axis_provenance(
        ['ND-PiezoZ', 'None', 'None'], positioners)
    assert devices == ['ND-PiezoZ']
    assert physical == ['Z']

    devices, physical = scan_axis_provenance(
        ['ND-GalvoX', 'ND-PiezoZ', 'Weird'], positioners)
    assert devices == ['ND-GalvoX', 'ND-PiezoZ', 'Weird']
    assert physical == ['X', 'Z', '?']

    assert scan_axis_provenance([], positioners) == ([], [])
    assert scan_axis_provenance(['None'], {}) == ([], [])


def test_annotation_text_serializes_numpy_at_any_depth():
    """Shared-attribute values may be NumPy arrays/scalars, nested in lists
    or dicts; they must JSON-encode instead of raising (a snapshot or stream
    finalize must never fail over metadata)."""
    import json

    import numpy as np

    from imswitch.imcontrol.model.managers.recording_metadata import (
        _annotation_text,
    )

    assert json.loads(_annotation_text(np.array([1, 2]))) == [1, 2]
    assert json.loads(_annotation_text(np.float32(0.5))) == 0.5
    assert json.loads(_annotation_text(
        {'roi': np.array([0, 4]), 'gain': np.int64(3),
         'flags': [np.bool_(True), b'ok']}
    )) == {'roi': [0, 4], 'gain': 3, 'flags': [True, 'ok']}
    # Unknown objects fall back to str() rather than failing the recording.
    class _Odd:
        def __str__(self):
            return 'odd'
    assert json.loads(_annotation_text([_Odd()])) == ['odd']


def test_scan_axis_provenance_drops_collapsed_axes():
    """An assigned axis whose length/step collapses to a single step emits no
    waveform (GalvoScanDesigner active-axis collapse), so provenance must not
    claim it was scanned. Regression: collapsed X + active Z used to claim
    both X and Z."""
    from types import SimpleNamespace
    from imswitch.imcontrol.model.scan_parameters import scan_axis_provenance

    positioners = {
        'ND-GalvoX': SimpleNamespace(axes=['X']),
        'ND-PiezoZ': SimpleNamespace(axes=['Z']),
        'ND-GalvoY': SimpleNamespace(axes=['Y']),
    }

    # Collapsed X (one step) followed by active Z: only Z is claimed.
    devices, physical = scan_axis_provenance(
        ['ND-GalvoX', 'ND-PiezoZ'], positioners,
        axis_lengths=[0.4, 10.0], axis_step_sizes=[1.0, 0.5])
    assert devices == ['ND-PiezoZ']
    assert physical == ['Z']

    # The 1-length/1-step dummy entries build_analog appends for non-scan
    # axes are filtered by the same rule.
    devices, physical = scan_axis_provenance(
        ['ND-PiezoZ', 'ND-GalvoX', 'ND-GalvoY'], positioners,
        axis_lengths=[10.0, 1.0, 1.0], axis_step_sizes=[0.5, 1.0, 1.0])
    assert devices == ['ND-PiezoZ']
    assert physical == ['Z']

    # Both axes active: both claimed, in dim order.
    devices, physical = scan_axis_provenance(
        ['ND-GalvoX', 'ND-PiezoZ'], positioners,
        axis_lengths=[10.0, 10.0], axis_step_sizes=[0.5, 0.5])
    assert devices == ['ND-GalvoX', 'ND-PiezoZ']
    assert physical == ['X', 'Z']

    # No lengths/steps given: legacy behavior, every assigned device kept.
    devices, _ = scan_axis_provenance(['ND-GalvoX', 'ND-PiezoZ'], positioners)
    assert devices == ['ND-GalvoX', 'ND-PiezoZ']

    # An entry beyond the length/step lists is kept (misaligned caller).
    devices, _ = scan_axis_provenance(
        ['ND-GalvoX', 'ND-PiezoZ'], positioners,
        axis_lengths=[10.0], axis_step_sizes=[0.5])
    assert devices == ['ND-GalvoX', 'ND-PiezoZ']
