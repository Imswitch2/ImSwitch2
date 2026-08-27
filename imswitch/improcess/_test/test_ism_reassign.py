"""Focused tests for ISM reassignment backends and MoNaLISA integration."""

import importlib.util
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess.reconstructors.ism_reassign.kernel import (
    gpu_available,
    patternfinder_to_xrecon,
    reconstruct_ism,
    reconstruct_ism_cpu,
    reconstruct_ism_gpu,
)
from imswitch.improcess.live import InMemoryStackWrapper
from imswitch.improcess.reconstructors import available_reconstructor_ids
from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor


def test_ism_reassign_is_registered():
    assert "ism-reassign" in available_reconstructor_ids()


def test_patternfinder_to_xrecon_convention():
    # PatternFinder: row_offset, col_offset, row_period, col_period.
    period, phase = patternfinder_to_xrecon((2.5, 3.0, 10.0, 12.0))
    np.testing.assert_allclose(period, [12.0, 10.0])
    np.testing.assert_allclose(phase, [0.75, 0.75])


def test_patternfinder_to_xrecon_wraps_offsets():
    period, phase = patternfinder_to_xrecon((12.5, 15.0, 10.0, 12.0))
    np.testing.assert_allclose(period, [12.0, 10.0])
    np.testing.assert_allclose(phase, [0.75, 0.75])


def test_cpu_kernel_smoke():
    rng = np.random.default_rng(3)
    raw = rng.random((16, 32, 32), dtype=np.float32)
    image, geometry = reconstruct_ism_cpu(
        raw,
        pattern_period=(8.0, 8.0),
        pattern_phase=(0.25, 0.25),
        scanning_orientation="X-Y+",
        psf_fwhm_nm=200.0,
        pixel_size_nm=100.0,
        oversampling=2.0,
        ism_shift=0.5,
    )
    assert image.shape == (geometry.output_pixels, geometry.output_pixels)
    assert image.dtype == np.float32
    assert np.isfinite(image).all()


def test_cpu_dispatcher_matches_cpu_entrypoint():
    rng = np.random.default_rng(7)
    raw = rng.random((16, 32, 32), dtype=np.float32)
    kwargs = dict(
        pattern_period=(8.0, 8.0),
        pattern_phase=(0.25, 0.25),
        scanning_orientation="X-Y+",
        psf_fwhm_nm=200.0,
        pixel_size_nm=100.0,
        oversampling=2.0,
        ism_shift=0.5,
    )
    direct, direct_geometry = reconstruct_ism_cpu(raw, **kwargs)
    dispatched, dispatched_geometry = reconstruct_ism(raw, device="CPU", **kwargs)
    np.testing.assert_array_equal(dispatched, direct)
    assert dispatched_geometry == direct_geometry


def test_gpu_kernel_smoke_when_cuda_available():
    if importlib.util.find_spec("cupy") is None or not gpu_available():
        pytest.skip("CuPy/CUDA not available")

    # Small synthetic stack: the smoke test validates CUDA execution and the
    # output contract, not reconstruction quality.
    rng = np.random.default_rng(3)
    raw = rng.random((16, 32, 32), dtype=np.float32)
    image, geometry = reconstruct_ism_gpu(
        raw,
        pattern_period=(8.0, 8.0),
        pattern_phase=(0.25, 0.25),
        scanning_orientation="X+Y-",
        psf_fwhm_nm=200.0,
        pixel_size_nm=100.0,
        oversampling=2.0,
        ism_shift=0.5,
    )
    assert image.shape == (geometry.output_pixels, geometry.output_pixels)
    assert image.dtype == np.float32
    assert np.isfinite(image).all()



def test_cpu_gpu_kernel_parity_when_cuda_available():
    if importlib.util.find_spec("cupy") is None or not gpu_available():
        pytest.skip("CuPy/CUDA not available")

    rng = np.random.default_rng(11)
    raw = rng.random((16, 32, 32), dtype=np.float32)
    kwargs = dict(
        pattern_period=(8.0, 8.0),
        pattern_phase=(0.25, 0.25),
        scanning_orientation="X-Y+",
        psf_fwhm_nm=200.0,
        pixel_size_nm=100.0,
        oversampling=2.0,
        ism_shift=0.5,
        remove_mean_of_patch=True,
    )
    cpu_image, cpu_geometry = reconstruct_ism_cpu(raw, **kwargs)
    gpu_image, gpu_geometry = reconstruct_ism_gpu(raw, **kwargs)
    np.testing.assert_allclose(gpu_image, cpu_image, rtol=1e-4, atol=1e-4)
    assert gpu_geometry == cpu_geometry



def _monalisa_ism_params(*, timepoints=2, unidirectional=True, device="GPU"):
    return {
        "reconstruction_method": "ISM reassignment",
        "device": device,
        "pixel_size_nm": 100.0,
        "row_offset": 2.0,
        "col_offset": 2.0,
        "row_period": 8.0,
        "col_period": 8.0,
        "psf_fwhm_nm": 200.0,
        "bg_modelling": "Constant",
        "bleaching_correction": False,
        "sweep_enabled": False,
        "ism_reassign_oversampling": 2.0,
        "ism_reassign_shift": 0.5,
        "ism_reassign_remove_mean_of_patch": True,
        "ism_reassign_frame_batch_size": None,
        "scan_params": {
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["pos", "pos", "pos", "pos"],
            "steps": ["4", "4", "1", str(timepoints)],
            "step_sizes": ["200", "200", "1", "1"],
            "unidirectional": unidirectional,
        },
    }


def test_monalisa_ism_uses_worker_policy_only_for_new_method():
    reconstructor = MonalisaReconstructor()
    assert reconstructor.execution_policy_for(
        {"reconstruction_method": "ISM reassignment"}
    ) == "worker"
    assert reconstructor.execution_policy_for(
        {"reconstruction_method": "Fast Gauss MoNaLISA"}
    ) == "inline"


def test_monalisa_scan_params_translate_to_xrecon_orientation():
    reconstructor = MonalisaReconstructor()
    scan_params = {
        "dimensions": ["Up-Down", "Right-Left", "Back-Front", "Timepoints"],
        "directions": ["neg", "pos", "pos", "pos"],
        "unidirectional": False,
    }
    assert reconstructor._scan_params_to_xrecon_orientation(scan_params) == "Y-X+b"



def test_monalisa_ism_transfers_fast_gauss_orientation(monkeypatch):
    from imswitch.improcess.reconstructors.monalisa import reconstructor as recon_module

    reconstructor = MonalisaReconstructor()
    params = _monalisa_ism_params(timepoints=1, unidirectional=False)
    params.update({
        "fast_gauss_footprint_num_rects": 3,
        "fast_gauss_sampling_mode": "Bilinear (legacy)",
    })
    geometry = {
        "nx_s": 4,
        "ny_s": 4,
        "scan_params": dict(params["scan_params"]),
    }
    loc = SimpleNamespace(
        xp=8.0, xo=2.0, yp=8.0, yo=2.0,
        nx_c=2, ny_c=2, num_rows=32, num_cols=32,
    )

    class DummySession:
        @staticmethod
        def _resolve_localization(_data, _params):
            return loc

        @staticmethod
        def _resolve_gaussian_sigma_px(_params):
            return 1.0

        @staticmethod
        def _resolve_pinhole_radius_px(_params, _sigma):
            return None

        @staticmethod
        def _resolve_fit_background(_params):
            return True

    class DummyProcessor:
        @staticmethod
        def process_chunk(data):
            return np.arange(data.shape[0] * 4, dtype=np.float32).reshape(
                data.shape[0], 4
            )

    monkeypatch.setattr(reconstructor, "make_session", lambda: DummySession())
    monkeypatch.setattr(
        recon_module, "make_gauss_processor", lambda *args, **kwargs: DummyProcessor()
    )
    monkeypatch.setattr(recon_module, "get_orientation", lambda *args: "-y+x")
    monkeypatch.setattr(
        recon_module,
        "auto_detect_scan_orientation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Integrated ISM must not use classic MoNaLISA orientation detection")
        ),
    )

    scanning, resolved, label, score = reconstructor._resolve_ism_scan_orientation(
        np.zeros((16, 32, 32), dtype=np.float32),
        params,
        geometry,
        params["scan_params"],
    )

    assert scanning == "Y+X-b"
    assert resolved["dimensions"][:2] == ["Up-Down", "Right-Left"]
    assert resolved["directions"][:2] == ["neg", "pos"]
    assert label == "Fast Gauss -y+x"
    assert np.isfinite(score)


@pytest.mark.parametrize(
    "fast_gauss_orientation, expected",
    [
        ("+x+y", "X-Y-"),
        ("+x-y", "X-Y+"),
        ("-x+y", "X+Y-"),
        ("-x-y", "X+Y+"),
        ("+y+x", "Y-X-"),
        ("+y-x", "Y-X+"),
        ("-y+x", "Y+X-"),
        ("-y-x", "Y+X+"),
    ],
)
def test_fast_gauss_orientation_inverts_signs_for_xrecon(
    fast_gauss_orientation, expected
):
    reconstructor = MonalisaReconstructor()
    assert reconstructor._fast_gauss_orientation_to_xrecon(
        fast_gauss_orientation, bidirectional=False
    ) == expected


def test_fast_gauss_orientation_to_xrecon_preserves_bidirectional_suffix():
    reconstructor = MonalisaReconstructor()
    assert reconstructor._fast_gauss_orientation_to_xrecon(
        "+x-y", bidirectional=True
    ) == "X-Y+b"

def test_monalisa_ism_stacks_timepoints_into_monalisa_result(monkeypatch):
    from imswitch.improcess.reconstructors.ism_reassign import kernel as ism_kernel

    reconstructor = MonalisaReconstructor()
    params = _monalisa_ism_params(timepoints=2)
    raw = np.arange(32 * 32 * 32, dtype=np.float32).reshape(32, 32, 32)
    data_obj = InMemoryStackWrapper(
        name="ism-timelapse", dataset_name="det", data=raw, attrs={}
    )

    def fake_orientation(first_stack, _params, geometry, source_scan_params):
        resolved = dict(geometry["scan_params"])
        resolved["dimensions"] = list(geometry["scan_params"]["dimensions"])
        resolved["directions"] = ["pos", "neg", "pos", "pos"]
        resolved["unidirectional"] = bool(source_scan_params["unidirectional"])
        return "X+Y-", resolved, "R-L+ / U-D-", 12.5

    calls = []

    def fake_reconstruct(raw_stack, **kwargs):
        calls.append((np.asarray(raw_stack).copy(), dict(kwargs)))
        image = np.full((12, 12), len(calls), dtype=np.float32)
        return image, SimpleNamespace(output_pixel_size_nm=37.5)

    monkeypatch.setattr(
        reconstructor, "_resolve_ism_scan_orientation", fake_orientation
    )
    monkeypatch.setattr(ism_kernel, "reconstruct_ism", fake_reconstruct)

    result = reconstructor.process(data_obj, params)

    assert len(calls) == 2
    assert calls[0][0].shape == (16, 32, 32)
    assert calls[1][0].shape == (16, 32, 32)
    assert calls[0][1]["scanning_orientation"] == "X+Y-"
    assert calls[1][1]["scanning_orientation"] == "X+Y-"
    assert calls[0][1]["device"] == "GPU"
    assert calls[1][1]["device"] == "GPU"
    assert result.data.shape == (1, 1, 2, 1, 12, 12)
    np.testing.assert_array_equal(result.data[0, 0, 0, 0], 1.0)
    np.testing.assert_array_equal(result.data[0, 0, 1, 0], 2.0)
    assert result.output_pixel_size_nm == pytest.approx((37.5, 37.5))
    assert result.recon_diagnostics["num_timepoints"] == 2
    assert result.recon_diagnostics["scanning_orientation"] == "X+Y-"



def test_monalisa_ism_passes_cpu_backend_to_shared_dispatcher(monkeypatch):
    from imswitch.improcess.reconstructors.ism_reassign import kernel as ism_kernel

    reconstructor = MonalisaReconstructor()
    params = _monalisa_ism_params(timepoints=1, device="CPU")
    raw = np.zeros((16, 32, 32), dtype=np.float32)
    data_obj = InMemoryStackWrapper(
        name="ism-cpu", dataset_name="det", data=raw, attrs={}
    )

    def fake_orientation(first_stack, _params, geometry, source_scan_params):
        resolved = dict(geometry["scan_params"])
        resolved["dimensions"] = list(geometry["scan_params"]["dimensions"])
        resolved["directions"] = list(geometry["scan_params"]["directions"])
        return "X-Y+", resolved, "Fast Gauss +x-y", 1.0

    calls = []

    def fake_reconstruct(raw_stack, **kwargs):
        calls.append(dict(kwargs))
        return np.zeros((12, 12), dtype=np.float32), SimpleNamespace(
            output_pixel_size_nm=37.5
        )

    monkeypatch.setattr(
        reconstructor, "_resolve_ism_scan_orientation", fake_orientation
    )
    monkeypatch.setattr(ism_kernel, "reconstruct_ism", fake_reconstruct)

    result = reconstructor.process(data_obj, params)

    assert len(calls) == 1
    assert calls[0]["device"] == "CPU"
    assert result.recon_diagnostics["backend"] == "CPU"



def test_monalisa_tiff_save_prefers_reconstructed_pixel_size(monkeypatch, tmp_path):
    from imswitch.improcess.reconstructors.monalisa import result as result_module
    from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult

    captured = {}

    class FakeWriter:
        def __init__(self, path, **kwargs):
            captured["path"] = path
            captured["init"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, data, **kwargs):
            captured["shape"] = np.asarray(data).shape
            captured["write"] = kwargs

    monkeypatch.setattr(result_module.tiff, "TiffWriter", FakeWriter)
    result = MonalisaProcessingResult(
        name="ism",
        data=np.zeros((1, 1, 2, 1, 8, 8), dtype=np.float32),
        scan_params={
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["pos", "pos", "pos", "pos"],
            "steps": ["4", "4", "1", "2"],
            "step_sizes": ["200", "300", "1", "1"],
            "unidirectional": True,
        },
        output_pixel_size_nm=(40.0, 50.0),
    )

    result.save(tmp_path / "ism.tif")

    assert captured["write"]["resolution"] == pytest.approx((200.0, 250.0))
    assert captured["shape"] == (2, 1, 1, 8, 8)
