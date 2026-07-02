"""Display-layer channel controls for ImProcess."""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.luts import IMAGE_LUTS


class ChannelControlsDialog(QtWidgets.QDialog):
    """Small ImageJ-like channel/layer visibility and LUT editor."""

    sigLayerVisibilityChanged = QtCore.Signal(str, bool)
    sigLayerLutChanged = QtCore.Signal(str, str)

    def __init__(self, parent=None, *, lut_choices=None):
        super().__init__(parent)
        self.setWindowTitle("Channels")
        self.setModal(False)
        self.setMinimumWidth(420)
        self._lut_choices = tuple(lut_choices or IMAGE_LUTS)
        self._updating = False

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Show", "Layer", "LUT"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)

        close_button = QtWidgets.QPushButton("Close")
        close_button.clicked.connect(self.close)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(close_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(buttons)

    def setLayerStates(self, states: list[dict]) -> None:
        self._updating = True
        try:
            self.table.setRowCount(len(states))
            for row, state in enumerate(states):
                self._set_row(row, state)
        finally:
            self._updating = False

    def _set_row(self, row: int, state: dict) -> None:
        layer_id = str(state["id"])

        visible = QtWidgets.QCheckBox()
        visible.setChecked(bool(state.get("visible", True)))
        visible.toggled.connect(
            lambda checked, current_id=layer_id: self._visibility_changed(
                current_id, checked
            )
        )
        self.table.setCellWidget(row, 0, visible)

        label = QtWidgets.QTableWidgetItem(str(state.get("name", layer_id)))
        label.setData(QtCore.Qt.UserRole, layer_id)
        self.table.setItem(row, 1, label)

        lut_combo = QtWidgets.QComboBox()
        for lut_id, label_text in self._lut_choices:
            lut_combo.addItem(label_text, lut_id)
        index = lut_combo.findData(str(state.get("colormap", "grayclip")))
        if index < 0:
            index = lut_combo.findData("grayclip")
        lut_combo.setCurrentIndex(max(0, index))
        lut_combo.activated.connect(
            lambda _index, combo=lut_combo, current_id=layer_id: self._lut_changed(
                current_id,
                str(combo.currentData()),
            )
        )
        self.table.setCellWidget(row, 2, lut_combo)

    def _visibility_changed(self, layer_id: str, visible: bool) -> None:
        if not self._updating:
            self.sigLayerVisibilityChanged.emit(layer_id, bool(visible))

    def _lut_changed(self, layer_id: str, lut_id: str) -> None:
        if not self._updating:
            self.sigLayerLutChanged.emit(layer_id, str(lut_id))


__all__ = ["ChannelControlsDialog"]
