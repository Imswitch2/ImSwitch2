"""
Record a series of scans at increasing laser power into one until-stop
recording. Shows the safe way to drive scans from a script:

* ``runScanAndWait`` waits for exactly the scan it started, however the
  scan's signals are timed, and raises if the start was refused.
* Recording start/stop are wrapped in ``callAndWaitForSignal`` so the waiter
  exists before the call that triggers the signal.
* Everything after ``try`` still runs its ``finally`` when the script is
  stopped: cancellation is delivered once, then cleanup gets its own window.

NOTE: This script needs the Recording, Laser and Scan widgets in your setup.
"""

import numpy as np

mainWindow.setCurrentModule('imcontrol')
signals = api.imcontrol.signals()
laser = api.imcontrol.getLaserNames()[0]   # check this really is the laser you mean
powers = np.array([0, 0, 0, 3, 5, 10, 25, 50, 100])

api.imcontrol.setRecModeUntilStop()
try:
    callAndWaitForSignal(signals.recordingStarted, api.imcontrol.startRecording, timeout=30)
    for power in powers:
        api.imcontrol.changeScanPower(laser, float(power))
        runScanAndWait(timeout=600)
        getLogger().info(f'scan at {power} done')
finally:
    # stopRecording() reports whether a recording was active, i.e. whether a
    # recordingEnded will follow at all.
    waitForRecordingToEnd = getWaitForSignal(signals.recordingEnded, timeout=60)
    if api.imcontrol.stopRecording():
        waitForRecordingToEnd()


# Copyright (C) 2020-2026 ImSwitch developers
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
