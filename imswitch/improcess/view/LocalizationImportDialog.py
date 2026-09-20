"""Column-mapping dialog for importing an arbitrary localization CSV.

ThunderSTORM and Picasso files are read without asking anything, because they
declare their own columns and units. Everything else needs a human to say what
the columns mean, and this is where that happens.

The guesswork itself lives in
:mod:`~imswitch.improcess.analysis.smlm_import` so it can be tested without Qt;
this dialog only presents it and collects the corrections.
"""

from __future__ import annotations

from pathlib import Path

from qtpy import QtWidgets

from imswitch.improcess.analysis.smlm_import import (
    LENGTH_UNIT_CHOICES,
    guess_column_mapping,
    guess_length_unit,
    read_csv_headers,
)
from imswitch.improcess.model.localization_schema import (
    LOCALIZATION_COLUMNS,
    REQUIRED_INPUT_COLUMNS,
)

#: Human labels for the canonical columns, in the order the dialog lists them.
#: Mandatory ones first; the rest are genuinely optional and left blank when a
#: file has nothing to put in them.
_ROW_LABELS: dict[str, str] = {
    "x_nm": "X position",
    "y_nm": "Y position",
    "z_nm": "Z position",
    "frame": "Frame",
    "photons": "Photons / intensity",
    "sigma_x_nm": "PSF width X",
    "sigma_y_nm": "PSF width Y",
    "sigma_z_nm": "PSF width Z",
    "lp_x_nm": "Precision X",
    "lp_y_nm": "Precision Y",
    "lp_z_nm": "Precision Z",
}

_NOT_MAPPED = "— not in file —"


class LocalizationImportDialog(QtWidgets.QDialog):
    """Map the columns of a delimited file onto the canonical schema."""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Import localizations — {Path(path).name}")
        self._path = Path(path)
        self._headers = read_csv_headers(self._path)

        guessed = guess_column_mapping(self._headers)
        layout = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            f"<b>{self._path.name}</b> is not a format ImProcess recognises, so "
            f"its columns need naming. Positions are required; everything else "
            f"is optional."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self._combos: dict[str, QtWidgets.QComboBox] = {}
        for canonical in LOCALIZATION_COLUMNS:
            combo = QtWidgets.QComboBox()
            combo.addItem(_NOT_MAPPED, None)
            for header in self._headers:
                combo.addItem(header, header)
            preset = guessed.get(canonical)
            if preset is not None:
                combo.setCurrentIndex(self._headers.index(preset) + 1)
            label = _ROW_LABELS.get(canonical, canonical)
            if canonical in REQUIRED_INPUT_COLUMNS:
                label = f"{label} *"
            form.addRow(f"{label}:", combo)
            self._combos[canonical] = combo
        layout.addLayout(form)

        options = QtWidgets.QFormLayout()
        self._unit = QtWidgets.QComboBox()
        self._unit.addItems(LENGTH_UNIT_CHOICES)
        detected = guess_length_unit(self._headers)
        if detected in LENGTH_UNIT_CHOICES:
            self._unit.setCurrentIndex(LENGTH_UNIT_CHOICES.index(detected))
        options.addRow("Position unit:", self._unit)

        self._pixelSize = QtWidgets.QDoubleSpinBox()
        self._pixelSize.setRange(0.0, 100000.0)
        self._pixelSize.setDecimals(2)
        self._pixelSize.setValue(0.0)
        self._pixelSize.setSpecialValueText("unknown")
        self._pixelSize.setSuffix(" nm")
        options.addRow("Camera pixel size:", self._pixelSize)

        self._frameBase = QtWidgets.QSpinBox()
        self._frameBase.setRange(0, 1)
        self._frameBase.setValue(0)
        self._frameBase.setToolTip(
            "The number the file gives its first frame. ThunderSTORM counts "
            "from 1; most other tools count from 0."
        )
        options.addRow("First frame number:", self._frameBase)
        layout.addLayout(options)

        self._warning = QtWidgets.QLabel()
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet("color: palette(link);")
        layout.addWidget(self._warning)

        self._buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        for combo in self._combos.values():
            combo.currentIndexChanged.connect(self._revalidate)
        self._unit.currentIndexChanged.connect(self._revalidate)
        self._pixelSize.valueChanged.connect(self._revalidate)
        self._revalidate()

    # -- state ------------------------------------------------------------

    def column_mapping(self) -> dict[str, str]:
        """Canonical column -> header, omitting anything left unmapped."""
        return {
            canonical: combo.currentData()
            for canonical, combo in self._combos.items()
            if combo.currentData() is not None
        }

    def length_unit(self) -> str:
        return self._unit.currentText()

    def pixel_size_nm(self) -> float | None:
        value = float(self._pixelSize.value())
        return value if value > 0 else None

    def frame_base(self) -> int:
        return int(self._frameBase.value())

    def import_kwargs(self) -> dict:
        """Everything ``read_generic_csv`` needs, besides the path."""
        return {
            "mapping": self.column_mapping(),
            "unit": self.length_unit(),
            "pixel_size_nm": self.pixel_size_nm(),
            "frame_base": self.frame_base(),
        }

    # -- validation -------------------------------------------------------

    def _problem(self) -> str | None:
        """Why the current selection cannot be imported, if it cannot."""
        mapping = self.column_mapping()
        missing = [name for name in REQUIRED_INPUT_COLUMNS if name not in mapping]
        if missing:
            labels = ", ".join(_ROW_LABELS.get(name, name) for name in missing)
            return f"Choose a column for: {labels}."
        if self.length_unit() == "px" and self.pixel_size_nm() is None:
            # Reading pixels without a pixel size would silently rescale
            # everything, so the dialog refuses rather than the reader.
            return "Positions are in pixels, so a camera pixel size is needed."
        return None

    def _advice(self) -> str:
        """Non-blocking notes about what will be assumed."""
        notes = []
        if self.pixel_size_nm() is None:
            notes.append(
                "No pixel size: coordinates are unaffected, but the preview "
                "scale and any pixel-native export will use an assumed value."
            )
        mapping = self.column_mapping()
        if "photons" not in mapping:
            notes.append("No photon column: intensity-based filtering will be unavailable.")
        if not {"lp_x_nm", "lp_y_nm"} & set(mapping):
            notes.append(
                "No precision column: rendering falls back to PSF width, which "
                "is broader than a localization uncertainty."
            )
        return " ".join(notes)

    def _revalidate(self) -> None:
        problem = self._problem()
        self._buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(problem is None)
        self._warning.setText(problem or self._advice())

    # -- entry point ------------------------------------------------------

    @classmethod
    def get_import_kwargs(cls, path, parent=None) -> dict | None:
        """Show the dialog; return ``read_generic_csv`` kwargs, or None."""
        dialog = cls(path, parent=parent)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.import_kwargs()


__all__ = ["LocalizationImportDialog"]
