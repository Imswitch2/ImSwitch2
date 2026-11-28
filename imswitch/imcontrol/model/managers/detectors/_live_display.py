import time

import numpy as np


class LiveDisplayThrottle:
    """Deterministic wall-clock rate limiter for live scan-preview redraws.

    The point-scan detectors (APD, PMT) fill the image one scan line at a time
    on the GUI thread. They used to push the whole in-progress image to napari
    on a random subset of lines::

        if np.random.rand() < min(500 / sum(image.shape), 0.05):
            self.sigImageUpdated.emit(...)

    That gate depends on the image *shape*, not on wall-clock time, so the
    redraw rate is uncontrolled: a fast, small scan floods the GUI with
    full-image redraws (laggy UI) while a slow, large scan barely refreshes
    (choppy preview). It is also non-deterministic, which makes the behaviour
    impossible to test.

    This helper caps the live redraw rate at a fixed interval regardless of
    line rate or image size. The first call after construction / ``reset()``
    always fires so the preview appears immediately.
    """

    def __init__(self, min_interval_s: float = 0.05, clock=time.monotonic):
        self._min_interval_s = max(0.0, float(min_interval_s))
        self._clock = clock
        self._last = None

    @property
    def interval_s(self) -> float:
        """The least time between two redraws, in seconds."""
        return self._min_interval_s

    def due(self) -> bool:
        """Return True if enough time has elapsed to emit another redraw.

        Returns True (and arms the next interval) when called for the first
        time or when at least ``min_interval_s`` has passed since the last
        True. Otherwise returns False so the caller skips the redraw.
        """
        now = self._clock()
        if self._last is None or (now - self._last) >= self._min_interval_s:
            self._last = now
            return True
        return False

    def reset(self) -> None:
        """Forget the last emit time so the next ``due()`` fires immediately.

        Call at the start of each scan so the first line of a new frame always
        refreshes the preview, even if it lands within one interval of the
        previous scan's final redraw.
        """
        self._last = None


def display_from_raw(raw, linestep, mode, view_index, display_ndim, write_index=None):
    """The display image of a point-scan buffer, and where the scan writes in it.

    ``raw`` is the manager's accumulation buffer, ``(...outer..., Ny, Nx)``,
    with the line steps outermost (``(S, ...outer..., Ny, Nx)``) when
    ``linestep`` > 1. The image is what a frame boundary has always published:
    the buffer squeezed, and with line steps reduced by ``mode`` (``'max'``,
    ``'sum'``, ``'slice'`` at ``view_index``; None keeps them as a dimension),
    then cut down to ``display_ndim`` dimensions and never below 2 (a
    single-line scan is a ``(1, N)`` image).

    ``write_index`` is the index of the line being written into ``raw``'s
    leading axes (all but the last two). Returned with the image is the same
    position in the image's leading axes -- the plane the viewer follows --
    or None when there is none (no index given, or no leading axes left).
    Without a reduction the image is a view of ``raw``, as before.
    """
    raw = np.asarray(raw)
    leading = list(raw.shape[:-2])
    index = None if write_index is None else [int(v) for v in write_index]
    if index is not None and len(index) != len(leading):
        index = None

    # squeeze: singleton axes go, and with them their (zero) index
    keep = [axis for axis, size in enumerate(raw.shape) if size != 1]
    im = np.squeeze(raw)
    if index is not None:
        index = [index[axis] for axis in range(len(leading)) if axis in keep]

    if int(linestep) > 1:
        if mode in ('max', 'sum', 'slice'):
            if mode == 'max':
                im = np.nanmax(im, axis=0)
            elif mode == 'sum':
                im = np.nansum(im, axis=0)
            else:
                im = im[int(view_index) % im.shape[0]]
            if index is not None:
                index = index[1:]

    while im.ndim > int(display_ndim) and im.ndim > 2:
        im = np.squeeze(im[0])
        if index is not None:
            index = index[1:]
    if im.ndim < 2:
        im = im.reshape(1, -1)
        index = None
    if index is not None:
        index = index[:max(0, im.ndim - 2)]
        if not index:
            index = None
    return im, None if index is None else tuple(index)
