import numpy as np

from .localizer import localizer


class PatternFinder:
    def findPattern(self, image, xp_guess=None, yp_guess=None):
        """Find pattern as [row_offset, col_offset, row_period, col_period].

        ``xp_guess``/``yp_guess`` seed the localizer's period search (in px;
        x == column axis, y == row axis). The search window only covers
        roughly +-20% around the guess, so callers that already hold pattern
        parameters (widget values, a previous fit) should pass them instead
        of relying on the 10 px default.
        """
        kwargs = {}
        for key, guess in (("xp_guess", xp_guess), ("yp_guess", yp_guess)):
            try:
                if guess is not None and float(guess) > 0:
                    kwargs[key] = float(guess)
            except (TypeError, ValueError):
                pass
        loc = localizer(image, **kwargs)
        return [loc.yo, loc.xo, loc.yp, loc.xp]

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
