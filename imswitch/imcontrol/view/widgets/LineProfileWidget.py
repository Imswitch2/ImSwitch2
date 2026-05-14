import numpy as np
from qtpy import QtCore, QtWidgets
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from scipy.ndimage import map_coordinates

from .basewidgets import Widget


class LineProfileWidget(Widget):
    """Displays intensity profiles driven by the active viewer tool.

    - Line tool   → line profile in red, no legend.
    - Rectangle   → x-projection (red) and y-projection (green) in one plot.
    """

    sigLineChanged = QtCore.Signal(object)       # ((r0,c0),(r1,c1)) or None
    sigRectangleChanged = QtCore.Signal(object)  # (r0,c0,r1,c1) or None

    _BG   = '#262930'
    _AX   = '#2b2b2b'
    _FG   = '#ffffff'
    _GRID = '#3a3a3a'

    def __init__(self, *args, napariViewer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._napariViewer = napariViewer

        self._figure = Figure(figsize=(5, 3))
        self._figure.patch.set_facecolor(self._BG)
        self._ax = self._figure.add_subplot(111)
        self._styleAxes()

        self._canvas = FigureCanvas(self._figure)
        self._canvas.setStyleSheet(f'background-color: {self._BG};')
        
        # Width spinbox for neighbourhood averaging
        self._widthSpinBox = QtWidgets.QSpinBox()
        self._widthSpinBox.setRange(1, 99)
        self._widthSpinBox.setSingleStep(2)
        self._widthSpinBox.setValue(1)
        self._widthSpinBox.setToolTip("Number of perpendicular samples for averaging (odd values only)")
        
        widthLayout = QtWidgets.QHBoxLayout()
        widthLayout.addWidget(QtWidgets.QLabel("Width (n):"))
        widthLayout.addWidget(self._widthSpinBox)
        widthLayout.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(widthLayout)
        layout.addWidget(self._canvas)
        self.setLayout(layout)
        
        self._last_line_endpoints = None
        self._widthSpinBox.valueChanged.connect(lambda: self._updateLineProfile(self._last_line_endpoints))

        self.sigLineChanged.connect(self._updateLineProfile)
        self.sigRectangleChanged.connect(self._updateRectangleProfile)

    # ------------------------------------------------------------------
    def _styleAxes(self):
        ax = self._ax
        ax.set_facecolor(self._AX)
        ax.tick_params(colors=self._FG, labelsize=7)
        ax.xaxis.label.set_color(self._FG)
        ax.yaxis.label.set_color(self._FG)
        ax.title.set_color(self._FG)
        for spine in ax.spines.values():
            spine.set_color(self._GRID)
        ax.grid(True, color=self._GRID, linewidth=0.5, alpha=0.6)

    def _redraw(self, title, xlabel, ylabel):
        self._ax.clear()
        self._styleAxes()
        self._ax.set_title(title, fontsize=9)
        self._ax.set_xlabel(xlabel, fontsize=8)
        self._ax.set_ylabel(ylabel, fontsize=8)

    # ------------------------------------------------------------------
    # Line profile
    # ------------------------------------------------------------------
    def _updateLineProfile(self, endpoints):
        self._redraw('Line Profile', 'Distance (px)', 'Intensity')
        self._last_line_endpoints = endpoints
        if endpoints is not None and self._napariViewer is not None:
            (r0, c0), (r1, c1) = endpoints
            n = self._widthSpinBox.value()
            for layer in self._visibleImageLayers():
                profile = self._computeLineProfile(layer.data, r0, c0, r1, c1, n)
                if profile is not None:
                    self._ax.plot(np.arange(len(profile)), profile,
                                  color='red', linewidth=1.5)
        self._figure.tight_layout(pad=1.5)
        self._canvas.draw()

    def _computeLineProfile(self, image_data, r0, c0, r1, c1, n=1):
        try:
            img = (image_data if image_data.ndim == 2
                   else image_data[tuple([0] * (image_data.ndim - 2))])
            
            # Number of sample points along the line
            num_points = max(int(np.ceil(np.hypot(r1 - r0, c1 - c0))) + 1, 2)
            
            if n == 1:
                # Current behavior: single line profile
                return map_coordinates(img,
                                       [np.linspace(r0, r1, num_points),
                                        np.linspace(c0, c1, num_points)],
                                       order=1, mode='constant', cval=0)
            else:
                # Neighbourhood averaging with perpendicular sampling
                # Line direction and length
                drow = r1 - r0
                dcol = c1 - c0
                L = np.hypot(drow, dcol)
                
                # Perpendicular unit vector: (-dcol, drow) / L
                perp_r = -dcol / L if L > 0 else 0
                perp_c = drow / L if L > 0 else 0
                
                # Sample positions along the line
                r_samples = np.linspace(r0, r1, num_points)
                c_samples = np.linspace(c0, c1, num_points)
                
                # Perpendicular offsets
                offsets = np.linspace(-(n - 1) / 2, (n - 1) / 2, n)
                
                # Gaussian weights: exp(-k² / (2*sigma²)) where sigma = n/4
                sigma = n / 4.0
                weights = np.exp(-offsets**2 / (2 * sigma**2))
                weights /= weights.sum()
                
                # Compute weighted average profile
                profile = np.zeros(num_points)
                for i, offset in enumerate(offsets):
                    # Sample positions offset perpendicular to the line
                    r_offset = r_samples + offset * perp_r
                    c_offset = c_samples + offset * perp_c
                    
                    # Sample at these positions
                    samples = map_coordinates(img,
                                             [r_offset, c_offset],
                                             order=1, mode='constant', cval=0)
                    profile += weights[i] * samples
                
                return profile
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Rectangle integrated-intensity projections
    # ------------------------------------------------------------------
    def _updateRectangleProfile(self, bounds):
        self._redraw('Rectangle Projections', 'Distance (px)', 'Mean Intensity')
        if bounds is None or self._napariViewer is None:
            self._canvas.draw()
            return

        r0, c0, r1, c1 = bounds
        rlo, rhi = int(round(min(r0, r1))), int(round(max(r0, r1)))
        clo, chi = int(round(min(c0, c1))), int(round(max(c0, c1)))

        plotted = False
        for layer in self._visibleImageLayers():
            img = layer.data
            if img.ndim > 2:
                img = img[tuple([0] * (img.ndim - 2))]
            H, W = img.shape
            rc0, rc1 = max(0, rlo), min(H, rhi)
            cc0, cc1 = max(0, clo), min(W, chi)
            if rc0 >= rc1 or cc0 >= cc1:
                continue
            roi = img[rc0:rc1, cc0:cc1].astype(float)
            
            # Rectangle dimensions (pixels)
            roi_height = rc1 - rc0
            roi_width = cc1 - cc0
            
            # x-projection: sum along rows (axis=0), normalized by rectangle height
            # This gives the mean intensity in the y-direction for each x position.
            # Divide by height to show density/mean rather than total integrated signal,
            # making x and y profiles comparable even for non-square rectangles.
            x_profile = roi.sum(axis=0) / roi_height
            x_distance = np.arange(roi_width)  # Distance from start of ROI, not absolute position
            self._ax.plot(x_distance, x_profile,
                          color='red', linewidth=1.5, label='x')
            
            # y-projection: sum along columns (axis=1), normalized by rectangle width
            # This gives the mean intensity in the x-direction for each y position.
            # Divide by width to show density/mean rather than total integrated signal,
            # making x and y profiles comparable even for non-square rectangles.
            y_profile = roi.sum(axis=1) / roi_width
            y_distance = np.arange(roi_height)  # Distance from start of ROI, not absolute position
            self._ax.plot(y_distance, y_profile,
                          color='#00cc44', linewidth=1.5, label='y')
            
            plotted = True
            break  # one layer is enough for single-layer setups

        if plotted:
            self._ax.legend(fontsize=8, facecolor=self._AX,
                            edgecolor=self._GRID, labelcolor=self._FG,
                            handlelength=1.2, borderpad=0.5)

        self._figure.tight_layout(pad=1.5)
        self._canvas.draw()

    # ------------------------------------------------------------------
    def _visibleImageLayers(self):
        if self._napariViewer is None:
            return []
        return [
            layer for layer in self._napariViewer.layers
            if (hasattr(layer, 'data') and layer.visible
                and isinstance(layer.data, np.ndarray)
                and layer.data.ndim >= 2
                and not layer.name.startswith('_')
                and layer.name != 'Viewer Tools')
        ]


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
