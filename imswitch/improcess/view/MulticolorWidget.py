"""Interactive multicolor strip registration panel for ImProcess."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.multicolor import (
    alignment_summary,
    apply_alignment,
    apply_alignment_to_result_data,
    extract_alignment,
    extract_calibration_volume,
    load_alignment,
    output_axis_labels,
    parse_x_bounds,
    save_alignment,
)
from imswitch.improcess.analysis.projections import axis_labels_for_shape


class MulticolorWidget(QtWidgets.QWidget):
    """Register and apply three-color X-strip alignment from active image layers."""

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._alignment: dict | None = None

        self.xBoundsEdit = QtWidgets.QLineEdit()
        self.xBoundsEdit.setPlaceholderText("blank = equal thirds, or x0,x1,x2,x3")

        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItems(["maxproj", "volume", "descriptor_3d"])

        self.referenceSpin = QtWidgets.QSpinBox()
        self.referenceSpin.setRange(0, 2)
        self.referenceSpin.setValue(0)

        self.timeSpin = QtWidgets.QSpinBox()
        self.timeSpin.setRange(0, 999999)
        self.timeSpin.setValue(0)

        self.alignmentPathEdit = QtWidgets.QLineEdit()
        self.alignmentPathEdit.setPlaceholderText("alignment .h5")
        self.browseLoadButton = QtWidgets.QPushButton("Browse")
        self.browseSaveButton = QtWidgets.QPushButton("Save As")

        self.beadSigmaSpin = QtWidgets.QDoubleSpinBox()
        self.beadSigmaSpin.setRange(0.01, 100.0)
        self.beadSigmaSpin.setDecimals(3)
        self.beadSigmaSpin.setValue(1.5)

        self.beadMinDistSpin = QtWidgets.QSpinBox()
        self.beadMinDistSpin.setRange(1, 9999)
        self.beadMinDistSpin.setValue(6)

        self.beadThresholdSpin = QtWidgets.QDoubleSpinBox()
        self.beadThresholdSpin.setRange(0.0, 1.0)
        self.beadThresholdSpin.setDecimals(3)
        self.beadThresholdSpin.setSingleStep(0.05)
        self.beadThresholdSpin.setValue(0.5)

        self.matchMaxDistSpin = QtWidgets.QDoubleSpinBox()
        self.matchMaxDistSpin.setRange(0.1, 10000.0)
        self.matchMaxDistSpin.setDecimals(2)
        self.matchMaxDistSpin.setValue(25.0)

        self.ransacIterSpin = QtWidgets.QSpinBox()
        self.ransacIterSpin.setRange(1, 1000000)
        self.ransacIterSpin.setValue(2000)

        self.ransacInlierSpin = QtWidgets.QDoubleSpinBox()
        self.ransacInlierSpin.setRange(0.01, 1000.0)
        self.ransacInlierSpin.setDecimals(2)
        self.ransacInlierSpin.setValue(3.0)

        self.registerButton = QtWidgets.QPushButton("Register")
        self.loadButton = QtWidgets.QPushButton("Load")
        self.applyButton = QtWidgets.QPushButton("Apply")
        self.saveCurrentButton = QtWidgets.QPushButton("Save Current")

        self.summaryLabel = QtWidgets.QLabel("Run registration or load an alignment.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        file_row = QtWidgets.QHBoxLayout()
        file_row.addWidget(self.alignmentPathEdit, 1)
        file_row.addWidget(self.browseLoadButton)
        file_row.addWidget(self.browseSaveButton)

        form = QtWidgets.QFormLayout()
        form.addRow("X bounds", self.xBoundsEdit)
        form.addRow("Mode", self.modeCombo)
        form.addRow("Reference", self.referenceSpin)
        form.addRow("Timepoint", self.timeSpin)
        form.addRow("Alignment", file_row)
        form.addRow("Bead sigma", self.beadSigmaSpin)
        form.addRow("Bead min dist", self.beadMinDistSpin)
        form.addRow("Bead threshold", self.beadThresholdSpin)
        form.addRow("Match max dist", self.matchMaxDistSpin)
        form.addRow("RANSAC iter", self.ransacIterSpin)
        form.addRow("RANSAC inlier px", self.ransacInlierSpin)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.registerButton)
        buttons.addWidget(self.loadButton)
        buttons.addWidget(self.applyButton)
        buttons.addWidget(self.saveCurrentButton)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(form)
        layout.addLayout(buttons)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.registerButton.clicked.connect(self.register)
        self.loadButton.clicked.connect(self.load_alignment_file)
        self.applyButton.clicked.connect(self.apply)
        self.saveCurrentButton.clicked.connect(self.save_current_alignment)
        self.browseLoadButton.clicked.connect(self.browse_load_path)
        self.browseSaveButton.clicked.connect(self.browse_save_path)

    def register(self) -> None:
        layer = self._active_image_layer()
        if layer is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        try:
            data = np.asarray(layer.data)
            labels = self._axis_labels(layer, data)
            volume = extract_calibration_volume(data, labels, self.timeSpin.value())
            x_bounds = parse_x_bounds(self.xBoundsEdit.text(), volume.shape[-1])
            alignment = extract_alignment(
                volume,
                x_bounds=x_bounds,
                mode=self.modeCombo.currentText(),
                reference_channel=self.referenceSpin.value(),
                bead_sigma=self.beadSigmaSpin.value(),
                bead_min_dist=self.beadMinDistSpin.value(),
                bead_thr_rel=self.beadThresholdSpin.value(),
                match_max_dist=self.matchMaxDistSpin.value(),
                ransac_n_iter=self.ransacIterSpin.value(),
                ransac_inlier_px=self.ransacInlierSpin.value(),
            )
            self._alignment = alignment
            preview = apply_alignment(volume, alignment)
            self._viewer.add_image(
                preview,
                name=f"{getattr(layer, 'name', 'image')} multicolor registration",
                metadata={
                    "axis_labels": ["C", "Z", "Y", "X"],
                    "multicolor_alignment": alignment_summary(alignment),
                },
            )
            path = self.alignmentPathEdit.text().strip()
            if path:
                save_alignment(alignment, Path(path))
            self.summaryLabel.setText(alignment_summary(alignment))
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def apply(self) -> None:
        layer = self._active_image_layer()
        if layer is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        try:
            alignment = self._alignment_or_loaded()
            data = np.asarray(layer.data)
            labels = self._axis_labels(layer, data)
            aligned = apply_alignment_to_result_data(data, labels, alignment)
            out_labels = output_axis_labels(labels)
            self._viewer.add_image(
                aligned,
                name=f"{getattr(layer, 'name', 'image')} multicolor aligned",
                metadata={
                    "axis_labels": out_labels,
                    "multicolor_alignment": alignment_summary(alignment),
                },
            )
            self.summaryLabel.setText(
                f"Applied {alignment_summary(alignment)}: {data.shape} -> {aligned.shape}"
            )
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def browse_load_path(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load Multicolor Alignment",
            "",
            "HDF5 files (*.h5 *.hdf5 *.hdf)",
        )
        if path:
            self.alignmentPathEdit.setText(path)
            self.load_alignment_file()

    def browse_save_path(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Multicolor Alignment",
            "",
            "HDF5 files (*.h5 *.hdf5 *.hdf)",
        )
        if path:
            self.alignmentPathEdit.setText(path)

    def load_alignment_file(self) -> None:
        try:
            path = self.alignmentPathEdit.text().strip()
            if not path:
                self.summaryLabel.setText("Choose an alignment file.")
                return
            self._alignment = load_alignment(Path(path))
            self.summaryLabel.setText(alignment_summary(self._alignment))
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def save_current_alignment(self) -> None:
        if self._alignment is None:
            self.summaryLabel.setText("No alignment has been registered or loaded.")
            return
        try:
            path = self.alignmentPathEdit.text().strip()
            if not path:
                self.browse_save_path()
                path = self.alignmentPathEdit.text().strip()
            if path:
                save_alignment(self._alignment, Path(path))
                self.summaryLabel.setText(f"Saved {alignment_summary(self._alignment)}")
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def _alignment_or_loaded(self) -> dict:
        if self._alignment is not None:
            return self._alignment
        path = self.alignmentPathEdit.text().strip()
        if not path:
            raise ValueError("Register or load an alignment first.")
        self._alignment = load_alignment(Path(path))
        return self._alignment

    def _axis_labels(self, layer, data: np.ndarray) -> list[str]:
        try:
            labels = list(layer.metadata.get("axis_labels", []))
        except Exception:
            labels = []
        return axis_labels_for_shape(data, labels)

    def _active_image_layer(self):
        try:
            active = self._viewer.layers.selection.active
        except Exception:
            active = None
        if self._is_image_layer(active):
            return active
        for layer in self._viewer.layers:
            if self._is_image_layer(layer):
                return layer
        return None

    @staticmethod
    def _is_image_layer(layer) -> bool:
        return (
            layer is not None
            and hasattr(layer, "data")
            and isinstance(layer.data, np.ndarray)
            and layer.data.ndim >= 3
            and getattr(layer, "visible", True)
            and not str(getattr(layer, "name", "")).startswith("_")
        )
