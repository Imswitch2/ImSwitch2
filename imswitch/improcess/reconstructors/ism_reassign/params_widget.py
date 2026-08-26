"""Parameter widget for the isolated GPU ISM reassignment reconstructor."""

from __future__ import annotations

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtWidgets


_ORIENTATIONS = [
    "X+Y-",
    "X+Y+",
    "X-Y-",
    "X-Y+",
    "Y+X-",
    "Y+X+",
    "Y-X-",
    "Y-X+",
]


class IsmReassignParamsWidget(QtWidgets.QWidget):
    """Small parameter tree that also plugs into ImProcess's pattern overlay."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.p = Parameter.create(
            name="ISM reassignment",
            type="group",
            children=[
                {
                    "name": "Pixel size",
                    "type": "float",
                    "value": 100.0,
                    "limits": (0.01, 1e6),
                    "suffix": "nm",
                },
                {
                    "name": "Show pattern",
                    "type": "bool",
                    "value": False,
                    "tip": "Overlay the rectangular lattice on the raw mean image.",
                },
                {
                    "name": "Pattern",
                    "type": "group",
                    "children": [
                        {"name": "Find pattern", "type": "action"},
                        {"name": "Row-offset", "type": "float", "value": 4.0, "limits": (0, 9999)},
                        {"name": "Col-offset", "type": "float", "value": 4.0, "limits": (0, 9999)},
                        {"name": "Row-period", "type": "float", "value": 9.8, "limits": (0.01, 9999)},
                        {"name": "Col-period", "type": "float", "value": 9.8, "limits": (0.01, 9999)},
                    ],
                },
                {
                    "name": "ISM options",
                    "type": "group",
                    "children": [
                        {
                            "name": "PSF FWHM",
                            "type": "float",
                            "value": 200.0,
                            "limits": (0.01, 9999),
                            "suffix": "nm",
                        },
                        {
                            "name": "Oversampling",
                            "type": "float",
                            "value": 2.0,
                            "limits": (1.0, 8.0),
                        },
                        {
                            "name": "ISM shift",
                            "type": "float",
                            "value": 0.5,
                            "limits": (-4.0, 4.0),
                            "tip": "Pixel-reassignment fraction; 0.5 is the standard matched-PSF value.",
                        },
                        {
                            "name": "Subtract patch mean",
                            "type": "bool",
                            "value": True,
                        },
                        {
                            "name": "Scan orientation",
                            "type": "list",
                            "value": "X+Y-",
                            "values": _ORIENTATIONS,
                        },
                        {
                            "name": "Bidirectional scan",
                            "type": "bool",
                            "value": False,
                            "tip": "Reverse every second fast-scan row before reconstruction.",
                        },
                        {
                            "name": "GPU frame batch",
                            "type": "int",
                            "value": 0,
                            "limits": (0, 100000),
                            "tip": "0 processes all scan frames together (fastest). Lower values reduce temporary GPU memory.",
                        },
                    ],
                },
            ],
        )
        self.tree = ParameterTree()
        self.tree.setParameters(self.p, showTop=False)
        layout.addWidget(self.tree)

    def get_values(self) -> dict:
        pattern = self.p.param("Pattern")
        options = self.p.param("ISM options")
        orientation = str(options.param("Scan orientation").value())
        if bool(options.param("Bidirectional scan").value()):
            orientation += "b"
        return {
            "pixel_size_nm": float(self.p.param("Pixel size").value()),
            "row_offset": float(pattern.param("Row-offset").value()),
            "col_offset": float(pattern.param("Col-offset").value()),
            "row_period": float(pattern.param("Row-period").value()),
            "col_period": float(pattern.param("Col-period").value()),
            "psf_fwhm_nm": float(options.param("PSF FWHM").value()),
            "oversampling": float(options.param("Oversampling").value()),
            "ism_shift": float(options.param("ISM shift").value()),
            "remove_mean_of_patch": bool(options.param("Subtract patch mean").value()),
            "scanning_orientation": orientation,
            "frame_batch_size": int(options.param("GPU frame batch").value()) or None,
        }

    def set_pattern_params(
        self,
        row_offset: float,
        col_offset: float,
        row_period: float,
        col_period: float,
    ) -> None:
        pattern = self.p.param("Pattern")
        pattern.param("Row-offset").setValue(float(row_offset))
        pattern.param("Col-offset").setValue(float(col_offset))
        pattern.param("Row-period").setValue(float(row_period))
        pattern.param("Col-period").setValue(float(col_period))

    def set_show_pattern(self, visible: bool) -> None:
        self.p.param("Show pattern").setValue(bool(visible))


__all__ = ["IsmReassignParamsWidget"]
