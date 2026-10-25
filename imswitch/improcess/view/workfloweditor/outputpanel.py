"""The inspector's lower half: what the selected step made in this session.

A large preview of one output -- an image with a plane slider, a curve in a
plot, the first rows of a table, labels in colour -- with its shape, axes,
kind and timing; the error when the step failed (a Python step's line and
what it printed); and the actions: *Run to here*, *Show in viewer*, *Pin to
the results list*. Large previews are computed off the GUI thread by the
window, which hands them in with :meth:`set_preview`.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from .canvas import style as st
from .thumbnails import image_from_rgb

_STATUS_WORDS = {
    "": "Not run in this session yet.",
    "done": "Up to date.",
    "outdated": "Outdated: something it depends on changed since this was made.",
    "running": "Running…",
    "waiting": "Waiting for the steps before it.",
    "failed": "Failed.",
    "would-write": "Write files is off: this step only says what it would write.",
}


class OutputPanel(QtWidgets.QWidget):
    """See the module docstring."""

    sigRunToHere = QtCore.Signal()
    sigShowInViewer = QtCore.Signal(str)
    sigPin = QtCore.Signal(str)
    #: port, plane (-1 for the default view): the window computes and calls set_preview
    sigPreviewWanted = QtCore.Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ports: list[str] = []
        self._previews: dict = {}
        self._planes = 1
        self._plot = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(5)
        title = QtWidgets.QLabel("OUTPUT")
        title.setStyleSheet("font-weight: bold; letter-spacing: 1px; padding-top: 6px;")
        title.setEnabled(False)
        layout.addWidget(title)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.port = QtWidgets.QComboBox()
        self.port.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.port.setMinimumContentsLength(8)
        self.port.currentIndexChanged.connect(self._port_changed)
        layout.addWidget(self.port)
        self.image = QtWidgets.QLabel()
        self.image.setAlignment(QtCore.Qt.AlignCenter)
        # Ignored horizontally: the picture follows the panel's width, never sets it.
        self.image.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.image.setFixedHeight(260)
        self.image.setStyleSheet("background: #0c1218; border-radius: 6px;")
        self._pixmap = None
        layout.addWidget(self.image)
        plane_row = QtWidgets.QHBoxLayout()
        self.plane = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.plane.setToolTip("Which plane of the stack to show; at the far left, a projection over the stack")
        self.plane.valueChanged.connect(self._plane_changed)
        self.plane_label = QtWidgets.QLabel()
        plane_row.addWidget(self.plane, 1)
        plane_row.addWidget(self.plane_label)
        self._plane_row = QtWidgets.QWidget()
        self._plane_row.setLayout(plane_row)
        layout.addWidget(self._plane_row)
        self.table = QtWidgets.QTableWidget()
        self.table.setMaximumHeight(200)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.table)
        self.plot_box = QtWidgets.QWidget()
        self.plot_box.setLayout(QtWidgets.QVBoxLayout())
        self.plot_box.layout().setContentsMargins(0, 0, 0, 0)
        self.plot_box.setMinimumHeight(200)
        layout.addWidget(self.plot_box)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.info.setEnabled(False)
        layout.addWidget(self.info)
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        self.error.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.error.setStyleSheet("background: #46161a; color: #ffd6d4; border: 1px solid #e2554f;"
                                 " border-radius: 6px; padding: 6px;")
        layout.addWidget(self.error)
        self.printed = QtWidgets.QPlainTextEdit()
        self.printed.setReadOnly(True)
        self.printed.setMaximumHeight(110)
        layout.addWidget(self.printed)
        actions = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("▶| Run to here")
        self.run_button.setToolTip("Run this step and what it needs; what is up to date comes from the cache (F6)")
        self.run_button.clicked.connect(self.sigRunToHere.emit)
        self.view_button = QtWidgets.QPushButton("Show in viewer")
        self.view_button.setToolTip("Show this output in the viewer (or the Graph panel, for a curve) until the "
                                    "next preview; it is not added to the results list")
        self.view_button.clicked.connect(lambda: self.sigShowInViewer.emit(self.current_port()))
        self.pin_button = QtWidgets.QPushButton("Pin")
        self.pin_button.setToolTip("Add this output to the results list, with its history, like any result")
        self.pin_button.clicked.connect(lambda: self.sigPin.emit(self.current_port()))
        for button in (self.run_button, self.view_button, self.pin_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.clear()

    # -- content -------------------------------------------------------------------

    def current_port(self) -> str:
        return str(self.port.currentData() or "")

    def clear(self) -> None:
        self.show_state("", None, None)

    def show_state(self, status: str, entry, error: dict | None, *, cached: bool = False, would_write=None,
                   wrote=()) -> None:
        """Show a step's run state: ``entry`` is its cache entry (or ``None``)."""
        words = _STATUS_WORDS.get(status, status)
        if status == "done" and entry is not None:
            words = f"Done in {_seconds(entry.seconds)}" + (" · taken from the cache in the last run" if cached else "")
        if status == "would-write" and would_write is not None:
            words = f"Write files is off: would write {getattr(would_write, 'name', would_write)}"
        if wrote:
            words = "Wrote " + ", ".join(getattr(path, "name", str(path)) for path in wrote)
        self.status.setText(words)
        self._previews = dict(entry.previews) if entry is not None else {}
        self._ports = list(self._previews)
        self.port.blockSignals(True)
        self.port.clear()
        for port in self._ports:
            preview = self._previews[port]
            self.port.addItem(f"{port} · {st.KIND_WORDS.get(preview.kind, preview.kind)}", port)
        self.port.blockSignals(False)
        self.port.setVisible(len(self._ports) > 1)
        held = entry is not None and entry.held
        self.view_button.setEnabled(held and status in ("done", "outdated"))
        self.pin_button.setEnabled(held and status == "done")
        self.run_button.setEnabled(status not in ("running", "waiting"))
        if error:
            from html import escape

            line = error.get("line")
            heading = f"Failed at line {line}" if line else "Failed"
            self.error.setText(f"<b>{heading}</b><br>{escape(failure_text(error, with_line=False))}")
            self.error.show()
            printed = str(error.get("stdout") or "")
            self.printed.setPlainText(printed)
            self.printed.setVisible(bool(printed))
        else:
            self.error.hide()
            self.printed.hide()
        self._show_port(dim=status == "outdated")

    def _port_changed(self, _index: int) -> None:
        self._show_port()

    def _show_port(self, dim: bool = False) -> None:
        port = self.current_port()
        preview = self._previews.get(port)
        self.image.hide()
        self._plane_row.hide()
        self.table.hide()
        self.plot_box.hide()
        if preview is None:
            self.info.setText("")
            return
        self.info.setText(" · ".join(part for part in (
            st.KIND_WORDS.get(preview.kind, preview.kind), preview.describe(),
            "a projection over a sample of the planes" if preview.sampled else "") if part))
        if preview.rgb is not None:
            self._planes = max(1, int(preview.planes))
            self._set_image(preview, dim)
            if self._planes > 1:
                self.plane.blockSignals(True)
                self.plane.setRange(-1, self._planes - 1)
                self.plane.setValue(int(preview.plane))
                self.plane.blockSignals(False)
                self._plane_label(int(preview.plane))
                self._plane_row.show()
            self.sigPreviewWanted.emit(port, int(self.plane.value()) if self._planes > 1 else -1)
        elif preview.curve is not None:
            self._show_curve(preview)
        elif preview.rows or preview.columns:
            self._show_table(preview)

    def _set_image(self, preview, dim: bool = False) -> None:
        from qtpy import QtGui

        self._pixmap = QtGui.QPixmap.fromImage(image_from_rgb(preview.rgb))
        self.image.setEnabled(not dim)
        self.image.show()
        self._fit_image()

    def _fit_image(self) -> None:
        if self._pixmap is None:
            return
        target = QtCore.QSize(max(self.image.width(), 120), self.image.height())
        self.image.setPixmap(self._pixmap.scaled(target, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_image()

    def _plane_label(self, plane: int) -> None:
        self.plane_label.setText("projection" if plane < 0 else f"plane {plane + 1} / {self._planes}")

    def _plane_changed(self, value: int) -> None:
        self._plane_label(value)
        self.sigPreviewWanted.emit(self.current_port(), int(value))

    def set_preview(self, port: str, preview) -> None:
        """A larger preview (or another plane) arrived for ``port``."""
        if port != self.current_port() or preview is None or preview.rgb is None:
            return
        self._set_image(preview)

    def _show_curve(self, preview) -> None:
        try:
            import pyqtgraph as pg
        except ImportError:   # pragma: no cover - pyqtgraph is a dependency
            return
        if self._plot is None:
            self._plot = pg.PlotWidget()
            self._plot.setBackground("#0c1218")
            self.plot_box.layout().addWidget(self._plot)
        self._plot.clear()
        values = preview.extra.get("full", preview.curve)
        self._plot.plot(values, pen=pg.mkPen(st.KIND_COLOURS["curve"], width=2))
        self.plot_box.show()

    def show_full_curve(self, x, y) -> None:
        if self._plot is None:
            return
        import pyqtgraph as pg

        self._plot.clear()
        self._plot.plot(x, y, pen=pg.mkPen(st.KIND_COLOURS["curve"], width=2))

    def _show_table(self, preview) -> None:
        self.table.clear()
        self.table.setColumnCount(len(preview.columns))
        self.table.setHorizontalHeaderLabels(list(preview.columns))
        self.table.setRowCount(len(preview.rows))
        for row, values in enumerate(preview.rows):
            for column, value in enumerate(values):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
        self.table.show()


def failure_text(failure: dict, *, with_line: bool = True) -> str:
    """A failure in words: ``line 11: ValueError: ...`` once, not twice (a
    Python step's message already starts with its line)."""
    import re

    message = str(failure.get("message", "") or "")
    line = failure.get("line")
    if line:
        message = re.sub(rf"^line {int(line)}: ", "", message)
        return f"line {line}: {message}" if with_line else message
    return message


def _seconds(value: float) -> str:
    value = float(value)
    return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.1f} s"


__all__ = ["OutputPanel", "failure_text"]
