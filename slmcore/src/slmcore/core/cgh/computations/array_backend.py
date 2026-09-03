"""Shared CPU/GPU array backend selection for CGH computations."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import numpy as np


CPU_BACKEND = "CPU"
GPU_BACKEND = "GPU"


@lru_cache(maxsize=1)
def gpu_available() -> bool:
    """Return whether CuPy can execute a minimal CUDA operation.

    The result is cached for the lifetime of the process so registration/UI
    construction does not repeatedly initialize or probe CUDA.
    """
    try:
        import cupy as cp

        if int(cp.cuda.runtime.getDeviceCount()) <= 0:
            return False
        probe = cp.ones((1,),dtype=cp.float32)
        probe *= cp.float32(2.0)
        cp.cuda.get_current_stream().synchronize()
        return bool(float(probe.get()[0]) == 2.0)
    except Exception:
        return False


def available_backends() -> tuple[str,...]:
    """Return backend choices that are safe to expose in the GUI."""
    return (CPU_BACKEND,GPU_BACKEND) if gpu_available() else (CPU_BACKEND,)


def default_backend() -> str:
    """Prefer GPU when it passed the startup availability probe."""
    return GPU_BACKEND if gpu_available() else CPU_BACKEND


def normalize_backend(value: Any) -> str:
    """Normalize one user/config backend value to ``CPU`` or ``GPU``."""
    text = str(value).strip().upper()
    aliases = {
        "CPU":CPU_BACKEND,
        "NUMPY":CPU_BACKEND,
        "GPU":GPU_BACKEND,
        "CUDA":GPU_BACKEND,
        "CUPY":GPU_BACKEND,
    }
    try:
        return aliases[text]
    except KeyError as error:
        raise ValueError(
            f"backend must be one of {available_backends()}, got {value!r}"
        ) from error


def get_array_module(backend: Any):
    """Return ``(xp, using_gpu, diagnostic_name)`` for the selected backend.

    GPU is intentionally not allowed to silently fall back to CPU. The GUI only
    exposes GPU after the startup probe succeeds; if CUDA later becomes
    unusable, failing the computation keeps backend comparisons unambiguous.
    """
    selected = normalize_backend(backend)
    if selected == CPU_BACKEND:
        return np,False,"numpy"

    if not gpu_available():
        raise RuntimeError(
            "GPU backend was selected but CUDA/CuPy is not available in this "
            "process. Select CPU and retry."
        )

    import cupy as cp
    return cp,True,"cupy"


def to_numpy(value: Any,using_gpu: bool) -> np.ndarray:
    """Detach one backend array as a NumPy array."""
    return value.get() if using_gpu else np.asarray(value)


def to_float(value: Any,using_gpu: bool) -> float:
    """Detach one scalar reduction without transferring its source array."""
    if using_gpu:
        return float(value.get())
    return float(value)


__all__ = [
    "CPU_BACKEND",
    "GPU_BACKEND",
    "available_backends",
    "default_backend",
    "get_array_module",
    "gpu_available",
    "normalize_backend",
    "to_float",
    "to_numpy",
]
