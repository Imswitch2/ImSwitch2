"""Parameter panel of the MoNaLISA lattice reconstructor."""

from __future__ import annotations

import numpy as np
from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets

from ..monalisa.scan_frame import ORIENTATIONS
from .scan import recorded_scan

AUTO = "from the frames"

DEFAULT_PARAMS = {
    "scan_steps_fast": 0,
    "scan_steps_slow": 0,
    "scan_step_nm": 0.0,
    "pixel_size_nm": 0.0,
    "orientation": AUTO,
    "reach_sigma": 2.5,
    "joint_fit": True,
    "background": "constant",
    "frame_gain": True,
    "cell_offsets": True,
    "reassignment": "auto",
    "shift_factor": 0.0,
    "sharpen": False,
    "sharpen_sigma_px": 1.5,
    "sharpen_regularization": 0.1,
    "pinhole_stack": False,
}


class MonalisaLatticeParamsWidget(QtWidgets.QWidget):
    """What is left to choose once the geometry is read from the frames.

    A 0 in the *Scan* group leaves the value to the recording: the steps and
    the step size to its metadata, the pixel size to the lattice (a scan that
    covers the cell once has the step that follows from the cell).

    *Show found foci* draws the foci the pipeline reads from the frames on
    the raw-data viewer, with a line saying where they came from: the sanity
    check before a reconstruction, like the localizer's preview.
    """

    sigPreviewToggled = QtCore.Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        tip_zero = "0: taken from the recording"
        params = [
            {"name": "Recording", "type": "str", "value": "no recording", "readonly": True},
            {"name": "Output pixel", "type": "str", "value": "", "readonly": True},
            {"name": "Scan", "type": "group", "children": [
                {"name": "Steps, fast axis", "type": "int", "limits": (0, 10000),
                 "value": DEFAULT_PARAMS["scan_steps_fast"], "tip": tip_zero},
                {"name": "Steps, slow axis", "type": "int", "limits": (0, 10000),
                 "value": DEFAULT_PARAMS["scan_steps_slow"], "tip": tip_zero},
                {"name": "Step", "type": "float", "suffix": "nm", "limits": (0, 10000),
                 "value": DEFAULT_PARAMS["scan_step_nm"], "tip": tip_zero},
                {"name": "Camera pixel", "type": "float", "suffix": "nm",
                 "limits": (0, 10000), "value": DEFAULT_PARAMS["pixel_size_nm"],
                 "tip": "0: the scan is taken to cover the cell of the lattice once"},
                {"name": "Orientation", "type": "list",
                 "limits": [AUTO, *ORIENTATIONS], "value": DEFAULT_PARAMS["orientation"],
                 "tip": "Fast axis first: -x+y scans x backwards within a line"},
            ]},
            {"name": "Estimator", "type": "group", "children": [
                {"name": "Footprint", "type": "float", "suffix": "spot sigma",
                 "limits": (1.0, 5.0), "step": 0.25, "value": DEFAULT_PARAMS["reach_sigma"],
                 "tip": "Radius of the pixels used around every focus"},
                {"name": "Fit foci jointly", "type": "bool",
                 "value": DEFAULT_PARAMS["joint_fit"],
                 "tip": "Off: every focus as if it were alone (the fast Gauss estimator)"},
                {"name": "Background", "type": "list",
                 "limits": ["constant", "none", "auto"],
                 "value": DEFAULT_PARAMS["background"],
                 "tip": "auto: a haze term where the mean frame calls for one"},
            ]},
            {"name": "Corrections", "type": "group", "children": [
                {"name": "Frame gain", "type": "bool", "value": DEFAULT_PARAMS["frame_gain"],
                 "tip": "Divide out the brightness of the frames over the scan"},
                {"name": "Cell offsets", "type": "bool",
                 "value": DEFAULT_PARAMS["cell_offsets"],
                 "tip": "Take the offset of every focus off, read from the cell borders"},
                {"name": "Reassignment", "type": "list",
                 "limits": ["auto", "off", "fixed"],
                 "value": DEFAULT_PARAMS["reassignment"],
                 "tip": "auto: measure the shift factor and reassign when foci are wide"},
                {"name": "Shift factor", "type": "float", "limits": (0.0, 1.0),
                 "step": 0.05, "value": DEFAULT_PARAMS["shift_factor"],
                 "tip": "Used when Reassignment is fixed"},
            ]},
            {"name": "Outputs", "type": "group", "children": [
                {"name": "Sharpened image", "type": "bool",
                 "value": DEFAULT_PARAMS["sharpen"]},
                {"name": "Sharpening blur", "type": "float", "suffix": "px",
                 "limits": (0.1, 10.0), "step": 0.25,
                 "value": DEFAULT_PARAMS["sharpen_sigma_px"],
                 "tip": "Width of the blur the filter undoes, output pixels"},
                {"name": "Sharpening regularization", "type": "float",
                 "limits": (0.001, 10.0), "step": 0.01,
                 "value": DEFAULT_PARAMS["sharpen_regularization"],
                 "tip": "Smaller sharpens more and amplifies more noise"},
                {"name": "Pinhole stack (sanity check)", "type": "bool",
                 "value": DEFAULT_PARAMS["pinhole_stack"],
                 "tip": "The raw frames as one image per pixel of the footprint"},
            ]},
        ]
        self.p = Parameter.create(name="params", type="group", children=params)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)

        self.previewCheckbox = QtWidgets.QCheckBox("Show found foci")
        self.previewCheckbox.setToolTip(
            "Draw the foci read from the frames on the raw-data viewer, and "
            "say which frame they were read from and how clearly"
        )
        self.previewCheckbox.toggled.connect(self.sigPreviewToggled)
        self.previewStatusLabel = QtWidgets.QLabel("")
        self.previewStatusLabel.setStyleSheet(
            "color: palette(mid); font-size: 9pt; padding: 0px 4px;"
        )
        self.previewStatusLabel.setWordWrap(True)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.tree)
        layout.addWidget(self.previewCheckbox)
        layout.addWidget(self.previewStatusLabel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

    def get_values(self) -> dict:
        scan = self.p.param("Scan")
        estimator = self.p.param("Estimator")
        corrections = self.p.param("Corrections")
        outputs = self.p.param("Outputs")
        return {
            "scan_steps_fast": int(scan.param("Steps, fast axis").value()),
            "scan_steps_slow": int(scan.param("Steps, slow axis").value()),
            "scan_step_nm": float(scan.param("Step").value()),
            "pixel_size_nm": float(scan.param("Camera pixel").value()),
            "orientation": str(scan.param("Orientation").value()),
            "reach_sigma": float(estimator.param("Footprint").value()),
            "joint_fit": bool(estimator.param("Fit foci jointly").value()),
            "background": str(estimator.param("Background").value()),
            "frame_gain": bool(corrections.param("Frame gain").value()),
            "cell_offsets": bool(corrections.param("Cell offsets").value()),
            "reassignment": str(corrections.param("Reassignment").value()),
            "shift_factor": float(corrections.param("Shift factor").value()),
            "sharpen": bool(outputs.param("Sharpened image").value()),
            "sharpen_sigma_px": float(outputs.param("Sharpening blur").value()),
            "sharpen_regularization": float(
                outputs.param("Sharpening regularization").value()
            ),
            "pinhole_stack": bool(outputs.param("Pinhole stack (sanity check)").value()),
        }

    def load_from_attrs(self, attrs: dict, data_obj=None) -> None:
        """Show what the selected recording says about its scan.

        Called by the main view when a recording is selected. It changes no
        parameter: what the user has set stays set.
        """
        try:
            num_frames = int(getattr(data_obj, "numFrames", 0) or 0)
        except Exception:
            num_frames = 0
        if num_frames <= 0:
            self.p.param("Recording").setValue("no recording")
            return
        try:
            text = recorded_scan(attrs or {}, num_frames).describe()
        except ValueError as exc:
            text = str(exc)
        self.p.param("Recording").setValue(text)

    def setPreviewStatus(self, text: str) -> None:
        """Say what the preview found, or why it shows nothing."""
        self.previewStatusLabel.setText(str(text))

    def setOutputPixelSize(self, pixel_size_nm) -> None:
        """Show the pixel of the last reconstruction, in um (called by the manager).

        Takes the size in nm as the result carries it, one number or the
        ``(y_nm, x_nm)`` pair of the MoNaLISA result.
        """
        sizes = np.atleast_1d(np.asarray(pixel_size_nm or 0.0, dtype=float))
        if not sizes.size or not np.all(sizes > 0):
            self.p.param("Output pixel").setValue("")
            return
        if sizes.size > 1 and abs(sizes[0] - sizes[-1]) >= 0.01:
            text = f"Y {sizes[0] / 1000:.4g} / X {sizes[-1] / 1000:.4g} µm"
        else:
            text = f"{sizes[0] / 1000:.4g} µm"
        self.p.param("Output pixel").setValue(text)


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
