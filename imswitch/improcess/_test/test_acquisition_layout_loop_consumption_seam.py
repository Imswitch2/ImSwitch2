"""Seam tests: every layout consumer places or refuses every loop.

Audit condition 4. Each consumer used to select the loop kinds it knew by a
private whitelist and let the rest fall through its index arithmetic: a
``repeat`` loop the Advanced producer emits for a two-pulse scan gave
MoNaLISA two slots per position with the second pulse overwriting the first,
the live reader sized a stack as ``scan_x * scan_y`` and ignored conditions,
BeadRec folded a time lapse into one raster, and SNOUTY took cycle/plane
counts without checking their order. All of them now go through
``select_loops`` in imcommon, which raises ``UnconsumedLoopError`` for a loop
the consumer neither places nor declares folded.

The rule for provenance is the contract's: a *declared* layout the consumer
cannot place is refused with an error; an *inferred* one is declined so the
older path still gets its chance.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    RecordedEventSpan,
    UnconsumedLoopError,
)
from imswitch.improcess.live.sources import _frames_per_stack_from_layout
from imswitch.improcess.model.acquisition_layout_resolver import (
    ResolvedAcquisitionLayout,
)
from imswitch.improcess.reconstructors.beadrec.reconstructor import (
    raster_geometry_from_layout,
)
from imswitch.improcess.reconstructors.monalisa.coeffs_to_image import (
    placement_from_layout,
)
from imswitch.improcess.reconstructors.monalisa.reconstructor import (
    MonalisaReconstructor,
)
from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
    scan_params_from_layout,
)
from imswitch.improcess.reconstructors.snouty.metadata import (
    recorded_snouty_geometry,
)

AXIS_LABELS = {
    'r_l_text': 'Right-Left',
    'u_d_text': 'Up-Down',
    'b_f_text': 'Back-Front',
    'timepoints_text': 'Timepoints',
    'p_text': 'pos',
    'n_text': 'neg',
}


def _layout(*loops, spans=None):
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector='Cam',
        storage_axes=('frame', 'detector_y', 'detector_x'),
        event_loops=tuple(loops),
        recorded_event_spans=spans,
    )


def _resolved(layout, *, authoritative):
    """A resolved layout as the resolver would hand it to a consumer.

    Whether a layout may refuse a reconstruction is decided by the layout's
    own ``provenance`` (``recorded`` / ``user-override`` state fact; an
    adapter's inference does not), which is what the resolver reports.
    """
    if authoritative:
        return ResolvedAcquisitionLayout(
            layout=replace(layout, provenance='recorded'),
            source='explicit', confidence='certain',
        )
    return ResolvedAcquisitionLayout(
        layout=replace(layout, provenance='legacy-adapter'),
        source='scan-stage-legacy', confidence='high',
    )


TWO_PULSE = _layout(
    AcquisitionLoop('scan_y', 'scan_y', 3),
    AcquisitionLoop('scan_x', 'scan_x', 4),
    AcquisitionLoop('repeat', 'repeat', 2),
)


def test_monalisa_placement_refuses_a_loop_it_cannot_place():
    with pytest.raises(UnconsumedLoopError, match="MoNaLISA placement.*'repeat'"):
        placement_from_layout(TWO_PULSE)


@pytest.mark.parametrize('authoritative', [True, False], ids=['declared', 'inferred'])
def test_monalisa_classic_and_fast_paths_refuse_declared_and_decline_inferred(authoritative):
    reconstructor = MonalisaReconstructor()
    resolved = _resolved(TWO_PULSE, authoritative=authoritative)
    attrs = {
        'ScanStage:axis_startpos': [0.0, 0.0, 0.0],
        'ScanStage:axis_length': [0.2, 0.15, 1.0],
        'ScanStage:axis_step_size': [0.05, 0.05, 1.0],
    }
    data_obj = SimpleNamespace(acquisition_layout=resolved)

    if authoritative:
        with pytest.raises(ValueError, match="'repeat'"):
            reconstructor._placement_for(data_obj, 24)
        with pytest.raises(ValueError, match="'repeat'"):
            reconstructor._fast_gauss_geometry_from_layout(resolved, attrs, 24)
    else:
        assert reconstructor._placement_for(data_obj, 24) is None
        assert reconstructor._fast_gauss_geometry_from_layout(resolved, attrs, 24) is None


def test_live_reader_sizes_a_stack_as_everything_one_time_point_produces():
    layout = _layout(
        AcquisitionLoop('time', 'time', 2),
        AcquisitionLoop('scan_y', 'scan_y', 3),
        AcquisitionLoop('condition', 'condition', 2),
        AcquisitionLoop('scan_x', 'scan_x', 4),
        AcquisitionLoop('repeat', 'repeat', 2),
    )
    # 3 rows x 2 conditions x 4 columns x 2 pulses -- not scan_x * scan_y = 12.
    assert _frames_per_stack_from_layout(_resolved(layout, authoritative=True)) == 48

    gated = _layout(
        AcquisitionLoop('time', 'time', 2),
        AcquisitionLoop('scan_y', 'scan_y', 3),
        AcquisitionLoop('condition', 'condition', 2),
        AcquisitionLoop('scan_x', 'scan_x', 4),
        spans=(RecordedEventSpan(start=4, count=4, period=8, repeats=6),),
    )
    assert _frames_per_stack_from_layout(_resolved(gated, authoritative=True)) == 12

    plain_lapse = _layout(AcquisitionLoop('time', 'time', 6))
    assert _frames_per_stack_from_layout(_resolved(plain_lapse, authoritative=True)) is None


def test_beadrec_refuses_a_time_lapse_it_would_have_folded_into_one_raster():
    lapse = _layout(
        AcquisitionLoop('time', 'time', 3),
        AcquisitionLoop('scan_y', 'scan_y', 3),
        AcquisitionLoop('scan_x', 'scan_x', 4),
    )
    with pytest.raises(ValueError, match="BeadRec.*'time'"):
        raster_geometry_from_layout(lapse, authoritative=True)
    assert raster_geometry_from_layout(lapse, authoritative=False) is None

    raster = _layout(
        AcquisitionLoop('scan_y', 'scan_y', 3),
        AcquisitionLoop('condition', 'condition', 2),
        AcquisitionLoop('scan_x', 'scan_x', 4, step=0.5, unit='um'),
    )
    dims, steps, condition = raster_geometry_from_layout(raster)
    assert dims == (4, 3) and condition.count == 2


def test_snouty_checks_the_order_restack_assumes_and_refuses_extra_loops():
    def data_obj(layout, *, authoritative=True):
        return SimpleNamespace(acquisition_layout=_resolved(layout, authoritative=authoritative))

    good = _layout(
        AcquisitionLoop('time', 'time', 2),
        AcquisitionLoop('cycle', 'cycle', 5),
        AcquisitionLoop('plane', 'plane', 3),
    )
    assert recorded_snouty_geometry(data_obj(good)) == (5, 3, 2)

    transposed = _layout(
        AcquisitionLoop('plane', 'plane', 3),
        AcquisitionLoop('cycle', 'cycle', 5),
    )
    with pytest.raises(ValueError, match='plane loop immediately inside the cycle'):
        recorded_snouty_geometry(data_obj(transposed))
    assert recorded_snouty_geometry(data_obj(transposed, authoritative=False)) is None

    with_raster = _layout(
        AcquisitionLoop('cycle', 'cycle', 5),
        AcquisitionLoop('plane', 'plane', 3),
        AcquisitionLoop('scan_x', 'scan_x', 4),
    )
    with pytest.raises(ValueError, match="SNOUTY restack.*'scan_x'"):
        recorded_snouty_geometry(data_obj(with_raster))


def test_scan_dialog_declines_a_layout_with_a_loop_it_has_no_slot_for():
    assert scan_params_from_layout(SimpleNamespace(layout=TWO_PULSE, is_usable=True), AXIS_LABELS) is None
    plain = _layout(AcquisitionLoop('scan_y', 'scan_y', 3), AcquisitionLoop('scan_x', 'scan_x', 4))
    assert scan_params_from_layout(SimpleNamespace(layout=plain, is_usable=True), AXIS_LABELS)['steps'] == ['4', '3', '1', '1']
