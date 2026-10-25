"""The parts of the Expert scan panel (docs/design/plans/
scan-engines-and-expert-scan.md §4, §7.11, §7.12): the scan described top to
bottom -- the order of its dimensions, where each positioner goes, what happens
within one step, its pauses and what each detector records -- beside whether
the scan engine can run it and a preview with every device on its own lane.

The panel speaks the user's words and the model keeps its own (§7.12): a
loop is a *dimension* here, a drive a *positioner*, the step length the
*dwell*, an event a *pulse*, an *exposure*, a *power level* or a *move*.

Every part edits the panel's tables (``ScanWidgetExpert``): they stay the one
state, so a value typed here and in the tables is the same value, and one that
does not parse is reported as before. A part keeps no program of its own; it
is drawn again from the tables after every edit, leaving alone the field the
user is typing in, and writes a field only when the user changed it.
"""

from __future__ import annotations

import math
import re
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

#: How each loop kind reads on the panel: (one, many).
KIND_NAMES = {
    'scan_x': ('X position', 'X positions'), 'scan_y': ('Y position', 'Y positions'),
    'scan_z': ('Z position', 'Z positions'), 'cycle': ('cycle', 'cycles'),
    'plane': ('plane', 'planes'), 'time': ('timepoint', 'timepoints'),
    'condition': ('phase', 'phases'), 'repeat': ('repeat', 'repeats'),
    'tile': ('tile', 'tiles'), 'position': ('position', 'positions'),
}

#: A dimension's order, in words.
TRAVERSAL_WORDS = {'forward': 'forward', 'reverse': 'backward', 'serpentine': 'serpentine'}

#: The model's values in words, for the choices the parts offer.
TRANSITION_WORDS = {'jump': 'jumps', 'linear': 'moves linearly', 'smooth': 'moves smoothly'}
PLACEMENT_WORDS = {'in-step': 'at the end of each step', 'between-steps': 'between steps'}
ORIGIN_WORDS = {'absolute': 'absolute', 'captured': 'relative to where it starts'}
RESTORE_WORDS = {'captured': 'back to where it started', 'first': 'back to its first position',
                 'park': 'to its park position', 'hold': 'it stays where it ended'}
EVENT_WORDS = {'ttl': 'pulse', 'detector': 'exposure', 'level': 'power',
               'offset': 'move', 'marker': 'marker'}
SHAPE_WORDS = {'hold': 'held', 'ramp': 'ramped'}
DURING_WORDS = {'stationary': 'while the stage stands still', 'any': 'any time, even while moving'}
TIMEOUT_WORDS = {'fail': 'the scan fails', 'continue': 'it carries on'}
HOLD_WORDS = {'idle': 'off', 'hold': 'held as they are'}

#: What a verdict looks like in the check: (glyph, colour role).
VERDICT_STYLE = {'native': ('✓', 'ok'), 'emulated': ('~', 'warn'),
                 'impossible': ('✕', 'bad'), 'unknown': ('?', 'muted')}

#: Colours for the roles above: (on a dark theme, on a light one).
ROLE_COLORS = {'ok': ('#5DCAA5', '#0F6E56'), 'warn': ('#EF9F27', '#854F0B'),
               'bad': ('#F09595', '#A32D2D'), 'muted': ('#B4B2A9', '#5F5E5A'),
               'accent': ('#85B7EB', '#185FA5')}

#: A device with no wavelength: positioners and detectors.
POSITIONER_COLOR = '#F0997B'
DETECTOR_COLOR = '#B4B2A9'


def dark_theme() -> bool:
    """Whether the panel is drawn on a dark background: ImSwitch's own style
    sheet (qdarkstyle), else the palette."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        return True
    sheet = app.styleSheet() or ''
    if sheet:
        try:
            from qdarkstyle import DarkPalette
            if DarkPalette.COLOR_BACKGROUND_1.lower() in sheet.lower():
                return True
        except ImportError:
            pass
    return app.palette().color(QtGui.QPalette.Window).lightness() < 128


def role_color(role) -> str:
    dark, light = ROLE_COLORS[role]
    return dark if dark_theme() else light


def wavelength_color(nm: float) -> QtGui.QColor:
    """The colour of light of ``nm`` nanometres (Bruton's approximation of
    the visible spectrum), lifted towards white so a deep violet or red still
    reads on a dark panel. Past the visible red (a STED or two-photon line)
    it turns crimson, so 640 and 775 nm stay apart."""
    nm = max(float(nm), 380.0)
    if nm < 440:
        r, g, b = (440 - nm) / 60, 0.0, 1.0
    elif nm < 490:
        r, g, b = 0.0, (nm - 440) / 50, 1.0
    elif nm < 510:
        r, g, b = 0.0, 1.0, (510 - nm) / 20
    elif nm < 580:
        r, g, b = (nm - 510) / 70, 1.0, 0.0
    elif nm < 645:
        r, g, b = 1.0, (645 - nm) / 65, 0.0
    elif nm <= 700:
        r, g, b = 1.0, 0.0, 0.0
    else:
        towards = min(1.0, (nm - 700) / 100)        # 800 nm and beyond: all the way
        r, g, b = 1.0 - 0.3 * towards, 0.0, 0.55 * towards
    lift = 0.35        # a third of the way to white: the dim ends stay visible
    return QtGui.QColor.fromRgbF(r + (1 - r) * lift, g + (1 - g) * lift, b + (1 - b) * lift)


def device_color(name: str, wavelengths: Dict[str, float], kind: str) -> QtGui.QColor:
    if name in wavelengths and wavelengths[name]:
        return wavelength_color(wavelengths[name])
    return QtGui.QColor(POSITIONER_COLOR if kind == 'positioner' else DETECTOR_COLOR)


def ms_text(seconds) -> str:
    """Seconds as ms, as many digits as the value has."""
    return f'{float(seconds) * 1000.0:.12g}'


def duration_text(seconds: float) -> str:
    if seconds >= 60:
        return f'{seconds / 60:.3g} min'
    if seconds >= 1:
        return f'{seconds:.4g} s'
    if seconds >= 1e-3:
        return f'{seconds * 1e3:.4g} ms'
    return f'{seconds * 1e6:.4g} µs'


def _quiet_set(widget, setter, value):
    """Set a field the user is not typing in, without emitting its signals."""
    if widget.hasFocus():
        return
    blocker = QtCore.QSignalBlocker(widget)
    try:
        setter(value)
        if isinstance(widget, QtWidgets.QLineEdit):
            widget.setModified(False)
    finally:
        del blocker


def _set_combo(combo: QtWidgets.QComboBox, value: str):
    index = combo.findData(value)
    if index < 0:
        index = combo.findText(value)
    if index < 0 and value:
        combo.addItem(value, value)          # a value the choices lack stays visible
        index = combo.count() - 1
    if index >= 0:
        _quiet_set(combo, combo.setCurrentIndex, index)


def _combo(choices, words=None, tooltip='') -> QtWidgets.QComboBox:
    combo = QtWidgets.QComboBox()
    for value in choices:
        combo.addItem((words or {}).get(value, value), value)
    combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
    if tooltip:
        combo.setToolTip(tooltip)
    return combo


def _field(width_chars: int, tooltip='') -> QtWidgets.QLineEdit:
    line = QtWidgets.QLineEdit()
    metrics = line.fontMetrics()
    line.setFixedWidth(metrics.horizontalAdvance('0' * width_chars) + metrics.height())
    if tooltip:
        line.setToolTip(tooltip)
    return line


def _on_commit(line: QtWidgets.QLineEdit, write: Callable[[str], None]):
    """Write ``line``'s text when the user leaves a value they changed: a
    field left untouched writes nothing, so it cannot land on a row that moved
    under it."""
    def commit():
        if line.isModified():
            line.setModified(False)
            write(line.text())
    line.editingFinished.connect(commit)


def _muted(text='') -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setObjectName('expertMuted')
    label.setWordWrap(True)
    return label


def _group(*widgets) -> QtWidgets.QWidget:
    """Widgets that wrap together: a label stays with its field."""
    host = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(max(2, host.fontMetrics().averageCharWidth() // 2))
    for widget in widgets:
        layout.addWidget(QtWidgets.QLabel(widget) if isinstance(widget, str) else widget)
    return host


def _remove_button(tooltip, slot) -> QtWidgets.QToolButton:
    button = QtWidgets.QToolButton()
    button.setText('✕')
    button.setAutoRaise(True)
    button.setToolTip(tooltip)
    button.clicked.connect(slot)
    return button


class WheelGuard(QtCore.QObject):
    """Scrolling over a field the user has not clicked into scrolls the panel
    instead of changing the field's value: in a long panel of inline fields,
    the wheel would otherwise edit the program on its way down."""

    def __init__(self, scrollArea: QtWidgets.QScrollArea, parent=None):
        super().__init__(parent)
        self._area = scrollArea

    def watch(self, root: QtWidgets.QWidget):
        for kind in (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox):
            for widget in root.findChildren(kind):
                if not widget.property('expertWheelGuard'):
                    widget.setProperty('expertWheelGuard', True)
                    widget.setFocusPolicy(QtCore.Qt.StrongFocus)
                    widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Wheel and not obj.hasFocus():
            QtWidgets.QApplication.sendEvent(self._area.viewport(), event)
            return True
        return False


class FlowLayout(QtWidgets.QLayout):
    """Items left to right, wrapping onto the next line when the width runs out
    (the Qt flow-layout example)."""

    def __init__(self, parent=None, spacing=None):
        super().__init__(parent)
        self._items: List[QtWidgets.QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QtCore.QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, dry=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QtCore.QSize(margins.left() + margins.right(),
                                   margins.top() + margins.bottom())

    def _gap(self):
        if self._spacing is not None:
            return self._spacing
        return QtWidgets.QApplication.style().pixelMetric(
            QtWidgets.QStyle.PM_LayoutHorizontalSpacing) or 6

    def _arrange(self, rect, dry):
        x, y, line = rect.x(), rect.y(), 0
        gap = self._gap()
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > rect.right() and line > 0:
                x, y, line = rect.x(), y + line + gap, 0
            if not dry:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += hint.width() + gap
            line = max(line, hint.height())
        return y + line - rect.y()


class Card(QtWidgets.QFrame):
    """A titled part of the panel."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName('expertCard')
        self.headerHost = QtWidgets.QWidget()
        self.header = FlowLayout(self.headerHost)
        self.titleLabel = QtWidgets.QLabel(title)
        self.titleLabel.setObjectName('expertCardTitle')
        self.header.addWidget(self.titleLabel)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.headerHost)
        layout.addLayout(self.body)


class Chip(QtWidgets.QLabel):
    """A short fact in a pill, in a role's colour (``ok``, ``warn``, ``bad``,
    ``muted``, ``accent``) or neutral."""

    clicked = QtCore.Signal()

    def __init__(self, text='', role=None, parent=None):
        super().__init__(text, parent)
        self.setObjectName('expertChip')
        self.setRole(role)

    def setRole(self, role):
        if role not in ROLE_COLORS:
            self.setStyleSheet('')
            return
        color = role_color(role)
        self.setStyleSheet(f'QLabel#expertChip {{ color: {color}; border-color: {color}; }}')

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def _menu_button(text, tooltip='') -> QtWidgets.QToolButton:
    button = QtWidgets.QToolButton()
    button.setText(text)
    button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
    button.setMenu(QtWidgets.QMenu(button))
    if tooltip:
        button.setToolTip(tooltip)
    return button


def _clear(layout, keep=()):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None and widget not in keep:
            widget.deleteLater()


# ---------------------------------------------------------------------------
# The scan order: the loops, outermost first
# ---------------------------------------------------------------------------

class VisitStrip(QtWidgets.QWidget):
    """The positions one positioner visits, step by step, as a line through
    points: an interleaved or serpentine order is seen at a glance. The
    points are shaded by the outer of the two dimensions shown, and the
    lowest and highest positions are written at the right edge."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._positions: List[float] = []
        self._shades: List[int] = []
        self._color = QtGui.QColor(POSITIONER_COLOR)

    def barsThatFit(self) -> int:
        return max(1, self.width() // max(2, self.fontMetrics().averageCharWidth()))

    def setVisits(self, positions, shades, color):
        self._positions, self._shades = list(positions), list(shades)
        self._color = QtGui.QColor(color)
        self.update()

    def sizeHint(self):
        return QtCore.QSize(self.fontMetrics().averageCharWidth() * 40,
                            self.fontMetrics().height() * 4)

    def minimumSizeHint(self):
        return QtCore.QSize(0, self.fontMetrics().height() * 4)

    def paintEvent(self, _event):
        if not self._positions:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        metrics = self.fontMetrics()
        low, high = min(self._positions), max(self._positions)
        span = (high - low) or 1.0
        labels = [f'{high:.6g}', f'{low:.6g}'] if high > low else [f'{low:.6g} µm']
        labelW = max(metrics.horizontalAdvance(t) for t in labels) + metrics.averageCharWidth()
        radius = max(2.0, metrics.height() / 5)
        plotW = max(1.0, self.width() - labelW - 2 * radius)
        top, bottom = radius + 1, self.height() - radius - 1
        gap = plotW / len(self._positions)
        text = self.palette().color(QtGui.QPalette.WindowText)
        muted = QtGui.QColor(text)
        muted.setAlphaF(0.6)

        def point(index, position):
            return QtCore.QPointF(radius + gap * (index + 0.5),
                                  bottom - (position - low) / span * (bottom - top))

        points = [point(i, p) for i, p in enumerate(self._positions)]
        line = QtGui.QColor(self._color)
        line.setAlphaF(0.6)
        painter.setPen(QtGui.QPen(line, 1.2))
        if len(points) > 1:
            painter.drawPolyline(QtGui.QPolygonF(points))
        painter.setPen(QtCore.Qt.NoPen)
        for centre, shade in zip(points, self._shades):
            color = QtGui.QColor(self._color)
            color = color.lighter(100 + 35 * (shade % 3)) if shade % 2 else color.darker(
                100 + 25 * (shade % 3))
            painter.setBrush(color)
            painter.drawEllipse(centre, radius, radius)
        painter.setPen(muted)
        x = self.width() - labelW + metrics.averageCharWidth()
        if high > low:
            painter.drawText(QtCore.QPointF(x, top + metrics.ascent() / 2), labels[0])
            painter.drawText(QtCore.QPointF(x, bottom + metrics.ascent() / 2), labels[1])
        else:
            painter.drawText(QtCore.QPointF(x, (top + bottom) / 2 + metrics.ascent() / 2),
                             labels[0])
        painter.end()


class OrderCard(Card):
    """The dimensions as nested blocks, slowest first, each with its count
    and order; the pauses before a dimension's iterations as a badge; the
    order the first positioner visits its positions in."""

    def __init__(self, panel, parent=None):
        super().__init__('Scan order', parent)
        self._panel = panel
        self.header.addWidget(_muted('slowest to fastest'))
        self.flowHost = QtWidgets.QWidget()
        self.flow = FlowLayout(self.flowHost)
        self.body.addWidget(self.flowHost)
        self.addButton = _menu_button('+ Dimension', 'Add a dimension inside the others: '
                                                    'timepoints, planes, cycles, tiles, ...')
        for kind, (_one, many) in KIND_NAMES.items():
            action = self.addButton.menu().addAction(many.capitalize())
            action.triggered.connect(lambda _c=False, k=kind: panel.addLoop(k))
        self.strip = VisitStrip()
        self.stripLabel = _muted()
        self.body.addWidget(self.strip)
        self.body.addWidget(self.stripLabel)
        self._chips: List[_LoopChip] = []
        self._signature = None

    def refresh(self):
        loops = self._panel.tableRows('loops')
        waits = self._panel.tableRows('waits')
        signature = tuple((row['id'], row['kind']) for row in loops)
        if signature != self._signature:
            self._rebuild(loops)
            self._signature = signature
        kinds = [row['kind'] for row in loops]
        for chip, row in zip(self._chips, loops):
            chip.update_from(row, waits, kinds.count(row['kind']) > 1)
        self._drawStrip()

    def _rebuild(self, loops):
        _clear(self.flow, keep=(self.addButton,))
        self._chips = []
        for index, _row in enumerate(loops):
            if index:
                arrow = QtWidgets.QLabel('›')
                arrow.setObjectName('expertMuted')
                self.flow.addWidget(arrow)
            chip = _LoopChip(self._panel, index)
            self._chips.append(chip)
            self.flow.addWidget(chip)
        self.flow.addWidget(self.addButton)
        self.flowHost.updateGeometry()

    def _drawStrip(self):
        drawn = self._panel.visitStrip(self.strip.barsThatFit())
        if drawn[0] is None:
            self.strip.setVisits([], [], POSITIONER_COLOR)
            self.stripLabel.setText(drawn[1])
            self.strip.setVisible(bool(self._panel.tableRows('drives')))
            return
        positions, shades, color, text = drawn
        self.strip.setVisible(True)
        self.strip.setVisits(positions, shades, color)
        self.stripLabel.setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._drawStrip()


class _LoopChip(QtWidgets.QFrame):
    def __init__(self, panel, index: int):
        super().__init__()
        self.setObjectName('expertLoop')
        self._panel, self._index = panel, index
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 4, 2)
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(1, 2 ** 31 - 1)
        self.count.setKeyboardTracking(False)
        self.count.valueChanged.connect(
            lambda v: panel.setTableValue('loops', self._index, 'count', str(v)))
        self.name = QtWidgets.QLabel()
        # the outermost dimension has none outside it to alternate with
        orders = ('forward', 'reverse', 'serpentine') if index else ('forward', 'reverse')
        self.order = _combo(orders, TRAVERSAL_WORDS,
                            'The order its iterations run in. Serpentine: every other pass '
                            'of the dimension outside it runs backwards.')
        self.order.currentIndexChanged.connect(
            lambda _i: self._setOrder(self.order.currentData(), 1))
        self.badge = QtWidgets.QLabel()
        self.badge.setObjectName('expertMuted')
        self.more = _menu_button('⋯', 'Rename, move, change or remove this dimension.')
        self.more.setAutoRaise(True)
        self.more.menu().aboutToShow.connect(self._fillMenu)
        for widget in (self.count, self.name, self.order, self.badge, self.more):
            layout.addWidget(widget)

    def update_from(self, row, waits, ambiguous):
        kind = row['kind']
        one, many = KIND_NAMES.get(kind, (kind, kind))
        try:
            count = int(row['count'])
            _quiet_set(self.count, self.count.setValue, count)
            self.count.setStyleSheet('')
            self.count.setToolTip('How many iterations.')
        except ValueError:
            count = 0
            self.count.setStyleSheet(f'color: {role_color("bad")};')
            self.count.setToolTip(f'The table holds {row["count"]!r}, not a count.')
        name = one if count == 1 else many
        self.name.setText(f'{name} ({row["id"]})' if ambiguous else name)
        self.name.setToolTip(f'Dimension {row["id"]!r} ({kind}). The positioners\' formulas '
                             'and the events\' anchors name it by its id.')
        traversal = row['traversal'] or 'forward'
        _set_combo(self.order, traversal)
        alternates = f', alternating with {row["parity_loops"]}' if row['parity_loops'] else ''
        self.order.setToolTip(f'Order: {TRAVERSAL_WORDS.get(traversal, traversal)}'
                              f'{alternates}.')
        here = [w for w in waits if w['at'] == row['id']]
        self.badge.setText(', '.join(_wait_badge(w) for w in here))
        self.badge.setVisible(bool(here))

    def _setOrder(self, traversal, outside):
        """``traversal``; a serpentine alternates with the ``outside``
        dimensions right outside it, outermost first -- the only parity the
        model takes."""
        loops = self._panel.tableRows('loops')
        parity = ([loop['id'] for loop in loops[self._index - outside:self._index]]
                  if traversal == 'serpentine' else [])
        self._panel.setTableValue('loops', self._index, 'parity_loops', ', '.join(parity))
        self._panel.setTableValue('loops', self._index, 'traversal', traversal)

    def _fillMenu(self):
        menu = self.more.menu()
        menu.clear()
        panel = self._panel
        loops = panel.tableRows('loops')
        row = loops[self._index]
        rename = menu.addAction('Rename…')
        rename.triggered.connect(self._rename)
        outwards = menu.addAction('Make slower (move outwards)')
        outwards.setEnabled(self._index > 0)
        outwards.triggered.connect(lambda: panel.moveLoop(self._index, -1))
        inwards = menu.addAction('Make faster (move inwards)')
        inwards.setEnabled(self._index < len(loops) - 1)
        inwards.triggered.connect(lambda: panel.moveLoop(self._index, +1))
        kinds = menu.addMenu('Kind')
        for kind, (_one, many) in KIND_NAMES.items():
            action = kinds.addAction(many.capitalize())
            action.setCheckable(True)
            action.setChecked(kind == row['kind'])
            action.triggered.connect(
                lambda _c=False, k=kind: panel.setTableValue('loops', self._index, 'kind', k))
        if row['traversal'] == 'serpentine' and self._index:
            parity = menu.addMenu('Serpentine alternates with')
            group = QtWidgets.QActionGroup(parity)
            current = [p.strip() for p in row['parity_loops'].split(',') if p.strip()]
            for outside in range(1, self._index + 1):
                ids = [loop['id'] for loop in loops[self._index - outside:self._index]]
                action = parity.addAction(', '.join(ids))
                action.setCheckable(True)
                action.setChecked(ids == current)
                group.addAction(action)
                action.triggered.connect(
                    lambda _c=False, n=outside: self._setOrder('serpentine', n))
        menu.addSeparator()
        remove = menu.addAction('Remove')
        remove.triggered.connect(lambda: panel.removeLoop(self._index))

    def _rename(self):
        row = self._panel.tableRows('loops')[self._index]
        name, ok = QtWidgets.QInputDialog.getText(
            self, 'Rename dimension', 'Its id, as the positioners\' formulas and the events '
                                      'name it:', text=row['id'])
        if ok and name.strip() and name.strip() != row['id']:
            self._panel.renameLoop(self._index, name.strip())


def _wait_badge(wait) -> str:
    if wait['trigger'] == 'time':
        return f'pause {wait["interval_s"] or "?"} ms'
    return 'wait for a trigger'


# ---------------------------------------------------------------------------
# Positions: each positioner as a sentence over the dimensions
# ---------------------------------------------------------------------------

class PositionsCard(Card):
    def __init__(self, panel, parent=None):
        super().__init__('Positions', parent)
        self._panel = panel
        self.rowsHost = QtWidgets.QVBoxLayout()
        self.body.addLayout(self.rowsHost)
        self.addButton = _menu_button('+ Positioner', 'Move a positioner with the dimensions.')
        self.addButton.menu().aboutToShow.connect(self._fillAdd)
        self.header.addWidget(self.addButton)
        self.empty = _muted('No positioner moves: the scan switches lasers and detectors only.')
        self.body.addWidget(self.empty)
        self._editors: List[_DriveEditor] = []
        self._signature = None

    def _fillAdd(self):
        menu = self.addButton.menu()
        menu.clear()
        for name in self._panel.devices.get('positioner', ()):
            action = menu.addAction(name)
            action.triggered.connect(lambda _c=False, n=name: self._panel.addDrive(n))

    def refresh(self):
        drives = self._panel.tableRows('drives')
        loops = self._panel.loopNames()
        signature = (len(drives), tuple(loops.items()))
        if signature != self._signature:
            for editor in self._editors:
                editor.setParent(None)
                editor.deleteLater()
            self._editors = [_DriveEditor(self._panel, index, loops)
                             for index in range(len(drives))]
            for editor in self._editors:
                self.rowsHost.addWidget(editor)
            self._signature = signature
        for index, (editor, row) in enumerate(zip(self._editors, drives)):
            editor.update_from(row, self._panel.driveRange(index))
        self.empty.setVisible(not drives)


class _DriveEditor(QtWidgets.QFrame):
    """One positioner as two sentences: where it goes (*Z starts at 1 µm,
    moves 0.25 µm per cycle and 0.5 µm per plane*; a dimension that does not
    move it is behind *+ also per…*) and how it moves."""

    def __init__(self, panel, index, loops: Dict[str, str]):
        super().__init__()
        self.setObjectName('expertRow')
        self._panel, self._index, self._loops = panel, index, dict(loops)
        self._revealed = set()
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)

        formulaHost = QtWidgets.QWidget()
        formula = FlowLayout(formulaHost)
        self.device = _combo(panel.devices.get('positioner', ()))
        self.device.currentIndexChanged.connect(
            lambda _i: self._write('device', self.device.currentData()))
        self.offset = _field(6, 'Where it is at the first iteration of every dimension, in µm.')
        _on_commit(self.offset, lambda t: self._write('offset_um', t))
        formula.addWidget(_group(self.device, 'starts at', self.offset, 'µm'))
        self.weights = {}
        for loop, name in self._loops.items():
            weight = _field(5, f'How far it moves for each {name} ({loop}), in µm. '
                               'Blank: this dimension does not move it.')
            weight.setPlaceholderText('0')
            _on_commit(weight, lambda t, lp=loop: self._write(f'w:{lp}', t))
            lead = QtWidgets.QLabel('moves')
            group = _group(lead, weight, f'µm per {name}')
            formula.addWidget(group)
            self.weights[loop] = (group, weight, lead)
        self.alsoButton = _menu_button('+ also per…', 'Move it with another dimension too.')
        self.alsoButton.menu().aboutToShow.connect(self._fillAlso)
        formula.addWidget(self.alsoButton)
        self.tableText = QtWidgets.QLabel()
        self.tableText.setObjectName('expertMuted')
        formula.addWidget(self.tableText)
        outer.addWidget(formulaHost)
        self.range = _muted()
        outer.addWidget(self.range)

        motionHost = QtWidgets.QWidget()
        motion = FlowLayout(motionHost)
        self.transition = _combo(('jump', 'linear', 'smooth'), TRANSITION_WORDS,
                                 'How it goes from one position to the next.')
        self.transition.currentIndexChanged.connect(
            lambda _i: self._write('transition', self.transition.currentData()))
        self.placement = _combo(('in-step', 'between-steps'), PLACEMENT_WORDS,
                                'At the end of each step (inside its dwell), or in a pause '
                                'between steps.')
        self.placement.currentIndexChanged.connect(
            lambda _i: self._write('placement', self.placement.currentData()))
        self.move = _field(4, 'Time the move takes, in ms.')
        _on_commit(self.move, lambda t: self._write('move_s', t))
        self.settle = _field(4, 'Time it then needs to stand still, in ms.')
        _on_commit(self.settle, lambda t: self._write('settle_s', t))
        self.ret = _field(4, 'Time to return to the start of a line, after every pass of the '
                             'fastest dimension, in ms.')
        _on_commit(self.ret, lambda t: self._write('return_s', t))
        self.origin = _combo(('absolute', 'captured'), ORIGIN_WORDS,
                             'Positions as written, or added to where it is when the scan '
                             'starts.')
        self.origin.currentIndexChanged.connect(
            lambda _i: self._write('origin', self.origin.currentData()))
        self.restore = _combo(('captured', 'first', 'park', 'hold'), RESTORE_WORDS,
                              'Where it goes after the scan.')
        self.restore.currentIndexChanged.connect(
            lambda _i: self._write('restore', self.restore.currentData()))
        for widget in (_group('Moves:', self.transition, self.placement),
                       _group('taking', self.move, 'ms,'),
                       _group('then settles', self.settle, 'ms.'),
                       _group('Return to the line start:', self.ret, 'ms.')):
            motion.addWidget(widget)
        outer.addWidget(motionHost)
        afterHost = QtWidgets.QWidget()
        after = FlowLayout(afterHost)
        for widget in (_group('Positions are', self.origin, '.'),
                       _group('After the scan:', self.restore),
                       _remove_button('Remove this positioner from the scan.',
                                      lambda: panel.removeTableRow('drives', self._index))):
            after.addWidget(widget)
        outer.addWidget(afterHost)

    def _write(self, key, text):
        self._panel.setTableValue('drives', self._index, key, text)

    def _hidden(self):
        """The dimensions that do not move this positioner and are not
        revealed: a blank or zero weight."""
        row = self._panel.tableRows('drives')[self._index]
        hidden = []
        for loop in self._loops:
            text = row.get(f'w:{loop}', '').strip()
            try:
                moves = bool(text) and float(text) != 0
            except ValueError:
                moves = True                   # a value that does not read stays in view
            if not moves and loop not in self._revealed:
                hidden.append(loop)
        return hidden

    def _fillAlso(self):
        menu = self.alsoButton.menu()
        menu.clear()
        for loop in self._hidden():
            action = menu.addAction(self._loops[loop])
            action.triggered.connect(lambda _c=False, lp=loop: self._reveal(lp))

    def _reveal(self, loop):
        self._revealed.add(loop)
        self._panel.refreshParts()
        self.weights[loop][1].setFocus()

    def update_from(self, row, span):
        _set_combo(self.device, row['device'])
        _quiet_set(self.offset, self.offset.setText, row['offset_um'])
        tabled = bool(row['table_loop'])
        hidden = set(self._hidden())
        first = True
        for loop, (group, weight, lead) in self.weights.items():
            _quiet_set(weight, weight.setText, row.get(f'w:{loop}', ''))
            shown = not tabled and loop not in hidden
            group.setVisible(shown)
            if shown:
                lead.setText('and moves' if first else 'and')
                first = False
        self.alsoButton.setVisible(not tabled and len(hidden) > 0)
        self.tableText.setVisible(tabled)
        if tabled:
            self.tableText.setText(f'and follows the table over {row["table_loop"]}: '
                                   f'{row["table_um"]} µm (edit it in Tables)')
        self.range.setText(span)
        self.range.setVisible(bool(span))
        _set_combo(self.transition, row['transition'])
        _set_combo(self.placement, row['placement'])
        _quiet_set(self.move, self.move.setText, row['move_s'])
        _quiet_set(self.settle, self.settle.setText, row['settle_s'])
        _quiet_set(self.ret, self.ret.setText, row['return_s'])
        _set_combo(self.origin, row['origin'])
        _set_combo(self.restore, row['restore'])


# ---------------------------------------------------------------------------
# Inside every step: the timeline, drawn and edited
# ---------------------------------------------------------------------------

class StepCanvas(QtWidgets.QWidget):
    """One step's events as bars on a time axis, a row per device: drag a bar
    to move it, its edges to resize it (a narrow bar: just outside them), or
    nudge the selected one with the arrow keys (Shift: ten ticks); Delete
    removes it. Times snap to the engine's grid when the check knows it and
    stay inside the step; the time a drive moves inside the step is shaded."""

    sigSelected = QtCore.Signal(int)
    sigChanged = QtCore.Signal(int, float, float)      # row, start_s, end_s (as stored)
    sigDelete = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._events: List[dict] = []
        self._rows: Dict[str, int] = {}
        self._step_s: Optional[float] = None
        self._grid_s: Optional[float] = None
        self._moving: List[tuple] = []        # (start_s, end_s, label)
        self._phase: Optional[int] = None
        self._selected = -1
        self._drag = None                     # (event, mode, press_x, start_s, end_s)
        self._preview: Dict[int, tuple] = {}
        self._editable = True
        self._labelW, self._t0, self._t1 = 0, 0.0, 1e-3
        #: Called before a press acts: the panel brings the drawing up to date
        #: with an edit committed by this very click.
        self.beforePress: Optional[Callable[[], None]] = None

    # -- what it shows ---------------------------------------------------------

    def setContent(self, events, step_s, grid_s, moving, phase):
        """``events``: dicts with row, device, label, start_s, end_s (from the
        step's end when ``from_end``), color, kind, phases, tag."""
        self._events, self._step_s, self._grid_s = list(events), step_s, grid_s
        self._moving, self._phase = list(moving), phase
        self._rows = {}
        for event in self._events:
            self._rows.setdefault(event['device'], len(self._rows))
        if self._drag is None:
            self._preview = {}
        self._layout()
        self.updateGeometry()
        self.update()

    def setSelected(self, row):
        self._selected = row
        self.update()

    def selected(self):
        return self._selected

    def setEditable(self, on):
        """While not editable the drawing is only drawn: presses, moves and
        keys edit nothing and select nothing, and nothing is selected."""
        self._editable = bool(on)
        if not self._editable:
            self._drag = None
            self._preview = {}
            QtWidgets.QToolTip.hideText()
            self.setCursor(QtCore.Qt.ArrowCursor)
            self.setSelected(-1)

    def events(self):
        return list(self._events)

    def sizeHint(self):
        metrics = self.fontMetrics()
        return QtCore.QSize(self._labelW + metrics.averageCharWidth() * 48,
                            self._rowHeight() * (len(self._rows) + 2))

    def minimumSizeHint(self):
        # every device's row and the ruler: the layout keeps them, inside the
        # panel's scroll area
        return QtCore.QSize(self._labelW + self.fontMetrics().averageCharWidth() * 12,
                            self._rowHeight() * (len(self._rows) + 2))

    # -- geometry (cached: a paint asks for it once per bar) ---------------------

    def _layout(self):
        metrics = self.fontMetrics()
        names = list(self._rows) or ['Camera']
        self._labelW = max(metrics.horizontalAdvance(n) for n in names) \
            + metrics.averageCharWidth() * 2
        starts, ends = [0.0], [self._step_s or 0.0]
        for event in self._events:
            start, stop = self._times(event)
            starts.append(start)
            ends.append(stop)
        self._t0, self._t1 = min(starts), max(ends) or 1e-3
        if self._t1 <= self._t0:
            self._t1 = self._t0 + 1e-3

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout()

    def _rowHeight(self):
        return int(self.fontMetrics().height() * 1.9)

    def _usable(self):
        return max(1.0, self.width() - self._labelW - self.fontMetrics().averageCharWidth())

    def _x(self, t):
        return self._labelW + self._usable() * (t - self._t0) / (self._t1 - self._t0)

    def _t(self, x):
        return self._t0 + (x - self._labelW) / self._usable() * (self._t1 - self._t0)

    def _times(self, event):
        if event['row'] in self._preview:
            return self._preview[event['row']]
        start, stop = event['start_s'], event['end_s']
        if event.get('from_end') and self._step_s is not None:
            start, stop = self._step_s + start, self._step_s + stop
        return start, stop

    def _barRect(self, event):
        start, stop = self._times(event)
        top = self._rowHeight() * (self._rows[event['device']] + 1) + self._rowHeight() * 0.2
        return QtCore.QRectF(self._x(start), top, max(2.0, self._x(stop) - self._x(start)),
                             self._rowHeight() * 0.6)

    def tick(self):
        """One step of the engine's grid; without one, the finest decimal a
        pixel can show."""
        if self._grid_s:
            return self._grid_s
        pixel = (self._t1 - self._t0) / self._usable()
        return 10 ** math.floor(math.log10(pixel)) if pixel > 0 else 1e-6

    def _snap(self, seconds):
        tick = self.tick()
        return round(seconds / tick) * tick

    def _bounds(self):
        """Where a bar may go: inside the step, when its length is known."""
        if self._step_s is None:
            return -math.inf, math.inf
        return 0.0, self._step_s

    def _moved(self, start, stop, mode, delta):
        """A bar moved by ``delta`` s, snapped, inside the step, and at least
        one tick long."""
        lo, hi = self._bounds()
        tick = self.tick()
        if mode == 'move':
            length = stop - start
            new = min(max(self._snap(start + delta), lo), max(lo, hi - length))
            return new, new + length
        if mode == 'start':
            return min(max(self._snap(start + delta), lo), stop - tick), stop
        return start, max(min(self._snap(stop + delta), hi), start + tick)

    # -- painting ------------------------------------------------------------------

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        text = self.palette().color(QtGui.QPalette.WindowText)
        faint, muted = QtGui.QColor(text), QtGui.QColor(text)
        faint.setAlphaF(0.25)
        muted.setAlphaF(0.6)
        metrics = self.fontMetrics()
        bottom = self._rowHeight() * (len(self._rows) + 1)
        top = self._rowHeight() * 0.8
        # the ruler: about one tick per eight characters
        for tick in _nice_ticks(self._t0, self._t1, max(2, int(
                self._usable() / (metrics.averageCharWidth() * 8)))):
            x = self._x(tick)
            painter.setPen(QtGui.QPen(faint, 1))
            painter.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, bottom))
            painter.setPen(muted)
            label = f'{tick * 1e3:g}'
            width = metrics.horizontalAdvance(label)
            left = min(max(x - width / 2, self._labelW), self.width() - width)
            painter.drawText(QtCore.QPointF(left, metrics.ascent()), label)
        painter.setPen(muted)
        painter.drawText(QtCore.QPointF(0, metrics.ascent()), 'ms')
        if self._step_s is not None:
            painter.setPen(QtGui.QPen(text, 1.5))
            for edge in (0.0, self._step_s):
                x = self._x(edge)
                painter.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, bottom))
        # the time a drive moves inside the step
        for start, stop, label in self._moving:
            rect = QtCore.QRectF(self._x(start), top, self._x(stop) - self._x(start),
                                 bottom - top)
            painter.fillRect(rect, QtGui.QBrush(faint, QtCore.Qt.BDiagPattern))
            painter.setPen(muted)
            painter.drawText(QtCore.QPointF(rect.left() + 2, bottom + metrics.ascent()), label)
        # the device rows and their events
        painter.setPen(text)
        for device, row in self._rows.items():
            y = self._rowHeight() * (row + 1)
            name = metrics.elidedText(device, QtCore.Qt.ElideRight, self._labelW - 2)
            painter.drawText(QtCore.QPointF(0, y + self._rowHeight() / 2 + metrics.ascent() / 2
                                            - metrics.descent() / 2), name)
        lo, hi = self._bounds()
        for event in self._events:
            rect = self._barRect(event)
            color = QtGui.QColor(event['color'])
            if not (self._phase is None or event['phases'] is None
                    or self._phase in event['phases']):
                color.setAlphaF(0.25)
            start, stop = self._times(event)
            outside = start < lo - 1e-15 or stop > hi + 1e-15
            painter.setBrush(QtGui.QBrush(color, QtCore.Qt.Dense4Pattern)
                             if event['kind'] == 'detector' else color)
            pen = QtGui.QPen(QtGui.QColor(role_color('bad')) if outside else color.darker(130),
                             2 if outside else 1)
            if event['row'] == self._selected:
                pen = QtGui.QPen(text, 2)
            painter.setPen(pen)
            painter.drawRoundedRect(rect, 3, 3)
            if event.get('tag'):
                painter.setPen(QtGui.QColor('#000000') if color.lightnessF() > 0.55 else text)
                clipped = metrics.elidedText(event['tag'], QtCore.Qt.ElideRight,
                                             int(rect.width()) - 4)
                painter.drawText(QtCore.QPointF(rect.left() + 3, rect.center().y()
                                                + metrics.ascent() / 2 - 1), clipped)
        painter.end()

    # -- editing -------------------------------------------------------------------

    def _hit(self, pos):
        edge = self.fontMetrics().averageCharWidth()
        point = QtCore.QPointF(pos)
        for event in reversed(self._events):           # the one drawn on top
            rect = self._barRect(event)
            if not rect.adjusted(-edge, 0, edge, 0).contains(point):
                continue
            if rect.width() >= 3 * edge:
                if point.x() - rect.left() <= edge:
                    return event, 'start'
                if rect.right() - point.x() <= edge:
                    return event, 'end'
                return event, 'move'
            # a narrow bar moves from inside, resizes from just outside
            if point.x() < rect.left():
                return event, 'start'
            if point.x() > rect.right():
                return event, 'end'
            return event, 'move'
        return None, None

    def _stored(self, event, start, stop):
        if event.get('from_end') and self._step_s is not None:
            return start - self._step_s, stop - self._step_s
        return start, stop

    def mousePressEvent(self, event):
        if not self._editable or event.button() != QtCore.Qt.LeftButton:
            return
        if self.beforePress is not None:
            self.beforePress()
        hit, mode = self._hit(event.pos())
        if hit is None:
            return
        self._selected = hit['row']
        self.sigSelected.emit(hit['row'])
        start, stop = self._times(hit)
        self._drag = (hit, mode, event.pos().x(), start, stop)
        self.update()

    def mouseMoveEvent(self, event):
        if not self._editable:
            self.setCursor(QtCore.Qt.ArrowCursor)
            return
        if self._drag is None:
            _hit, mode = self._hit(event.pos())
            self.setCursor(QtCore.Qt.SizeHorCursor if mode in ('start', 'end') else
                           QtCore.Qt.OpenHandCursor if mode == 'move' else QtCore.Qt.ArrowCursor)
            return
        hit, mode, x0, start, stop = self._drag
        new = self._moved(start, stop, mode, self._t(event.pos().x()) - self._t(x0))
        self._preview[hit['row']] = new
        QtWidgets.QToolTip.showText(
            self.mapToGlobal(event.pos()),
            f'{hit["label"]}: {new[0] * 1e3:.6g} to {new[1] * 1e3:.6g} ms', self)
        self.update()

    def mouseReleaseEvent(self, event):
        if not self._editable or self._drag is None or event.button() != QtCore.Qt.LeftButton:
            return
        hit = self._drag[0]
        self._drag = None
        QtWidgets.QToolTip.hideText()
        new = self._preview.pop(hit['row'], None)
        if new is not None and new != self._times(hit):
            self.sigChanged.emit(hit['row'], *self._stored(hit, *new))
        self.update()

    def keyPressEvent(self, event):
        if not self._editable:
            super().keyPressEvent(event)
            return
        chosen = next((e for e in self._events if e['row'] == self._selected), None)
        key = event.key()
        if chosen is not None and key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right):
            if self.beforePress is not None:
                self.beforePress()
                chosen = next((e for e in self._events if e['row'] == self._selected), None)
                if chosen is None:
                    return
            ticks = 10 if event.modifiers() & QtCore.Qt.ShiftModifier else 1
            delta = ticks * self.tick() * (-1 if key == QtCore.Qt.Key_Left else 1)
            start, stop = self._times(chosen)
            new = self._moved(start, stop, 'move', delta)
            if new != (start, stop):
                self.sigChanged.emit(chosen['row'], *self._stored(chosen, *new))
            return
        if chosen is not None and key in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace):
            self.sigDelete.emit(chosen['row'])
            return
        if self._events and key in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down):
            rows = [e['row'] for e in self._events]
            here = rows.index(self._selected) if self._selected in rows else -1
            there = min(max(here + (1 if key == QtCore.Qt.Key_Down else -1), 0), len(rows) - 1)
            self._selected = rows[there]
            self.sigSelected.emit(self._selected)
            self.update()
            return
        super().keyPressEvent(event)


def _nice_ticks(t0, t1, most):
    """Round tick positions from ``t0`` to ``t1``, at most ``most`` of them."""
    span = t1 - t0
    if span <= 0:
        return [t0]
    raw = span / most
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    first = math.ceil(t0 / step - 1e-9)
    return [i * step for i in range(first, int(t1 / step + 1e-9) + 1)]


#: What the step card adds: (button, event kind, the devices it offers).
ADD_EVENT_BUTTONS = (('+ Pulse', 'ttl', 'laser'), ('+ Exposure', 'detector', 'detector'),
                     ('+ Power level', 'level', 'laser'), ('+ Move', 'offset', 'positioner'))


class StepCard(Card):
    """Within one step: its dwell, the timeline drawn and edited, and the
    selected event's fields."""

    def __init__(self, panel, parent=None):
        super().__init__('Within one step', parent)
        self._panel = panel
        self.stepField = _field(6, 'The dwell: the length of every step, in ms.')
        self.stepField.setPlaceholderText('set')
        _on_commit(self.stepField, panel.stepPar.setText)
        self.phase = QtWidgets.QComboBox()
        self.phase.setToolTip('Show the step as one phase plays it.')
        self.phase.currentIndexChanged.connect(lambda _i: self.refresh())
        self.addButtons: Dict[str, QtWidgets.QToolButton] = {}
        self._readOnly = False
        self.dwellGroup = _group('dwell', self.stepField, 'ms')
        self.dwellText = _muted('')
        self.dwellText.setVisible(False)
        self.header.addWidget(self.dwellGroup)
        self.header.addWidget(self.dwellText)
        self.header.addWidget(self.phase)
        for title, kind, group in ADD_EVENT_BUTTONS:
            button = _menu_button(title, f'Add a {title[2:].lower()} of a {group} to every step.')
            button.menu().aboutToShow.connect(
                lambda b=button, k=kind, g=group: self._fillAdd(b, k, g))
            self.addButtons[kind] = button
            self.header.addWidget(button)
        self.canvas = StepCanvas()
        self.canvas.beforePress = panel.flushRefresh
        self.canvas.sigSelected.connect(self._select)
        self.canvas.sigChanged.connect(self._dragged)
        self.canvas.sigDelete.connect(lambda row: panel.removeTableRow('timeline', row))
        self.body.addWidget(self.canvas)
        self.hint = _muted('Hatched: a positioner moves and settles. Drag a bar to move it, an '
                           'edge to resize it; arrow keys nudge the selected one.')
        self.body.addWidget(self.hint)
        self.noteHost = QtWidgets.QWidget()
        self.notes = FlowLayout(self.noteHost)
        self.body.addWidget(self.noteHost)
        self.inspector = _EventInspector(panel)
        self.body.addWidget(self.inspector)

    def _fillAdd(self, button, kind, group):
        menu = button.menu()
        menu.clear()
        names = self._panel.devices.get(group, ())
        for name in names:
            action = menu.addAction(name)
            action.triggered.connect(lambda _c=False, n=name, k=kind: self._add(n, k))
        if not names:
            menu.addAction(f'No {group} in this setup').setEnabled(False)

    def _add(self, device, kind):
        row = self._panel.addEvent(device, kind)
        if row is not None:
            self._panel.flushRefresh()
            self._select(row)

    def _select(self, row):
        self.canvas.setSelected(row)
        self.inspector.show_row(row)

    def _dragged(self, row, start_s, end_s):
        self._panel.setTimes(row, start_s, end_s)

    def setReadOnly(self, on):
        """The step as a view (the Simple view): what adds to it, edits it or
        selects in it is not there -- no add buttons, no inspector, no hint,
        no dragging, no keys -- and the dwell is text. ``refresh`` keeps it so
        after every report; ``setReadOnly(False)`` brings everything back."""
        on = bool(on)
        if on == self._readOnly:
            return
        self._readOnly = on
        self.canvas.setEditable(not on)
        self.dwellGroup.setVisible(not on)
        for button in self.addButtons.values():
            button.setVisible(not on)
        self.refresh()

    def refresh(self):
        panel = self._panel
        _quiet_set(self.stepField, self.stepField.setText, panel.stepPar.text())
        dwell = panel.stepPar.text().strip()
        self.dwellText.setText(f'dwell {dwell} ms')
        self.dwellText.setVisible(self._readOnly and bool(dwell))
        self.stepField.setStyleSheet('' if panel.stepSeconds() is not None else
                                     f'border: 1px solid {role_color("bad")};')
        phases = panel.phaseCount()
        wanted = ['Every phase'] + [f'Phase {i}' for i in range(phases)] if phases > 1 else []
        if [self.phase.itemText(i) for i in range(self.phase.count())] != wanted:
            blocker = QtCore.QSignalBlocker(self.phase)
            self.phase.clear()
            self.phase.addItems(wanted)
            del blocker
        self.phase.setVisible(bool(wanted))
        phase = self.phase.currentIndex() - 1 if self.phase.currentIndex() > 0 else None
        events, moving, notes = panel.stepContent()
        self.canvas.setContent(events, panel.stepSeconds(), panel.gridSeconds(), moving, phase)
        self.hint.setVisible(bool(events) and not self._readOnly)
        _clear(self.notes)
        for note in notes:
            if isinstance(note, tuple):           # (text, button title, action)
                text, title, action = note
                self.notes.addWidget(_muted(text))
                if not self._readOnly:
                    button = QtWidgets.QToolButton()
                    button.setText(title)
                    button.clicked.connect(action)
                    self.notes.addWidget(button)
            else:
                self.notes.addWidget(_muted(note))
        self.noteHost.setVisible(bool(notes))
        if self._readOnly:
            self.canvas.setSelected(-1)
            self.inspector.setVisible(False)
            return
        rows = len(panel.tableRows('timeline'))
        if self.canvas.selected() >= rows:
            self.canvas.setSelected(-1)
        if self.canvas.selected() < 0 and rows:
            self.canvas.setSelected(0)
        self.inspector.show_row(self.canvas.selected())


class _EventInspector(QtWidgets.QFrame):
    """The selected event's fields: what, when and how much; More shows the
    rest -- phases, whether it needs the drives still, its anchor, its id."""

    def __init__(self, panel):
        super().__init__()
        self.setObjectName('expertInspector')
        self._panel, self._row = panel, -1
        host = FlowLayout(self)
        self.device = QtWidgets.QComboBox()
        self.device.currentIndexChanged.connect(
            lambda _i: self._write('device', self.device.currentText()))
        self.kind = _combo(('ttl', 'detector', 'level', 'offset', 'marker'), EVENT_WORDS)
        self.kind.currentIndexChanged.connect(
            lambda _i: self._write('kind', self.kind.currentData()))
        self.start = _field(6, 'When it starts, in ms from the step\'s start (from its end '
                               'for the last step of a pass: then ≤ 0).')
        _on_commit(self.start, lambda t: self._write('start_s', t))
        self.end = _field(6, 'When it ends, in ms.')
        _on_commit(self.end, lambda t: self._write('end_s', t))
        self.value = _field(5, 'The power level, or how far it moves (µm).')
        _on_commit(self.value, lambda t: self._write('value', t))
        self.shape = _combo(('hold', 'ramp'), SHAPE_WORDS, 'Held through the event, or ramped '
                                                           'to the value.')
        self.shape.currentIndexChanged.connect(
            lambda _i: self._write('shape', self.shape.currentData()))
        self.valueGroup = _group('value', self.value, self.shape)
        self.moreButton = QtWidgets.QToolButton()
        self.moreButton.setText('More')
        self.moreButton.setCheckable(True)
        self.moreButton.setAutoRaise(True)
        self.moreButton.toggled.connect(lambda _on: self.show_row(self._row))
        self.phases = _field(5, 'The phases it plays in, as 0, 2, … Blank: every phase.')
        self.phases.setPlaceholderText('every')
        _on_commit(self.phases, lambda t: self._write('phases', t))
        self.phasesGroup = _group('phases', self.phases)
        self.during = _combo(('stationary', 'any'), DURING_WORDS,
                             'Whether it may play while a positioner moves or settles.')
        self.during.currentIndexChanged.connect(
            lambda _i: self._write('during', self.during.currentData()))
        self.still = _field(8, 'The positioners that must stand still under it. Blank: every '
                               'positioner.')
        self.still.setPlaceholderText('every positioner')
        _on_commit(self.still, lambda t: self._write('still', t))
        self.stillGroup = _group('waits for:', self.still)
        self.anchor = QtWidgets.QComboBox()
        self.anchor.setToolTip('Every step, or only the first or last step of each pass of '
                               'a loop.')
        self.anchor.currentIndexChanged.connect(
            lambda _i: self._write('anchor', self.anchor.currentData() or 'step'))
        self.ident = _field(7, 'A name, for detector bindings and separations.')
        _on_commit(self.ident, lambda t: self._write('id', t))
        self.identGroup = _group('id', self.ident)
        self.remove = _remove_button('Remove this event.',
                                     lambda: panel.removeTableRow('timeline', self._row))
        self._more = [self.phasesGroup, self.during, self.stillGroup, self.anchor,
                      self.identGroup]
        for widget in [_group(self.device, self.kind), _group('from', self.start),
                       _group('to', self.end, 'ms'), self.valueGroup, self.moreButton,
                       *self._more, self.remove]:
            host.addWidget(widget)

    def _write(self, key, text):
        if self._row >= 0:
            self._panel.setTableValue('timeline', self._row, key, text)

    def show_row(self, row):
        rows = self._panel.tableRows('timeline')
        self._row = row if 0 <= row < len(rows) else -1
        self.setVisible(self._row >= 0)
        if self._row < 0:
            return
        values = rows[self._row]
        devices = self._panel.timelineDevices()
        if [self.device.itemText(i) for i in range(self.device.count())] != devices:
            blocker = QtCore.QSignalBlocker(self.device)
            self.device.clear()
            self.device.addItems(devices)
            del blocker
        _set_combo(self.device, values['device'])
        _set_combo(self.kind, values['kind'])
        _quiet_set(self.start, self.start.setText, values['start_s'])
        _quiet_set(self.end, self.end.setText, values['end_s'])
        _quiet_set(self.value, self.value.setText, values['value'])
        _set_combo(self.shape, values['shape'] or 'hold')
        self.valueGroup.setVisible(values['kind'] in ('level', 'offset') or bool(values['value']))
        more = self.moreButton.isChecked()
        _quiet_set(self.phases, self.phases.setText, values['phases'])
        self.phasesGroup.setVisible(more and (self._panel.phaseCount() > 1
                                              or bool(values['phases'])))
        _set_combo(self.during, values['during'])
        self.during.setVisible(more)
        _quiet_set(self.still, self.still.setText, values['still'])
        self.stillGroup.setVisible(more and (values['during'] != 'any' or bool(values['still'])))
        anchors = [('every step', 'step')]
        for loop in self._panel.tableRows('loops'):
            if loop['id']:
                anchors += [(f'first step of each pass of {loop["id"]}',
                             f'pass-start:{loop["id"]}'),
                            (f'last step of each pass of {loop["id"]}', f'pass-end:{loop["id"]}')]
        current = [(self.anchor.itemText(i), self.anchor.itemData(i))
                   for i in range(self.anchor.count())]
        if current != anchors:
            blocker = QtCore.QSignalBlocker(self.anchor)
            self.anchor.clear()
            for text, data in anchors:
                self.anchor.addItem(text, data)
            del blocker
        _set_combo(self.anchor, values['anchor'] or 'step')
        self.anchor.setVisible(more)
        _quiet_set(self.ident, self.ident.setText, values['id'])
        self.identGroup.setVisible(more)
        self.moreButton.setText('Less' if more else 'More')


# ---------------------------------------------------------------------------
# Pauses and triggers, and recording, as sentences
# ---------------------------------------------------------------------------

class WaitsCard(Card):
    def __init__(self, panel, parent=None):
        super().__init__('Pauses and triggers', parent)
        self._panel = panel
        self.rowsHost = QtWidgets.QVBoxLayout()
        self.body.addLayout(self.rowsHost)
        self.empty = _muted('None: the steps follow one another.')
        self.body.addWidget(self.empty)
        self.pauseButton = QtWidgets.QToolButton()
        self.pauseButton.setText('+ Pause')
        self.pauseButton.setToolTip('A fixed pause before the scan, every step or every '
                                    'iteration of a dimension.')
        self.pauseButton.clicked.connect(lambda: panel.addWait('time'))
        self.triggerButton = QtWidgets.QToolButton()
        self.triggerButton.setText('+ Wait for trigger')
        self.triggerButton.setToolTip('Wait for an external trigger before going on.')
        self.triggerButton.clicked.connect(lambda: panel.addWait('external'))
        self.header.addWidget(self.pauseButton)
        self.header.addWidget(self.triggerButton)
        self._editors: List[_WaitEditor] = []
        self._signature = None

    def refresh(self):
        waits = self._panel.tableRows('waits')
        loops = tuple((row['id'], row['kind']) for row in self._panel.tableRows('loops')
                      if row['id'])
        signature = (len(waits), loops)
        if signature != self._signature:
            for editor in self._editors:
                editor.setParent(None)
                editor.deleteLater()
            self._editors = [_WaitEditor(self._panel, i, loops) for i in range(len(waits))]
            for editor in self._editors:
                self.rowsHost.addWidget(editor)
            self._signature = signature
        for editor, row in zip(self._editors, waits):
            editor.update_from(row)
        self.empty.setVisible(not waits)


class _WaitEditor(QtWidgets.QWidget):
    def __init__(self, panel, index, loops):
        super().__init__()
        self._panel, self._index = panel, index
        flow = FlowLayout(self)
        self.at = QtWidgets.QComboBox()
        self.at.addItem('the scan starts', 'start')
        self.at.addItem('every step', 'step')
        kinds = [kind for _loop, kind in loops]
        for loop, kind in loops:
            one = KIND_NAMES.get(kind, (loop,))[0]
            self.at.addItem(f'each {one} ({loop})' if kinds.count(kind) > 1 else f'each {one}',
                            loop)
        self.at.currentIndexChanged.connect(lambda _i: self._write('at', self.at.currentData()))
        self.trigger = _combo(('time', 'external'), {'time': 'pause', 'external':
                                                     'wait for a trigger'})
        self.trigger.currentIndexChanged.connect(
            lambda _i: self._write('trigger', self.trigger.currentData()))
        self.interval = _field(5, 'How long a pause lasts, in ms; for a trigger, the shortest '
                                  'time between two releases.')
        self.interval.setPlaceholderText('set')
        _on_commit(self.interval, lambda t: self._write('interval_s', t))
        self.timeout = _field(5, 'How long to wait for the trigger, in ms. Blank: for ever.')
        self.timeout.setPlaceholderText('never')
        _on_commit(self.timeout, lambda t: self._write('timeout_s', t))
        self.onTimeout = _combo(('fail', 'continue'), TIMEOUT_WORDS)
        self.onTimeout.currentIndexChanged.connect(
            lambda _i: self._write('on_timeout', self.onTimeout.currentData()))
        self.hold = _combo(('idle', 'hold'), HOLD_WORDS,
                           'The lasers and camera windows while it waits.')
        self.hold.currentIndexChanged.connect(lambda _i: self._write('hold',
                                                                     self.hold.currentData()))
        self.timeoutGroup = _group('then after', self.timeout, 'ms', self.onTimeout)
        for widget in (_group('Before', self.at), _group(self.trigger, self.interval, 'ms'),
                       self.timeoutGroup, _group('meanwhile the lasers are', self.hold),
                       _remove_button('Remove this pause.',
                                      lambda: panel.removeTableRow('waits', self._index))):
            flow.addWidget(widget)

    def _write(self, key, text):
        self._panel.setTableValue('waits', self._index, key, text)

    def update_from(self, row):
        _set_combo(self.at, row['at'])
        _set_combo(self.trigger, row['trigger'])
        _quiet_set(self.interval, self.interval.setText, row['interval_s'])
        _quiet_set(self.timeout, self.timeout.setText, row['timeout_s'])
        _set_combo(self.onTimeout, row['on_timeout'])
        _set_combo(self.hold, row['hold'])
        self.timeoutGroup.setVisible(row['trigger'] != 'time')


class RecordingCard(Card):
    def __init__(self, panel, parent=None):
        super().__init__('Recording', parent)
        self._panel = panel
        self.rowsHost = QtWidgets.QVBoxLayout()
        self.body.addLayout(self.rowsHost)
        self.status = QtWidgets.QVBoxLayout()
        self.body.addLayout(self.status)
        self.addButton = _menu_button('+ Detector', 'Say what a detector records; without '
                                                    'it, a detector is recorded as its trigger '
                                                    'line fires.')
        self.addButton.menu().aboutToShow.connect(self._fillAdd)
        self.header.addWidget(self.addButton)
        self._editors: List[_BindingEditor] = []
        self._signature = None
        self._lines: List[str] = []

    def _fillAdd(self):
        menu = self.addButton.menu()
        menu.clear()
        for name in self._panel.devices.get('detector', ()):
            action = menu.addAction(name)
            action.triggered.connect(lambda _c=False, n=name: self._panel.addBinding(n))

    def setLines(self, lines):
        self._lines = list(lines)
        self._drawLines()

    def _drawLines(self):
        _clear(self.status)
        lines = self._lines or (['Each detector is recorded as its trigger line fires.']
                                if not self._panel.tableRows('detectors') else [])
        for line in lines:
            self.status.addWidget(_muted(line))

    def refresh(self):
        bindings = self._panel.tableRows('detectors')
        events = tuple(self._panel.eventChoices())
        signature = (len(bindings), events)
        if signature != self._signature:
            for editor in self._editors:
                editor.setParent(None)
                editor.deleteLater()
            self._editors = [_BindingEditor(self._panel, i, events)
                             for i in range(len(bindings))]
            for editor in self._editors:
                self.rowsHost.addWidget(editor)
            self._signature = signature
        for editor, row in zip(self._editors, bindings):
            editor.update_from(row)
        self._drawLines()


class _BindingEditor(QtWidgets.QWidget):
    """What one detector records, as a sentence. A camera records frames,
    one per trigger window; a point detector (APD, PMT) integrates each whole
    step into one pixel of an image. Those are the only combinations the
    model accepts (bindings), so the window and assembly are said, not
    offered; choosing the other kind of detector rewrites them."""

    def __init__(self, panel, index, events):
        super().__init__()
        self._panel, self._index = panel, index
        flow = FlowLayout(self)
        self.detector = _combo(panel.devices.get('detector', ()))
        self.detector.currentIndexChanged.connect(
            lambda _i: panel.setBindingDetector(self._index, self.detector.currentData()))
        self.perStep = QtWidgets.QSpinBox()
        self.perStep.setRange(1, 2 ** 31 - 1)
        self.perStep.setKeyboardTracking(False)
        self.perStep.valueChanged.connect(lambda v: self._write('per_step', str(v)))
        self.events = _combo(events, tooltip='What triggers it: an event\'s id, or a device '
                                             'whose events trigger it.')
        self.events.setEditable(True)
        self.events.lineEdit().editingFinished.connect(
            lambda: self._write('events', self.events.currentText()))
        self.events.activated.connect(lambda _i: self._write('events', self.events.currentText()))
        self.phases = _field(5, 'The phases it takes part in, as 0, 2, … Blank: every phase.')
        self.phases.setPlaceholderText('every')
        _on_commit(self.phases, lambda t: self._write('phases', t))
        self.framesWord = QtWidgets.QLabel('frames per step, triggered by')
        self.frames = _group('records', self.perStep, self.framesWord, self.events)
        self.pixels = QtWidgets.QLabel('integrates each whole step into one pixel of an image')
        self.odd = QtWidgets.QLabel()
        self.odd.setObjectName('expertProblems')
        for widget in (self.detector, self.frames, self.pixels, self.odd,
                       _group('in phases', self.phases),
                       _remove_button('Remove: the detector is then recorded as its trigger '
                                      'line fires.',
                                      lambda: panel.removeTableRow('detectors', self._index))):
            flow.addWidget(widget)

    def _write(self, key, text):
        self._panel.setTableValue('detectors', self._index, key, text)

    def update_from(self, row):
        _set_combo(self.detector, row['detector'])
        try:
            _quiet_set(self.perStep, self.perStep.setValue, int(row['per_step']))
            self.framesWord.setText(('frame' if int(row['per_step']) == 1 else 'frames')
                                    + ' per step, triggered by')
        except ValueError:
            pass
        if not self.events.lineEdit().hasFocus():
            blocker = QtCore.QSignalBlocker(self.events)
            self.events.setEditText(row['events'])
            del blocker
        _quiet_set(self.phases, self.phases.setText, row['phases'])
        point = self._panel.isPointDetector(row['detector'])
        expected = ('step', 'image') if point else ('event', 'stream')
        usual = (row['window'], row['assembly']) == expected
        self.frames.setVisible(not point and usual)
        self.pixels.setVisible(point and usual)
        self.odd.setVisible(not usual)
        if not usual:
            self.odd.setText(f'window {row["window"]!r}, assembly {row["assembly"]!r} (set in '
                             'Tables): the check says whether the model takes it')


# ---------------------------------------------------------------------------
# A trajectory, and a program yet to be written
# ---------------------------------------------------------------------------

class TrajectoryCard(Card):
    """A trajectory program: its path in words, and its template to edit it
    (a path is not loops, so the loop parts do not describe it)."""

    def __init__(self, panel, parent=None):
        super().__init__('Path', parent)
        self.text = QtWidgets.QLabel()
        self.text.setWordWrap(True)
        self.body.addWidget(self.text)
        self.events = _muted()
        self.body.addWidget(self.events)
        self.editButton = QtWidgets.QPushButton('Edit the path…')
        self.editButton.clicked.connect(
            lambda: panel.sigPresetRequested.emit(self._preset))
        self.header.addWidget(self.editButton)
        self._preset = ''

    def setTrajectory(self, info):
        self._preset = info.get('preset', '')
        self.text.setText(info.get('text', ''))
        self.events.setText(info.get('events', ''))
        self.editButton.setEnabled(bool(self._preset))


class StartCard(Card):
    """An empty scan: start from a template or a trajectory, or add the
    first dimension and build it by hand."""

    def __init__(self, panel, parent=None):
        super().__init__('Start from a template', parent)
        self.flowHost = QtWidgets.QWidget()
        self.flow = FlowLayout(self.flowHost)
        self.body.addWidget(self.flowHost)
        self.body.addWidget(_muted('A template asks for a few values and writes the scan, '
                                   'which you then edit below; a trajectory follows a path '
                                   'instead of dimensions.'))
        self.buildHost = QtWidgets.QWidget()
        self.build = FlowLayout(self.buildHost)
        self.body.addWidget(self.buildHost)
        self._panel = panel

    def setBuildVisible(self, on):
        """The row that builds a scan by hand: not in the Simple view."""
        self.buildHost.setVisible(on)

    def setTemplates(self, templates, trajectories=()):
        _clear(self.flow)
        _clear(self.build)
        for preset_id, title in templates:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(
                lambda _c=False, p=preset_id: self._panel.sigPresetRequested.emit(p))
            self.flow.addWidget(button)
        if trajectories:
            paths = _menu_button('Trajectory ▾', 'Two scanners along a free path: a circle, a '
                                                 'spiral, a Lissajous figure, points, Python.')
            for preset_id, title in trajectories:
                action = paths.menu().addAction(title)
                action.triggered.connect(
                    lambda _c=False, p=preset_id: self._panel.sigPresetRequested.emit(p))
            self.flow.addWidget(paths)
        self.build.addWidget(_muted('Or build it yourself:'))
        add = _menu_button('+ Dimension')
        for kind, (_one, many) in KIND_NAMES.items():
            action = add.menu().addAction(many.capitalize())
            action.triggered.connect(lambda _c=False, k=kind: self._panel.addLoop(k))
        self.build.addWidget(add)


# ---------------------------------------------------------------------------
# What the engine makes of it
# ---------------------------------------------------------------------------

_STEP_NUMBER = re.compile(r'\bsteps? \d+( of \d+)?(, \d+)*')


def collapse(lines: Sequence[str]) -> List[str]:
    """The same problem in many steps, once: the first, and how many more."""
    out, counts, order = {}, {}, []
    for line in lines:
        key = _STEP_NUMBER.sub('step N', line)
        if key not in out:
            out[key] = line
            counts[key] = 0
            order.append(key)
        else:
            counts[key] += 1
    return [out[k] + (f' (and {counts[k]} more like it)' if counts[k] else '') for k in order]


class CheckCard(Card):
    """Whether the engine can run it: yes or no in the title, what stops the
    run in red (each the once), then each feature that is played differently
    or not known, and the notes; the whole report on request."""

    def __init__(self, report: QtWidgets.QPlainTextEdit, parent=None):
        super().__init__('Can it run?', parent)
        self.verdict = Chip('', 'muted')
        self.verdict.setVisible(False)
        self.header.addWidget(self.verdict)
        self.problems = QtWidgets.QLabel()
        self.problems.setObjectName('expertProblems')
        self.problems.setWordWrap(True)
        self.problems.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.problems.setVisible(False)
        self.body.addWidget(self.problems)
        self.summaryHost = QtWidgets.QWidget()
        self.summary = FlowLayout(self.summaryHost)
        self.body.addWidget(self.summaryHost)
        self.details = QtWidgets.QVBoxLayout()
        self.body.addLayout(self.details)
        self.reportButton = QtWidgets.QToolButton()
        self.reportButton.setText('Full report')
        self.reportButton.setCheckable(True)
        self.reportButton.setAutoRaise(True)
        self.header.addWidget(self.reportButton)
        self.report = report
        self.report.setVisible(False)
        self.reportButton.toggled.connect(self.report.setVisible)
        self.body.addWidget(self.report)

    def setCheck(self, check):
        engine = check.get('engine')
        if 'runnable' in check:
            where = f', on {engine}' if engine else ''
            self.verdict.setText(f'Yes{where}' if check['runnable'] else f'No{where}')
            self.verdict.setRole('ok' if check['runnable'] else 'bad')
        self.verdict.setVisible('runnable' in check)
        problems = collapse(check.get('problems') or [])
        self.problems.setText('\n'.join(problems))
        self.problems.setVisible(bool(problems))
        _clear(self.summary)
        _clear(self.details)
        verdicts = check.get('verdicts') or []
        stated = '\n'.join(problems)
        others = [v for v in verdicts if v[0] != 'native']
        if verdicts and not others:
            self.summary.addWidget(Chip('Runs exactly as written', 'ok'))
        elif others:
            counts = {}
            for verdict, _feature, _reason in others:
                counts[verdict] = counts.get(verdict, 0) + 1
            for verdict, count in counts.items():
                # implemented another way is a fact to know, not a warning:
                # the NI-DAQ plays every stepped scan as waveforms. What
                # runs differently from the request is the header's chip.
                role = 'muted' if verdict == 'emulated' else VERDICT_STYLE.get(
                    verdict, ('', 'muted'))[1]
                word = {'emulated': 'implemented another way',
                        'impossible': 'impossible here',
                        'unknown': 'not known'}.get(verdict, verdict)
                self.summary.addWidget(Chip(f'{count} {word}', role))
        details = []
        for verdict, feature, reason in verdicts:
            if verdict == 'native' and not reason:
                continue
            if reason and reason in stated:
                continue                       # said above already
            details.append((verdict, f'{feature}' + (f': {reason}' if reason else '')))
        shown = collapse([text for _v, text in details])
        verdict_of = {text: verdict for verdict, text in details}
        for text in shown:
            base = re.sub(r' \(and \d+ more like it\)$', '', text)
            verdict = verdict_of.get(base, 'unknown')
            glyph, role = VERDICT_STYLE.get(verdict, ('·', 'muted'))
            if verdict == 'emulated':
                role = 'muted'
            label = QtWidgets.QLabel(f'{glyph}  {text}')
            label.setWordWrap(True)
            label.setStyleSheet(f'color: {role_color(role)};')
            label.setToolTip(verdict)
            self.details.addWidget(label)
        for note in check.get('notes') or []:
            self.details.addWidget(_muted(note))


class LanePreview(Card):
    """Every device on its own lane, like a logic analyser: the drives' positions
    scaled into their lanes, each line high or low; one step, one pass or all
    that is drawn."""

    ZOOMS = ('One step', 'One pass', 'All')

    def __init__(self, plotButton: QtWidgets.QAbstractButton, parent=None):
        super().__init__('Preview', parent)
        self.stale = _muted('')
        self.header.addWidget(self.stale)
        self.zoom = QtWidgets.QButtonGroup(self)
        for index, title in enumerate(self.ZOOMS):
            button = QtWidgets.QToolButton()
            button.setText(title)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setChecked(index == 0)
            self.zoom.addButton(button, index)
            self.header.addWidget(button)
        self.zoom.buttonClicked.connect(lambda _b: self._applyZoom())
        self.live = QtWidgets.QCheckBox('Auto-update')
        self.live.setToolTip('Show the preview again after every edit the engine accepts. A '
                             'long scan takes longer to draw.')
        self.header.addWidget(self.live)
        self.plotButton = plotButton
        self.header.addWidget(plotButton)
        self.problem = QtWidgets.QLabel()
        self.problem.setObjectName('expertProblems')
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        self.body.addWidget(self.problem)
        self.note = _muted()
        self.note.setVisible(False)
        self.body.addWidget(self.note)
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.setLabel('bottom', 'time', units='s')
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setVisible(False)
        self.empty = _muted('Show the preview to see every device, lane by lane.')
        self.body.addWidget(self.empty)
        self.body.addWidget(self.plot)
        self._time = np.zeros(0)
        self._windows = (None, None, None)

    def drawn(self) -> bool:
        return not self.plot.isHidden()

    def setStale(self, stale):
        self.stale.setText('out of date' if stale and self.drawn() else '')

    def setWindows(self, step, pass_, run):
        """Each zoom's (start_s, end_s), where known."""
        self._windows = (step, pass_, run)

    def showProblem(self, text):
        """Why nothing is drawn: the old drawing goes, the reason shows."""
        self.plot.clear()
        self.plot.setVisible(False)
        self.problem.setText(text)
        self.problem.setVisible(True)
        self.empty.setVisible(False)
        self.stale.setText('')

    def showTraces(self, time_s, traces: Dict[str, np.ndarray], colors: Dict[str, QtGui.QColor],
                   analog: Sequence[str] = (), note=''):
        self.plot.clear()
        self.problem.setVisible(False)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        # the panel's font, as the style sheet has set it by now
        for side in ('left', 'bottom'):
            self.plot.getAxis(side).setStyle(tickFont=self.font())
            self.plot.getAxis(side).label.setFont(self.font())
        self._time = np.asarray(time_s, dtype=float)
        analog = set(analog) or {name for name, values in traces.items()
                                 if not np.isin(np.unique(values), (0.0, 1.0)).all()}
        names = sorted(traces, key=lambda n: (n not in analog, list(traces).index(n)))
        ticks = []
        for lane, name in enumerate(names):
            values = np.asarray(traces[name], dtype=float)
            base = len(names) - 1 - lane
            if name in analog:
                low, high = float(np.nanmin(values)), float(np.nanmax(values))
                scaled = (values - low) / ((high - low) or 1.0) * 0.8
                label = f'{name} ({low:.4g} to {high:.4g})'
            else:
                scaled = values * 0.8
                label = name
            curve = self.plot.plot(self._time, base + 0.1 + scaled,
                                   pen=pg.mkPen(colors.get(name, QtGui.QColor(DETECTOR_COLOR)),
                                                width=1.5))
            curve.setClipToView(True)
            curve.setDownsampling(auto=True, method='peak')
            ticks.append((base + 0.5, label))
        self.plot.getAxis('left').setTicks([ticks])
        self.plot.setYRange(0, len(names), padding=0.02)
        self.plot.setMinimumHeight(self.fontMetrics().height() * (2 * len(names) + 4))
        self.plot.setVisible(True)
        self.empty.setVisible(False)
        self.stale.setText('')
        self._applyZoom()

    def _applyZoom(self):
        if not self._time.size:
            return
        window = self._windows[self.zoom.checkedId()] if self.zoom.checkedId() >= 0 else None
        if window is None:
            window = (float(self._time[0]), float(self._time[-1]))
        self.plot.setXRange(window[0], window[1], padding=0)


class AdaptiveColumns(QtWidgets.QWidget):
    """The editor and the check side by side when both fit at their preferred
    widths, one above the other when they do not."""

    def __init__(self, left: Sequence[QtWidgets.QWidget], right: Sequence[QtWidgets.QWidget],
                 parent=None):
        super().__init__(parent)
        self.leftColumn, self.rightColumn = QtWidgets.QWidget(), QtWidgets.QWidget()
        for column, widgets in ((self.leftColumn, left), (self.rightColumn, right)):
            layout = QtWidgets.QVBoxLayout(column)
            layout.setContentsMargins(0, 0, 0, 0)
            for widget in widgets:
                layout.addWidget(widget)
            layout.addStretch(1)
        self.box = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.TopToBottom, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        # The minimum is the stacked one: side by side, the sum of the two
        # columns would keep the panel from ever narrowing back to one.
        self.box.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
        self.box.addWidget(self.leftColumn)
        self.box.addWidget(self.rightColumn)

    def minimumSizeHint(self):
        left, right = self.leftColumn.minimumSizeHint(), self.rightColumn.minimumSizeHint()
        return QtCore.QSize(max(left.width(), right.width()), left.height() + right.height())

    def twoColumns(self) -> bool:
        return self.box.direction() == QtWidgets.QBoxLayout.LeftToRight

    #: Side by side, the editor and the check take the width in this ratio.
    SHARES = (3, 2)

    def threshold(self) -> int:
        """The width from which the columns stand side by side: the editor at
        its preferred width plus the check's share of it -- from the ratio
        the columns share the width in, not from what the check holds, so a
        preview drawn into it never flips the arrangement."""
        editor = self.leftColumn.sizeHint().width()
        return editor + editor * self.SHARES[1] // self.SHARES[0] + self.box.spacing()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.rearrange()

    def rearrange(self):
        """Choose side by side or stacked for the width now. A resize does it;
        so must a change of which cards are shown, which changes the width the
        editor asks for."""
        wanted = (QtWidgets.QBoxLayout.LeftToRight if self.width() >= self.threshold()
                  else QtWidgets.QBoxLayout.TopToBottom)
        if self.box.direction() != wanted:
            self.box.setDirection(wanted)
            # side by side the editor takes the larger share; stacked, each
            # its own height
            two = wanted == QtWidgets.QBoxLayout.LeftToRight
            self.box.setStretch(0, self.SHARES[0] if two else 0)
            self.box.setStretch(1, self.SHARES[1] if two else 0)


__all__ = ['AdaptiveColumns', 'Card', 'CheckCard', 'Chip', 'FlowLayout', 'LanePreview',
           'OrderCard', 'PositionsCard', 'RecordingCard', 'StartCard', 'StepCanvas', 'StepCard',
           'TrajectoryCard', 'VisitStrip', 'WaitsCard', 'WheelGuard', 'collapse', 'dark_theme',
           'device_color', 'duration_text', 'ms_text', 'role_color', 'wavelength_color']


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
