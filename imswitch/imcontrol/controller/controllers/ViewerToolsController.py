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

import numpy as np

from ..basecontrollers import ImConWidgetController


class ViewerToolsController(ImConWidgetController):
    """Linked to ViewerToolsWidget.

    All five tools (pan/rectangle/line/crosshair/grid) share the single
    ViewerToolManager Shapes layer.  Switching tools clears the previous
    shapes automatically.
    """

    def __init__(self, *args, imageWidget=None, **kwargs):
        super().__init__(*args, **kwargs)

        self._toolManager = imageWidget.toolManager if imageWidget else None
        self._viewer = imageWidget.napariViewer if imageWidget else None

        self._crosshair_click_cb = None

        if imageWidget is None:
            return

        self._widget.sigToolSelected.connect(self._on_tool_selected)

    # ------------------------------------------------------------------
    def _on_tool_selected(self, tool):
        self._deactivate_crosshair_click()

        if not self._toolManager:
            return

        self._toolManager.clear_shapes()

        if tool == 'crosshair':
            self._toolManager.set_mode('pan')
            self._activate_crosshair_click()

        elif tool == 'grid':
            self._toolManager.set_mode('pan')
            H, W = self._largest_image_shape()
            self._toolManager.draw_grid(H, W)

        elif tool in ('rectangle', 'line'):
            self._toolManager.set_mode(tool)

        elif tool == 'pan':
            self._toolManager.set_mode('pan')

    # ------------------------------------------------------------------
    def _largest_image_shape(self):
        """Return (H, W) of the largest visible image layer, or (512, 512) fallback."""
        best = (512, 512)
        if self._viewer is None:
            return best
        for layer in self._viewer.layers:
            if (hasattr(layer, 'data') and layer.visible
                    and isinstance(layer.data, np.ndarray) and layer.data.ndim >= 2):
                h, w = int(layer.data.shape[-2]), int(layer.data.shape[-1])
                if h * w > best[0] * best[1]:
                    best = (h, w)
        return best

    # ------------------------------------------------------------------
    def _activate_crosshair_click(self):
        if self._viewer is None:
            return

        toolManager = self._toolManager

        def _cb(viewer, event):
            if event.type != 'mouse_press':
                return
            pos = event.position
            if len(pos) >= 2:
                row, col = float(pos[-2]), float(pos[-1])
                if toolManager:
                    toolManager.place_crosshair(row, col)

        self._crosshair_click_cb = _cb
        self._viewer.mouse_drag_callbacks.append(_cb)

    def _deactivate_crosshair_click(self):
        if self._crosshair_click_cb is not None and self._viewer is not None:
            try:
                self._viewer.mouse_drag_callbacks.remove(self._crosshair_click_cb)
            except ValueError:
                pass
        self._crosshair_click_cb = None

    def closeEvent(self):
        self._deactivate_crosshair_click()
