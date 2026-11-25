"""Docking ImControl panels after startup, and the dialogs that go with it.

``ImConMainView`` builds its docks once, from the setup file. This module
adds one more dock while the application runs -- where the setup's layout
would have put it -- and takes it away again when building its controller
fails, so that a panel is either fully there or not at all. It also holds
the placeholder a panel shows instead of an empty dock when the section it
needs is missing, the *Add panel* dialog and the *save this panel?* choice.

The view keeps its own small hooks (``addPanel``, ``removePanel``,
``replacePlaceholder``); the work is done here.
"""

from typing import Optional

from pyqtgraph.dockarea import Dock
from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.model import panels


# --------------------------------------------------------------------------
# Placeholder for a panel whose section is missing
# --------------------------------------------------------------------------

class NotConfiguredPanel(QtWidgets.QWidget):
    """Stands in a dock for a panel that would do nothing without its setup
    section -- "Tiling is not configured" -- with a Configure… button when
    the section can be configured while ImSwitch runs."""

    sigConfigure = QtCore.Signal(str)

    def __init__(self, spec: panels.PanelSpec, parent=None):
        super().__init__(parent)
        self.key = spec.key
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        title = QtWidgets.QLabel(f'<b>{spec.displayName} is not configured</b>')
        layout.addWidget(title)
        if spec.runtimeLoadable:
            text = (f'This setup lists the {spec.displayName} panel but has no '
                    f'<code>{spec.section}</code> section, so the panel would do '
                    f'nothing. Configure it now; you can then keep the '
                    f'configuration for this session or save it to the setup file.')
        else:
            text = (f'This setup lists the {spec.displayName} panel but has no '
                    f'<code>{spec.section}</code> section, so the panel does nothing. '
                    f'Add the section in Hardware &gt; Edit hardware configuration… '
                    f'and restart ImSwitch.')
        self.message = QtWidgets.QLabel(text)
        self.message.setWordWrap(True)
        self.message.setTextFormat(QtCore.Qt.RichText)
        layout.addWidget(self.message)
        self.configureButton = None
        if spec.runtimeLoadable:
            self.configureButton = QtWidgets.QPushButton('Configure…')
            self.configureButton.clicked.connect(lambda: self.sigConfigure.emit(self.key))
            row = QtWidgets.QHBoxLayout()
            row.addWidget(self.configureButton)
            row.addStretch()
            layout.addLayout(row)
        layout.addStretch()


def needsPlaceholder(key: str, setupInfo) -> bool:
    """Whether the panel ``key`` would be an empty dock on this setup."""
    spec = panels.panelSpec(key)
    return bool(spec is not None and spec.silentWithoutSection
                and getattr(setupInfo, spec.section, None) is None)


# --------------------------------------------------------------------------
# Placing a dock after startup
# --------------------------------------------------------------------------

def dockInfoFor(view, key):
    """(side, dock info) the setup's layout gives ``key``."""
    for side in ('right', 'left'):
        info = view._panelDockInfos.get(side, {}).get(key)
        if info is not None:
            return side, info
    return None, None


def _placement(view, key):
    """Where to put the dock for ``key``: (position, anchor dock or None),
    following the dock tables -- tabbed with a docked panel of the same row,
    else below the last docked panel above it, else above the first below."""
    side, info = dockInfoFor(view, key)
    if info is None:
        side, rows = 'right', {}
    else:
        rows = view._panelDockInfos[side]
    docked = [(k, i) for k, i in rows.items()
              if k != key and k in view.docks and view.docks[k].area is not None]
    if info is not None:
        same = [k for k, i in docked if i.yPosition == info.yPosition]
        if same:
            return 'above', view.docks[same[-1]]
        above = [k for k, i in docked if i.yPosition < info.yPosition]
        if above:
            return 'bottom', view.docks[above[-1]]
        below = [k for k, i in docked if i.yPosition > info.yPosition]
        if below:
            return 'top', view.docks[below[0]]
    image = view.docks.get('Image')
    if image is not None:
        return side, image
    return side, None


def _insertIntoState(state, anchorName: Optional[str], newName: str, position: str):
    """The saved layout ``state`` with dock ``newName`` placed relative to
    ``anchorName`` the way ``DockArea.addDock`` placed it. Returns None when
    the anchor is not in it (the dock then goes where restoreState puts
    extra docks)."""
    new = ('dock', newName, {})
    orientation = {'above': 'tab', 'below': 'tab', 'bottom': 'vertical', 'top': 'vertical',
                   'left': 'horizontal', 'right': 'horizontal'}[position]
    after = position in ('below', 'bottom', 'right')

    def visit(node):
        kind, payload = node[0], node[1]
        if kind == 'dock':
            if payload != anchorName:
                return node, False
            if orientation == 'tab':
                return ('tab', [node, new], {'index': 1}), True
            children = [node, new] if after else [new, node]
            return (orientation, children, {'sizes': [100, 100]}), True
        children = list(payload)
        for index, child in enumerate(children):
            if child[0] == 'dock' and child[1] == anchorName and kind == orientation:
                children.insert(index + 1 if after else index, new)
                options = dict(node[2])
                if 'sizes' in options:
                    sizes = list(options['sizes'])
                    sizes.insert(index + 1 if after else index,
                                 max(1, sum(sizes) // max(1, len(sizes))))
                    options['sizes'] = sizes
                if kind == 'tab':
                    options['index'] = index + 1 if after else index
                return (kind, children, options), True
            replaced, found = visit(child)
            if found:
                children[index] = replaced
                return (kind, children, node[2]), True
        return node, False

    if not isinstance(state, dict) or not state.get('main'):
        return None
    main, found = visit(state['main'])
    if not found:
        return None
    return {**state, 'main': main}


def addPanelDock(view, key: str, widget) -> Dock:
    """Dock ``widget`` as panel ``key`` where the setup's layout puts it, and
    record it in the default layout View > Reset panel layout returns to."""
    spec = panels.panelSpec(key)
    _, info = dockInfoFor(view, key)
    name = info.name if info is not None else (spec.displayName if spec else key)
    dock = Dock(name, size=(1, 1))
    dock.addWidget(widget)
    position, anchor = _placement(view, key)
    view.dockArea.addDock(dock, position, anchor)
    if position == 'above' and hasattr(dock, 'raiseDock'):
        dock.raiseDock()
    defaultState = getattr(view, '_defaultDockState', None)
    if defaultState is not None:
        updated = _insertIntoState(defaultState, anchor.name() if anchor is not None else None,
                                   name, position)
        if updated is not None:
            view._defaultDockState = updated
    return dock


def removePanelDock(view, key: str) -> None:
    """Take the dock of panel ``key`` out again (rollback)."""
    dock = view.docks.pop(key, None)
    if dock is None:
        return
    try:
        dock.close()
    except Exception:  # noqa: BLE001 -- best effort; the dock is dropped either way
        dock.setParent(None)


def showPanel(view, key: str) -> None:
    if key == 'Log' and hasattr(view, 'logAction'):
        view.logAction.setChecked(True)  # keeps the View > Log tick in step
    dock = view.docks.get(key)
    if dock is None:
        return
    dock.show()
    if hasattr(dock, 'raiseDock'):
        try:
            dock.raiseDock()
        except Exception:  # noqa: BLE001 -- not in a tab container
            pass


# --------------------------------------------------------------------------
# The Add panel dialog
# --------------------------------------------------------------------------

class AddPanelDialog(QtWidgets.QDialog):
    """Lists every panel, grouped as the Config Studio groups them: those
    that can be added now are enabled, the others greyed with the reason."""

    sigAddRequested = QtCore.Signal(str)

    _ACTION_TEXT = {'show': 'Open: shows it', 'add': 'Can be added',
                    'configure': 'Can be added after configuring'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Add panel')
        self.resize(560, 520)
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            'Add a panel to this session. It stays until ImSwitch closes; '
            'you are asked whether to save it to the setup file. Greyed panels '
            'can only be added in the setup file (hover for why).'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(['Panel', 'Status'])
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemDoubleClicked.connect(lambda item, _col: self._request(item))
        self.tree.currentItemChanged.connect(lambda *_: self._syncButton())
        layout.addWidget(self.tree, 1)
        self.status = QtWidgets.QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QtWidgets.QDialogButtonBox()
        self.addButton = buttons.addButton('Add', QtWidgets.QDialogButtonBox.AcceptRole)
        self.addButton.clicked.connect(lambda: self._request(self.tree.currentItem()))
        closeButton = buttons.addButton(QtWidgets.QDialogButtonBox.Close)
        closeButton.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._syncButton()

    def setEntries(self, entries):
        """``entries``: (PanelSpec, Availability) pairs."""
        current = self.tree.currentItem()
        currentKey = current.data(0, QtCore.Qt.UserRole) if current is not None else None
        self.tree.clear()
        groups = {}
        for spec, availability in entries:
            group = groups.get(spec.group)
            if group is None:
                group = QtWidgets.QTreeWidgetItem([spec.group, ''])
                group.setFlags(QtCore.Qt.ItemIsEnabled)
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                groups[spec.group] = group
            status = (self._ACTION_TEXT[availability.action] if availability.possible
                      else availability.reason)
            item = QtWidgets.QTreeWidgetItem([spec.displayName, status])
            item.setData(0, QtCore.Qt.UserRole, spec.key)
            tip = availability.reason or status
            item.setToolTip(0, tip)
            item.setToolTip(1, tip)
            if not availability.possible:
                item.setFlags(QtCore.Qt.NoItemFlags)
            group.addChild(item)
            if spec.key == currentKey and availability.possible:
                self.tree.setCurrentItem(item)
        order = {name: index for index, name in enumerate(panels.GROUP_ORDER)}
        for name in sorted(groups, key=lambda g: order.get(g, len(order))):
            self.tree.addTopLevelItem(groups[name])
            groups[name].setExpanded(True)
        self._syncButton()

    def entryFor(self, key):
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            for j in range(group.childCount()):
                if group.child(j).data(0, QtCore.Qt.UserRole) == key:
                    return group.child(j)
        return None

    def setStatus(self, text: str):
        self.status.setText(text)

    def _syncButton(self):
        item = self.tree.currentItem()
        self.addButton.setEnabled(bool(
            item is not None and item.data(0, QtCore.Qt.UserRole)
            and item.flags() & QtCore.Qt.ItemIsEnabled))

    def _request(self, item):
        if item is None:
            return
        key = item.data(0, QtCore.Qt.UserRole)
        if key and item.flags() & QtCore.Qt.ItemIsEnabled:
            self.sigAddRequested.emit(key)


# --------------------------------------------------------------------------
# Keep for this session, or save?
# --------------------------------------------------------------------------

class SaveChoiceDialog(QtWidgets.QDialog):
    """After a panel is added: keep it for this session only (the default),
    save it to the setup file ImSwitch runs on, or save a new setup."""

    SESSION, CURRENT, NEW = 'session', 'current', 'new'

    def __init__(self, title: str, message: str, details: str = '', parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.choice = self.SESSION
        layout = QtWidgets.QVBoxLayout(self)
        label = QtWidgets.QLabel(message)
        label.setWordWrap(True)
        label.setTextFormat(QtCore.Qt.RichText)
        layout.addWidget(label)
        if details:
            detailLabel = QtWidgets.QLabel(details)
            detailLabel.setWordWrap(True)
            detailLabel.setTextFormat(QtCore.Qt.RichText)
            detailLabel.setStyleSheet('color: palette(mid);')
            layout.addWidget(detailLabel)
        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        self.buttons = {}
        for choice, text in ((self.SESSION, 'Keep for this session only'),
                             (self.CURRENT, 'Save to current setup'),
                             (self.NEW, 'Save as new setup…')):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(lambda _=False, c=choice: self._choose(c))
            row.addWidget(button)
            self.buttons[choice] = button
        self.buttons[self.SESSION].setDefault(True)
        layout.addLayout(row)
        self.resize(560, self.sizeHint().height())

    def _choose(self, choice):
        self.choice = choice
        self.accept()
