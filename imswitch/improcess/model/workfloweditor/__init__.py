"""The workflow editor's model, as ImProcess uses it.

The model lives in :mod:`imswitch.imcommon.model.workfloweditor` (shared
with ImControl's pipeline designer); this package re-exports it under the
path ImProcess always used and adds what is ImProcess's own:
:func:`build_catalog` (:mod:`.catalog`), a document from a result's
provenance (:mod:`.document`), :func:`execute` (:mod:`.session`), the
recipes (:mod:`.recipes`), and ImProcess's workflow folder and options file
(:mod:`.folders`, :mod:`.preferences`).
"""

from imswitch.imcommon.model.workfloweditor import (
    PROCESSOR,
    RECONSTRUCTOR,
    Catalog,
    DocumentError,
    PluginEntry,
    PortOption,
    WorkflowDocument,
)

from . import document as _document  # noqa: F401 - gives WorkflowDocument.from_result
from .catalog import build_catalog

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
