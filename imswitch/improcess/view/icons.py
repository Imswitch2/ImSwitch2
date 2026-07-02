"""Semantic icons for ImProcess toolbars and menus."""

from __future__ import annotations

from qtpy import QtGui, QtWidgets


IMPROCESS_ICON_NAMES = {
    "auto-contrast": "mdi6.auto-fix",
    "brightness-contrast": "mdi6.brightness-6",
    "reset-contrast": "mdi6.backup-restore",
    "channels": "mdi6.layers-triple",
    "duplicate": "mdi6.content-duplicate",
    "crop-substack": "mdi6.crop",
    "max-projection": "mdi6.arrow-collapse-down",
    "split-stack": "mdi6.call-split",
    "split-channels": "mdi6.arrow-split-vertical",
    "merge-channels": "mdi6.call-merge",
    "make-composite": "mdi6.layers",
    "make-rgb": "mdi6.palette",
    "reset-view": "mdi6.fit-to-screen",
    "roi-manager": "mdi6.vector-rectangle",
    "projection": "mdi6.axis-arrow",
    "segmentation": "mdi6.select-group",
    "frc": "mdi6.chart-line",
    "psf-resolution": "mdi6.bullseye-arrow",
    "colocalization": "mdi6.chart-scatter-plot",
    "multicolor-registration": "mdi6.palette-swatch",
}

_FALLBACK_STANDARDS = {
    "auto-contrast": QtWidgets.QStyle.SP_DialogApplyButton,
    "brightness-contrast": QtWidgets.QStyle.SP_FileDialogDetailedView,
    "reset-contrast": QtWidgets.QStyle.SP_BrowserReload,
    "channels": QtWidgets.QStyle.SP_FileDialogInfoView,
    "duplicate": QtWidgets.QStyle.SP_FileDialogNewFolder,
    "crop-substack": QtWidgets.QStyle.SP_DialogSaveButton,
    "max-projection": QtWidgets.QStyle.SP_ArrowDown,
    "split-stack": QtWidgets.QStyle.SP_FileDialogListView,
    "split-channels": QtWidgets.QStyle.SP_DirIcon,
    "merge-channels": QtWidgets.QStyle.SP_DialogOpenButton,
    "make-composite": QtWidgets.QStyle.SP_FileDialogContentsView,
    "make-rgb": QtWidgets.QStyle.SP_DriveHDIcon,
    "reset-view": QtWidgets.QStyle.SP_ComputerIcon,
    "roi-manager": QtWidgets.QStyle.SP_FileDialogListView,
    "projection": QtWidgets.QStyle.SP_ArrowDown,
    "segmentation": QtWidgets.QStyle.SP_DialogApplyButton,
    "frc": QtWidgets.QStyle.SP_BrowserReload,
    "psf-resolution": QtWidgets.QStyle.SP_DialogHelpButton,
    "colocalization": QtWidgets.QStyle.SP_DirLinkIcon,
    "multicolor-registration": QtWidgets.QStyle.SP_DriveNetIcon,
}


def improcessIcon(action_id: str, owner=None) -> QtGui.QIcon:
    """Return a semantic toolbar icon, falling back to Qt built-ins."""
    icon_name = IMPROCESS_ICON_NAMES.get(str(action_id))
    if icon_name:
        icon = _qtawesomeIcon(icon_name, owner)
        if icon is not None:
            return icon
    return _fallbackIcon(str(action_id), owner)


def _qtawesomeIcon(icon_name: str, owner=None) -> QtGui.QIcon | None:
    try:
        import qtawesome as qta
    except Exception:
        return None
    try:
        return qta.icon(icon_name, color=_paletteIconColor(owner))
    except Exception:
        return None


def _fallbackIcon(action_id: str, owner=None) -> QtGui.QIcon:
    style = _style(owner)
    standard = _FALLBACK_STANDARDS.get(action_id, QtWidgets.QStyle.SP_FileIcon)
    return style.standardIcon(standard)


def _style(owner=None):
    if owner is not None and hasattr(owner, "style"):
        return owner.style()
    app = QtWidgets.QApplication.instance()
    if app is not None:
        return app.style()
    return QtWidgets.QApplication.style()


def _paletteIconColor(owner=None) -> str:
    if owner is not None and hasattr(owner, "palette"):
        palette = owner.palette()
    else:
        app = QtWidgets.QApplication.instance()
        palette = app.palette() if app is not None else QtGui.QPalette()
    return palette.color(QtGui.QPalette.ButtonText).name()


__all__ = ["IMPROCESS_ICON_NAMES", "improcessIcon"]
