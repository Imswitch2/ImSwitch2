import time


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
