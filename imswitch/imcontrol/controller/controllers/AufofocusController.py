import time

import numpy as np

from imswitch.imcommon.framework import Thread, Timer
from imswitch.imcommon.model import initLogger, APIExport
from ..basecontrollers import ImConWidgetController

# global axis for Z-positioning - should be Z
gAxis = "Z" 
T_DEBOUNCE = .2
class AutofocusController(ImConWidgetController):
    """Linked to AutofocusWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self)

        if self._setupInfo.autofocus is None:
            return

        self.camera = self._setupInfo.autofocus.camera
        self.positioner = self._setupInfo.autofocus.positioner
        #self._master.detectorsManager[self.camera].crop(*self.cropFrame)

        # Connect AutofocusWidget buttons
        self._widget.focusButton.clicked.connect(self.focusButton)

        self._master.detectorsManager[self.camera].startAcquisition()
        self.__processDataThread = ProcessDataThread(self)

    def __del__(self):
        self.__processDataThread.quit()
        self.__processDataThread.wait()
        if hasattr(super(), '__del__'):
            super().__del__()

    def focusButton(self):
        rangez = float(self._widget.zStepRangeEdit.text())
        resolutionz = float(self._widget.zStepSizeEdit.text())
        self._widget.focusButton.setText('Stop')
        self.autoFocus(rangez,resolutionz)
        self._widget.focusButton.setText('Autofocus')

    @APIExport(runOnUIThread=True)
    # Update focus lock
    def autoFocus(self, rangez=100, resolutionz=10):

        '''
        The stage moves from -rangez...+rangez with a resolution of resolutionz
        For every stage-position a camera frame is captured and a contrast curve is determined

        '''
        # determine optimal focus position by stepping through all z-positions and cacluate the focus metric
        self.focusPointSignal = self.__processDataThread.update(rangez,resolutionz)

class ProcessDataThread(Thread):
    def __init__(self, controller, *args, **kwargs):
        self._controller = controller
        super().__init__(*args, **kwargs)

    def grabCameraFrame(self):
        detectorManager = self._controller._master.detectorsManager[self._controller.camera]
        self.latestimg = detectorManager.getLatestFrame()
        return self.latestimg

    def update(self, rangez, resolutionz):
        # Get current Z position
        positioner = self._controller._master.positionersManager[self._controller.positioner]
        current_z = positioner.position[gAxis]
        
        # Compute start and end positions centered on current Z
        start = current_z - rangez / 2
        end = current_z + rangez / 2
        n_steps = max(3, int(rangez / resolutionz))
        z_positions = [start + i * resolutionz for i in range(n_steps)]
        
        focus_vals = []
        
        # Scan through Z positions
        for z in z_positions:
            # Move to absolute position
            positioner.setPosition(z, gAxis)
            time.sleep(0.15)
            
            # Grab frame
            self._controller._logger.debug(f'Moving focus to {z:.2f}')
            img = self.grabCameraFrame()
            
            # Compute gradient variance focus metric
            img_float = img.astype(float)
            gx = np.diff(img_float, axis=1)
            gy = np.diff(img_float, axis=0)
            focus_val = float(np.var(gx) + np.var(gy))
            focus_vals.append(focus_val)
            
            self._controller._logger.debug(f'Z={z:.2f}, focus={focus_val:.2f}')
        
        # Fit parabola and find peak
        coeffs = np.polyfit(z_positions, focus_vals, 2)
        
        if coeffs[0] < 0:  # Concave down - valid peak
            z_focus = -coeffs[1] / (2 * coeffs[0])
            # Clamp to range
            z_focus = max(min(z_positions), min(z_focus, max(z_positions)))
        else:  # Fall back to grid maximum
            z_focus = z_positions[np.argmax(focus_vals)]
        
        # Move to optimal focus position
        positioner.setPosition(z_focus, gAxis)
        
        # Update plot
        self._controller._widget.focusPlotCurve.setData(z_positions, focus_vals)
        
        self._controller._logger.debug(f'Optimal focus at Z={z_focus:.2f}')
        
        return z_focus

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
