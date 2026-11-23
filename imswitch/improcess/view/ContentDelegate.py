"""Two-line list entries: the name, and beneath it an icon and a line saying
what the entry holds.

Used by the results list of the viewer and the file list of the Data dock.
The second line comes from :mod:`imswitch.improcess.model.content_summary`
(an image, a Z-stack of 21 planes, a curve of 128 points, a table of 42
rows, a PSF measurement, ...), so a list of names becomes a list that can be
read without selecting each entry. The icon is a qtawesome icon (Material
Design Icons), drawn in the text colour at reduced opacity so it reads as
metadata, not as a button.

The delegate reads the entry's object from the item's data role 1, which is
where both lists have always kept it (``item.setData(1, obj)``), and never
calls ``setIcon`` on the item: role 1 is Qt's decoration role, so an item
icon would overwrite the object.
"""

from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.content_summary import ContentSummary
from imswitch.improcess.view.icons import namedIcon

#: Item data role the lists keep their object (result / DataObj) under.
OBJECT_ROLE = 1

_PAD_X = 6
_PAD_Y = 3
_GAP = 2
_MUTED_ALPHA = 0.62
_DETAIL_SCALE = 0.86


class ContentDelegate(QtWidgets.QStyledItemDelegate):
    """Draw each item as its name over an icon plus a content line.

    ``describe(obj) -> ContentSummary`` turns the item's object into what to
    show; it is called once per object and state (see :meth:`_state`), so a
    describer may look at a result's data without slowing the list down.
    """

    def __init__(self, describe, parent=None, *, role: int = OBJECT_ROLE):
        super().__init__(parent)
        self._describe = describe
        self._role = role
        self._summaries: dict[int, tuple[object, tuple, ContentSummary]] = {}
        self._icons: dict[tuple[str, str], QtGui.QIcon] = {}

    # -- content ------------------------------------------------------------ #
    def summaryFor(self, index: QtCore.QModelIndex) -> ContentSummary | None:
        obj = index.data(self._role)
        if obj is None:
            return None
        key, state = id(obj), self._state(obj)
        cached = self._summaries.get(key)
        if cached is not None and cached[0] is obj and cached[1] == state:
            return cached[2]
        try:
            summary = self._describe(obj)
        except Exception:
            summary = ContentSummary("unknown", "")
        if len(self._summaries) > 1024:
            self._summaries.clear()
        self._summaries[key] = (obj, state, summary)
        return summary

    @staticmethod
    def _state(obj) -> tuple:
        """What may change on a listed object without the object being
        replaced: a file being loaded into memory or opened."""
        return (
            bool(getattr(obj, "dataMaterialized", False)),
            getattr(obj, "_dataSource", None) is not None,
        )

    def invalidate(self) -> None:
        """Forget the cached summaries (after an object changed in place)."""
        self._summaries.clear()

    # -- geometry ----------------------------------------------------------- #
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if self.summaryFor(index) is None:
            return size
        line = option.fontMetrics.height()
        return QtCore.QSize(size.width(), 2 * line + _GAP + 2 * _PAD_Y)

    # -- painting ----------------------------------------------------------- #
    def paint(self, painter, option, index):
        summary = self.summaryFor(index)
        if summary is None:
            super().paint(painter, option, index)
            return
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QtWidgets.QApplication.style()
        name = opt.text
        # Background and selection only; the text and the (object-valued,
        # hence empty) decoration are ours to draw.
        opt.text = ""
        opt.icon = QtGui.QIcon()
        opt.features &= ~QtWidgets.QStyleOptionViewItem.HasDecoration
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, widget)

        selected = bool(opt.state & QtWidgets.QStyle.State_Selected)
        role = QtGui.QPalette.HighlightedText if selected else QtGui.QPalette.Text
        text_color = opt.palette.color(role)
        muted = QtGui.QColor(text_color)
        muted.setAlphaF(_MUTED_ALPHA)

        rect = opt.rect.adjusted(_PAD_X, _PAD_Y, -_PAD_X, -_PAD_Y)
        metrics = opt.fontMetrics
        line = metrics.height()
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setFont(opt.font)
        painter.setPen(text_color)
        name_rect = QtCore.QRect(rect.left(), rect.top(), rect.width(), line)
        painter.drawText(name_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                         metrics.elidedText(name, QtCore.Qt.TextElideMode.ElideMiddle, rect.width()))

        detail_font = _scaled(opt.font, _DETAIL_SCALE)
        detail_metrics = QtGui.QFontMetrics(detail_font)
        icon_side = max(10, detail_metrics.height())
        icon_rect = QtCore.QRect(rect.left(), rect.top() + line + _GAP, icon_side, icon_side)
        self._icon(summary.icon_name, muted).paint(painter, icon_rect)
        text_left = icon_rect.right() + 2 * _GAP + 1
        detail_rect = QtCore.QRect(text_left, icon_rect.top(), rect.right() - text_left, icon_side)
        painter.setFont(detail_font)
        painter.setPen(muted)
        painter.drawText(detail_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                         detail_metrics.elidedText(summary.line, QtCore.Qt.TextElideMode.ElideRight,
                                                   detail_rect.width()))
        painter.restore()

    def _icon(self, icon_name: str, color: QtGui.QColor) -> QtGui.QIcon:
        key = (icon_name, color.name(QtGui.QColor.HexArgb))
        icon = self._icons.get(key)
        if icon is None:
            icon = namedIcon(icon_name, color=color.name(QtGui.QColor.HexArgb), owner=self.parent())
            self._icons[key] = icon
        return icon

    # -- tooltip ------------------------------------------------------------ #
    def helpEvent(self, event, view, option, index):
        summary = self.summaryFor(index)
        if summary is None or event.type() != QtCore.QEvent.ToolTip:
            return super().helpEvent(event, view, option, index)
        name = index.data(QtCore.Qt.DisplayRole) or ""
        QtWidgets.QToolTip.showText(event.globalPos(), f"{name}\n{summary.line}", view)
        return True


def _scaled(font: QtGui.QFont, factor: float) -> QtGui.QFont:
    scaled = QtGui.QFont(font)
    if font.pointSizeF() > 0:
        scaled.setPointSizeF(max(6.0, font.pointSizeF() * factor))
    elif font.pixelSize() > 0:
        scaled.setPixelSize(max(8, int(round(font.pixelSize() * factor))))
    return scaled


__all__ = ["ContentDelegate", "OBJECT_ROLE"]
