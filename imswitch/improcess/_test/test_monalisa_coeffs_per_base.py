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

from imswitch.improcess.reconstructors.monalisa.coeffs_to_image import (
    coeffs_to_image,
)


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
    data_obj = SimpleNamespace(
        numFrames=648,
        attrs={
            'ScanStage:target_device': [b'X', b'Y', b'Z'],
            'ScanStage:positive_direction': [True, True, True],
            'ScanStage:axis_length': [0.9, 0.9, 1.0],
            'ScanStage:axis_step_size': [0.05, 0.05, 1.0],
            'ScanTTL:Nx': 18,
            'ScanTTL:Ny': 18,
            'ScanTTL:n_linesteps': 2,
        },
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
