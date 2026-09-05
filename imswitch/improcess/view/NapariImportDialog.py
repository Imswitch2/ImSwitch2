"""Ask which napari layer to import, and which result it came from.

Both choices are the user's. A plugin can add any number of layers, and the
session only knows which layers ImProcess itself added; guessing "the newest
one belongs to the last result sent" would be wrong often enough to be
worse than asking. When a verified adapter declared an output mapping for a
layer, the dialog pre-selects it and says so.
"""

from __future__ import annotations

from qtpy import QtWidgets


class NapariImportDialog(QtWidgets.QDialog):
    def __init__(
        self,
        parent,
        layers,
        results,
        *,
        suggestions=None,
        preselected_layer=None,
        preselected_result=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Import napari layer as result")
        self._layers = list(layers)
        self._results = list(results)
        self._suggestions = dict(suggestions or {})

        layout = QtWidgets.QFormLayout(self)
        self.layerCombo = QtWidgets.QComboBox()
        for layer in self._layers:
            label = f"{getattr(layer, 'name', '?')}  [{type(layer).__name__}]"
            if id(layer) in self._suggestions:
                endpoint, mapping, _uid = self._suggestions[id(layer)]
                label += f"  — {mapping.label or mapping.result_kind} from {endpoint.label}"
            self.layerCombo.addItem(label)
        layout.addRow("Layer:", self.layerCombo)

        self.resultCombo = QtWidgets.QComboBox()
        for result in self._results:
            self.resultCombo.addItem(str(getattr(result, "name", "result")))
        layout.addRow("Derived from result:", self.resultCombo)

        self.nameEdit = QtWidgets.QLineEdit()
        layout.addRow("Result name:", self.nameEdit)

        self.infoLabel = QtWidgets.QLabel("")
        self.infoLabel.setWordWrap(True)
        layout.addRow(self.infoLabel)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self.layerCombo.currentIndexChanged.connect(self._layerChanged)
        if preselected_layer is not None and preselected_layer in self._layers:
            self.layerCombo.setCurrentIndex(self._layers.index(preselected_layer))
        if preselected_result is not None and preselected_result in self._results:
            self.resultCombo.setCurrentIndex(self._results.index(preselected_result))
        self._layerChanged(self.layerCombo.currentIndex())
        if not self._layers:
            self.infoLabel.setText("The viewer has no plugin-created layers to import.")
        if not self._results:
            self.infoLabel.setText("No results are loaded; import needs a source result.")

    def _layerChanged(self, index: int) -> None:
        if not (0 <= index < len(self._layers)):
            return
        layer = self._layers[index]
        self.nameEdit.setText(str(getattr(layer, "name", "") or "imported"))
        suggestion = self._suggestions.get(id(layer))
        if suggestion is None:
            self.infoLabel.setText(
                "No adapter mapping for this layer: it gets a fresh coordinate space."
            )
            return
        endpoint, mapping, result_uid = suggestion
        for position, result in enumerate(self._results):
            if str(getattr(result, "result_uid", "")) == str(result_uid):
                self.resultCombo.setCurrentIndex(position)
                break
        grid = "may inherit the source grid" if mapping.preserves_grid else "gets a fresh coordinate space"
        self.infoLabel.setText(
            f"{endpoint.label} declares this layer as '{mapping.label or mapping.result_kind}'; it {grid}."
        )

    def selection(self):
        layer_index = self.layerCombo.currentIndex()
        result_index = self.resultCombo.currentIndex()
        if not (0 <= layer_index < len(self._layers)) or not (0 <= result_index < len(self._results)):
            return None
        layer = self._layers[layer_index]
        suggestion = self._suggestions.get(id(layer))
        endpoint = suggestion[0] if suggestion else None
        return layer, self._results[result_index], self.nameEdit.text().strip() or None, endpoint

    @classmethod
    def choose(cls, parent, layers, results, *, suggestions=None, preselected_layer=None, preselected_result=None):
        dialog = cls(
            parent, layers, results, suggestions=suggestions,
            preselected_layer=preselected_layer, preselected_result=preselected_result,
        )
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selection()


__all__ = ["NapariImportDialog"]


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
