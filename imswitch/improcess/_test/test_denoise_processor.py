from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.model import ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.denoise import DenoiseProcessor, DenoisedResult


class _Result(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def test_denoise_processor_registered():
    assert 'denoise' in available_processor_ids()


def test_denoise_applies_to_two_dim_results():
    processor = DenoiseProcessor()
    gate = processor.applies_to

    assert gate(_Result(name='x', data=np.zeros((4, 4)), axis_labels=['Y', 'X']))
    assert gate(_Result(name='x', data=np.zeros((2, 4, 4)), axis_labels=['T', 'Y', 'X']))
    assert not gate(_Result(name='x', data=np.zeros((4,)), axis_labels=['L']))


def test_denoise_extract_2d_stack_picks_t_axis():
    data = np.arange(2 * 3 * 4 * 5, dtype=np.float32).reshape(2, 3, 4, 5)
    result = _Result(name='x', data=data, axis_labels=['T', 'C', 'Y', 'X'])

    stack, indexer, spatial = DenoiseProcessor._extract_2d_stack(result)

    assert stack.shape == (2, 4, 5)
    assert spatial == (4, 5)
    # T iterated fully, C pinned to index 0, Y/X kept whole.
    assert indexer == (slice(None), 0, slice(None), slice(None))
    np.testing.assert_array_equal(stack, data[:, 0])


def test_denoise_extract_2d_stack_picks_first_leading_axis_when_no_t():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    result = _Result(name='x', data=data, axis_labels=['Z', 'Y', 'X'])

    stack, indexer, _ = DenoiseProcessor._extract_2d_stack(result)

    assert stack.shape == (3, 4, 5)
    assert indexer == (slice(None), slice(None), slice(None))


def test_denoise_extract_2d_stack_promotes_pure_2d():
    data = np.zeros((4, 5), dtype=np.float32)
    result = _Result(name='x', data=data, axis_labels=['Y', 'X'])

    stack, indexer, spatial = DenoiseProcessor._extract_2d_stack(result)

    assert stack.shape == (1, 4, 5)
    assert spatial == (4, 5)
    assert indexer == ()


def test_denoise_apply_raises_without_torch(monkeypatch):
    """If pytorch is missing or Denoiser.denoising_available is False, apply()
    must surface a clear error instead of silently no-oping."""
    processor = DenoiseProcessor()

    class _FakeDenoiser:
        denoising_available = False
        model = None

        def init_model(self, *_a, **_kw):
            pass

        def load_model(self, *_a, **_kw):
            pass

        def predict(self, *_a, **_kw):
            raise AssertionError("predict() must not run when denoising is unavailable")

    monkeypatch.setattr(
        'imswitch.improcess.processors.denoise.processor.Denoiser',
        lambda: _FakeDenoiser(),
        raising=False,
    )
    import imswitch.improcess.model as improcess_model
    monkeypatch.setattr(improcess_model, 'Denoiser', lambda: _FakeDenoiser(), raising=False)

    result = _Result(name='x', data=np.zeros((2, 4, 4)), axis_labels=['T', 'Y', 'X'])
    with pytest.raises(RuntimeError, match='Denoising is not available'):
        processor.apply(result, {'model_name': 'whatever', 'crop_size': 16})


def test_denoise_apply_runs_with_stub_denoiser(monkeypatch):
    """End-to-end shape check: apply() embeds the prediction in the result and
    returns a DenoisedResult with matching axis labels."""

    class _StubDenoiser:
        denoising_available = True
        model = object()

        def init_model(self, *_a, **_kw):
            pass

        def load_model(self, *_a, **_kw):
            pass

        def predict(self, data, crop_size, pad=True, clip_neg=True):
            return np.ones_like(data, dtype=np.float32) * 0.5

    monkeypatch.setattr(
        'imswitch.improcess.processors.denoise.processor.Denoiser',
        lambda: _StubDenoiser(),
        raising=False,
    )
    import imswitch.improcess.model as improcess_model
    monkeypatch.setattr(improcess_model, 'Denoiser', lambda: _StubDenoiser(), raising=False)

    data = np.arange(2 * 4 * 4, dtype=np.float32).reshape(2, 4, 4)
    result = _Result(
        name='stack',
        data=data,
        axis_labels=['T', 'Y', 'X'],
        axis_scales=[1.0, 0.2, 0.3],
        scale_unit='um',
    )
    processor = DenoiseProcessor()

    denoised = processor.apply(
        result,
        {'model_name': 'fakeRCAN', 'crop_size': 16, 'pad': True},
    )

    assert isinstance(denoised, DenoisedResult)
    assert denoised.axis_labels == ['T', 'Y', 'X']
    assert denoised.axis_scales == [1.0, 0.2, 0.3]
    assert denoised.scale_unit == 'um'
    assert denoised.data.shape == data.shape
    assert denoised.model_type == 'UNetRCAN'  # auto from 'RCAN' substring
    assert denoised.name == 'stack_denoise'
    # The stub returns 0.5 everywhere; embedding preserves that for T-iterated planes.
    np.testing.assert_allclose(denoised.data, 0.5)
