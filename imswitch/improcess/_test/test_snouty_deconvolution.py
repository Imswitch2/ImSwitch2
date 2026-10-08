"""Tests for the SNOUTY deconvolution reconstructor plugin."""

import os

import h5py
import numpy as np
import pytest
from scipy import ndimage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model import DataObj
from imswitch.improcess.reconstructors.snouty.deskew_cpu import (
    DeskewProcessorCPU,
    build_deskew_transform,
)
from imswitch.improcess.reconstructors.snouty.metadata import DEFAULT_PARAMS
from imswitch.improcess.reconstructors.snouty_deconvolution import (
    SnoutyDeconvolutionReconstructor,
)
from imswitch.improcess.reconstructors.snouty_deconvolution.deconvolve import (
    DeconvolutionProcessorCPU,
    DeconvolutionProcessorGPU,
)
from imswitch.improcess.reconstructors.snouty_deconvolution.defaults import (
    DECONVOLUTION_DEFAULTS,
)
from imswitch.improcess.reconstructors.snouty_deconvolution.kernel import (
    crop_to_support,
    effective_kernel,
    light_sheet_profile,
    pad_to_odd,
)
from imswitch.improcess.reconstructors.snouty_deconvolution.psf import (
    richards_wolf_psf,
    richards_wolf_radial,
)

GEOMETRY = dict(
    c_px=116.0, alpha_deg=35.0, dy=210.0, sample_vx_size=200.0,
    camera_offset=100.0, flip_data=False,
)


def _small_kernel(shape=(5, 7, 5)):
    axes = [np.arange(n) - (n - 1) / 2 for n in shape]
    zz, yy, xx = np.meshgrid(*axes, indexing="ij")
    kernel = np.exp(-(zz ** 2 / 2 + yy ** 2 / 3 + xx ** 2 / 2) / 2).astype(np.float32)
    return kernel / kernel.sum()


def _phantom(processor):
    """A blob plus two points on the processor's output grid, and its camera stack."""
    z, y, x = np.meshgrid(*(np.arange(n) for n in processor.out_shape), indexing="ij")
    true = 500 * np.exp(-(((z - 5) / 1.5) ** 2 + ((y - 20) / 4) ** 2 + ((x - 6) / 2) ** 2) / 2)
    true = true.astype(np.float32)
    true[3, 10, 3] = 3000
    true[8, 32, 9] = 3000
    simulated = processor.forward(true)
    cam = np.zeros(processor._mask.shape, np.float32)
    cam[processor._mask] = simulated
    stack = np.transpose(cam, (1, 0, 2)) + GEOMETRY["camera_offset"]
    return true, stack


def _corr(a, b):
    return float(np.corrcoef(np.ravel(a), np.ravel(b))[0, 1])


def _poisson_divergence(data, model):
    model = np.maximum(model, 1e-12)
    logterm = np.where(data > 0, data * np.log(np.maximum(data, 1e-12) / model), 0.0)
    return float(np.sum(model - data + logterm))


@pytest.fixture
def processor():
    proc = DeconvolutionProcessorCPU(GEOMETRY, _small_kernel())
    proc.camera_values(np.full((24, 32, 20), 100.0, np.float32))
    return proc


class _OriginalKirchhoffSimpson:
    """Verbatim port of Deconvolution_GUI's adaptive integrator, the reference for the PSF."""

    def __init__(self, defocus, ni, na, wavelength):
        import math

        self.math = math
        self.defocus, self.ni, self.na, self.wavelength = defocus, ni, na, wavelength
        self.tol, self.k_required = 0.1, 5

    def integrand(self, theta, r):
        from scipy.special import jn

        m = self.math
        s, c = m.sin(theta), m.cos(theta)
        sq = m.sqrt(c) * s
        k = 2 * m.pi * self.ni / (self.wavelength * 1e-9)
        x = k * s * r
        b0, b1 = sq * (1 + c) * jn(0, x), sq * s * jn(1, x)
        b2 = sq * (1 - c) * jn(2, x) if x != 0 else 0.0
        w = k * self.defocus * c
        cw, sw = m.cos(w), m.sin(w)
        return np.array([[b0 * cw, b0 * sw], [b1 * cw, b1 * sw], [b2 * cw, b2 * sw]])

    def _integral(self, va, even, odd, vb, d):
        re = va[:, 0] + 2 * even[:, 0] + 4 * odd[:, 0] + vb[:, 0]
        im = va[:, 1] + 2 * even[:, 1] + 4 * odd[:, 1] + vb[:, 1]
        return (re[0] ** 2 + im[0] ** 2 + 2 * (re[1] ** 2 + im[1] ** 2) + re[2] ** 2 + im[2] ** 2) * d ** 2

    def calculate(self, r):
        b = self.math.asin(self.na / self.ni)
        n, d, k, iteration = 2, b / 2, 0, 1
        even, odd = np.zeros((3, 2)), self.integrand(b / 2, r)
        va, vb = self.integrand(0.0, r), self.integrand(b, r)
        current = self._integral(va, even, odd, vb, d)
        previous = current
        while k < self.k_required and iteration < 10000:
            iteration += 1
            n *= 2
            d /= 2
            even += odd
            odd = np.zeros((3, 2))
            for i in range(1, n, 2):
                odd += self.integrand(i * d, r)
            current = self._integral(va, even, odd, vb, d)
            difference = abs(previous - current) / (current if current != 0 else 1e-5)
            k = k + 1 if difference <= self.tol else 0
            previous = current
        return current


class TestPSF:
    def test_radial_profile_matches_the_original_integrator(self):
        radii = np.array([0.0, 100.0, 250.0, 800.0]) * 1e-9
        defocus = np.array([0.0, 500.0]) * 1e-9
        mine = richards_wolf_radial(radii, defocus, 1.1, 510.0, 1.5)
        original = np.array([
            [_OriginalKirchhoffSimpson(z, 1.5, 1.1, 510.0).calculate(r) for r in radii]
            for z in defocus
        ])
        mine = mine / mine[0, 0]
        original = original / original[0, 0]
        assert np.allclose(mine, original, rtol=1e-4, atol=1e-6)

    def test_richards_wolf_psf_is_centred_symmetric_and_diffraction_sized(self):
        psf = richards_wolf_psf(41, 100.0, 1.1, 510.0, 1.5)
        assert psf.shape == (41, 41, 41)
        assert psf.dtype == np.float32
        assert np.unravel_index(psf.argmax(), psf.shape) == (20, 20, 20)
        assert psf.max() == pytest.approx(1.0)
        assert np.allclose(psf, psf[::-1], rtol=1e-5, atol=1e-6)
        assert np.allclose(psf, psf[:, ::-1], rtol=1e-5, atol=1e-6)
        assert np.allclose(psf, np.swapaxes(psf, 1, 2), rtol=1e-5, atol=1e-6)
        lateral = psf[20, 20]
        axial = psf[:, 20, 20]
        lateral_fwhm = np.count_nonzero(lateral >= 0.5) * 100.0
        axial_fwhm = np.count_nonzero(axial >= 0.5) * 100.0
        assert 200.0 <= lateral_fwhm <= 400.0
        assert 600.0 <= axial_fwhm <= 1400.0
        assert axial_fwhm > lateral_fwhm

    def test_psf_is_cached_per_parameter_set(self):
        first = richards_wolf_psf(9, 200.0, 1.1, 510.0, 1.5)
        assert richards_wolf_psf(9, 200.0, 1.1, 510.0, 1.5) is first

    def test_na_above_immersion_index_is_rejected(self):
        with pytest.raises(ValueError):
            richards_wolf_psf(9, 200.0, 1.6, 510.0, 1.5)


class TestKernel:
    def test_sheet_lies_in_the_camera_plane(self):
        """The sheet's extent along the tilted camera row direction exceeds that along its normal."""
        shape = (31, 31, 5)
        sheet = light_sheet_profile(shape, 35.0, 200.0, 1200.0, 1200.0, 0.0)
        alpha = np.deg2rad(35.0)
        centre = 15
        along = [sheet[int(round(centre + t * np.sin(alpha))), int(round(centre + t * np.cos(alpha))), 2]
                 for t in range(-8, 9)]
        across = [sheet[int(round(centre + t * np.cos(alpha))), int(round(centre - t * np.sin(alpha))), 2]
                  for t in range(-8, 9)]
        assert min(along) > 0.9
        assert min(across) < 0.05
        assert sheet[centre, centre, 2] == pytest.approx(1.0)

    def test_effective_kernel_is_odd_unit_sum_and_non_negative(self):
        params = {**GEOMETRY, **DECONVOLUTION_DEFAULTS, "psf_size_px": 21}
        kernel = effective_kernel(params)
        assert kernel.ndim == 3
        assert all(n % 2 == 1 for n in kernel.shape)
        assert kernel.min() >= 0
        assert kernel.sum() == pytest.approx(1.0, abs=1e-5)
        assert np.unravel_index(kernel.argmax(), kernel.shape) == tuple(n // 2 for n in kernel.shape)

    def test_pixel_footprint_is_applied_for_large_camera_pixels(self):
        """A 116 nm pixel on 50 nm voxels folds the pixel footprint in; still odd, unit sum."""
        params = {
            **GEOMETRY, **DECONVOLUTION_DEFAULTS,
            "sample_vx_size": 50.0, "psf_size_px": 41,
        }
        kernel = effective_kernel(params)
        assert all(n % 2 == 1 for n in kernel.shape)
        assert kernel.sum() == pytest.approx(1.0, abs=1e-5)
        assert kernel.min() >= 0
        assert kernel.shape[0] > kernel.shape[2]

    def test_crop_and_pad(self):
        volume = np.zeros((6, 6, 6))
        volume[1:4, 2:5, 0:2] = 1.0
        cropped = crop_to_support(volume, 0.5)
        assert cropped.shape == (3, 3, 2)
        assert pad_to_odd(cropped).shape == (3, 3, 3)
        assert pad_to_odd(np.ones((3, 5, 7))).shape == (3, 5, 7)

    def test_psf_file_is_loaded(self, tmp_path):
        import tifffile

        psf = richards_wolf_psf(11, 200.0, 1.1, 510.0, 1.5)
        path = tmp_path / "psf.tif"
        tifffile.imwrite(str(path), psf)
        params = {**GEOMETRY, **DECONVOLUTION_DEFAULTS, "psf_path": str(path), "psf_size_px": 41}
        generated = effective_kernel({**params, "psf_path": "", "psf_size_px": 11})
        loaded = effective_kernel(params)
        assert loaded.shape == generated.shape
        assert np.allclose(loaded, generated, atol=1e-6)


class TestOperators:
    def test_output_grid_matches_the_deskew(self, processor):
        deskew = DeskewProcessorCPU(GEOMETRY)
        assert np.allclose(processor.M, deskew.M)
        assert processor.out_shape == deskew.process_stack(np.zeros((24, 32, 20), np.float32)).shape
        assert processor.output_shape((24, 32, 20)) == processor.out_shape

    def test_shared_transform_is_the_documented_matrix(self):
        alpha = np.deg2rad(35.0)
        M = build_deskew_transform(116.0, alpha, 210.0, 200.0)
        expected = np.array([
            [116 * np.sin(alpha), 0.0, 0.0],
            [116 * np.cos(alpha), 210.0, 0.0],
            [0.0, 0.0, 116.0],
        ]) / 200.0
        assert np.allclose(M, expected)

    def test_forward_matches_the_gui_kernel_definition(self, processor):
        """The GUI's convTransform: kernel[-k-1] summed over the neighbourhood of the rounded coordinate."""
        rng = np.random.default_rng(0)
        x = rng.random(processor.out_shape).astype(np.float32)
        gui = ndimage.correlate(x, processor.kernel[::-1, ::-1, ::-1], mode="constant", cval=0.0)
        expected = gui.ravel()[processor._flat_idx]
        assert np.allclose(processor.forward(x), expected, rtol=1e-4, atol=1e-5)

    def test_adjoint_is_exact(self, processor):
        rng = np.random.default_rng(1)
        x = rng.random(processor.out_shape).astype(np.float32)
        y = rng.random(processor._flat_idx.shape).astype(np.float32)
        lhs = float(np.dot(processor.forward(x), y))
        rhs = float(np.sum(x * processor.adjoint(y)))
        assert lhs == pytest.approx(rhs, rel=1e-5)

    def test_even_kernel_is_rejected(self):
        with pytest.raises(ValueError):
            DeconvolutionProcessorCPU(GEOMETRY, np.ones((4, 5, 5), np.float32))

    def test_flip_data_reverses_the_plane_order(self):
        proc = DeconvolutionProcessorCPU({**GEOMETRY, "flip_data": True}, _small_kernel())
        stack = np.arange(24 * 32 * 20, dtype=np.float32).reshape(24, 32, 20) + 100
        flipped = proc.camera_values(stack)
        straight = DeconvolutionProcessorCPU(GEOMETRY, _small_kernel()).camera_values(stack[::-1])
        assert np.array_equal(flipped, straight)


class TestRichardsonLucy:
    def test_noise_free_phantom_converges_towards_the_truth(self, processor):
        true, stack = _phantom(processor)
        deskewed = DeskewProcessorCPU(GEOMETRY).process_stack(stack)
        data = processor.camera_values(stack)
        divergences = []
        correlations = []
        for iterations in (1, 20, 100):
            estimate = processor.run(stack, iterations)
            divergences.append(_poisson_divergence(data, processor.forward(estimate)))
            correlations.append(_corr(estimate, true))
        assert divergences == sorted(divergences, reverse=True)
        assert correlations[-1] > 0.9
        assert correlations[-1] > _corr(deskewed, true)
        assert estimate.min() >= 0
        assert float(estimate.sum()) == pytest.approx(float(true.sum()), rel=0.02)

    def test_loop_reproduces_the_gui_loop_with_a_fresh_canvas(self, processor):
        """The original update, written with ndimage and a canvas rebuilt per iteration."""
        _true, stack = _phantom(processor)
        stack = np.random.default_rng(7).poisson(stack).astype(np.float32)
        data = processor.camera_values(stack)
        kernel = processor.kernel
        n_out = int(np.prod(processor.out_shape))

        def forward(x):
            return ndimage.correlate(x, kernel[::-1, ::-1, ::-1], mode="constant").ravel()[processor._flat_idx]

        def adjoint(v):
            canvas = np.bincount(processor._flat_idx, weights=v, minlength=n_out).reshape(processor.out_shape)
            return ndimage.correlate(canvas, kernel, mode="constant")

        ht1 = adjoint(np.ones_like(data))
        ht1 = np.maximum(ht1, 0.3 * ht1.max())
        reference = np.ones(processor.out_shape)
        for _ in range(4):
            reference = reference * adjoint(data / np.maximum(forward(reference), 1e-12)) / ht1
        assert np.allclose(processor.run(stack, 4), reference, rtol=1e-4, atol=1e-3)

    def test_adjoint_canvas_is_rebuilt_every_iteration(self, processor):
        """Two iterations from a flat start equal one iteration continued from the first."""
        _true, stack = _phantom(processor)
        two = processor.run(stack, 2)
        first = processor.run(stack, 1)
        continued = processor.run(stack, 1, initial=first)
        assert np.allclose(two, continued, rtol=1e-5, atol=1e-4)

    def test_gradient_consent_runs_and_is_bounded(self):
        proc = DeconvolutionProcessorCPU(GEOMETRY, _small_kernel(), gradient_consent=True, seed=3)
        proc.camera_values(np.full((24, 32, 20), 100.0, np.float32))
        true, stack = _phantom(proc)
        stack = np.random.default_rng(5).poisson(stack).astype(np.float32)
        estimate = proc.run(stack, 10)
        assert estimate.shape == proc.out_shape
        assert np.isfinite(estimate).all()
        assert _corr(estimate, true) > _corr(DeskewProcessorCPU(GEOMETRY).process_stack(stack), true)

    def test_progress_and_cancel_hooks_are_called(self, processor):
        _true, stack = _phantom(processor)
        seen = []
        calls = {"cancel": 0}

        def cancel():
            calls["cancel"] += 1

        processor.run(stack, 3, progress=lambda done, total: seen.append((done, total)), cancel=cancel)
        assert seen == [(1, 3), (2, 3), (3, 3)]
        assert calls["cancel"] == 3

    def test_gpu_backend_without_cupy_raises_a_clear_error(self):
        import importlib.util

        if importlib.util.find_spec("cupy") is not None:
            pytest.skip("cupy installed; the error path does not apply")
        with pytest.raises(RuntimeError, match="cupy"):
            DeconvolutionProcessorGPU(GEOMETRY, _small_kernel())

    def test_gpu_cpu_parity(self, processor):
        import importlib.util

        if importlib.util.find_spec("cupy") is None:
            pytest.skip("cupy not installed")
        try:
            import cupy as cp
            cp.zeros(1)
        except Exception:
            pytest.skip("cupy not usable")
        _true, stack = _phantom(processor)
        gpu = DeconvolutionProcessorGPU(GEOMETRY, _small_kernel())
        cpu_estimate = processor.run(stack, 5)
        gpu_estimate = cp.asnumpy(gpu.run(cp.asarray(stack), 5))
        assert np.allclose(cpu_estimate, gpu_estimate, rtol=1e-3, atol=1e-2)


@pytest.fixture
def data_obj(tmp_path):
    rng = np.random.RandomState(42)
    stack = rng.rand(8, 16, 16).astype(np.float32) * 100.0 + DEFAULT_PARAMS["camera_offset"] + 1.0
    path = tmp_path / "deconv.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("data", data=stack)
    return DataObj(str(path), "deconv")


class _Context:
    def __init__(self):
        self.reports = []
        self.checks = 0

    def report(self, phase, completed, total, message=""):
        self.reports.append((phase, completed, total, message))

    def check_cancelled(self):
        self.checks += 1


class TestReconstructor:
    def _params(self, **overrides):
        params = SnoutyDeconvolutionReconstructor.default_params()
        params.update(psf_size_px=9, iterations=3)
        params.update(overrides)
        return params

    def test_attributes(self):
        plugin = SnoutyDeconvolutionReconstructor()
        assert plugin.id == "snouty-deconvolution"
        assert set(DEFAULT_PARAMS) < set(plugin.default_params())
        assert set(DECONVOLUTION_DEFAULTS) < set(plugin.default_params())

    def test_registered(self):
        from imswitch.improcess.reconstructors import available_reconstructor_ids

        assert "snouty-deconvolution" in available_reconstructor_ids()

    def test_single_timepoint_lands_on_the_deskew_grid(self, data_obj):
        from imswitch.improcess.reconstructors.snouty import SnoutyReconstructor

        params = self._params()
        context = _Context()
        result = SnoutyDeconvolutionReconstructor().process(data_obj, params, context)
        deskew = SnoutyReconstructor().process(data_obj, {k: params[k] for k in SnoutyReconstructor.default_params()})
        assert result.data.shape == deskew.data.shape
        assert result.axis_labels == ["Z", "Y", "X"]
        assert result.data.dtype == np.float32
        assert np.isfinite(result.data).all()
        assert result.data.max() > 0
        assert result.name.endswith("_deconvolved")
        assert result.params["iterations"] == 3
        assert [r[0] for r in context.reports][0] == "allocate"
        assert [r[0] for r in context.reports][-1] == "finalize"
        assert sum(1 for r in context.reports if r[0] == "assemble") == 3
        assert context.checks == 3

    def test_multi_timepoint_is_4d(self, data_obj):
        result = SnoutyDeconvolutionReconstructor().process(data_obj, self._params(n_timepoints=2))
        assert result.data.ndim == 4
        assert result.data.shape[0] == 2
        assert result.axis_labels == ["T", "Z", "Y", "X"]

    def test_bad_geometry_is_rejected_before_any_work(self, data_obj):
        with pytest.raises(ValueError, match="alpha_deg"):
            SnoutyDeconvolutionReconstructor().process(data_obj, self._params(alpha_deg=0.0))
        with pytest.raises(ValueError, match="Iterations"):
            SnoutyDeconvolutionReconstructor().process(data_obj, self._params(iterations=0))

    def test_widget_values_match_default_params(self):
        from qtpy import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        assert app is not None
        widget = SnoutyDeconvolutionReconstructor().make_param_widget(QtWidgets.QWidget())
        assert widget.get_values() == SnoutyDeconvolutionReconstructor.default_params()
        widget.load_from_attrs({"Detector:Cam:Camera pixel size": 0.116})
        assert widget.get_values()["c_px"] == pytest.approx(116.0)


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
