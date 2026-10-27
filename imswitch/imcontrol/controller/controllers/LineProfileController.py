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

from ..basecontrollers import ImConWidgetController


class LineProfileController(ImConWidgetController):
    """Linked to LineProfileWidget.

    The panel is the shared Profile widget, which draws through the viewer's
    tool broker, samples the image and follows new frames on its own; there
    is nothing left for a controller to relay. It used to translate
    ViewerToolManager shape changes into plot updates, which is also why the
    profile only changed when the shape did and never with the image.

    Viewer Tools' drawing buttons reach the panel through
    ``ViewerToolsController``, which is handed the widget directly.
    """
