"""The canvas's visual language: colours by kind and by step, sizes, fonts.

The canvas is always dark (as Blender's node editor is), whatever the rest
of the window uses: cables of every kind's colour must read against it, and a
thumbnail's contrast should not depend on the theme.
"""

from __future__ import annotations

from qtpy import QtGui

CANVAS_BG = "#111921"
GRID_DOT = "#223040"
NODE_BODY = "#1c2732"
NODE_BORDER = "#2e3e4f"
NODE_SELECTED = "#9b9bff"
TEXT = "#e3e6ea"
TEXT_DIM = "#93a0ad"
CODE_TEXT = "#c3d1a3"
CODE_BG = "#121a22"
OUTDATED = "#e8913a"
ERROR = "#e2554f"
DONE = "#46b978"
RUNNING = "#5aa9e6"
HIGHLIGHT = "#ffffff"

#: What each kind of cable looks like, and what the legend calls it.
KIND_COLOURS = {
    "recording": "#a3adb8",
    "image": "#5aa9e6",
    "composite": "#4fd1c5",
    "labels": "#f2a541",
    "curve": "#7bd389",
    "table": "#c792ea",
    "localization": "#ff7a90",
    "rgb": "#f6e05e",
    "any": "#d9dee4",
    "unknown": "#6c7c8c",
}
KIND_WORDS = {
    "recording": "recording",
    "image": "image",
    "composite": "multichannel",
    "labels": "labels",
    "curve": "curve",
    "table": "table",
    "localization": "localizations",
    "rgb": "RGB",
    "any": "decided when run",
    "unknown": "not declared",
}
LEGEND_KINDS = ("recording", "image", "labels", "curve", "table", "localization")

STEP_COLOURS = {
    "source": "#3c9a6e",
    "reconstruct": "#3b8bb5",
    "consolidate": "#3b6db5",
    "save": "#c27a38",
}
CATEGORY_COLOURS = {
    "Filters": "#6666cc",
    "Dimensions and channels": "#8a62c9",
    "Math": "#b2873a",
    "Measurement": "#c4577c",
    "Registration": "#4a9fa0",
    "Restoration": "#5c8fd6",
    "SMLM": "#d0645a",
    "Scripting": "#a8905f",
    "Segmentation": "#5ea85a",
    "Transform": "#7d7fd8",
    "Visualization": "#cf6fb4",
}
OTHER_COLOUR = "#7a8794"

NODE_W = 210
HEADER_H = 44
PORT_R = 6.5
PORT_GAP = 24
PORT_TOP = HEADER_H + 20
THUMB_W, THUMB_H = 190, 106
H_GAP = 64
V_GAP = 36
RADIUS = 9.0


def colour(name: str) -> QtGui.QColor:
    return QtGui.QColor(name)


def kind_colour(kind: str) -> QtGui.QColor:
    return QtGui.QColor(KIND_COLOURS.get(str(kind), KIND_COLOURS["unknown"]))


def step_colour(step_kind: str, category: str = "") -> QtGui.QColor:
    if step_kind in STEP_COLOURS:
        return QtGui.QColor(STEP_COLOURS[step_kind])
    return QtGui.QColor(CATEGORY_COLOURS.get(category, OTHER_COLOUR))


def font(size: float, *, bold: bool = False, mono: bool = False) -> QtGui.QFont:
    if mono:
        result = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    else:
        result = QtGui.QFont()
    result.setPointSizeF(size)
    result.setBold(bold)
    return result


__all__ = [name for name in dir() if name.isupper() or name in ("colour", "kind_colour", "step_colour", "font")]
