"""ImSwitch Config Studio -- the visual editor for hardware setup files.

Lives inside the package rather than beside it so that it ships with every
install: imcontrol opens it from ``Tools > Edit hardware configuration...``,
and an installed or frozen ImSwitch has no ``utility_scripts/`` directory to
reach into. ``utility_scripts/imswitch_config_editor.py`` is kept as a
launcher for the standalone run.

Importing this package pulls in the whole editor, including its manager
catalog and built-in templates, so imcontrol imports it lazily -- when the
menu action fires, not at startup.
"""

from .editor import MainWindow, main

__all__ = ['MainWindow', 'main']


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
