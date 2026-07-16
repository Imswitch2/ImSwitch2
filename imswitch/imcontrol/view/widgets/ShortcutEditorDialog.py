"""imcontrol shortcut editor: shared dialog + setup-JSON persistence.

The dialog itself moved to imswitch.imcommon.view.ShortcutEditorDialog (it has
no imcontrol dependencies); this wrapper keeps the historical constructor
signature and persists overrides into the active setup configuration.
"""

from typing import Optional

from imswitch.imcommon.view.ShortcutEditorDialog import (
    ShortcutEditorDialog as _SharedShortcutEditorDialog,
)
from imswitch.imcommon.controller.ShortcutManager import ShortcutManager


class ShortcutEditorDialog(_SharedShortcutEditorDialog):
    """Shared editor persisting to the imcontrol setup JSON."""

    def __init__(self, parent, shortcutManager: ShortcutManager,
                 setupInfo: Optional[object] = None):
        self._setupInfo = setupInfo
        super().__init__(
            parent, shortcutManager, persistCallback=self._persistToSetupInfo
        )

    def _persistToSetupInfo(self, shortcutsMap) -> None:
        from imswitch.imcontrol.model import configfiletools

        options, _ = configfiletools.loadOptions()
        setupInfo = self._setupInfo
        if setupInfo is None:
            from imswitch.imcontrol.view.guitools import ViewSetupInfo

            setupInfo = configfiletools.loadSetupInfo(options, ViewSetupInfo)
            self._setupInfo = setupInfo

        setupInfo.shortcuts = shortcutsMap
        configfiletools.saveSetupInfo(options, setupInfo)


__all__ = ["ShortcutEditorDialog"]
