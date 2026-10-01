"""Ask which napari layer to import, from which result, with which mapping.

Nothing is inferred behind the user's back: the layer, the source result and
the adapter mapping are three explicit choices. A mapping is pre-selected
only when napari's own record says the layer came out of that endpoint's
session (see ``NapariEndpointController._mappingSuggestions``); every other
mapping an open session could offer is listed for the user to pick, and the
default is "none: a fresh coordinate space".
"""

from __future__ import annotations

from qtpy import QtWidgets

_NO_MAPPING = "None — fresh coordinate space"


class NapariImportDialog(QtWidgets.QDialog):
    def __init__(
        self,
        parent,
        layers,
        results,
        *,
        suggestions=None,
        candidates=None,
        preselected_layer=None,
        preselected_result=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Import napari layer as result")
        self._layers = list(layers)
        self._results = list(results)
        # A mapping belongs to a session opened on one result; one whose
        # result is not in this dialog cannot be chosen (it would silently
        # attach to whichever result happened to be selected).
        known = {str(getattr(result, "result_uid", "")) for result in self._results}
        self._suggestions = {
            key: value for key, value in dict(suggestions or {}).items() if str(value[2]) in known
        }
        self._candidates = {
            key: [c for c in choices if str(c[2]) in known]
            for key, choices in dict(candidates or {}).items()
        }
        # Candidates the mapping combo currently lists for the chosen layer;
        # index 0 is always "no mapping".
        self._mappingChoices: list = []

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

        self.mappingCombo = QtWidgets.QComboBox()
        layout.addRow("Adapter mapping:", self.mappingCombo)

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
        self.resultCombo.currentIndexChanged.connect(self._resultChanged)
        self.mappingCombo.currentIndexChanged.connect(self._mappingChanged)
        if preselected_layer is not None and preselected_layer in self._layers:
            self.layerCombo.setCurrentIndex(self._layers.index(preselected_layer))
        if preselected_result is not None and preselected_result in self._results:
            self.resultCombo.setCurrentIndex(self._results.index(preselected_result))
        self._layerChanged(self.layerCombo.currentIndex())
        if not self._layers:
            self.infoLabel.setText("The viewer has no plugin-created layers to import.")
        if not self._results:
            self.infoLabel.setText("No results are loaded; import needs a source result.")

    # -- reactions ------------------------------------------------------------

    def _layerChanged(self, index: int) -> None:
        if not (0 <= index < len(self._layers)):
            return
        layer = self._layers[index]
        self.nameEdit.setText(str(getattr(layer, "name", "") or "imported"))
        self._fillMappings(layer)
        suggestion = self._suggestions.get(id(layer))
        if suggestion is None:
            self.mappingCombo.setCurrentIndex(0)
            self._describe(None)
            return
        endpoint, mapping, result_uid = suggestion
        for position, result in enumerate(self._results):
            if str(getattr(result, "result_uid", "")) == str(result_uid):
                self.resultCombo.setCurrentIndex(position)
                break
        for position, choice in enumerate(self._mappingChoices):
            if choice is not None and choice[0] is endpoint and choice[1] is mapping:
                self.mappingCombo.setCurrentIndex(position)
                break
        self._describe(suggestion, suggested=True)

    def _resultChanged(self, index: int) -> None:
        """A mapping belongs to the session of *one* result: changing the
        result away from it drops the mapping rather than carrying it over."""
        choice = self._currentMapping()
        if choice is None or not (0 <= index < len(self._results)):
            return
        selected_uid = str(getattr(self._results[index], "result_uid", ""))
        if str(choice[2]) != selected_uid:
            self.mappingCombo.setCurrentIndex(0)

    def _mappingChanged(self, _index: int) -> None:
        choice = self._currentMapping()
        if choice is None:
            self._describe(None)
            return
        # The mapping's session was opened on one result; select it, so the
        # grid the mapping may inherit is that result's.
        for position, result in enumerate(self._results):
            if str(getattr(result, "result_uid", "")) == str(choice[2]):
                if self.resultCombo.currentIndex() != position:
                    self.resultCombo.setCurrentIndex(position)
                break
        self._describe(choice)

    # -- helpers --------------------------------------------------------------

    def _fillMappings(self, layer) -> None:
        self.mappingCombo.blockSignals(True)
        self.mappingCombo.clear()
        self._mappingChoices = [None]
        self.mappingCombo.addItem(_NO_MAPPING)
        for endpoint, mapping, result_uid in self._candidates.get(id(layer), []):
            self._mappingChoices.append((endpoint, mapping, result_uid))
            self.mappingCombo.addItem(
                f"{endpoint.label}: {mapping.label or mapping.result_kind}"
                + (" (may inherit the source grid)" if mapping.preserves_grid else "")
            )
        self.mappingCombo.setCurrentIndex(0)
        self.mappingCombo.blockSignals(False)

    def _currentMapping(self):
        index = self.mappingCombo.currentIndex()
        if not (0 <= index < len(self._mappingChoices)):
            return None
        return self._mappingChoices[index]

    def _describe(self, choice, *, suggested: bool = False) -> None:
        if choice is None:
            self.infoLabel.setText(
                "No adapter mapping: the layer gets a fresh coordinate space."
            )
            return
        endpoint, mapping, _uid = choice
        grid = "may inherit the source grid" if mapping.preserves_grid else "gets a fresh coordinate space"
        origin = "napari records this layer as made by that session" if suggested else "chosen explicitly"
        self.infoLabel.setText(
            f"{endpoint.label} declares this layer as '{mapping.label or mapping.result_kind}'; "
            f"it {grid} ({origin})."
        )

    def selection(self):
        layer_index = self.layerCombo.currentIndex()
        result_index = self.resultCombo.currentIndex()
        if not (0 <= layer_index < len(self._layers)) or not (0 <= result_index < len(self._results)):
            return None
        layer = self._layers[layer_index]
        result = self._results[result_index]
        choice = self._currentMapping()
        endpoint = None
        if choice is not None and str(choice[2]) == str(getattr(result, "result_uid", "")):
            # Revalidated on accept: the mapping applies only with the
            # result its session was opened on.
            endpoint = choice[0]
        return layer, result, self.nameEdit.text().strip() or None, endpoint

    @classmethod
    def choose(cls, parent, layers, results, *, suggestions=None, candidates=None,
               preselected_layer=None, preselected_result=None):
        dialog = cls(
            parent, layers, results, suggestions=suggestions, candidates=candidates,
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
