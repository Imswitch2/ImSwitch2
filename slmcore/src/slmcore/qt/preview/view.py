from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore,QtGui,QtWidgets

from ..sections.display import SectionsDisplayMode


class SLMPreviewView(QtWidgets.QWidget):
    """Raw reusable SLM frame viewer with no container/layout assumptions."""

    def __init__(self,parent=None) -> None:
        super().__init__(parent)
        self._current_frame: np.ndarray | None = None
        self._sections_host = None
        self._highlight_active_section = True
        self._highlighted_section_key: str | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)

        self.graphics_widget = pg.GraphicsLayoutWidget(self)
        self.view_box = self.graphics_widget.addViewBox(row=0,col=0)
        self.view_box.setAspectLocked(True)
        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.view_box.addItem(self.image_item)

        # Permanent outline of the physical SLM area. Unlike the active-section
        # highlight, this remains visible even when no frame is loaded.
        self._slm_boundary = QtWidgets.QGraphicsRectItem()
        boundary_pen = QtGui.QPen(QtGui.QColor(120,125,132,190))
        boundary_pen.setWidth(1)
        boundary_pen.setCosmetic(True)
        self._slm_boundary.setPen(boundary_pen)
        self._slm_boundary.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        self._slm_boundary.setZValue(900)
        self._slm_boundary.hide()
        self.view_box.addItem(self._slm_boundary)

        self._section_highlight = QtWidgets.QGraphicsRectItem()
        pen = QtGui.QPen(QtGui.QColor("red"))
        pen.setWidth(2)
        pen.setCosmetic(True)
        self._section_highlight.setPen(pen)
        self._section_highlight.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        self._section_highlight.setZValue(1000)
        self._section_highlight.hide()
        self.view_box.addItem(self._section_highlight)

        layout.addWidget(self.graphics_widget)

    @property
    def current_frame(self) -> np.ndarray | None:
        return self._current_frame

    @property
    def highlighted_section_key(self) -> str | None:
        return self._highlighted_section_key

    @property
    def section_highlight_visible(self) -> bool:
        return bool(self._section_highlight.isVisible())

    def set_frame(self,frame: np.ndarray) -> None:
        frame = np.asarray(frame)
        if frame.ndim != 2:
            raise ValueError("SLM frame must be a two-dimensional array")
        self._current_frame = frame
        self.image_item.setImage(frame,autoLevels=True)
        self._refresh_overlays()

    def clear_frame(self) -> None:
        self._current_frame = None
        self.image_item.clear()
        self._refresh_overlays()

    def set_section_highlight_enabled(self,enabled: bool) -> None:
        self._highlight_active_section = bool(enabled)
        self._refresh_section_highlight()

    def reset_view(self) -> None:
        if self._current_frame is not None:
            self.image_item.setImage(self._current_frame,autoLevels=False)
        self.view_box.autoRange()

    def bind_sections_host(
        self,section_host,*,highlight_active_section: bool=True,
    ) -> None:
        """Bind physical SLM/section overlays to a ``SectionsViewHost``."""
        if section_host is self._sections_host:
            self._highlight_active_section = bool(highlight_active_section)
            self._refresh_overlays()
            return

        self._disconnect_sections_host()
        self._sections_host = section_host
        self._highlight_active_section = bool(highlight_active_section)

        if section_host is not None:
            section_host.sigCurrentSectionChanged.connect(
                self._refresh_overlays
            )
            section_host.sigSectionsChanged.connect(
                self._refresh_overlays
            )
            section_host.sigDisplayModeChanged.connect(
                self._refresh_overlays
            )
        self._refresh_overlays()

    def _disconnect_sections_host(self) -> None:
        host = self._sections_host
        if host is None:
            return
        for signal_name in (
            "sigCurrentSectionChanged",
            "sigSectionsChanged",
            "sigDisplayModeChanged",
        ):
            signal = getattr(host,signal_name,None)
            if signal is None:
                continue
            try:
                signal.disconnect(self._refresh_overlays)
            except (TypeError,RuntimeError):
                pass
        self._sections_host = None

    def _refresh_overlays(self,*_args) -> None:
        self._refresh_slm_boundary()
        self._refresh_section_highlight()

    def _refresh_slm_boundary(self) -> None:
        host = self._sections_host
        if host is None:
            self._slm_boundary.hide()
            return

        geometries = host.section_geometries()
        if not geometries:
            self._slm_boundary.hide()
            return

        x0 = min(float(geometry.x) for geometry in geometries.values())
        y0 = min(float(geometry.y) for geometry in geometries.values())
        x1 = max(
            float(geometry.x + geometry.width)
            for geometry in geometries.values()
        )
        y1 = max(
            float(geometry.y + geometry.height)
            for geometry in geometries.values()
        )

        self._slm_boundary.setRect(
            QtCore.QRectF(
                x0,
                y0,
                x1 - x0,
                y1 - y0,
            )
        )
        self._slm_boundary.show()

        # With no image loaded, the boundary is the only item defining the
        # physical SLM extent, so make sure it is brought into view.
        if self._current_frame is None:
            self.view_box.autoRange()

    def _refresh_section_highlight(self,*_args) -> None:
        self._highlighted_section_key = None
        host = self._sections_host
        if (
            self._current_frame is None
            or not self._highlight_active_section
            or host is None
            or host.display_mode != SectionsDisplayMode.TABS
        ):
            self._section_highlight.hide()
            return

        geometries = host.section_geometries()
        if len(geometries) <= 1:
            self._section_highlight.hide()
            return

        section_key = host.current_section_key()
        geometry = geometries.get(section_key)
        if geometry is None:
            self._section_highlight.hide()
            return

        self._section_highlight.setRect(
            QtCore.QRectF(
                float(geometry.x),
                float(geometry.y),
                float(geometry.width),
                float(geometry.height),
            )
        )
        self._highlighted_section_key = section_key
        self._section_highlight.show()

    def closeEvent(self,event) -> None:
        self._disconnect_sections_host()
        super().closeEvent(event)