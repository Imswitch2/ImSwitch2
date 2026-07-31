"""Reassemble a saved tiling dataset into one mosaic.

A tiling run writes a folder of OME images, each carrying its own stage
position, plus a ``tiles.json`` manifest and a ``TileConfiguration.txt``. This
reconstructor reads that folder back and stitches it — with a more careful
alignment pass than the live preview can afford, and without discarding the Z
planes of a 3D scan.

Open any file from the dataset folder (a tile, the mosaic, or the manifest);
the reconstructor finds the manifest beside it.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import tifffile as tiff
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.tile_mosaic import (
    assemble,
    load_dataset,
    refine_positions,
)
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import Reconstructor

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class TilingMosaicResult(ProcessingResult):
    """A stitched mosaic, 2-D or volumetric."""

    def save(self, path: Path, fmt: str = "tiff") -> None:
        path = Path(path)
        if fmt != "tiff":
            raise ValueError(
                f'TilingMosaicResult only supports fmt="tiff", got "{fmt}"'
            )
        data = np.asarray(self.data)
        metadata = {"axes": "".join(self.axis_labels)}
        scales = self.axis_scales or []
        by_label = dict(zip(self.axis_labels, scales))
        for label, key in (("X", "PhysicalSizeX"), ("Y", "PhysicalSizeY"),
                           ("Z", "PhysicalSizeZ")):
            if label in by_label:
                metadata[key] = float(by_label[label])
                metadata[f"{key}Unit"] = "µm"
        tiff.imwrite(str(path), data, ome=True, metadata=metadata)


class _TilingParamsWidget(QtWidgets.QWidget):
    """Assembly options for a saved tile dataset."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)

        layout.addWidget(QtWidgets.QLabel(
            "Reassembles a tiling dataset.\n"
            "Open any file from the run's folder."
        ))

        self.refineCheck = QtWidgets.QCheckBox("Refine alignment")
        self.refineCheck.setChecked(True)
        self.refineCheck.setToolTip(
            "Cross-correlate each tile against its neighbours instead of\n"
            "trusting the recorded stage positions. Slower, but recovers\n"
            "stage error the live preview could not."
        )
        layout.addWidget(self.refineCheck)

        self.blendCheck = QtWidgets.QCheckBox("Mean overlaps")
        self.blendCheck.setChecked(True)
        self.blendCheck.setToolTip(
            "Average overlapping pixels. Unchecked, later tiles overwrite\n"
            "earlier ones, which leaves visible seams but no ghosting."
        )
        layout.addWidget(self.blendCheck)

        shiftRow = QtWidgets.QHBoxLayout()
        shiftRow.addWidget(QtWidgets.QLabel("Max shift (px):"))
        self.maxShiftSpin = QtWidgets.QDoubleSpinBox()
        self.maxShiftSpin.setRange(0.0, 100000.0)
        self.maxShiftSpin.setDecimals(0)
        self.maxShiftSpin.setValue(0.0)
        self.maxShiftSpin.setSpecialValueText("auto")
        self.maxShiftSpin.setToolTip(
            "Reject corrections larger than this, which are usually false\n"
            "matches on repeating structure. 0 derives a limit from the\n"
            "tile size."
        )
        shiftRow.addWidget(self.maxShiftSpin)
        shiftRow.addStretch()
        layout.addLayout(shiftRow)

        self.projectCheck = QtWidgets.QCheckBox("Project volumes to 2D")
        self.projectCheck.setToolTip(
            "Maximum-project a volumetric mosaic instead of keeping every\n"
            "plane. Useful for a quick look at a large 3D run."
        )
        layout.addWidget(self.projectCheck)

        layout.addStretch()

    def get_values(self) -> dict:
        maxShift = self.maxShiftSpin.value()
        return {
            "refine": self.refineCheck.isChecked(),
            "blend": self.blendCheck.isChecked(),
            "max_shift_px": maxShift if maxShift > 0 else None,
            "project": self.projectCheck.isChecked(),
        }


class TilingReconstructor(Reconstructor):
    """Stitch a saved tiling dataset back into one image."""

    name = "Tiling mosaic"
    id = "tiling-mosaic"
    file_extensions = ["json", "tiff", "tif", "ome.tiff", "hdf5", "h5", "zarr"]
    description = "Reassemble a saved tiling run into a stitched mosaic"
    default_save_subdir = "mosaic"

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return _TilingParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget):
        # The geometry is in the dataset's own manifest; nothing to ask for.
        return None

    def process(self, data_obj: "DataObj", params: dict) -> TilingMosaicResult:
        source = getattr(data_obj, "dataPath", None) or getattr(
            data_obj, "name", None
        )
        if not source:
            raise ValueError(
                "The tiling reconstructor needs a file on disk: open a file "
                "from the tiling run's folder."
            )

        dataset = load_dataset(Path(source))

        moved = 0
        if params.get("refine", True):
            moved = refine_positions(dataset, params.get("max_shift_px"))

        mosaic = assemble(dataset, blend=params.get("blend", True))

        pixel_y, pixel_x = dataset.pixel_size_um
        if mosaic.ndim == 3 and params.get("project", False):
            mosaic = mosaic.max(axis=0)

        if mosaic.ndim == 3:
            axis_labels = ["Z", "Y", "X"]
            axis_scales = [dataset.z_step_um or 1.0, pixel_y, pixel_x]
        else:
            axis_labels = ["Y", "X"]
            axis_scales = [pixel_y, pixel_x]

        name = f"{Path(source).parent.name} mosaic"
        if moved:
            name = f"{name} (refined {moved}/{len(dataset.tiles)})"

        return TilingMosaicResult(
            name=name,
            data=mosaic,
            axis_labels=axis_labels,
            view_modes=[ViewMode("Standard", tuple(range(mosaic.ndim)))],
            axis_scales=axis_scales,
            scale_unit="µm",
        )
