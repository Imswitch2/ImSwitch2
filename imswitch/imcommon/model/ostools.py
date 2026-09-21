import os
import subprocess
import sys


def openFolderInOS(folderPath):
    """ Open a folder in the OS's default file browser. """
    try:
        if sys.platform == 'darwin':
            subprocess.check_call(['open', folderPath])
        elif sys.platform == 'linux':
            subprocess.check_call(['xdg-open', folderPath])
        elif sys.platform == 'win32':
            os.startfile(folderPath)
    except FileNotFoundError or subprocess.CalledProcessError as err:
        raise OSToolsError(err)


def restartSoftware(module='imswitch'):
    """ Restarts the software immediately, without shutting anything down.

    ``os.execv`` replaces this process image, so no finalizer runs: hardware
    managers are never closed and whatever state the devices are in is what the
    new process inherits. Prefer :func:`requestRestart`, which lets the normal
    shutdown run first and restarts from ``launchApp``.

    The command line is carried over, so a session started with ``--debug`` or
    ``--scale`` comes back the same way.
    """
    if getattr(sys, 'frozen', False):
        # A frozen build has no interpreter to hand a -m to; sys.executable is
        # the bundled application itself.
        os.execv(sys.executable, [sys.executable] + sys.argv[1:])
    else:
        os.execv(sys.executable,
                 ['"' + sys.executable + '"', '-m', module] + sys.argv[1:])


_restartRequest = None


def requestRestart(module='imswitch'):
    """ Ask for ImSwitch to be restarted once it has finished shutting down.

    Closing the main window still runs the ordinary shutdown -- widget states
    saved, controller workers drained, hardware managers finalized -- and
    ``launchApp`` then re-execs instead of exiting. This is the difference
    between a restart that leaves a laser as it was found and one that does
    not, which is why it exists alongside :func:`restartSoftware`.
    """
    global _restartRequest
    _restartRequest = module


def restartAfterShutdown(closeApplication, module='imswitch'):
    """ Ask for a restart, then close the application so it can happen.

    The single way anything in ImSwitch should restart itself. ``closeApplication``
    is the main window's ``close``; if it returns False the close was vetoed --
    an unsaved-work prompt the user backed out of, a module that refused -- so
    nothing is going to restart and the request is withdrawn rather than left
    armed for whenever the window is next closed.
    """
    requestRestart(module)
    if closeApplication() is False:
        cancelRestart()


def cancelRestart():
    """ Withdraw a pending restart request. """
    global _restartRequest
    _restartRequest = None


def restartRequested():
    """ The module to restart into, or None if no restart was asked for. """
    return _restartRequest


class OSToolsError(Exception):
    pass


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
