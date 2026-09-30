"""The ImProcess workflow editor window.

A sibling of the setup config editor: a non-modal window over a Qt-free
document (:mod:`imswitch.improcess.model.workfloweditor`). Import the
window lazily, from the controller that opens it, so ImProcess does not pay
for pyqtgraph's parameter tree until someone asks for the editor.
"""

from .editor import MainWindow

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
