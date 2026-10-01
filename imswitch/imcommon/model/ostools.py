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

    ``argv[0]`` is the interpreter path *unquoted* on POSIX. It used to be
    wrapped in literal double quotes -- a Windows habit, where ``execv`` joins
    the arguments into one command line and a path with spaces needs them.
    On macOS and Linux the quotes are part of the string, and the restarted
    Python takes its ``sys.executable`` from that argv[0]: a path that does
    not exist. The first restart works; the next one, from the restarted
    process, fails with ``FileNotFoundError``. Windows still gets its quotes,
    around every argument that needs them.
    """
    executable = _interpreter()
    if getattr(sys, 'frozen', False):
        # A frozen build has no interpreter to hand a -m to; sys.executable is
        # the bundled application itself.
        argv = [executable] + sys.argv[1:]
    else:
        argv = [executable, '-m', module] + sys.argv[1:]
    os.execv(executable, [_quotedForPlatform(arg) for arg in argv])


def _interpreter():
    """ The interpreter to re-exec: ``sys.executable``, unquoted if a previous
    restart by the old code left it wrapped in literal quotes.

    A Python started with argv[0] ``'"/env/bin/python"'`` reports
    ``sys.executable`` as ``'<cwd>/"/env/bin/python"'``; the real path is what
    the quotes enclose. """
    executable = sys.executable
    if os.path.exists(executable) or executable.count('"') < 2:
        return executable
    inner = executable[executable.index('"') + 1:executable.rindex('"')]
    return inner if os.path.isabs(inner) and os.path.exists(inner) else executable


def _quotedForPlatform(arg):
    """ Quote an argument for Windows' command-line joining; leave POSIX alone. """
    if sys.platform == 'win32' and any(c.isspace() for c in arg) and not arg.startswith('"'):
        return '"' + arg + '"'
    return arg


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
