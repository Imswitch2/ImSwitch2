import threading
import time

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model import APIExport
from ..basecontrollers import ImConWidgetController

_Z_AXIS = 'Z'
_SETTLE_S = 0.15


class AutofocusController(ImConWidgetController):
    """Linked to AutofocusWidget."""

    sigFocusDone = QtCore.Signal(float)  # optimal z position

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self._setupInfo.autofocus is None:
            return

        self.camera = self._setupInfo.autofocus.camera
        self.positioner = self._setupInfo.autofocus.positioner
        self._focusing = False

        self._master.detectorsManager[self.camera].startAcquisition()

        self._widget.focusButton.clicked.connect(self._onFocusButton)
        self.sigFocusDone.connect(self._onFocusDone)

    def _onFocusButton(self):
        try:
            rangez = float(self._widget.zStepRangeEdit.text())
            resolutionz = float(self._widget.zStepSizeEdit.text())
        except ValueError:
            return
        self.autoFocus(rangez, resolutionz)

    @APIExport()
    def autoFocus(self, rangez: float = 100.0, resolutionz: float = 10.0) -> None:
        """Start an autofocus scan asynchronously.

        Scans +-rangez/2 around the current Z in steps of resolutionz,
        fits a parabola to the gradient-variance focus metric, and moves
        to the estimated peak.
        """
        if self._focusing:
            return
        self._focusing = True
        self._widget.focusButton.setText('Focusing...')
        self._widget.focusButton.setEnabled(False)

        t = threading.Thread(
            target=self._runFocus,
            args=(rangez, resolutionz),
            daemon=True,
        )
        t.start()

    def _runFocus(self, rangez: float, resolutionz: float) -> None:
        try:
            positioner = self._master.positionersManager[self.positioner]
            detector = self._master.detectorsManager[self.camera]

            current_z = positioner.position[_Z_AXIS]
            n_steps = max(3, int(rangez / resolutionz))
            z_positions = [current_z - rangez / 2 + i * resolutionz
                           for i in range(n_steps)]

            focus_vals = []
            for z in z_positions:
                positioner.setPosition(z, _Z_AXIS)
                time.sleep(_SETTLE_S)
                img = detector.getLatestFrame().astype(np.float64)
                gx = np.diff(img, axis=1)
                gy = np.diff(img, axis=0)
                focus_vals.append(float(np.var(gx) + np.var(gy)))

            coeffs = np.polyfit(z_positions, focus_vals, 2)
            if coeffs[0] < 0:
                z_focus = float(-coeffs[1] / (2 * coeffs[0]))
                z_focus = float(np.clip(z_focus, min(z_positions), max(z_positions)))
            else:
                z_focus = z_positions[int(np.argmax(focus_vals))]

            positioner.setPosition(z_focus, _Z_AXIS)
            self._logger.debug(f'Autofocus: optimal Z = {z_focus:.2f}')

            QtCore.QMetaObject.invokeMethod(
                self, '_updatePlot',
                QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG('PyQt_PyObject', z_positions),
                QtCore.Q_ARG('PyQt_PyObject', focus_vals),
            )
            self.sigFocusDone.emit(z_focus)

        except Exception as e:
            self._logger.error(f'Autofocus failed: {e}', exc_info=True)
        finally:
            self._focusing = False

    @QtCore.Slot(object, object)
    def _updatePlot(self, z_positions, focus_vals):
        self._widget.focusPlotCurve.setData(z_positions, focus_vals)

    @QtCore.Slot(float)
    def _onFocusDone(self, z_focus: float):
        self._widget.focusButton.setText('Autofocus')
        self._widget.focusButton.setEnabled(True)
        self._widget.focusButton.setChecked(False)


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
