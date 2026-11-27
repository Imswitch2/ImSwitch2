"""ImSwitch Config Studio -- the visual editor for hardware setup files.

Lives inside the package rather than beside it so that it ships with every
install: imcontrol opens it from ``Hardware > Edit hardware configuration...``,
and an installed or frozen ImSwitch has no ``utility_scripts/`` directory to
reach into. ``utility_scripts/imswitch_config_editor.py`` is kept as a
launcher for the standalone run.

The editor scans its manager catalog and every built-in template when it is
imported, so this package loads it only when ``MainWindow`` or ``main`` is
asked for -- imcontrol does that when the menu action fires, not at startup.
:mod:`.section_form`, the section form ImControl also shows when a panel is
added at run time, can be imported without it.
"""

__all__ = ['MainWindow', 'main']


def __getattr__(name):
    if name in __all__:
        from . import editor
        return getattr(editor, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


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
