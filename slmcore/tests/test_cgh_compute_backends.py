import numpy as np
import pytest

from slmcore.core.cgh.computations.array_backend import (
    CPU_BACKEND,GPU_BACKEND,available_backends,default_backend,get_array_module,
    gpu_available,
)
from slmcore.core.cgh.computations.direct_summation import (
    DIRECT_SUMMATION_PARAMS,_direct_spot_wgs,
)
from slmcore.core.cgh.computations.gerchberg_saxton import (
    GERCHBERG_SAXTON_PARAMS,_run_gerchberg_saxton,
)


def _raster_target(shape=(16,16)):
    target = np.zeros(shape,dtype=np.float64)
    target[4,5] = 1.0
    target[11,10] = 0.7
    target[7,12] = 0.45
    return target


def test_registered_backend_choices_follow_startup_gpu_probe():
    expected = available_backends()
    expected_default = default_backend()

    assert expected == ((CPU_BACKEND,GPU_BACKEND) if gpu_available() else (CPU_BACKEND,))
    assert expected_default == (GPU_BACKEND if gpu_available() else CPU_BACKEND)

    for specs in (GERCHBERG_SAXTON_PARAMS,DIRECT_SUMMATION_PARAMS):
        backend = specs["backend"]
        assert backend.choices == expected
        assert backend.default == expected_default
        assert backend.default in backend.choices


def test_cpu_gerchberg_saxton_reports_explicit_backend_without_warning():
    output = _run_gerchberg_saxton(
        target_intensity=_raster_target(),
        compute_params={
            "backend":CPU_BACKEND,
            "n_iterations":4,
            "weighted_gs":True,
            "phase_fixing":True,
            "phase_fixing_value":2,
        },
        initial_field=None,
    )

    assert output.diagnostics["backend"] == "numpy"
    assert output.warnings == ()
    assert output.pattern.shape == (16,16)
    assert output.target_phase.shape == (16,16)
    assert np.allclose(np.abs(output.pattern),1.0,rtol=0.0,atol=1e-12)
    assert len(output.metrics) == 4


def test_direct_summation_cpu_is_intentional_and_has_no_cuda_fallback_warning():
    output = _direct_spot_wgs(
        spot_positions_kxy=np.array(
            [[0.05,-0.07,0.11],[-0.08,0.03,0.09]],dtype=np.float32
        ),
        spot_intensities=np.array([1.0,0.8,0.6],dtype=np.float64),
        shape=(12,14),
        compute_params={
            "backend":CPU_BACKEND,
            "n_iterations":2,
            "weighted_gs":True,
        },
        initial_field=None,
    )

    assert output.diagnostics["backend"] == "numpy"
    assert output.warnings == ()


def test_gpu_backend_never_silently_falls_back(monkeypatch):
    import slmcore.core.cgh.computations.array_backend as backend_module

    monkeypatch.setattr(backend_module,"gpu_available",lambda:False)
    with pytest.raises(RuntimeError,match="GPU backend was selected"):
        get_array_module(GPU_BACKEND)


@pytest.mark.skipif(not gpu_available(),reason="CUDA/CuPy backend unavailable")
def test_gpu_gerchberg_saxton_matches_cpu_target_response():
    target = _raster_target((24,24))
    params = {
        "n_iterations":5,
        "weighted_gs":True,
        "phase_fixing":True,
        "phase_fixing_value":3,
        "seed":1,
    }
    cpu = _run_gerchberg_saxton(
        target_intensity=target,
        compute_params={**params,"backend":CPU_BACKEND},
        initial_field=None,
    )
    gpu = _run_gerchberg_saxton(
        target_intensity=target,
        compute_params={**params,"backend":GPU_BACKEND},
        initial_field=None,
    )

    support = target > 0
    cpu_intensity = np.abs(np.fft.fftshift(np.fft.fft2(cpu.pattern)))**2
    gpu_intensity = np.abs(np.fft.fftshift(np.fft.fft2(gpu.pattern)))**2
    cpu_response = cpu_intensity[support] / np.mean(cpu_intensity[support])
    gpu_response = gpu_intensity[support] / np.mean(gpu_intensity[support])

    assert gpu.diagnostics["backend"] == "cupy"
    assert np.allclose(cpu_response,gpu_response,rtol=2e-3,atol=2e-3)
    assert np.allclose(cpu.target_phase,gpu.target_phase,rtol=2e-3,atol=2e-3)
