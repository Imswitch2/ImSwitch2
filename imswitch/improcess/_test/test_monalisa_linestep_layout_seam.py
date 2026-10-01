"""Seam tests: line-step layouts reach the fast-Gauss paths intact.

Audit condition 3. A legacy 18x18x2 line-step recording used to be *declined*
by the fast-Gauss geometry (any layout with conditions returned ``None``) and
then sized by the attribute ladder as ``nx * ny`` = 324 frames per stack, so
648 frames became two timepoints of the wrong image -- the motivating bug,
reproduced through the very path meant to retire it. Main's ``2463e9d6`` made
the ladder line-step aware but never reached this branch.

These tests feed that recording through the resolver twice -- once as legacy
metadata (legacy adapter, usable, not authoritative) and once as a recorded
layout (authoritative) -- and assert what the fast-Gauss offline and live
paths derive from it, which frames belong to condition 0, and what the scan
dialog is filled with. The two provenances must agree; the older ladder is not
consulted for either.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    encode_acquisition_layout,
)
from imswitch.improcess.model.acquisition_layout_resolver import (
    resolve_acquisition_layout,
)
from imswitch.improcess.reconstructors.monalisa.coeffs_to_image import (
    linestep_conditions_interleave_per_line,
    placement_from_layout,
)
from imswitch.improcess.reconstructors.monalisa.live_session import (
    MonalisaLiveSession,
)
from imswitch.improcess.reconstructors.monalisa.reconstructor import (
    MonalisaReconstructor,
)
from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
    scan_params_from_layout,
)

ROWS = COLS = 18
CONDITIONS = 2
FRAMES = ROWS * COLS * CONDITIONS  # 648, the motivating count

#: What an Advanced Scan line-step recording carried before the layout existed.
LEGACY_ATTRS = {
    'ScanStage:target_device': ['X', 'Y', 'Z'],
    'ScanStage:positive_direction': [True, True, True],
    'ScanStage:axis_startpos': [0.0, 0.0, 0.0],
    'ScanStage:axis_length': [0.9, 0.9, 1.0],
    'ScanStage:axis_step_size': [0.05, 0.05, 1.0],
    'ScanTTL:Nx': COLS,
    'ScanTTL:Ny': ROWS,
    'ScanTTL:n_linesteps': CONDITIONS,
}

AXIS_LABELS = {
    'r_l_text': 'Right-Left',
    'u_d_text': 'Up-Down',
    'b_f_text': 'Back-Front',
    'timepoints_text': 'Timepoints',
    'p_text': 'pos',
    'n_text': 'neg',
}


def _legacy_resolved():
    """The recording as the legacy adapter infers it: usable, not authoritative."""
    resolved = resolve_acquisition_layout(
        LEGACY_ATTRS, shape=(FRAMES, 8, 8), detector='Cam'
    )
    assert resolved.is_usable and not resolved.is_authoritative, (
        resolved.source,
        [issue.code for issue in resolved.issues],
    )
    return resolved


def _recorded_layout(condition_position='per-line'):
    """The same scan as a producer records it.

    ``per-line`` is the Advanced producer's chronology: every physical line is
    repeated once per condition, so the condition loop sits immediately
    outside ``scan_x``. ``per-image`` is one complete image per condition --
    a different frame order the fast paths cannot reassemble.
    """
    slow = AcquisitionLoop('scan_y', 'scan_y', ROWS, step=0.05, unit='um')
    condition = AcquisitionLoop(
        'condition', 'condition', CONDITIONS, labels=('A', 'B')
    )
    fast = AcquisitionLoop('scan_x', 'scan_x', COLS, step=0.05, unit='um')
    loops = (
        (slow, condition, fast)
        if condition_position == 'per-line'
        else (condition, slow, fast)
    )
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector='Cam',
        storage_axes=('frame', 'detector_y', 'detector_x'),
        event_loops=loops,
    )


def _recorded_resolved(condition_position='per-line'):
    layout = _recorded_layout(condition_position)
    resolved = resolve_acquisition_layout(
        {
            'AcquisitionLayout:schema': ACQUISITION_LAYOUT_SCHEMA,
            'AcquisitionLayout:json': encode_acquisition_layout(layout),
            **LEGACY_ATTRS,
        },
        shape=(FRAMES, 8, 8),
        detector='Cam',
    )
    assert resolved.is_authoritative, (
        resolved.source,
        [issue.code for issue in resolved.issues],
    )
    return resolved


BOTH_PROVENANCES = pytest.mark.parametrize(
    'resolved_factory',
    [_legacy_resolved, _recorded_resolved],
    ids=['legacy-adapter', 'recorded'],
)


def test_legacy_linestep_recording_resolves_to_a_per_line_interleaved_layout():
    resolved = _legacy_resolved()
    placement = placement_from_layout(resolved.layout)

    assert (placement.cols, placement.rows, placement.n_conditions, placement.n_time) == (
        COLS, ROWS, CONDITIONS, 1,
    )
    assert linestep_conditions_interleave_per_line(resolved.layout)

    # Condition 0 is the first line of every pair of lines: frames
    # 0..17, 36..53, 72..89, ... -- never the first 324 frames.
    condition_0 = [
        index for index, (t, _z, _y, _x) in enumerate(placement.slots) if t == 0
    ]
    assert condition_0 == [
        row * COLS * CONDITIONS + col for row in range(ROWS) for col in range(COLS)
    ]
    assert condition_0[:20] == list(range(18)) + [36, 37]


@BOTH_PROVENANCES
def test_fast_gauss_geometry_sizes_the_stack_from_the_layout(resolved_factory):
    """One interleaved stack of nx*ny*conditions frames, one timepoint."""
    geometry = MonalisaReconstructor()._fast_gauss_geometry_from_layout(
        resolved_factory(), LEGACY_ATTRS, FRAMES
    )

    assert geometry is not None, 'the layout path declined a line-step layout'
    assert geometry['frames_per_stack'] == FRAMES
    assert geometry['num_timepoints'] == 1
    assert geometry['n_linesteps'] == CONDITIONS
    assert geometry['scan_params']['steps'] == [COLS, ROWS, 1, CONDITIONS]
    assert geometry['scan_params']['n_linesteps'] == CONDITIONS
    # The session the offline path spins up reads its condition count here.
    assert geometry['attrs']['ScanTTL:n_linesteps'] == CONDITIONS


def test_the_layout_outranks_a_contradicting_linestep_attribute():
    attrs = dict(LEGACY_ATTRS, **{'ScanTTL:n_linesteps': 1})
    geometry = MonalisaReconstructor()._fast_gauss_geometry_from_layout(
        _recorded_resolved(), attrs, FRAMES
    )
    assert geometry['n_linesteps'] == CONDITIONS
    assert geometry['frames_per_stack'] == FRAMES


@BOTH_PROVENANCES
def test_live_session_takes_its_condition_count_from_the_layout(resolved_factory):
    stack_info = SimpleNamespace(acquisition_layout=resolved_factory())
    assert MonalisaLiveSession._geometry_from_recorded_layout(stack_info) == (
        COLS, ROWS, 1, CONDITIONS,
    )


def test_conditions_laid_out_per_image_are_still_refused():
    """Accepting interleaved conditions must not accept every condition loop."""
    assert not linestep_conditions_interleave_per_line(
        _recorded_layout('per-image')
    )
    with pytest.raises(ValueError, match='not interleaved per line'):
        MonalisaReconstructor()._fast_gauss_geometry_from_layout(
            _recorded_resolved('per-image'), LEGACY_ATTRS, FRAMES
        )


def test_scan_dialog_carries_conditions_on_the_timepoints_slot():
    """coeffs_to_image reads T = timepoints x conditions plus n_linesteps."""
    values = scan_params_from_layout(_legacy_resolved(), AXIS_LABELS)

    assert values['dimensions'] == ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints']
    assert values['steps'] == ['18', '18', '1', '2']
    assert values['n_linesteps'] == CONDITIONS
