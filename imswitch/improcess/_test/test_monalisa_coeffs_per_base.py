"""Contract tests for the MoNaLISA per-base reassembly fix.

Locks in the contract that SignalExtractor returns 4D
``(numBases, numFrames, gridRows, gridCols)`` and that MonalisaReconstructor
loops the leading Base axis through coeffs_to_image once per base, producing
6D ``(Dataset=1, Base, T, Z, Y, X)`` output.  Skips the real SignalExtractor
(Windows + CUDA DLL) by injecting a stub.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    RecordedEventSpan,
    TraversalRule,
)
from imswitch.improcess.reconstructors.monalisa.coeffs_to_image import (
    coeffs_to_image,
    coeffs_to_image_from_placement,
    placement_from_layout,
)
from imswitch.improcess.reconstructors.monalisa.result import (
    MonalisaProcessingResult,
)


def _advanced_layout(*, rows=18, cols=18, conditions=2, spans=None, traversal=()):
    """The Advanced Scan geometry behind the motivating regression."""
    loops = [AcquisitionLoop('scan_y', 'scan_y', rows, step=0.05, unit='um')]
    if conditions > 1:
        loops.append(
            AcquisitionLoop('linestep', 'condition', conditions, labels=('A', 'B'))
        )
    loops.append(AcquisitionLoop('scan_x', 'scan_x', cols, step=0.05, unit='um'))
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector='WidefieldCamera',
        storage_axes=('frame', 'detector_y', 'detector_x'),
        event_loops=tuple(loops),
        traversal=traversal,
        recorded_event_spans=spans,
        scan_source='ScanControllerAdvanced',
    )


def test_placement_deinterleaves_18_by_18_line_step_conditions():
    """648 frames are two per-line conditions, not two 324-frame time blocks.

    frame = ((scan_y * 2 + condition) * 18) + scan_x, so frames 0-17 are
    condition A of row 0 and 18-35 are condition B of that same row.
    """
    placement = placement_from_layout(_advanced_layout())

    assert len(placement.slots) == 648
    assert (placement.timepoints, placement.rows, placement.cols) == (2, 18, 18)
    assert placement.n_conditions == 2 and placement.n_time == 1
    assert placement.condition_labels == ('A', 'B')
    # (t, z, y, x) for the first frames of the first two physical rows.
    assert placement.slots[0] == (0, 0, 0, 0)
    assert placement.slots[17] == (0, 0, 0, 17)
    assert placement.slots[18] == (1, 0, 0, 0)
    assert placement.slots[36] == (0, 0, 1, 0)
    assert placement.slots[-1] == (1, 0, 17, 17)


def test_placement_reassembles_conditions_into_separate_planes():
    rows = cols = 18
    coeffs = np.arange(rows * cols * 2, dtype=np.float32).reshape(-1, 1, 1)

    image = coeffs_to_image_from_placement(
        coeffs, placement_from_layout(_advanced_layout())
    )

    assert image.shape == (2, 1, rows, cols)
    y, x = np.mgrid[0:rows, 0:cols]
    for condition in (0, 1):
        np.testing.assert_allclose(image[condition, 0], (y * 2 + condition) * cols + x)


def test_placement_uses_producer_parity_for_a_gated_detector():
    """Serpentine parity comes from the producer, not the recorded frames.

    A detector that recorded only condition B still sees reversed lines,
    because the unrecorded A traversal happened on the hardware.
    """
    rows, cols = 4, 3
    layout = _advanced_layout(
        rows=rows,
        cols=cols,
        spans=(RecordedEventSpan(cols, cols, stride=1, period=cols * 2, repeats=rows),),
        traversal=(TraversalRule('scan_x', 'serpentine', ('scan_y', 'linestep')),),
    )

    placement = placement_from_layout(layout)

    assert len(placement.slots) == rows * cols
    # flat = scan_y * 2 + condition and condition is always B (1), so every
    # recorded line has odd parity and runs backwards.
    assert [slot[3] for slot in placement.slots[:cols]] == [2, 1, 0]
    assert {slot[0] for slot in placement.slots} == {1}


def test_condition_projection_metadata_names_the_axis_condition():
    """The 6D result keeps T, so its metadata must say what T really holds."""
    coeffs = np.arange(648, dtype=np.float32).reshape(1, 1, 648, 1, 1)

    result = MonalisaProcessingResult.from_coeffs(
        name='scan',
        coeffs=coeffs,
        scan_params=_scan_params(18, 18),
        axis_label_map=_AXIS_LABELS,
        placement=placement_from_layout(_advanced_layout()),
    )

    assert result.data.shape == (1, 1, 2, 1, 18, 18)
    projection = result.acquisition_projection
    assert projection['axis'] == 'T'
    assert projection['display_name'] == 'Condition'
    assert projection['components'] == ('time', 'condition')
    assert projection['n_time'] == 1 and projection['n_conditions'] == 2
    assert projection['condition_labels'] == ('A', 'B')



# --- coeffs_to_image directly ----------------------------------------------


def _scan_params(rows, cols, t=1, z=1):
    return {
        'dimensions': ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints'],
        'directions': ['pos', 'pos', 'pos'],
        'steps': [str(cols), str(rows), str(z), str(t)],
        'step_sizes': ['35', '35', '35', '1'],
        'unidirectional': True,
    }


_AXIS_LABELS = {
    'r_l_text': 'Right-Left',
    'u_d_text': 'Up-Down',
    'b_f_text': 'Back-Front',
    'timepoints_text': 'Timepoints',
    'p_text': 'pos',
    'n_text': 'neg',
}


def test_coeffs_to_image_takes_3d_per_base_slice():
    rows, cols = 4, 5
    grid_rows, grid_cols = 3, 3
    frames = rows * cols  # 20

    # One scalar per pixel position per base — straightforward to verify.
    coeffs_one_base = np.arange(frames * grid_rows * grid_cols, dtype=np.float32)
    coeffs_one_base = coeffs_one_base.reshape(frames, grid_rows, grid_cols)

    im = coeffs_to_image(coeffs_one_base, _scan_params(rows, cols), _AXIS_LABELS)

    assert im.shape == (1, 1, rows * grid_rows, cols * grid_cols)
    # Sanity check: total mass survives the scatter-by-stride reassembly.
    np.testing.assert_allclose(im.sum(), coeffs_one_base.sum(), rtol=0, atol=1e-3)


def test_coeffs_to_image_bidirectional_reverses_fast_axis_with_fast_length():
    rows, cols = 3, 5
    coeffs = np.arange(rows * cols, dtype=np.float32).reshape(rows * cols, 1, 1)
    scan_params = _scan_params(rows, cols)
    scan_params['unidirectional'] = False

    im = coeffs_to_image(coeffs, scan_params, _AXIS_LABELS)

    assert im.shape == (1, 1, rows, cols)
    np.testing.assert_array_equal(
        im[0, 0],
        np.array(
            [
                [0, 1, 2, 3, 4],
                [9, 8, 7, 6, 5],
                [10, 11, 12, 13, 14],
            ],
            dtype=np.float32,
        ),
    )


def test_coeffs_to_image_deinterleaves_18_by_18_linestep_conditions():
    """648 frames are two per-line conditions, not two complete 324-frame scans."""
    rows = cols = 18
    num_linesteps = 2
    frames = rows * cols * num_linesteps
    coeffs = np.arange(frames, dtype=np.float32).reshape(frames, 1, 1)
    scan_params = _scan_params(rows, cols, t=num_linesteps)
    scan_params['n_linesteps'] = num_linesteps

    im = coeffs_to_image(coeffs, scan_params, _AXIS_LABELS)

    assert im.shape == (2, 1, 18, 18)
    # Condition A consists of the first 18 frames of every repeated line.
    np.testing.assert_array_equal(im[0, 0, 0], np.arange(0, 18))
    np.testing.assert_array_equal(im[0, 0, 1], np.arange(36, 54))
    np.testing.assert_array_equal(im[0, 0, -1], np.arange(612, 630))
    # Condition B consists of the next 18 frames of every repeated line.
    np.testing.assert_array_equal(im[1, 0, 0], np.arange(18, 36))
    np.testing.assert_array_equal(im[1, 0, 1], np.arange(54, 72))
    np.testing.assert_array_equal(im[1, 0, -1], np.arange(630, 648))


def test_coeffs_to_image_rejects_mismatched_frame_count():
    coeffs = np.zeros((7, 3, 3), dtype=np.float32)  # 7 frames doesn't fit 4x5
    with pytest.raises(ValueError, match='Coefficient frame count'):
        coeffs_to_image(coeffs, _scan_params(rows=4, cols=5), _AXIS_LABELS)


def test_recorded_linestep_metadata_sets_physical_grid_and_condition_count():
    """The UI-side metadata adapter must not infer sqrt(648)=25 for an 18x18 scan."""
    from imswitch.improcess.controller.MoNaLISAController import MoNaLISAController

    emitted = []
    controller = MoNaLISAController.__new__(MoNaLISAController)
    controller._widget = SimpleNamespace(
        r_l_text='Right-Left',
        u_d_text='Up-Down',
        b_f_text='Back-Front',
        timepoints_text='Timepoints',
        p_text='pos',
        n_text='neg',
    )
    controller._commChannel = SimpleNamespace(
        sigScanParamsUpdated=SimpleNamespace(
            emit=lambda *args: emitted.append(args)
        )
    )
    controller._scanParDict = _scan_params(rows=35, cols=35)
    attrs = {
        'ScanStage:target_device': [b'X', b'Y', b'Z'],
        'ScanStage:positive_direction': [True, True, True],
        'ScanStage:axis_length': [0.9, 0.9, 1.0],
        'ScanStage:axis_step_size': [0.05, 0.05, 1.0],
        'ScanTTL:Nx': 18,
        'ScanTTL:Ny': 18,
        'ScanTTL:n_linesteps': 2,
    }
    # On this branch the dialog is filled from the resolved layout, which the
    # legacy adapter infers from exactly these attributes; the controller no
    # longer re-parses them itself.
    from imswitch.improcess.model.acquisition_layout_resolver import (
        resolve_acquisition_layout,
    )

    data_obj = SimpleNamespace(
        numFrames=648,
        attrs=attrs,
        acquisition_layout=resolve_acquisition_layout(
            attrs, shape=(648, 8, 8), detector='Cam'
        ),
    )

    controller.parseScanParamsFromAttrs(data_obj)

    assert controller._scanParDict['steps'] == ['18', '18', '1', '2']
    assert controller._scanParDict['n_linesteps'] == 2
    assert emitted


# --- Full process() via a stub SignalExtractor -----------------------------


@pytest.fixture
def stub_extractor(monkeypatch):
    """Inject a SignalExtractor stub so process() runs on macOS/Linux too."""

    class _StubExtractor:
        def __init__(self):
            self.calls = []

        def extractSignal(self, data, sigmas, pattern, device):
            self.calls.append({
                'data_shape': data.shape,
                'sigmas': tuple(np.asarray(sigmas).tolist()),
                'pattern': tuple(pattern),
                'device': device,
            })
            num_bases = len(sigmas)
            num_frames = data.shape[0]
            grid_rows, grid_cols = 3, 3
            # Deterministic per-base values so we can verify each base is
            # reconstructed independently.
            coeffs = np.zeros(
                (num_bases, num_frames, grid_rows, grid_cols), dtype=np.float32
            )
            for b in range(num_bases):
                coeffs[b] = float(b + 1) * 100.0
            return coeffs

    stub = _StubExtractor()

    # Patch SignalExtractor inside the reconstructor module so _ensure
    # builds the stub instead of the CUDA-backed real one.
    monkeypatch.setattr(
        'imswitch.improcess.reconstructors.monalisa.reconstructor.SignalExtractor',
        lambda *_a, **_kw: stub,
    )
    return stub


def _make_data_obj(rows: int, cols: int):
    frames = rows * cols
    arr = np.arange(frames * 64 * 64, dtype=np.float32).reshape(frames, 64, 64)
    return SimpleNamespace(
        name='stub-data',
        data=arr,
        dataLoaded=True,
        attrs={},
        checkAndLoadData=lambda: None,
        checkAndUnloadData=lambda: None,
    )


def _params(rows: int, cols: int):
    return {
        'pixel_size_nm': 77.0,
        'device': 'CPU',
        'row_offset': 5.0,
        'col_offset': 5.0,
        'row_period': 11.0,
        'col_period': 11.0,
        'psf_fwhm_nm': 220.0,
        'bg_modelling': 'Constant',
        'bg_gaussian_size_nm': 500.0,
        'bleaching_correction': False,
        'scan_params': _scan_params(rows=rows, cols=cols),
    }


def test_monalisa_process_produces_6d_output_with_one_image_per_base(stub_extractor):
    from imswitch.improcess.reconstructors.monalisa.reconstructor import (
        MonalisaReconstructor,
    )

    reconstructor = MonalisaReconstructor()
    data_obj = _make_data_obj(rows=4, cols=5)

    result = reconstructor.process(data_obj, _params(rows=4, cols=5))

    assert stub_extractor.calls[0]['pattern'] == (5.0, 5.0, 11.0, 11.0)
    # 6D output: (Dataset=1, Base=2, T=1, Z=1, Y, X)
    assert result.data.ndim == 6
    assert result.data.shape[0] == 1            # Dataset
    assert result.data.shape[1] == 2            # Base — signal + constant BG
    # The two bases must NOT collapse to one another — the stub put 100 and
    # 200 in each base respectively so the totals must differ.
    base0_total = float(result.data[0, 0].sum())
    base1_total = float(result.data[0, 1].sum())
    assert base0_total > 0
    assert base1_total > base0_total  # background is brighter than signal here


def test_monalisa_process_raises_on_3d_extractor_output(monkeypatch):
    """Defensive: a future extractor regression returning the old 3D
    legacy shape must error loudly instead of silently producing junk."""
    from imswitch.improcess.reconstructors.monalisa.reconstructor import (
        MonalisaReconstructor,
    )

    class _Bad3DExtractor:
        def extractSignal(self, data, sigmas, pattern, device):
            return np.zeros((data.shape[0], 3, 3), dtype=np.float32)

    monkeypatch.setattr(
        'imswitch.improcess.reconstructors.monalisa.reconstructor.SignalExtractor',
        lambda *_a, **_kw: _Bad3DExtractor(),
    )

    reconstructor = MonalisaReconstructor()
    data_obj = _make_data_obj(rows=4, cols=5)

    with pytest.raises(ValueError, match='numBases'):
        reconstructor.process(data_obj, _params(rows=4, cols=5))
