import numpy as np

from .lattice import detect_lattice
from .localizer import _summed_image, detection_band, robust_localize


class PatternFinder:
    @staticmethod
    def _guess_kwargs(xp_guess, yp_guess):
        kwargs = {}
        for key, guess in (("xp_guess", xp_guess), ("yp_guess", yp_guess)):
            try:
                if guess is not None and float(guess) > 0:
                    kwargs[key] = float(guess)
            except (TypeError, ValueError):
                pass
        return kwargs

    def findPattern(self, image, xp_guess=None, yp_guess=None):
        """Find pattern as [row_offset, col_offset, row_period, col_period].

        Uses the guess-free 2D lattice detection to seed the precise 1D
        refinement, so the true period does not need to be near any guess.
        ``xp_guess``/``yp_guess`` (in px; x == column axis, y == row axis)
        only serve as fallback seeds when the 2D detection finds no usable
        spectral peaks. Raises ValueError for non-rectangular patterns —
        callers that can handle those should use
        :meth:`findPatternOrLattice`.
        """
        loc = robust_localize(image, **self._guess_kwargs(xp_guess, yp_guess))
        return [loc.yo, loc.xo, loc.yp, loc.xp]

    def findPatternOrLattice(self, image, xp_guess=None, yp_guess=None):
        """Find the pattern, whatever its geometry.

        Returns ``(pattern, lattice)``: for an axis-aligned rectangular grid,
        ``pattern`` is the ``[row_offset, col_offset, row_period, col_period]``
        list (and ``lattice`` its detected basis, when available); for any
        other Bravais lattice — rotated square, hexagonal — ``pattern`` is
        None and ``lattice`` carries the detected geometry, which the
        rectangular widget fields cannot express.

        Raises:
            ValueError: When no periodic pattern is found at all.
        """
        kwargs = self._guess_kwargs(xp_guess, yp_guess)
        try:
            loc = robust_localize(image, **kwargs)
            return [loc.yo, loc.xo, loc.yp, loc.xp], loc.lattice
        except ValueError:
            # Either no pattern at all, or a non-rectangular one. Detection
            # distinguishes the two: it raises its own descriptive error for
            # patternless data and returns the lattice otherwise.
            band = detection_band(kwargs.get("xp_guess"), kwargs.get("yp_guess"))
            lattice = detect_lattice(_summed_image(image), **band)
            if lattice.is_axis_aligned_rectangular(tol=0.05):
                # Detection says rectangular, yet the full localization
                # failed — surface that as the error it is.
                raise
            return None, lattice

    def find(self, image, xp_guess=None, yp_guess=None):
        """Compatibility alias for the plugin-style reconstructor API."""
        return self.findPattern(image, xp_guess=xp_guess, yp_guess=yp_guess)

    def findBestPeak(self, peaks):
        """ Finds the best peak in a list of peaks. """
        bestTwoPeaks = peaks[1]['prominences'].argsort()[-2::][::-1]
        prom1 = peaks[1]['prominences'][bestTwoPeaks[0]]
        prom2 = peaks[1]['prominences'][bestTwoPeaks[1]]
        if abs((prom1 - prom2) / (prom1 + prom2)) < 0.2:
            height1 = peaks[1]['peak_heights'][bestTwoPeaks[0]]
            height2 = peaks[1]['peak_heights'][bestTwoPeaks[1]]
            # Compute relative difference in heights: abs((h1-h2)/(h1+h2))
            # This gives a normalized measure of similarity in [0, 1]
            # where 0 means identical heights and 1 means maximally different
            if abs((height1 - height2) / (height1 + height2)) < 0.2:
                bestPeak = bestTwoPeaks.min()
            else:
                heights = np.array([height1, height2])
                highest = heights.argmax()
                bestPeak = bestTwoPeaks[highest]
        else:
            bestPeak = bestTwoPeaks[0]

        return bestPeak


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
