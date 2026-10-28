import re

from qtpy import QtCore, QtWidgets

from .basewidgets import Widget


#: Most positions a row shows as buttons; a selector with more positions is a drop-down. The State
#: column of the grid is about four 80 px buttons wide (the dock's width budget), so a fifth button
#: would widen every flip-mirror dock for the sake of one wheel.
MAX_BUTTON_POSITIONS = 4

#: Positions of a switch that has no ``state_names``: a classic two-state flip mirror.
DEFAULT_STATE_NAMES = {0: "0", 1: "1"}

_BUTTON_STYLE = """
QPushButton {
    padding: 2px 8px;
    border: 1px solid rgba(255,255,255,60);
}

QPushButton[edge="first"] {
    border-top-left-radius: 5px;
    border-bottom-left-radius: 5px;
}

QPushButton[edge="last"] {
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
}

QPushButton:hover {
    border: 1px solid rgba(255,255,255,120);
}

QPushButton:checked {
    background-color: rgba(120,180,255,120);
    border: 1px solid rgba(120,180,255,200);
}

QPushButton:disabled {
    color: rgba(255,255,255,70);
    border: 1px solid rgba(255,255,255,25);
}
"""


def parseStateNames(state_names):
    """``[(state, text), ...]`` sorted by state number.

    Keys may be ints or digit strings (a setup file's JSON keys are strings). ``None`` or an empty
    mapping gives the two-state default. Raises ``ValueError`` for a key that is not a whole number
    >= 0.
    """
    if not state_names:
        state_names = DEFAULT_STATE_NAMES
    parsed = {}
    for key, text in state_names.items():
        try:
            state = int(key)
        except (TypeError, ValueError):
            state = -1
        if state < 0 or isinstance(key, bool):
            raise ValueError(f"Flip mirror state names are keyed by state numbers 0, 1, ...; got {key!r}")
        parsed[state] = str(text)
    return sorted(parsed.items())


class FlipMirrorWidget(Widget):
    """Widget for controlling flip mirrors: switches with two or more named positions.

    A row with up to ``MAX_BUTTON_POSITIONS`` positions is a group of checkable buttons; a row with
    more is a drop-down. Both emit ``sigStateChanged(name, state)`` only for a choice the user makes.
    """

    sigStateChanged = QtCore.Signal(str, int)
    sigLinkChanged = QtCore.Signal(str, object)  # follower_name, master_name or None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.rows = {}
        self._building = False

        self._build_ui()

    def _build_ui(self):
        self._firstDataRow = 1

        self.grid = QtWidgets.QGridLayout()
        self.grid.setContentsMargins(6, 6, 6, 6)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)

        # Header row
        self.grid.addWidget(QtWidgets.QLabel("Name"), 0, 0, alignment=QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.grid.addWidget(QtWidgets.QLabel("State"), 0, 1, alignment=QtCore.Qt.AlignCenter)
        self.grid.addWidget(QtWidgets.QLabel("Follow"), 0, 2, alignment=QtCore.Qt.AlignCenter)
        self.grid.addWidget(QtWidgets.QLabel("Master"), 0, 3, alignment=QtCore.Qt.AlignCenter)

        self.grid.addWidget(QtWidgets.QLabel("Status"), 0, 4, alignment=QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        # Equal column distribution across the full widget width
        # for col in range(5):
        #     self.grid.setColumnStretch(col, 1)
        #     self.grid.setColumnMinimumWidth(col, 60)

        # Column sizing: distribute the table across the full widget width
        self.grid.setColumnStretch(0, 2)  # Name
        self.grid.setColumnStretch(1, 4)  # State
        self.grid.setColumnStretch(2, 1)  # Follow
        self.grid.setColumnStretch(3, 4)  # Master
        self.grid.setColumnStretch(4, 1)  # Status

        self.grid.setColumnMinimumWidth(0, 40)
        self.grid.setColumnMinimumWidth(1, 60)
        self.grid.setColumnMinimumWidth(2, 20)
        self.grid.setColumnMinimumWidth(3, 60)
        self.grid.setColumnMinimumWidth(4, 20)

        rootLayout = QtWidgets.QVBoxLayout()
        rootLayout.setContentsMargins(0, 0, 0, 0)
        rootLayout.addLayout(self.grid)
        rootLayout.addStretch(1)

        self.setLayout(rootLayout)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Maximum)

    def addFlipMirror(self, name, state_names=None):
        if name in self.rows:
            return

        states = parseStateNames(state_names)

        row_index = len(self.rows) + self._firstDataRow

        nameLabel = QtWidgets.QLabel(name)

        stateWidget = QtWidgets.QWidget()
        stateLayout = QtWidgets.QHBoxLayout()
        stateLayout.setContentsMargins(0, 0, 0, 0)
        stateLayout.setSpacing(0)
        stateWidget.setLayout(stateLayout)

        row = {}
        self._fillStateControls(name, row, states, stateLayout)

        followCheck = QtWidgets.QCheckBox()
        masterCombo = QtWidgets.QComboBox()
        masterCombo.setMinimumWidth(60)
        masterCombo.setMaximumWidth(150)

        statusLabel = QtWidgets.QLabel("—")
        statusLabel.setMinimumWidth(20)

        self.grid.addWidget(nameLabel, row_index, 0, alignment=QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.grid.addWidget(stateWidget, row_index, 1, alignment=QtCore.Qt.AlignCenter)
        self.grid.addWidget(followCheck, row_index, 2, alignment=QtCore.Qt.AlignCenter)
        self.grid.addWidget(masterCombo, row_index, 3, alignment=QtCore.Qt.AlignCenter)
        self.grid.addWidget(statusLabel, row_index, 4, alignment=QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        row.update({
            "nameLabel": nameLabel,
            "stateWidget": stateWidget,
            "followCheck": followCheck,
            "masterCombo": masterCombo,
            "statusLabel": statusLabel,
        })
        self.rows[name] = row

        followCheck.toggled.connect(lambda checked, n=name: self._onFollowToggled(n, checked))
        masterCombo.currentTextChanged.connect(lambda text, n=name: self._onMasterChanged(n, text))

    def _fillStateControls(self, name, row, states, stateLayout):
        """Buttons up to ``MAX_BUTTON_POSITIONS`` positions, a drop-down beyond; records the states shown."""
        row["states"] = [state for state, _ in states]
        row["stateNames"] = dict(states)
        if len(states) <= MAX_BUTTON_POSITIONS:
            self._fillStateButtons(name, row, states, stateLayout)
        else:
            self._fillStateCombo(name, row, states, stateLayout)

    def stateNames(self, name):
        """``{state: text}`` of the positions row ``name`` shows (empty for an unknown row)."""
        row = self.rows.get(name)
        return dict(row["stateNames"]) if row is not None else {}

    def rebuildFlipMirror(self, name, state_names=None):
        """Show row ``name`` with a new set of positions: its controls are removed and built again.

        For a switch whose number of positions was not known when the row was built (a device that
        was not connected then, and is a ten-position wheel now). Name, follow and master controls
        and the status text stay; the new state controls emit ``sigStateChanged`` exactly as the
        old ones did, none is selected until ``setState``, and ``setRowState`` decides whether they
        are enabled. An unknown row is added.
        """
        if name not in self.rows:
            self.addFlipMirror(name, state_names)
            return

        states = parseStateNames(state_names)
        row = self.rows[name]
        stateLayout = row["stateWidget"].layout()

        buttonGroup = row.pop("buttonGroup", None)
        if buttonGroup is not None:
            buttonGroup.setParent(None)
            buttonGroup.deleteLater()
        for key in [k for k in row if re.fullmatch(r"state\d+Btn", k)] + ["stateButtons", "stateCombo"]:
            row.pop(key, None)
        while stateLayout.count():
            item = stateLayout.takeAt(0)
            control = item.widget()
            if control is not None:
                control.setParent(None)
                control.deleteLater()

        self._fillStateControls(name, row, states, stateLayout)

    def _fillStateButtons(self, name, row, states, stateLayout):
        """One checkable button per position. The first two are ``state0Btn`` and ``state1Btn``."""
        buttonGroup = QtWidgets.QButtonGroup(self)
        buttonGroup.setExclusive(True)

        buttons = {}
        for position, (state, text) in enumerate(states):
            button = QtWidgets.QPushButton(text)
            button.setObjectName(f"state{position}Btn")
            button.setCheckable(True)
            button.setMinimumWidth(40)
            button.setMaximumWidth(80)
            button.setMinimumHeight(24)
            edge = "first" if position == 0 else ("last" if position == len(states) - 1 else "middle")
            button.setProperty("edge", edge)
            button.setStyleSheet(_BUTTON_STYLE)
            buttonGroup.addButton(button)
            stateLayout.addWidget(button)
            button.clicked.connect(lambda checked=False, n=name, s=state: self.sigStateChanged.emit(n, s))
            buttons[state] = button
            row[f"state{position}Btn"] = button

        row["buttonGroup"] = buttonGroup
        row["stateButtons"] = buttons

    def _fillStateCombo(self, name, row, states, stateLayout):
        """A drop-down listing every position; the item data is the state number."""
        stateCombo = QtWidgets.QComboBox()
        stateCombo.setObjectName("stateCombo")
        stateCombo.setMinimumHeight(24)
        for state, text in states:
            stateCombo.addItem(text, state)
        # ``activated`` fires for the user's choice only, like a button click; ``setState`` is silent.
        stateCombo.activated.connect(
            lambda index, n=name, c=stateCombo: self.sigStateChanged.emit(n, int(c.itemData(index))))
        stateLayout.addWidget(stateCombo)
        row["stateCombo"] = stateCombo

    def _onFollowToggled(self, name, checked):
        if self._building:
            return

        row = self.rows[name]
        combo = row["masterCombo"]

        if checked:
            master = combo.currentText()

            # If no master is selected yet, auto-select the first available one.
            if master == "—":
                for i in range(combo.count()):
                    candidate = combo.itemText(i)
                    if candidate != "—":
                        combo.setCurrentIndex(i)
                        master = candidate
                        break

            # Still no valid master: revert checkbox.
            if master == "—":
                row["followCheck"].blockSignals(True)
                row["followCheck"].setChecked(False)
                row["followCheck"].blockSignals(False)
                return

            self.sigLinkChanged.emit(name, master)

        else:
            self.sigLinkChanged.emit(name, None)

    def _onMasterChanged(self, name, master):
        if self._building:
            return

        row = self.rows[name]
        if row["followCheck"].isChecked() and master != "—":
            self.sigLinkChanged.emit(name, master)

    def setState(self, name, state):
        """Show ``state`` as the selected position; a state the row does not have selects none."""
        if name not in self.rows:
            return

        row = self.rows[name]
        state = int(state)

        combo = row.get("stateCombo")
        if combo is not None:
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(state))      # -1 (nothing shown) for an unknown state
            combo.blockSignals(False)
            return

        group = row["buttonGroup"]
        buttons = row["stateButtons"]
        # An exclusive group refuses to uncheck its checked button, so release it for the update.
        group.setExclusive(False)
        for position, button in buttons.items():
            button.blockSignals(True)
            button.setChecked(position == state)
            button.blockSignals(False)
        group.setExclusive(True)

    def setMasterChoices(self, name, choices, current_master=None):
        if name not in self.rows:
            return

        row = self.rows[name]
        combo = row["masterCombo"]

        self._building = True
        combo.blockSignals(True)

        combo.clear()
        combo.addItem("—")
        combo.addItems(list(choices))

        if current_master is not None:
            idx = combo.findText(current_master)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(0)

        combo.blockSignals(False)
        self._building = False

    def setLink(self, name, master_name):
        if name not in self.rows:
            return

        row = self.rows[name]

        self._building = True

        row["followCheck"].blockSignals(True)
        row["masterCombo"].blockSignals(True)

        is_linked = master_name is not None
        row["followCheck"].setChecked(is_linked)

        if is_linked:
            idx = row["masterCombo"].findText(master_name)
            if idx >= 0:
                row["masterCombo"].setCurrentIndex(idx)
        else:
            idx = row["masterCombo"].findText("—")
            if idx >= 0:
                row["masterCombo"].setCurrentIndex(idx)

        row["masterCombo"].setEnabled(is_linked)

        row["followCheck"].blockSignals(False)
        row["masterCombo"].blockSignals(False)

        self._building = False

    def setRowState(self, name, *, connected=True, state_enabled=True,
                link_enabled=True, status_text="OK"):
        if name not in self.rows:
            return

        row = self.rows[name]

        stateEnabled = bool(connected and state_enabled)
        if "stateCombo" in row:
            row["stateCombo"].setEnabled(stateEnabled)
        else:
            for button in row["stateButtons"].values():
                button.setEnabled(stateEnabled)

        row["followCheck"].setEnabled(bool(connected and link_enabled))

        # Keep combo enabled whenever linking is allowed.
        # This lets the user select the master before checking Follow.
        row["masterCombo"].setEnabled(bool(connected and link_enabled))

        row["statusLabel"].setText(status_text)

    def setAllEnabled(self, enabled):
        for name in self.rows:
            self.setRowState(
                name,
                connected=enabled,
                state_enabled=enabled,
                link_enabled=enabled,
                status_text="OK" if enabled else "Disabled",
            )
