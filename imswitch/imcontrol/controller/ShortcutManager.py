"""Back-compat shim: ShortcutManager moved to imswitch.imcommon.controller.

The manager has no imcontrol dependencies and is shared by imcontrol and
ImProcess; import it from imswitch.imcommon.controller.ShortcutManager.
"""

from imswitch.imcommon.controller.ShortcutManager import (
    ShortcutManager,
    computePositionerJogDefaults,
)

__all__ = ["ShortcutManager", "computePositionerJogDefaults"]
