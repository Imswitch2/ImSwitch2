"""The workflow editor's model: what it can offer, and what it is editing.

Qt-free. :mod:`catalog` describes every installed plugin the way the runner
sees it; :mod:`document` holds the workflow being edited and the operations
an editor needs on it, each answered with what is now wrong.
"""

from .catalog import PROCESSOR, RECONSTRUCTOR, Catalog, PluginEntry, build_catalog
from .document import DocumentError, PortOption, WorkflowDocument

__all__ = [
    "PROCESSOR",
    "RECONSTRUCTOR",
    "Catalog",
    "DocumentError",
    "PluginEntry",
    "PortOption",
    "WorkflowDocument",
    "build_catalog",
]


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
