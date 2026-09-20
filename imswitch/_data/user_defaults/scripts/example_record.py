"""
This script demonstrates some basic functions for recording, changing what
module is displayed, and waiting for signals to be emitted. Note that the
recording does not stop automatically if the script is terminated before it
finishes.

NOTE: This script will only work if the recording widget is enabled in your
current hardware control setup.
"""

getLogger().info('Starting recording in "until stop" mode...')
api.imcontrol.setRecModeUntilStop()
api.imcontrol.startRecording()

getLogger().info('Recording started. Showing hardware control tab for a few seconds before stopping.')
sleep(3)  # sleep() stops immediately when the script is stopped; time.sleep does not
mainWindow.setCurrentModule('imcontrol')
sleep(5)

getLogger().info('Going back to scripting tab.')
mainWindow.setCurrentModule('imscripting')
sleep(2)

getLogger().info('Stopping recording...')
# Create the waiter BEFORE the call that causes the signal: a signal emitted
# before the waiter exists is never seen. callAndWaitForSignal does both.
callAndWaitForSignal(api.imcontrol.signals().recordingEnded,
                     api.imcontrol.stopRecording, timeout=60)

getLogger().info('Recording stopped.')


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
