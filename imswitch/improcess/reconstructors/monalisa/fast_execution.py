"""Low-overhead Gaussian extraction, with optional benchmark overrides.

No Qt or acquisition-layout dependencies: a frame group is a set of frame
indices for one in-plane scan, regardless of acquisition traversal order.
"""

from __future__ import annotations

import numpy as np

from .extraction import ExtractionResult

# Keep the mature Fast-Gauss batching policy: transfer roughly 256 MiB of
# camera frames per extraction call rather than launching one GPU solve per
# T/Z stack.
FAST_EXTRACTION_CHUNK_BYTES = 256 * 1024**2


def _extract_fast_recording(
    frames: np.ndarray,
    operator,
    device: str,
    report,
    check_cancelled,
    *,
    amplitude_only: bool = True,
    frame_groups=None,
) -> ExtractionResult:
    """Extract with identical result ordering regardless of batching policy.

    ``frame_groups`` is a set of index arrays, one for each physical in-plane
    scan. It changes solver call boundaries only, never calibration ordering.
    """
    frames = np.asarray(frames)
    if frames.ndim != 3 or frames.shape[0] == 0:
        raise ValueError("Fast extraction expects a non-empty frame stack")
    total = int(frames.shape[0])
    per_frame = max(int(frames[0].nbytes), 1)
    chunk_frames = max(1, int(FAST_EXTRACTION_CHUNK_BYTES // per_frame))
    # Avoid materializing an index array for a potentially huge timelapse.
    # Contiguous Auto batches use a view of the raw frames, not a copy.
    groups = frame_groups if frame_groups is not None else ()
    amplitude = background = haze = noise = coefficients = None
    seen = np.zeros(total, dtype=bool)
    completed = 0
    if frame_groups is None:
        batches = (
            (np.arange(start, min(start + chunk_frames, total)),
             frames[start:min(start + chunk_frames, total)])
            for start in range(0, total, chunk_frames)
        )
    else:
        def grouped_batches():
            for group in groups:
                indices = np.asarray(group, dtype=int).ravel()
                for offset in range(0, len(indices), chunk_frames):
                    subset = indices[offset:offset + chunk_frames]
                    yield subset, frames[subset]
        batches = grouped_batches()
    for batch_index, batch in batches:
        indices = batch_index
        if indices.size == 0:
            continue
        if (np.any(indices < 0) or np.any(indices >= total)
                or np.unique(indices).size != indices.size or np.any(seen[indices])):
            raise ValueError("Invalid/duplicate extraction frame group")
        seen[indices] = True
        check_cancelled()
        report("allocate", completed, total,
               f"Extracting Gaussian amplitudes {completed + 1}-"
               f"{completed + len(batch_index)} of {total}")
        part = (operator.apply_fast_device(batch, device) if amplitude_only
                else operator.apply_device(batch, device))
        if amplitude is None:
            amplitude = np.empty((total, part.amplitude.shape[1]), dtype=part.amplitude.dtype)
            if part.background is not None:
                background = np.empty((total, part.background.shape[1]), dtype=part.background.dtype)
            if part.haze is not None:
                haze = np.empty((total, part.haze.shape[1]), dtype=part.haze.dtype)
            noise = np.empty((total,), dtype=part.noise_sigma.dtype)
            coefficients = np.empty((total, part.coefficients.shape[1]),
                                    dtype=part.coefficients.dtype)
        elif ((background is None) != (part.background is None)
              or (haze is None) != (part.haze is None)
              or coefficients.shape[1] != part.coefficients.shape[1]):
            raise ValueError("Inconsistent extracted coefficient shapes")
        amplitude[batch_index] = part.amplitude
        if background is not None:
            background[batch_index] = part.background
        if haze is not None:
            haze[batch_index] = part.haze
        noise[batch_index] = part.noise_sigma
        coefficients[batch_index] = part.coefficients
        completed += len(batch_index)
    if not np.all(seen):
        raise ValueError("Extraction groups do not cover the complete recording")
    report("allocate", total, total, "Gaussian extraction complete")
    return ExtractionResult(amplitude=amplitude, background=background, haze=haze,
                            noise_sigma=noise, coefficients=coefficients)
