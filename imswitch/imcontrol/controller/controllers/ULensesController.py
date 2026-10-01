import numpy as np

from ..basecontrollers import ImConWidgetController
from imswitch.imcontrol.model.ulenses_localizer import localizer


class ULensesController(ImConWidgetController):
    """ Linked to ULensesWidget. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Register the grid overlay with the image viewbox (like the other
        # overlays) so it is tracked in the viewbox's overlay list and receives
        # the detector pixel-scale broadcast, keeping the grid aligned to the
        # image napari draws scaled by the detector pixel size.
        self._commChannel.sigAddItemToVb.emit(self._widget.getPlotGraphicsItem())

        # Connect ULensesWidget signals
        self._widget.sigULensesClicked.connect(self.updateGrid)
        self._widget.sigUShowLensesChanged.connect(self.toggleULenses)
        self._widget.sigLocalizeBtnClicked.connect(self.localize)

    def localize(self):
        """Localize the uLens grid in the current detector image."""
        try:
            _, _, px, upx, upy = self._widget.getParameters()
            if px <= 0:
                raise ValueError("Pixel size must be positive")

            img_frame = self._commChannel.get_image()
            loc_res = localizer(
                img_frame,
                xp_guess=upx / px,
                yp_guess=upy / px,
            )
        except Exception as e:
            self._logger.warning(
                f"Auto localization failed - use manual mode: {e}"
            )
            return

        x_period = round(loc_res.xp * px, 2)
        x_offset = round(loc_res.xo, 2)
        y_period = round(loc_res.yp * px, 2)
        y_offset = round(loc_res.yo, 2)

        self._widget.setParameters(
            x=x_offset,
            y=y_offset,
            px=px,
            upx=x_period,
            upy=y_period,
        )
        self.updateGrid()

    def updateGrid(self):
        """ Updates plot with new parameters. """
        x, y, px, upx, upy = self._widget.getParameters()
        size_x, size_y = self._master.detectorsManager.execOnCurrent(lambda c: c.shape)
        pattern_x = np.arange(x, size_x, upx / px)
        pattern_y = np.arange(y, size_y, upy / px)
        grid = np.array(np.meshgrid(pattern_x, pattern_y)).T.reshape(-1, 2)
        self._widget.setData(x=grid[:, 0], y=grid[:, 1])

    def toggleULenses(self, show):
        """ Shows or hides grid. """
        self._widget.setULensesVisible(show)


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
