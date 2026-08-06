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
    refine_layout,
)
from imswitch.imcommon.model import initLogger
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
            "Cross-correlate every overlapping pair of tiles and solve for\n"
            "all positions at once, instead of trusting the recorded stage\n"
            "positions. Slower than the live preview's one-neighbour pass,\n"
            "but a single bad match cannot displace the rest of the run."
        )
        layout.addWidget(self.refineCheck)

        self.stageCheck = QtWidgets.QCheckBox("Start from stage positions")
        self.stageCheck.setChecked(True)
        self.stageCheck.setToolTip(
            "Lay the tiles out from the stage coordinates the run commanded,\n"
            "rather than the pixel positions it saved. The saved positions\n"
            "include whatever the live 'Align tiles' pass did during\n"
            "acquisition, and a bad live correction cannot be undone here:\n"
            "the tiles are correlated where those positions say they overlap.\n"
            "Uncheck to reassemble exactly the layout that was saved."
        )
        layout.addWidget(self.stageCheck)

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

        detectorRow = QtWidgets.QHBoxLayout()
        detectorRow.addWidget(QtWidgets.QLabel("Detector:"))
        self.detectorCombo = QtWidgets.QComboBox()
        self.detectorCombo.addItem("Aligned on", "")
        self.detectorCombo.setToolTip(
            "Which detector to assemble, for a run that saved several.\n"
            "They were all captured at the same stage positions, so they\n"
            "share one layout — it is solved once and every detector follows.\n"
            "'Aligned on' is the detector the run built its mosaic from."
        )
        detectorRow.addWidget(self.detectorCombo)
        detectorRow.addStretch()
        layout.addLayout(detectorRow)

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
            "stage_positions": self.stageCheck.isChecked(),
            "detector": self.detectorCombo.currentData() or None,
        }

    def setDetectors(self, names) -> None:
        """Offer the detectors a dataset saved, keeping the current choice."""
        previous = self.detectorCombo.currentData()
        self.detectorCombo.blockSignals(True)
        self.detectorCombo.clear()
        self.detectorCombo.addItem("Aligned on", "")
        for name in names or []:
            self.detectorCombo.addItem(name, name)
        index = self.detectorCombo.findData(previous)
        self.detectorCombo.setCurrentIndex(max(0, index))
        self.detectorCombo.blockSignals(False)
        self.detectorCombo.setVisible(bool(names))


class TilingReconstructor(Reconstructor):
    """Stitch a saved tiling dataset back into one image."""

    name = "Tiling mosaic"
    id = "tiling-mosaic"
    file_extensions = ["json", "tiff", "tif", "ome.tiff", "hdf5", "h5", "zarr"]
    description = "Reassemble a saved tiling run into a stitched mosaic"
    default_save_subdir = "mosaic"

    def __init__(self):
        super().__init__()
        self._logger = initLogger(self)

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

        # A hundred multi-megapixel tiles take tens of seconds, and this runs
        # on the calling thread, so every stage reports as it goes: without it
        # a working run is indistinguishable from a frozen one. The refinement
        # summary doubles as the diagnosis when a mosaic still looks wrong —
        # how many links held, how far they disagree, and whether the run fell
        # into groups that could not be tied to each other.
        progress = self._logger.info

        dataset = load_dataset(
            Path(source), progress=progress,
            prefer_stage_positions=params.get("stage_positions", True),
            detector=params.get("detector"),
        )

        moved = 0
        if params.get("refine", True):
            report = refine_layout(
                dataset, params.get("max_shift_px"), progress=progress
            )
            moved = report.moved

        mosaic = assemble(
            dataset, blend=params.get("blend", True), progress=progress
        )

        pixel_y, pixel_x = dataset.pixel_size_um
        axes = dataset.axes
        if mosaic.ndim > 2 and params.get("project", False):
            # Project every leading axis, whatever they are, down to the plane.
            mosaic = mosaic.max(axis=tuple(range(mosaic.ndim - 2)))
            axes = "YX"

        # Labelled from the manifest, not from the rank: a line-step run's
        # leading axis is C, and calling it Z would put a channel spacing in
        # the Z scale and mislabel it for every reader downstream.
        axis_labels = list(axes[-mosaic.ndim:])
        axis_scales = []
        for label in axis_labels:
            if label == "Y":
                axis_scales.append(pixel_y)
            elif label == "X":
                axis_scales.append(pixel_x)
            elif label == "Z":
                axis_scales.append(dataset.z_step_um or 1.0)
            else:
                # C, T or an axis the manifest could not name: index-valued,
                # so a unit spacing is the only honest answer.
                axis_scales.append(1.0)

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
