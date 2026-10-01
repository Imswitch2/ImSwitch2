"""Parameter widget for the SMLM localizer."""

from __future__ import annotations

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets


class SmlmParamsWidget(QtWidgets.QWidget):
    """Detection + fitting parameters for :class:`SmlmLocalizer`."""

    sigPreviewToggled = QtCore.Signal(bool)
    sigDetectionParamsChanged = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        params = [
            {"name": "Detection", "type": "group", "children": [
                {"name": "Net-gradient threshold", "type": "float", "value": 500.0,
                 "limits": (0.0, 1e9),
                 "tip": ("Minimum Picasso net gradient (inward slopes summed over "
                         "the ROI) for a candidate peak. Tune with the Preview "
                         "detection toggle: raise to reject noise, lower to catch "
                         "dim emitters.")},
                {"name": "Smoothing sigma", "type": "float", "value": 1.0,
                 "limits": (0.0, 20.0),
                 "tip": "Gaussian pre-smoothing width in pixels"},
                {"name": "ROI size", "type": "int", "value": 7,
                 "limits": (3, 31), "step": 2,
                 "tip": "Fitting window size in pixels (odd)"},
            ]},
            {"name": "Fitting", "type": "group", "children": [
                {"name": "Method", "type": "list",
                 "values": ["gausslq", "mle"], "value": "gausslq",
                 "tip": "gausslq: centroid + moments; mle: Poisson Gaussian MLE"},
            ]},
            {"name": "Calibration", "type": "group", "children": [
                # The value alone could not say whether it was a choice or a
                # default: it was always sent, so the recording's own
                # calibration was never consulted, and a stack calibrated
                # differently along Y than along X was localized as if it
                # were square without anyone deciding that.
                {"name": "Pixel size", "type": "list",
                 "values": ["From the recording", "Enter below"],
                 "value": "From the recording",
                 "tip": "Where the pixel size comes from. The recording's own "
                        "calibration is used unless you override it here"},
                {"name": "Pixel size (nm)", "type": "float", "value": 100.0,
                 "limits": (0.0, 1e6),
                 "tip": "Used when 'Pixel size' is set to 'Enter below'; "
                        "localizations are stored in nm"},
            ]},
        ]

        self.p = Parameter.create(name="params", type="group", children=params)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)

        self.previewCheckbox = QtWidgets.QCheckBox("Preview detection")
        self.previewCheckbox.setToolTip(
            "Show detected spots on the raw data viewer (detection only, no fitting)"
        )
        self.previewCheckbox.toggled.connect(self.sigPreviewToggled)

        # Debounce rapid parameter edits (spinbox arrows, typing) so the
        # preview recomputes at most once per pause instead of per keystroke.
        self._detectionDebounce = QtCore.QTimer(self)
        self._detectionDebounce.setSingleShot(True)
        self._detectionDebounce.setInterval(250)
        self._detectionDebounce.timeout.connect(self.sigDetectionParamsChanged)

        self.p.sigTreeStateChanged.connect(self._onTreeChanged)

        # Feedback line for the live preview: without it, "0 candidates at
        # this threshold" is indistinguishable from "preview not working".
        self.previewStatusLabel = QtWidgets.QLabel("")
        self.previewStatusLabel.setStyleSheet(
            'color: palette(mid); font-size: 9pt; padding: 0px 4px;'
        )
        self.previewStatusLabel.setWordWrap(True)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)
        layout.addWidget(self.previewCheckbox)
        layout.addWidget(self.previewStatusLabel)
        self.setLayout(layout)

    def _onTreeChanged(self, param, changes):
        for param, change, _data in changes:
            parent_name = param.parent().name() if param.parent() is not None else None
            if parent_name == "Detection":
                self._detectionDebounce.start()
                break

    def get_values(self) -> dict:
        detection = self.p.param("Detection")
        fitting = self.p.param("Fitting")
        calibration = self.p.param("Calibration")
        manual = str(calibration.param("Pixel size").value()) == "Enter below"
        return {
            "threshold": float(detection.param("Net-gradient threshold").value()),
            "sigma": float(detection.param("Smoothing sigma").value()),
            "roi": int(detection.param("ROI size").value()),
            "method": str(fitting.param("Method").value()),
            # Absent unless it was chosen, so the recording's calibration is
            # what answers -- and an anisotropic one can say so.
            "pixel_size_nm": (
                float(calibration.param("Pixel size (nm)").value())
                if manual else None
            ),
        }

    def get_detection_values(self) -> dict:
        """Return only detection parameters (threshold, sigma, ROI)."""
        detection = self.p.param("Detection")
        return {
            "threshold": float(detection.param("Net-gradient threshold").value()),
            "sigma": float(detection.param("Smoothing sigma").value()),
            "roi": int(detection.param("ROI size").value()),
        }

    def setPreviewStatus(self, text: str) -> None:
        """Show live-preview feedback (candidate count / no-data hint)."""
        self.previewStatusLabel.setText(str(text))


__all__ = ["SmlmParamsWidget"]
