"""WidefieldSTARSS parameter widget."""

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtWidgets


class WidefieldStarssParamsWidget(QtWidgets.QWidget):
    """Parameter tree for WidefieldSTARSS H/V analysis."""

    def __init__(self, parent=None):
        super().__init__(parent)
        params = [
            {"name": "Preset", "type": "list", "values": ["Custom", "Widefield cells", "Line PSF / split detection"], "value": "Custom"},
            {"name": "Pairing", "type": "group", "children": [
                {"name": "Current file role", "type": "list", "values": ["Auto", "H", "V"], "value": "Auto"},
                {"name": "Counterpart path", "type": "str", "value": ""},
            ]},
            {"name": "Loading", "type": "group", "children": [
                {"name": "Convention", "type": "list", "values": ["alternating", "block"], "value": "alternating"},
                {"name": "Start frame", "type": "int", "value": 0, "limits": (0, 1000000)},
                {"name": "Dark frames", "type": "int", "value": 0, "limits": (0, 1000000)},
                {"name": "Off/background frames", "type": "int", "value": 0, "limits": (0, 1000000)},
                {"name": "Sum stacks", "type": "bool", "value": False},
            ]},
            {"name": "Analysis", "type": "group", "children": [
                {"name": "Split detection", "type": "bool", "value": False},
                {"name": "Split Y", "type": "int", "value": 0, "limits": (0, 1000000)},
                {"name": "Segmentation mode", "type": "list", "values": ["none", "otsu", "psf_peaks", "line_psf"], "value": "none"},
                {"name": "Smooth sigma", "type": "float", "value": 2.0, "limits": (0.0, 1000.0)},
                {"name": "Intensity threshold", "type": "float", "value": 0.0},
                {"name": "Use intensity threshold", "type": "bool", "value": False},
            ]},
            {"name": "Segmentation", "type": "group", "children": [
                {"name": "Sigma", "type": "float", "value": 2.0, "limits": (0.0, 1000.0)},
                {"name": "Min size", "type": "int", "value": 200, "limits": (0, 1000000)},
                {"name": "Hole size", "type": "int", "value": 200, "limits": (0, 1000000)},
                {"name": "Threshold scale", "type": "float", "value": 1.0, "limits": (0.0, 1000.0)},
                {"name": "PSF sigma", "type": "float", "value": 2.0, "limits": (0.0, 1000.0)},
                {"name": "PSF min distance", "type": "int", "value": 5, "limits": (1, 1000000)},
                {"name": "PSF threshold rel", "type": "float", "value": 0.1, "limits": (0.0, 1.0)},
                {"name": "PSF radius", "type": "int", "value": 3, "limits": (1, 1000000)},
            ]},
        ]

        self.p = Parameter.create(name="params", type="group", children=params)
        self.p.param("Preset").sigValueChanged.connect(self._preset_changed)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)

        self.browseButton = QtWidgets.QPushButton("Browse counterpart...")
        self.browseButton.clicked.connect(self._browse_counterpart)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)
        layout.addWidget(self.browseButton)
        self.setLayout(layout)

    def get_values(self) -> dict:
        pairing = self.p.param("Pairing")
        loading = self.p.param("Loading")
        analysis = self.p.param("Analysis")
        segmentation = self.p.param("Segmentation")

        split_y = int(analysis.param("Split Y").value())
        counterpart_path = str(pairing.param("Counterpart path").value()).strip()

        return {
            "current_role": pairing.param("Current file role").value(),
            "counterpart_path": counterpart_path or None,
            "convention": loading.param("Convention").value(),
            "start_frame": int(loading.param("Start frame").value()),
            "n_dark": int(loading.param("Dark frames").value()),
            "n_off": int(loading.param("Off/background frames").value()),
            "sum_stacks": bool(loading.param("Sum stacks").value()),
            "split_detection": bool(analysis.param("Split detection").value()),
            "split_y": split_y if split_y > 0 else None,
            "segmentation_mode": analysis.param("Segmentation mode").value(),
            "smooth_sigma": float(analysis.param("Smooth sigma").value()),
            "intensity_threshold": (
                float(analysis.param("Intensity threshold").value())
                if bool(analysis.param("Use intensity threshold").value())
                else None
            ),
            "segmentation_sigma": float(segmentation.param("Sigma").value()),
            "min_size": int(segmentation.param("Min size").value()),
            "hole_size": int(segmentation.param("Hole size").value()),
            "threshold_scale": float(segmentation.param("Threshold scale").value()),
            "psf_sigma": float(segmentation.param("PSF sigma").value()),
            "psf_min_distance": int(segmentation.param("PSF min distance").value()),
            "psf_threshold_rel": float(segmentation.param("PSF threshold rel").value()),
            "psf_radius": int(segmentation.param("PSF radius").value()),
        }

    def _browse_counterpart(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select WidefieldSTARSS H/V counterpart",
            "",
            "TIFF files (*.tif *.tiff);;All files (*)",
        )
        if path:
            self.p.param("Pairing").param("Counterpart path").setValue(path)

    def _preset_changed(self, _param, value) -> None:
        if value == "Widefield cells":
            self._apply_values(
                {
                    "Loading": {
                        "Convention": "alternating",
                        "Start frame": 20,
                        "Sum stacks": False,
                    },
                    "Analysis": {
                        "Split detection": False,
                        "Segmentation mode": "otsu",
                        "Smooth sigma": 10.0,
                        "Use intensity threshold": False,
                    },
                    "Segmentation": {
                        "Sigma": 2.0,
                        "Min size": 200,
                        "Hole size": 200,
                        "Threshold scale": 1.0,
                    },
                }
            )
        elif value == "Line PSF / split detection":
            self._apply_values(
                {
                    "Loading": {
                        "Convention": "block",
                        "Start frame": 0,
                        "Sum stacks": False,
                    },
                    "Analysis": {
                        "Split detection": True,
                        "Segmentation mode": "line_psf",
                        "Smooth sigma": 1.0,
                        "Use intensity threshold": False,
                    },
                    "Segmentation": {
                        "Sigma": 1.0,
                        "Min size": 5,
                        "Hole size": 5,
                        "Threshold scale": 1.0,
                        "PSF sigma": 1.0,
                    },
                }
            )

    def _apply_values(self, values: dict[str, dict[str, object]]) -> None:
        for group_name, group_values in values.items():
            group = self.p.param(group_name)
            for name, value in group_values.items():
                group.param(name).setValue(value)
