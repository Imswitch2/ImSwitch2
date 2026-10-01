"""Where workflow files live by default: ``~/ImSwitchConfig/improcess_workflows``.

A sibling of the drop-in plugins folder, so a user who has one has the
other next to it. The editor's file list and its open/save dialogs start
here; a workflow can of course be opened from anywhere.
"""

import os

from imswitch.imcommon.model import dirtools

_WORKFLOWS_SUBDIR = "improcess_workflows"


def workflows_directory(*, create: bool = True) -> str:
    """The default workflow folder, created on first use when ``create``."""
    directory = os.path.join(dirtools.UserFileDirs.Root, _WORKFLOWS_SUBDIR)
    if create:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            # A read-only home must not break the editor; dialogs then start
            # wherever Qt starts them.
            pass
    return directory


__all__ = ["workflows_directory"]


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
