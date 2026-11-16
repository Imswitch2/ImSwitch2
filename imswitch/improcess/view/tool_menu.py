"""Analysis-tool menus grouped by category, Fiji style.

Clicking the toolbar's *Tools* button (or *Tools › Analysis tools* in the
menu bar) shows the categories; hovering one shows its tools. Every tool is
listed, loaded or not: a tool whose panel is already open is ticked, and
picking it brings that panel to the front instead of loading it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from qtpy import QtCore, QtWidgets

from imswitch.improcess.processors.categories import OTHER_CATEGORY, category_sort_key


@dataclass(frozen=True)
class ToolMenuEntry:
    """One tool as a menu shows it."""

    id: str
    title: str
    category: str = OTHER_CATEGORY
    description: str = ""
    open: bool = False


def as_tool_menu_entry(item) -> ToolMenuEntry:
    """A :class:`ToolMenuEntry` from an entry or an ``(id, title[, category[,
    description[, open]]])`` tuple."""
    if isinstance(item, ToolMenuEntry):
        return item
    tool_id, title, *rest = tuple(item)
    category = rest[0] if len(rest) > 0 else OTHER_CATEGORY
    description = rest[1] if len(rest) > 1 else ""
    is_open = bool(rest[2]) if len(rest) > 2 else False
    return ToolMenuEntry(
        id=str(tool_id),
        title=str(title or tool_id),
        category=str(category or OTHER_CATEGORY),
        description=str(description or ""),
        open=is_open,
    )


def populate_category_menu(
    menu: QtWidgets.QMenu,
    entries: Iterable,
    on_pick: Callable[[str], None],
    *,
    placeholder: str,
    footer_actions: Sequence[QtWidgets.QAction] = (),
) -> dict[str, QtWidgets.QAction]:
    """Rebuild ``menu`` as one submenu per category; returns the tool actions by id.

    Categories follow :func:`~imswitch.improcess.processors.categories.category_sort_key`,
    tools within one by title. ``on_pick`` receives the id of the tool picked.
    With no entries the menu holds ``placeholder``, disabled. ``footer_actions``
    (e.g. *Browse online plugins…*) follow a separator at the bottom.
    """
    for submenu in menu.findChildren(QtWidgets.QMenu, options=QtCore.Qt.FindDirectChildrenOnly):
        submenu.deleteLater()
    menu.clear()
    menu.setToolTipsVisible(True)

    groups: dict[str, list[ToolMenuEntry]] = {}
    for item in entries:
        entry = as_tool_menu_entry(item)
        groups.setdefault(entry.category, []).append(entry)

    actions: dict[str, QtWidgets.QAction] = {}
    if not groups:
        menu.addAction(placeholder).setEnabled(False)
    for category in sorted(groups, key=category_sort_key):
        submenu = menu.addMenu(category)
        submenu.setToolTipsVisible(True)
        submenu.setObjectName(f"toolCategory:{category}")
        for entry in sorted(groups[category], key=lambda e: (e.title.casefold(), e.id)):
            action = submenu.addAction(entry.title)
            tooltip = entry.description or f"Open the {entry.title} panel"
            if entry.open:
                tooltip = f"{tooltip} (open: brings its panel to the front)"
                action.setCheckable(True)
                action.setChecked(True)
            action.setToolTip(tooltip)
            action.setStatusTip(tooltip)
            action.setData(entry.id)
            action.triggered.connect(lambda _checked=False, tool_id=entry.id: on_pick(tool_id))
            actions[entry.id] = action
    if footer_actions:
        menu.addSeparator()
        for action in footer_actions:
            menu.addAction(action)
    return actions


def make_menu_button(menu: QtWidgets.QMenu, text: str, tooltip: str) -> QtWidgets.QToolButton:
    """A toolbar button that opens ``menu`` on click, labelled ``text ▾``."""
    button = QtWidgets.QToolButton()
    button.setText(text)
    button.setToolTip(tooltip)
    button.setStatusTip(tooltip)
    button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
    button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
    # Room for the drop-down arrow, which the dark theme draws over the
    # label's last letter otherwise.
    button.setStyleSheet("QToolButton { padding-right: 14px; }")
    button.setMenu(menu)
    return button


__all__ = ["ToolMenuEntry", "as_tool_menu_entry", "make_menu_button", "populate_category_menu"]
