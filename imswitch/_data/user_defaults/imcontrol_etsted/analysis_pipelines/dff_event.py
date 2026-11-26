"""etSTED analysis pipeline: a local rise in fluorescence (delta F / F).

Detects a place where the widefield camera frame has become brighter than it
was a moment ago, and hands it to etSTED as the centre of the slow scan.

What it does, per frame
-----------------------
1. Bin the frame by ``bin_px`` (mean) and smooth it a little (``smooth_px``,
   in binned pixels). Binning is what makes this cheap: the work is done on
   a map a sixteenth of the size at the default of 4.
2. Compare it with a per-pixel baseline, ``dF/F = (F - baseline) /
   max(baseline, min_baseline)``. The baseline is a running mean over
   ``baseline_frames`` frames (an exponential average once that many have
   arrived) that is updated ONLY at pixels that are quiet (dF/F below the
   threshold). A rising object therefore cannot pull its own baseline up and
   hide itself, while slow drift, bleaching and illumination changes are
   followed, because the quiet pixels keep averaging them in.
3. The first ``warmup_frames`` frames only build the baseline.
4. Candidates are the local maxima of dF/F above ``dff_threshold`` inside the
   binary mask (if one was recorded), strongest first. etSTED scans only the
   FIRST row; the others are logged. At most 5 are returned.

State lives in ``exinfo`` (a dict); etSTED hands it back on the next frame
and clears it after a triggered scan, so the baseline is rebuilt after each
scan from the (by then re-lit) sample.

Parameters (the panel makes one field of each; all arrive as numbers)
---------------------------------------------------------------------
dff_threshold    candidates need dF/F above this (0.5 = 50 % brighter). Raise
                 it until a quiet sample never triggers.
bin_px           binning factor, whole pixels (4). The reported position has
                 this resolution; the scan field is much larger than a pixel.
baseline_frames  time constant of the baseline in frames (20). Short follows
                 bleaching better, long hides slow events less.
smooth_px        Gaussian sigma in BINNED pixels (1.0); 0 = off.
min_baseline     floor of the denominator in camera counts above the
                 camera offset... in counts (20). It stops dark pixels, where
                 dF/F of noise is huge, from triggering. Set it to a few times
                 the camera's dark level plus the noise you see in the dark
                 parts of the sample.
warmup_frames    frames that only build the baseline (10). etSTED itself
                 ignores its first 5 frames.

Coordinates returned
--------------------
Rows of ``(c1, c2) = (column, row)`` of the FULL-resolution frame array
``img`` -- c1 is the pixel's x (index along axis 1, to the right), c2 its y
(index along axis 0, downwards) -- as the centre of the binned pixel, in
pixels with the first pixel's centre at 0.5 (a binned pixel ``j`` covers
full pixels ``j*bin_px`` to ``(j+1)*bin_px`` and is reported at
``(j + 0.5) * bin_px``).

That is the order the Transform calibration window fits: it shows the
low-resolution image transposed (``FLIP_WF_CALIB``), so the point you click
has ``(dim0, dim1) = (column, row)`` of the frame. The transform therefore
takes ``c1 = column`` and ``c2 = row``, and its first output drives the first
active scan axis of the loaded scan (X on a standard setup), its second
output the second (Y). The coefficient order is documented in
``imswitch.imcontrol.model.EtSTEDTransformService`` and written from an
affine map by ``imswitch/imcontrol/model/etsted_affine.py``.

Test modes (TestVisualize / TestValidate) also return the dF/F map (binned
resolution) as the analysis image.
"""
from collections import deque  # noqa: F401  (documents the type of prev_frames)

import numpy as np
from scipy import ndimage as ndi

MAX_CANDIDATES = 5


def dff_event(img, prev_frames, binary_mask, testmode, exinfo,
              dff_threshold=0.5, bin_px=4, baseline_frames=20, smooth_px=1.0,
              min_baseline=20.0, warmup_frames=10):
    b = max(1, int(round(bin_px)))
    height, width = img.shape[0] // b * b, img.shape[1] // b * b
    if height == 0 or width == 0:
        raise ValueError(f'dff_event: a {img.shape} frame cannot be binned by {b}.')
    small = img[:height, :width].reshape(height // b, b, width // b, b).mean(
        axis=(1, 3), dtype=np.float32)
    if smooth_px > 0:
        small = ndi.gaussian_filter(small, float(smooth_px))

    state = exinfo
    if (state is None or not isinstance(state, dict)
            or state['baseline'].shape != small.shape or state['bin_px'] != b):
        state = {'baseline': small.copy(), 'n': 1, 'bin_px': b}
        return _result(np.empty((0, 2)), state, testmode, np.zeros_like(small))

    baseline = state['baseline']
    dff = (small - baseline) / np.maximum(baseline, np.float32(max(min_baseline, 1e-6)))

    allowed = None
    if binary_mask is not None:
        mask = np.asarray(binary_mask)
        if mask.shape != img.shape:
            raise ValueError(
                f'dff_event: the binary mask {mask.shape} does not fit the frame '
                f'{img.shape}; record it again with the same detector.')
        allowed = mask[:height, :width].reshape(height // b, b, width // b, b).mean(
            axis=(1, 3)) >= 0.5
        dff = np.where(allowed, dff, 0.0).astype(np.float32, copy=False)

    # Quiet pixels average in: 1/n while the baseline is young, then 1/tau.
    n = state['n']
    rate = np.float32(1.0 / min(n + 1, max(float(baseline_frames), 1.0)))
    quiet = dff < dff_threshold
    baseline += np.where(quiet, (small - baseline) * rate, np.float32(0.0))
    state['n'] = n + 1

    coords = np.empty((0, 2))
    if state['n'] > int(warmup_frames):
        coords = _candidates(dff, dff_threshold, b)
    return _result(coords, state, testmode, dff)


def _candidates(dff, threshold, b):
    above = dff > threshold
    if not above.any():
        return np.empty((0, 2))
    peaks = above & (dff >= ndi.maximum_filter(dff, size=3, mode='nearest'))
    rows, cols = np.nonzero(peaks)
    order = np.argsort(-dff[rows, cols], kind='stable')[:MAX_CANDIDATES]
    return np.column_stack([(cols[order] + 0.5) * b, (rows[order] + 0.5) * b])


def _result(coords, state, testmode, dff):
    if testmode:
        return coords, state, dff
    return coords, state
