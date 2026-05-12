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

from ..basecontrollers import ImConWidgetController


class LineProfileController(ImConWidgetController):
    """Linked to LineProfileWidget.

    Listens to ViewerToolManager.sigShapesChanged and updates the plot only
    when the active tool is 'line' (line profile) or 'rectangle' (integrated
    intensity).  Crosshair/grid/pan mode changes are intentionally ignored.
    """

    def __init__(self, *args, imageToolManager=None, **kwargs):
        super().__init__(*args, **kwargs)

        if imageToolManager is None:
            return

        self._toolManager = imageToolManager
        self._toolManager.sigShapesChanged.connect(self._onShapesChanged)

    # ------------------------------------------------------------------
    def _onShapesChanged(self):
        mode = self._toolManager.get_mode()

        if mode == 'line':
            endpoints = self._findFirstLine()
            self._widget.sigLineChanged.emit(endpoints)

        elif mode == 'rectangle':
            bounds = self._findFirstRectangle()
            self._widget.sigRectangleChanged.emit(bounds)

        # crosshair / grid / pan → do nothing

    def _findFirstLine(self):
        shape_types = self._toolManager.get_shape_types()
        for i, stype in enumerate(shape_types):
            if stype == 'line':
                return self._toolManager.get_line_endpoints(i)
        return None

    def _findFirstRectangle(self):
        shape_types = self._toolManager.get_shape_types()
        for i, stype in enumerate(shape_types):
            if stype == 'rectangle':
                return self._toolManager.get_rectangle_bounds(i)
        return None
