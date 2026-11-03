"""The workflow editor's model: what it can offer, and what it is editing.

Qt-free, and free of any host module: :mod:`catalog` describes every plugin
the host's registry offers (the host builds it; ImProcess in
``imswitch.improcess.model.workfloweditor.catalog.build_catalog``);
:mod:`document` holds the workflow being edited and the operations an
editor needs on it, each answered with what is now wrong; :mod:`session`
plans and caches runs. Nothing here imports ``imswitch.improcess``: what
the editor needs of a host comes through the catalog and its registry.
"""

from .catalog import PROCESSOR, RECONSTRUCTOR, Catalog, PluginEntry
from .document import DocumentError, PortOption, WorkflowDocument

__all__ = [
    "PROCESSOR",
    "RECONSTRUCTOR",
    "Catalog",
    "DocumentError",
    "PluginEntry",
    "PortOption",
    "WorkflowDocument",
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
