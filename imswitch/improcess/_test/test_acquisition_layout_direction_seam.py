"""Seam tests: a negative stage direction reconstructs the same way everywhere.

Audit condition 1. The layout used to carry a negative direction twice -- as
the loop's physical ``direction`` and, redundantly, as a ``reverse``
traversal -- and the consumers disagreed on how many times to apply it:
MoNaLISA classic flipped twice, BeadRec and the scan dialog once, so one
recording from a rig with ``isPositiveDirection: false`` reconstructed as
mirror images. The rule now: traversal is chronology only, ``direction`` is a
sign applied exactly once by one imcommon helper.

These tests run the same negative-direction recording through the three
consumers and assert every frame lands in the same pixel, that both producers
of layouts (the scan-source builder and the legacy adapter) emit ``forward``
for a monotonically stepped negative axis, and that the fast-Gauss paths no
longer refuse such a layout as "reversed".
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    TraversalRule,
    encode_acquisition_layout,
)
from imswitch.improcess.model.acquisition_layout_resolver import (
    resolve_acquisition_layout,
)
from imswitch.improcess.reconstructors.beadrec.reconstructor import (
    raster_positions_from_layout,
)
from imswitch.improcess.reconstructors.monalisa.coeffs_to_image import (
    coeffs_to_image,
    placement_from_layout,
)
from imswitch.improcess.reconstructors.monalisa.reconstructor import (
    MonalisaReconstructor,
)
from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
    scan_params_from_layout,
)


class _OnePulseEach(dict):
    """Test stand-in for getNumCamTTL(): every detector is gated, one pulse per position.

    The builders no longer default an undeclared detector to one pulse -- that
    default is what let the recording gate compare a number with itself -- so
    a test that means "plain camera, one exposure per position" says so.
    """

    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return 1

    def get(self, key, default=None):
        return 1


ONE_PULSE_EACH = _OnePulseEach()


ROWS, COLS = 4, 5
FRAMES = ROWS * COLS

AXIS_LABELS = {
    'r_l_text': 'Right-Left',
    'u_d_text': 'Up-Down',
    'b_f_text': 'Back-Front',
    'timepoints_text': 'Timepoints',
    'p_text': 'pos',
    'n_text': 'neg',
}

STAGE_ATTRS = {
    'ScanStage:axis_startpos': [0.0, 0.0, 0.0],
    'ScanStage:axis_length': [0.25, 0.2, 1.0],
    'ScanStage:axis_step_size': [0.05, 0.05, 1.0],
}


def _negative_y_layout(traversal=None):
    """Y stepped in the negative direction, X positive; both monotonic."""
    loops = (
        AcquisitionLoop('scan_y', 'scan_y', ROWS, step=0.05, unit='um', direction=-1),
        AcquisitionLoop('scan_x', 'scan_x', COLS, step=0.05, unit='um', direction=1),
    )
    if traversal is None:
        traversal = tuple(TraversalRule(loop.id, 'forward') for loop in loops)
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector='Cam',
        storage_axes=('frame', 'detector_y', 'detector_x'),
        event_loops=loops,
        traversal=traversal,
    )


def _pixel_of_each_frame_via_dialog(layout):
    """Where the scan-dialog path puts every frame: (row, col) per frame."""
    values = scan_params_from_layout(
        SimpleNamespace(layout=layout, is_usable=True), AXIS_LABELS
    )
    assert values['directions'][:2] == ['pos', 'neg']
    scan_params = dict(values, unidirectional=True)
    coeffs = np.arange(FRAMES, dtype=np.float32).reshape(FRAMES, 1, 1)
    image = coeffs_to_image(coeffs, scan_params, AXIS_LABELS)
    assert image.shape == (1, 1, ROWS, COLS)
    pixel_of = {}
    for row in range(ROWS):
        for col in range(COLS):
            pixel_of[int(image[0, 0, row, col])] = (row, col)
    return [pixel_of[frame] for frame in range(FRAMES)]


def test_placement_beadrec_and_dialog_put_every_frame_in_the_same_pixel():
    layout = _negative_y_layout()

    placement = placement_from_layout(layout)
    via_placement = [(y, x) for (_t, _z, y, x) in placement.slots]
    via_beadrec = [
        divmod(index, COLS) for (_condition, index) in
        raster_positions_from_layout(layout, scan_dims=(COLS, ROWS))
    ]
    via_dialog = _pixel_of_each_frame_via_dialog(layout)

    assert via_placement == via_beadrec == via_dialog
    # The first frame is the first Y position visited, which on a negative
    # axis is the highest physical row; X runs the ordinary way.
    assert via_placement[0] == (ROWS - 1, 0)
    assert via_placement[COLS] == (ROWS - 2, 0)
    assert via_placement[-1] == (0, COLS - 1)


def test_the_scan_source_builder_emits_forward_for_a_negative_axis():
    from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
        build_point_scan_layouts,
    )

    # The ScanInfoContract shape the point-scan builder reads.
    scan_info = {
        'img_dims': [COLS, ROWS],
        'img_axes_phys': ['x', 'y'],
        'pixel_sizes': [0.05, 0.05],
        'n_linesteps': 1,
    }
    layout = build_point_scan_layouts(
        scan_info,
        ('Cam',),
        scan_source='ScanControllerPointScan',
        directions={'scan_x': 1, 'scan_y': -1, 'scan_z': 1},
        pulse_counts=ONE_PULSE_EACH,
    )['Cam']

    by_kind = {loop.kind: loop for loop in layout.event_loops}
    assert by_kind['scan_y'].direction == -1
    assert all(rule.order == 'forward' for rule in layout.traversal)


def test_the_legacy_adapter_emits_forward_for_a_negative_axis():
    attrs = dict(
        STAGE_ATTRS,
        **{
            'ScanStage:target_device': ['X', 'Y', 'Z'],
            'ScanStage:positive_direction': [True, False, True],
        },
    )
    resolved = resolve_acquisition_layout(attrs, shape=(FRAMES, 8, 8), detector='Cam')

    assert resolved.is_usable, [issue.code for issue in resolved.issues]
    by_kind = {loop.kind: loop for loop in resolved.layout.event_loops}
    assert by_kind['scan_y'].direction == -1
    assert by_kind['scan_x'].direction in (1, None)
    assert all(rule.order == 'forward' for rule in resolved.layout.traversal)


def test_fast_gauss_no_longer_refuses_a_negative_direction_as_reversed():
    """A monotonic negative axis is a plain raster to the contiguous-stack path."""
    layout = _negative_y_layout()
    resolved = resolve_acquisition_layout(
        {
            'AcquisitionLayout:schema': ACQUISITION_LAYOUT_SCHEMA,
            'AcquisitionLayout:json': encode_acquisition_layout(layout),
            **STAGE_ATTRS,
        },
        shape=(FRAMES, 8, 8),
        detector='Cam',
    )
    assert resolved.is_authoritative

    geometry = MonalisaReconstructor()._fast_gauss_geometry_from_layout(
        resolved, STAGE_ATTRS, FRAMES
    )

    assert geometry is not None
    assert (geometry['nx_s'], geometry['ny_s']) == (COLS, ROWS)
    assert geometry['frames_per_stack'] == FRAMES


def test_the_scan_dialog_widget_keeps_every_axis_direction(qtbot):
    """The widget used to set all three direction combos from directions[0].

    The dialog path is the one legitimate second reading of ``direction``; it
    only counts if the value survives the widget and comes back on OK.
    """
    from imswitch.improcess.view.ScanParamsDialog import ScanParamsDialog

    dialog = ScanParamsDialog(
        None, 'Right-Left', 'Up-Down', 'Back-Front', 'Timepoints', 'pos', 'neg'
    )
    qtbot.addWidget(dialog)
    values = scan_params_from_layout(
        SimpleNamespace(layout=_negative_y_layout(), is_usable=True), AXIS_LABELS
    )

    dialog.updateValues(values)

    assert dialog.getDimensions()[:3] == ['Right-Left', 'Up-Down', 'Back-Front']
    assert dialog.getDirections()[:3] == ['pos', 'neg', 'pos']
    assert dialog.getSteps() == [str(COLS), str(ROWS), '1', '1']
    assert dialog.unidirCheck.isChecked() is True


def test_scan_params_from_layout_declines_what_the_dialog_cannot_express():
    """No pre-fill is better than a pre-fill that reproduces a different scan."""
    from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
        scan_params_from_layout as from_layout,
    )

    def resolved(layout):
        return SimpleNamespace(layout=layout, is_usable=True)

    forward = _negative_y_layout()
    assert from_layout(resolved(forward), AXIS_LABELS)['unidirectional'] is True

    serpentine = _negative_y_layout(
        traversal=(
            TraversalRule('scan_y', 'forward'),
            TraversalRule('scan_x', 'serpentine', parity_loops=('scan_y',)),
        )
    )
    assert from_layout(resolved(serpentine), AXIS_LABELS)['unidirectional'] is False

    retrace = _negative_y_layout(
        traversal=(TraversalRule('scan_y', 'reverse'), TraversalRule('scan_x', 'forward'))
    )
    assert from_layout(resolved(retrace), AXIS_LABELS) is None

    per_image_conditions = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector='Cam',
        storage_axes=('frame', 'detector_y', 'detector_x'),
        event_loops=(
            AcquisitionLoop('condition', 'condition', 2),
            AcquisitionLoop('scan_y', 'scan_y', ROWS),
            AcquisitionLoop('scan_x', 'scan_x', COLS),
        ),
    )
    assert from_layout(resolved(per_image_conditions), AXIS_LABELS) is None


def test_orientation_signs_round_trip_between_layout_and_detection():
    from imswitch.improcess.reconstructors.monalisa.live_session import (
        directions_from_orientation,
        expected_orientation_signs,
    )

    assert directions_from_orientation('+x+y') == ['+', '+', '+', '+']
    assert directions_from_orientation('+x-y') == ['+', '-', '+', '+']
    assert directions_from_orientation('-y+x') == ['+', '-', '+', '+']
    assert directions_from_orientation('-x-y') == ['-', '-', '+', '+']
    assert expected_orientation_signs(_negative_y_layout()) == {'x': '+', 'y': '-'}
    assert expected_orientation_signs(None) is None


def test_a_genuine_retrace_is_still_a_reverse_traversal_the_fast_path_refuses():
    layout = _negative_y_layout(
        traversal=(TraversalRule('scan_y', 'reverse'), TraversalRule('scan_x', 'forward'))
    )
    resolved = resolve_acquisition_layout(
        {
            'AcquisitionLayout:schema': ACQUISITION_LAYOUT_SCHEMA,
            'AcquisitionLayout:json': encode_acquisition_layout(layout),
            **STAGE_ATTRS,
        },
        shape=(FRAMES, 8, 8),
        detector='Cam',
    )
    with pytest.raises(ValueError, match='reversed or serpentine'):
        MonalisaReconstructor()._fast_gauss_geometry_from_layout(
            resolved, STAGE_ATTRS, FRAMES
        )
