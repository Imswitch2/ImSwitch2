"""What ImProcess's workflow editor remembers between sessions. Qt-free.

Kept under ``workflowEditor`` in ``improcess_options.json`` (the file
:mod:`imswitch.improcess.model.folder_preferences` writes), so it describes
this computer and this user, and every other key in the file survives a
round trip. The settings themselves are
:mod:`imswitch.imcommon.model.workfloweditor.preferences`; this names the file.
"""

from __future__ import annotations

from imswitch.imcommon.model.workfloweditor.preferences import *  # noqa: F401,F403
from imswitch.imcommon.model.workfloweditor.preferences import (  # noqa: F401 - names callers import
    KEY,
    _RECENT,
)
from imswitch.imcommon.model.workfloweditor.preferences import EditorPreferences as _EditorPreferences


class EditorPreferences(_EditorPreferences):
    """The editor's settings in ``improcess_options.json`` unless ``path`` says otherwise."""

    def __init__(self, path=None):
        if path is None:
            from imswitch.improcess.model.folder_preferences import options_file_path

            path = options_file_path()
        super().__init__(path)


__all__ = ["EditorPreferences", "KEY"]


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
