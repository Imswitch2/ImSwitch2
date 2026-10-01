"""Interactive line and rectangle profiles for ImProcess results.

The widget is shared with imcontrol's Line Profile dock and lives in
``imswitch.imcommon.view.guitools.ProfileWidget``. This subclass only names
ImProcess's defaults, so a fix to either panel is a fix to both.
"""

from __future__ import annotations

from imswitch.imcommon.view.guitools.ProfileWidget import (
    ProfileWidget as _SharedProfileWidget,
)


class ProfileWidget(_SharedProfileWidget):
    """Draw line/rectangle ROIs on the reconstruction view and plot their profiles."""

    #: Stable owner key for the shared drawing tool (see ViewerToolService).
    TOOL_OWNER = "improcess.profile"
    EMPTY_HINT = "Draw a line or rectangle on the reconstruction view"


__all__ = ["ProfileWidget"]
