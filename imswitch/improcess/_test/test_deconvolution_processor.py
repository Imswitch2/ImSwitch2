import numpy as np
import pytest
from scipy import fft

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.deconvolution import (
    RichardsonLucyProcessor,
    richardson_lucy_2d,
    theoretical_otf,
)

PIXEL_NM = 65.0
WAVELENGTH_NM = 520.0
NA = 1.4


def _otf_for(shape, model="airy", **kwargs):
    return theoretical_otf(
        (2 * shape[0], 2 * shape[1]), (PIXEL_NM, PIXEL_NM), model=model,
        wavelength_nm=WAVELENGTH_NM, na=NA, **kwargs,
    )


def _blur(image, otf):
    """Forward model the processor assumes: reflective convolution."""
    h, w = image.shape
    mirrored = np.concatenate([image, image[::-1]], axis=0)
    mirrored = np.concatenate([mirrored, mirrored[:, ::-1]], axis=1)
    blurred = fft.irfft2(fft.rfft2(mirrored) * otf, s=mirrored.shape)
    return blurred[:h, :w]


def _beads(shape=(64, 64)):
    image = np.full(shape, 1.0, dtype=np.float32)
    image[20, 20] = image[40, 44] = image[40, 47] = 1000.0
    return image


def _calibrated(data, labels=("Y", "X")):
    return ArrayProcessingResult(
        name="beads", data=data, axis_labels=list(labels),
        axis_scales=[1.0] * (data.ndim - 2) + [PIXEL_NM / 1000.0] * 2, scale_unit="um",
    )


def test_registered():
    assert "deconvolve-rl" in available_processor_ids()


@pytest.mark.parametrize("model", ["airy", "gaussian"])
def test_psf_is_unit_sum_non_negative_and_symmetric(model):
    otf = _otf_for((32, 32), model=model)
    psf = fft.irfft2(otf, s=(64, 64))

    assert abs(otf[0, 0] - 1.0) < 1e-5
    assert psf.min() > -1e-6
    # Point-symmetric about the origin on the periodic grid, so it is its own adjoint.
    np.testing.assert_allclose(psf, np.roll(psf[::-1, ::-1], (1, 1), axis=(0, 1)), atol=1e-6)


def test_airy_cutoff_matches_two_na_over_lambda():
    """Beyond 2 NA / lambda the pupil passes nothing (the pixel sinc aside)."""
    shape = (256, 256)
    otf = np.abs(theoretical_otf(shape, (20.0, 20.0), wavelength_nm=WAVELENGTH_NM, na=NA))
    fx = fft.rfftfreq(shape[1], d=20.0)
    cutoff = 2 * NA / WAVELENGTH_NM
    # Small residue from making the truncated kernel non-negative.
    assert otf[0, fx > cutoff * 1.02].max() < 1e-2
    assert otf[0, fx < cutoff * 0.5].min() > 0.2


def test_measured_fwhm_sets_the_gaussian_width():
    otf = theoretical_otf((128, 128), (10.0, 10.0), model="gaussian", fwhm_nm=300.0)
    psf = np.fft.fftshift(fft.irfft2(otf, s=(128, 128)))
    profile = psf[64]
    above = np.flatnonzero(profile >= profile.max() / 2)
    assert (above[-1] - above[0] + 1) * 10.0 == pytest.approx(300.0, abs=20.0)


def test_rl_sharpens_and_keeps_flux():
    truth = _beads()
    otf = _otf_for(truth.shape)
    observed = _blur(truth, otf).astype(np.float32)

    restored = richardson_lucy_2d(observed, otf, 30)

    assert restored.sum() == pytest.approx(observed.sum(), rel=1e-3)
    assert restored[20, 20] > 2 * observed[20, 20]
    assert np.abs(restored - truth).sum() < np.abs(observed - truth).sum()
    # Two beads 3 px (195 nm) apart, just above lambda / 2NA (186 nm): the blur
    # merges them, deconvolution opens a dip between them.
    def dip(image):
        return image[40, 45:47].mean() / image[40, [44, 47]].mean()

    assert dip(observed) > 0.95
    assert dip(restored) < 0.6


def test_more_iterations_converge_further():
    truth = _beads()
    otf = _otf_for(truth.shape)
    observed = _blur(truth, otf).astype(np.float32)

    errors = [np.abs(richardson_lucy_2d(observed, otf, n) - truth).sum() for n in (1, 5, 25)]
    assert errors[0] > errors[1] > errors[2]


def test_flat_image_stays_flat_including_edges():
    plane = np.full((40, 30), 7.0, dtype=np.float32)
    restored = richardson_lucy_2d(plane, _otf_for(plane.shape), 10)
    np.testing.assert_allclose(restored, 7.0, rtol=1e-4)


def test_processor_deconvolves_each_plane_of_a_stack():
    plane = _blur(_beads(), _otf_for((64, 64))).astype(np.float32)
    stack = np.stack([plane, 2 * plane])
    source = _calibrated(stack, ("T", "Y", "X"))

    out = RichardsonLucyProcessor().apply(source, {**RichardsonLucyProcessor.default_params(), "iterations": 5})

    assert out.data.shape == stack.shape
    assert list(out.axis_labels) == ["T", "Y", "X"]
    np.testing.assert_allclose(out.data[1], 2 * out.data[0], rtol=1e-3, atol=1e-3)
    assert out.metadata["pixel_size_nm"] == [pytest.approx(PIXEL_NM)] * 2


def test_y_x_need_not_be_the_last_axes():
    plane = _blur(_beads(), _otf_for((64, 64))).astype(np.float32)
    params = {**RichardsonLucyProcessor.default_params(), "iterations": 3}
    last = RichardsonLucyProcessor().apply(_calibrated(plane[None], ("T", "Y", "X")), params)
    source = ArrayProcessingResult(
        name="yxc", data=plane[:, :, None], axis_labels=["Y", "X", "C"],
        axis_scales=[PIXEL_NM / 1000.0, PIXEL_NM / 1000.0, 1.0], scale_unit="um",
    )

    out = RichardsonLucyProcessor().apply(source, params)

    np.testing.assert_allclose(out.data[:, :, 0], last.data[0], rtol=1e-5)


def test_uncalibrated_result_needs_a_pixel_size():
    source = ArrayProcessingResult(name="raw", data=_beads(), axis_labels=["Y", "X"])
    params = dict(RichardsonLucyProcessor.default_params())

    with pytest.raises(ValueError, match="Pixel size"):
        RichardsonLucyProcessor().apply(source, params)

    out = RichardsonLucyProcessor().apply(source, {**params, "pixel_size_nm": PIXEL_NM, "iterations": 2})
    assert out.data.shape == (64, 64)
