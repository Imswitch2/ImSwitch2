import numpy as np

from ..basecontrollers import LiveUpdatedController


class FLIMHistController(LiveUpdatedController):
    """Drives FLIMHistWidget: turns each incoming detector frame into a
    histogram of valid per-pixel lifetimes.

    SwabianTimeTaggerManager writes fitted lifetimes (in seconds) into
    ``_image_display`` and zeros out any pixel whose intensity fell below
    ``min_counts_per_pixel``.  We squeeze the broadcast (1, Ny, Nx) frame,
    drop the zero pixels, convert seconds → nanoseconds and hand the flat
    array to the widget.

    The widget is detector-agnostic — if it's enabled for a non-FLIM
    detector it will simply show whatever > 0 values appear, which is
    meaningless but not harmful.  The user controls activity with the
    "Live update" checkbox on the widget itself.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.active = self._widget.isActive()

        # _current_frame_ns: latest poll's valid lifetimes for the in-progress
        # scan.  Replaced (never appended to) on every update — the Flim object
        # already accumulates photons internally, so successive polls of the
        # same scan are correlated, not independent.
        self._current_frame_ns: np.ndarray = np.empty(0, dtype=np.float32)

        # _accum: one entry per *completed* scan, holding that scan's final
        # valid-pixel lifetime array.  Only grows at scan boundaries so pixels
        # are never double-counted within a scan.
        self._accum: list = []

        # Wire widget signals
        self._widget.sigShowToggled.connect(self._on_show_toggled)
        self._widget.sigAccumulateToggled.connect(self._on_accumulate_toggled)
        self._widget.sigNBinsChanged.connect(self._on_nbins_changed)
        self._widget.sigRangeChanged.connect(self._on_range_changed)

        # Subscribe to detector frames and scan lifecycle
        self._commChannel.sigUpdateImage.connect(self.update)
        self._commChannel.sigScanStarted.connect(self._on_scan_started)
        self._commChannel.sigScanDone.connect(self._on_scan_done)

    # ------------------------------------------------------------------ #
    # Slot implementations                                                 #
    # ------------------------------------------------------------------ #

    def update(self, detectorName, im, init, scale, isCurrentDetector):
        """Receive a detector frame and refresh the histogram."""
        if not self.active or not isCurrentDetector:
            return
        if im is None:
            return

        arr = np.asarray(im)
        if arr.size <= 1:
            return  # placeholder/empty buffer

        # Lifetime images come in as (1, Ny, Nx) — squeeze the broadcast axis
        # but keep the original ndarray when the layout is already 2-D.
        arr = np.squeeze(arr)
        if arr.ndim < 2:
            return

        # Drop zero / non-positive pixels — those are the "below threshold"
        # placeholders SwabianTimeTaggerManager writes.  Convert seconds → ns.
        valid = arr[arr > 0].ravel()
        self._current_frame_ns = valid.astype(np.float32) * 1e9

        if self._widget.isAccumulating():
            # Accumulating mode: histogram is driven by sigScanDone, not by
            # individual polls.  Just keep the latest frame ready; do nothing
            # to the display here so the histogram stays in sync with scans.
            return

        self._accum.clear()
        self._widget.updateHistogram(self._current_frame_ns)

    def _on_scan_started(self):
        """New scan beginning — discard the in-progress frame so the next
        update starts fresh rather than blending into the previous scan."""
        self._current_frame_ns = np.empty(0, dtype=np.float32)

    def _on_scan_done(self):
        """Scan complete — append this scan's final snapshot to _accum and
        redraw the histogram once.  This keeps accumulation in sync with scan
        boundaries: one independent measurement added per completed scan."""
        if self._widget.isAccumulating() and self._current_frame_ns.size:
            self._accum.append(self._current_frame_ns.copy())
            data = np.concatenate(self._accum)
            self._widget.updateHistogram(data)
        self._current_frame_ns = np.empty(0, dtype=np.float32)

    def _on_show_toggled(self, enabled: bool):
        self.active = enabled
        if not enabled:
            self._accum.clear()
            self._current_frame_ns = np.empty(0, dtype=np.float32)

    def _on_accumulate_toggled(self, enabled: bool):
        if not enabled:
            self._accum.clear()

    def _on_nbins_changed(self, _n_bins: int):
        # Widget reads getNBins() on the next updateHistogram call; nothing
        # to push from the controller side.
        pass

    def _on_range_changed(self, _lo: float, _hi: float):
        # Same — widget reads getRange() on next update.
        pass


# Copyright (C) 2020-2021 ImSwitch developers
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
