"""ImageJ's *Properties…* — name, group and the full ROI style (P-7.4, F-22).

Edits what an ROI *looks like* and what it is called, never where it is: the
geometry fields are deliberately absent, so this dialog cannot be the thing
that silently moves a region. *Specify…* is where numeric geometry is entered,
and it says so in its title.

A field left as it was applies to nobody: a batch edit over five ROIs with
different colours must be able to set the group without flattening the colours
into one. Every control therefore has an explicit "leave alone" state, and only
the ones actually changed are returned.
"""

from __future__ import annotations

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.algorithms.roi_style import ROIStyle

#: Shown in a colour box that has not been set. Not a colour anyone would
#: choose, so "unchanged" is visibly distinct from "black".
_UNSET = "—"


class _ColorButton(QtWidgets.QPushButton):
    """A colour swatch that can also mean "leave this alone"."""

    def __init__(self, color: str | None, parent=None):
        super().__init__(parent)
        self._color = color
        self.setMinimumWidth(90)
        self.clicked.connect(self._pick)
        self._refresh()

    def _refresh(self) -> None:
        if self._color:
            self.setText(self._color)
            self.setStyleSheet(f"background-color: {self._color};")
        else:
            self.setText(_UNSET)
            self.setStyleSheet("")

    def _pick(self) -> None:
        initial = QtGui.QColor(self._color) if self._color else QtGui.QColor("#ffcc00")
        chosen = QtWidgets.QColorDialog.getColor(initial, self, "Choose a colour")
        if chosen.isValid():
            self._color = chosen.name()
            self._refresh()

    def color(self) -> str | None:
        return self._color

    def clear_color(self) -> None:
        self._color = None
        self._refresh()


class ROIPropertiesDialog(QtWidgets.QDialog):
    """Name, group and style for one ROI or a whole selection."""

    def __init__(self, rois, parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            "ROI Properties" if len(rois) == 1 else f"Properties — {len(rois)} ROIs"
        )
        self._rois = list(rois)
        single = len(self._rois) == 1
        first = self._rois[0] if self._rois else None
        style = (first.style if first is not None else None) or ROIStyle()

        self.nameEdit = QtWidgets.QLineEdit(first.name if single else "")
        self.nameEdit.setEnabled(single)
        if not single:
            self.nameEdit.setPlaceholderText("(names are not changed in bulk)")

        self.groupSpin = QtWidgets.QSpinBox()
        self.groupSpin.setRange(-1, 255)
        self.groupSpin.setSpecialValueText(_UNSET)   # -1 means "leave alone"
        self.groupSpin.setValue(int(first.group) if single and first else -1)
        self.groupSpin.setToolTip(
            "ImageJ's ROI group. Fiducial sets and channels are marked with it."
        )

        self.strokeColor = _ColorButton(style.stroke_color if single else None)
        self.fillColor = _ColorButton(style.fill_color if single else None)
        self.labelColor = _ColorButton(style.label_color if single else None)

        self.strokeWidth = QtWidgets.QDoubleSpinBox()
        self.strokeWidth.setRange(0.0, 20.0)
        self.strokeWidth.setDecimals(1)
        self.strokeWidth.setSpecialValueText(_UNSET)  # 0 means "leave alone"
        self.strokeWidth.setValue(
            float(style.stroke_width) if single and style.stroke_width else 0.0
        )

        self.fillOpacity = QtWidgets.QDoubleSpinBox()
        self.fillOpacity.setRange(-0.01, 1.0)
        self.fillOpacity.setSingleStep(0.05)
        self.fillOpacity.setDecimals(2)
        self.fillOpacity.setSpecialValueText(_UNSET)
        self.fillOpacity.setValue(float(style.fill_opacity) if single else -0.01)

        self.labelVisible = QtWidgets.QCheckBox("Show label")
        if single:
            self.labelVisible.setChecked(bool(style.label_visible))
        else:
            # Tri-state, starting partially checked: over a mixed selection
            # "leave alone" has to be expressible, and a plain checkbox cannot
            # say it.
            self.labelVisible.setTristate(True)
            self.labelVisible.setCheckState(QtCore.Qt.PartiallyChecked)

        form = QtWidgets.QFormLayout()
        form.addRow("Name:", self.nameEdit)
        form.addRow("Group:", self.groupSpin)
        form.addRow("Stroke colour:", self.strokeColor)
        form.addRow("Stroke width:", self.strokeWidth)
        form.addRow("Fill colour:", self.fillColor)
        form.addRow("Fill opacity:", self.fillOpacity)
        form.addRow("Label colour:", self.labelColor)
        form.addRow("", self.labelVisible)

        note = QtWidgets.QLabel(
            f"“{_UNSET}” leaves a field as it is. Geometry is not edited here — "
            "use Specify… for that."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#888; font-size:8pt;")

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def changes(self) -> dict:
        """Only the fields the user actually set.

        Returning a full style would overwrite every ROI in the selection with
        the first one's appearance, which is the opposite of a batch edit.
        """
        style_changes = {}
        if self.strokeColor.color():
            style_changes["stroke_color"] = self.strokeColor.color()
        if self.fillColor.color():
            style_changes["fill_color"] = self.fillColor.color()
        if self.labelColor.color():
            style_changes["label_color"] = self.labelColor.color()
        if self.strokeWidth.value() > 0:
            style_changes["stroke_width"] = float(self.strokeWidth.value())
        if self.fillOpacity.value() >= 0:
            style_changes["fill_opacity"] = float(self.fillOpacity.value())
        state = self.labelVisible.checkState()
        if state != QtCore.Qt.PartiallyChecked:
            style_changes["label_visible"] = state == QtCore.Qt.Checked

        changes: dict = {}
        if style_changes:
            changes["_style"] = style_changes
        if self.groupSpin.value() >= 0:
            changes["group"] = int(self.groupSpin.value())
        if self.nameEdit.isEnabled():
            name = self.nameEdit.text().strip()
            if name and self._rois and name != self._rois[0].name:
                changes["name"] = name
        return changes

    @classmethod
    def edit(cls, rois, parent=None) -> dict | None:
        dialog = cls(rois, parent)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            return dialog.changes()
        return None


class ROISpecifyDialog(QtWidgets.QDialog):
    """ImageJ's *Specify…*: an ROI at exact numeric coordinates."""

    def __init__(self, shape=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Specify ROI")
        height, width = (int(shape[0]), int(shape[1])) if shape else (0, 0)

        self.typeCombo = QtWidgets.QComboBox()
        self.typeCombo.addItems(["rectangle", "ellipse"])

        def _spin(value, maximum):
            box = QtWidgets.QSpinBox()
            box.setRange(-100000, 100000)
            box.setValue(int(value))
            if maximum:
                box.setToolTip(f"The image is {maximum} pixels on this axis")
            return box

        self.topSpin = _spin(0, height)
        self.leftSpin = _spin(0, width)
        self.heightSpin = _spin(max(1, height // 4) or 10, height)
        self.widthSpin = _spin(max(1, width // 4) or 10, width)
        self.centeredCheck = QtWidgets.QCheckBox("Centred on the image")
        self.centeredCheck.setToolTip(
            "Place the ROI at the centre of the image rather than at the "
            "coordinates above"
        )
        self._shape = (height, width)

        form = QtWidgets.QFormLayout()
        form.addRow("Type:", self.typeCombo)
        form.addRow("Width:", self.widthSpin)
        form.addRow("Height:", self.heightSpin)
        form.addRow("X (column):", self.leftSpin)
        form.addRow("Y (row):", self.topSpin)
        form.addRow("", self.centeredCheck)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def bounds(self) -> tuple[int, int, int, int]:
        """``(r0, r1, c0, c1)``, half-open, like every other ROI's."""
        height = max(1, int(self.heightSpin.value()))
        width = max(1, int(self.widthSpin.value()))
        if self.centeredCheck.isChecked() and all(self._shape):
            top = (self._shape[0] - height) // 2
            left = (self._shape[1] - width) // 2
        else:
            top, left = int(self.topSpin.value()), int(self.leftSpin.value())
        return (top, top + height, left, left + width)

    def roi_type(self) -> str:
        return self.typeCombo.currentText()

    @classmethod
    def specify(cls, shape=None, parent=None):
        dialog = cls(shape, parent)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            return dialog.roi_type(), dialog.bounds()
        return None


__all__ = ["ROIPropertiesDialog", "ROISpecifyDialog"]
