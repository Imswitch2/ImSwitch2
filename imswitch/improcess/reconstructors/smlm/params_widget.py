"""Parameter widget for the SMLM localizer."""

from __future__ import annotations

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtWidgets


class SmlmParamsWidget(QtWidgets.QWidget):
    """Detection + fitting parameters for :class:`SmlmLocalizer`."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        params = [
            {"name": "Detection", "type": "group", "children": [
                {"name": "Net-gradient threshold", "type": "float", "value": 100.0,
                 "limits": (0.0, 1e9),
                 "tip": "Minimum summed 4-direction gradient for a candidate peak"},
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
                {"name": "Pixel size (nm)", "type": "float", "value": 100.0,
                 "limits": (0.0, 1e6),
                 "tip": "Camera pixel size; localizations are stored in nm"},
            ]},
        ]

        self.p = Parameter.create(name="params", type="group", children=params)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)
        self.setLayout(layout)

    def get_values(self) -> dict:
        detection = self.p.param("Detection")
        fitting = self.p.param("Fitting")
        calibration = self.p.param("Calibration")
        return {
            "threshold": float(detection.param("Net-gradient threshold").value()),
            "sigma": float(detection.param("Smoothing sigma").value()),
            "roi": int(detection.param("ROI size").value()),
            "method": str(fitting.param("Method").value()),
            "pixel_size_nm": float(calibration.param("Pixel size (nm)").value()),
        }


__all__ = ["SmlmParamsWidget"]
