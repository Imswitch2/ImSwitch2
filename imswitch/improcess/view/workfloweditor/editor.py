"""The workflow editor window (now :mod:`imswitch.imcommon.view.workfloweditor.editor`)
with ImProcess's defaults filled in."""

from __future__ import annotations

from imswitch.imcommon.view.workfloweditor.editor import *  # noqa: F401,F403
from imswitch.imcommon.view.workfloweditor.editor import (  # noqa: F401 - names callers import
    AUTO_RUN_BUDGET_S,
    AUTO_RUN_DELAY_MS,
    _FILTER,
    _SHORTCUTS,
    _STYLE,
    _SUFFIXES,
    _has_python,
    _seconds,
)
from imswitch.imcommon.view.workfloweditor.editor import MainWindow as _MainWindow


class MainWindow(_MainWindow):
    """The editor over ImProcess's catalog (:func:`build_catalog`), starting
    in ImProcess's workflow folder and remembering its settings in
    ``improcess_options.json``, unless the caller gives its own."""

    def __init__(self, catalog=None, *, start_folder=None, preferences=None, parent=None,
                 title: str = "ImProcess workflow editor"):
        if catalog is None:
            from imswitch.improcess.model.workfloweditor.catalog import build_catalog

            catalog = build_catalog()
        if start_folder is None:
            from imswitch.improcess.model.workfloweditor.folders import workflows_directory

            start_folder = workflows_directory()
        if preferences is None:
            from imswitch.improcess.model.workfloweditor.preferences import EditorPreferences

            preferences = EditorPreferences()
        super().__init__(catalog, start_folder=start_folder, preferences=preferences, parent=parent, title=title)

    def rebuild_catalog(self):
        """A fresh catalog from a fresh full registry (drop-in plugins re-read)."""
        from imswitch.improcess.model.workfloweditor.catalog import build_catalog

        return build_catalog()


__all__ = ["MainWindow"]


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
