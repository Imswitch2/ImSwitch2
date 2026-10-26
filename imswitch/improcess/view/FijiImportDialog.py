"""Choose which of Fiji's open images to import.

One row per image, with where it seems to come from, so the user can tell the
image they sent from one they made in Fiji. Nothing is imported until OK; rows
the host cannot fetch (RGB) are shown but cannot be selected.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

#: The bit depths the host can fetch. Mirrors host._DTYPE_BY_BIT_DEPTH;
#: duplicated because the host module must not be imported here (it imports imagej).
_FETCHABLE_BIT_DEPTHS = (8, 16, 32)
_RGB_BIT_DEPTH = 24

RGB_TOOLTIP = "RGB images are not imported (convert to a stack in Fiji first)"

COLUMNS = ("Title", "ImageJ id", "Size", "Type", "Origin", "Imported")
_TITLE, _ID, _SIZE, _TYPE, _ORIGIN, _IMPORTED = range(len(COLUMNS))

_CHECK = "✓"
_TIMES = "×"


def format_size(dims_xyczt) -> str:
    """``X×Y, C c, Z z, T t``, leaving out the axes that hold a single plane."""
    x, y, c, z, t = (int(n) for n in dims_xyczt)
    parts = [f"{x}{_TIMES}{y}"]
    for count, suffix in ((c, "c"), (z, "z"), (t, "t")):
        if count > 1:
            parts.append(f"{count} {suffix}")
    return ", ".join(parts)


def format_type(bit_depth: int) -> str:
    return "RGB" if bit_depth == _RGB_BIT_DEPTH else f"{bit_depth}-bit"


class FijiImportDialog(QtWidgets.QDialog):
    #: The user asked for the list again; the owner re-lists and calls :meth:`setDescriptions`.
    sigRefreshRequested = QtCore.Signal()

    def __init__(self, descriptions, sent=None, imported=None, source_names=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import from Fiji")
        self._ids: list[int] = []

        layout = QtWidgets.QVBoxLayout(self)
        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.MultiSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.refreshButton = buttons.addButton("Refresh", QtWidgets.QDialogButtonBox.ActionRole)
        self.refreshButton.clicked.connect(lambda _checked=False: self.sigRefreshRequested.emit())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(760, 320)

        self.setDescriptions(descriptions, sent, imported, source_names)

    def setDescriptions(self, descriptions, sent=None, imported=None, source_names=None) -> None:
        """Replace the rows. Rows still present keep their selection; new ones get the default."""
        sent, imported, source_names = dict(sent or {}), set(imported or ()), dict(source_names or {})
        previous = set(self.selectedIds())
        known = set(self._ids)
        descriptions = list(descriptions)
        self.table.clearSelection()
        self.table.setRowCount(len(descriptions))
        self._ids = []
        for row, desc in enumerate(descriptions):
            image_id = desc.image_id
            self._ids.append(image_id)
            cells = {
                _TITLE: desc.title,
                _ID: str(image_id),
                _SIZE: format_size(desc.dims_xyczt),
                _TYPE: format_type(desc.bit_depth),
                _ORIGIN: self._origin(image_id, sent, source_names),
                _IMPORTED: _CHECK if image_id in imported else "",
            }
            fetchable = desc.bit_depth in _FETCHABLE_BIT_DEPTHS
            for column, text in cells.items():
                item = QtWidgets.QTableWidgetItem(text)
                if not fetchable:
                    item.setFlags(QtCore.Qt.NoItemFlags)
                    item.setToolTip(
                        RGB_TOOLTIP if desc.bit_depth == _RGB_BIT_DEPTH
                        else f"{desc.bit_depth}-bit images are not imported"
                    )
                self.table.setItem(row, column, item)
            if not fetchable:
                continue
            wanted = (image_id in previous) if image_id in known else (
                image_id not in imported and image_id not in sent
            )
            if wanted:
                self.table.selectRow(row)
        self.table.resizeColumnsToContents()

    @staticmethod
    def _origin(image_id, sent, source_names) -> str:
        name = source_names.get(image_id)
        if image_id in sent:
            return f"sent from {name}" if name else "sent from ImProcess"
        if name:
            return f"derived from {name} (lineage hint)"
        return "unknown"

    def selectedIds(self) -> list[int]:
        """The chosen image ids, in table order."""
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self._ids[row] for row in rows if row < len(self._ids)]
