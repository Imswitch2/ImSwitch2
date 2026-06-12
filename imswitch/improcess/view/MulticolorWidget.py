"""Interactive multicolor strip registration panel for ImProcess."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.multicolor import (
    alignment_summary,
    apply_alignment,
    apply_alignment_to_result_data,
    beads_to_global,
    detect_beads,
    extract_alignment,
    extract_calibration_volume,
    load_alignment,
    output_axis_labels,
    parse_bounds,
    save_alignment,
    split_axis_index,
    transform_details,
)
from imswitch.improcess.analysis.projections import axis_labels_for_shape


class MulticolorWidget(QtWidgets.QWidget):
    """Register and apply multicolor slice alignment from active image layers."""

    _SPLIT_PREVIEW_LAYER = "multicolor split preview"
    _BEAD_PREVIEW_LAYER = "multicolor beads"
    _CHANNEL_COLORS = ("cyan", "magenta", "yellow", "lime", "orange", "red")

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._alignment: dict | None = None
        self._bead_state: dict | None = None

        self.slicesSpin = QtWidgets.QSpinBox()
        self.slicesSpin.setRange(2, 16)
        self.slicesSpin.setValue(3)
        self.slicesSpin.setToolTip("How many channel slices the dataset is split into")

        self.axisCombo = QtWidgets.QComboBox()
        self.axisCombo.addItems(["X", "Y", "Z"])
        self.axisCombo.setToolTip("Axis along which the dataset is split")

        self.boundsEdit = QtWidgets.QLineEdit()
        self.boundsEdit.setPlaceholderText("blank = equal slices, or b0,b1,...,bN")

        self.previewSplitCheck = QtWidgets.QCheckBox("Show split preview")
        self.previewSplitCheck.setChecked(False)
        self.previewSplitCheck.setToolTip(
            "Overlay the split boundaries on the active image layer"
        )

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

        self.findBeadsButton = QtWidgets.QPushButton("Find Beads")
        self.findBeadsButton.setToolTip(
            "Step 1 (descriptor_3d): detect beads per channel and mark them in the viewer"
        )
        self.registerButton = QtWidgets.QPushButton("Register")
        self.registerButton.setToolTip(
            "Step 2: compute the alignment (reuses found beads when parameters match)"
        )
        self.loadButton = QtWidgets.QPushButton("Load")
        self.applyButton = QtWidgets.QPushButton("Apply")
        self.saveCurrentButton = QtWidgets.QPushButton("Save Current")

        self.summaryLabel = QtWidgets.QLabel("Run registration or load an alignment.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        self.resultsText = QtWidgets.QPlainTextEdit()
        self.resultsText.setReadOnly(True)
        self.resultsText.setMaximumHeight(150)
        self.resultsText.setPlaceholderText(
            "Bead counts and final transform values appear here."
        )

        file_row = QtWidgets.QHBoxLayout()
        file_row.addWidget(self.alignmentPathEdit, 1)
        file_row.addWidget(self.browseLoadButton)
        file_row.addWidget(self.browseSaveButton)

        split_row = QtWidgets.QHBoxLayout()
        split_row.addWidget(self.slicesSpin)
        split_row.addWidget(QtWidgets.QLabel("along"))
        split_row.addWidget(self.axisCombo)
        split_row.addStretch()

        form = QtWidgets.QFormLayout()
        form.addRow("Slices", split_row)
        form.addRow("Bounds", self.boundsEdit)
        form.addRow("", self.previewSplitCheck)
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
        buttons.addWidget(self.findBeadsButton)
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
        layout.addWidget(self.resultsText)
        self.setLayout(layout)

        self.findBeadsButton.clicked.connect(self.find_beads)
        self.registerButton.clicked.connect(self.register)
        self.loadButton.clicked.connect(self.load_alignment_file)
        self.applyButton.clicked.connect(self.apply)
        self.saveCurrentButton.clicked.connect(self.save_current_alignment)
        self.browseLoadButton.clicked.connect(self.browse_load_path)
        self.browseSaveButton.clicked.connect(self.browse_save_path)

        self.previewSplitCheck.toggled.connect(self._update_split_preview)
        self.slicesSpin.valueChanged.connect(self._on_slices_changed)
        self.axisCombo.currentTextChanged.connect(self._refresh_split_preview)
        self.boundsEdit.editingFinished.connect(self._refresh_split_preview)
        self.timeSpin.valueChanged.connect(self._refresh_split_preview)

    def find_beads(self) -> None:
        """Step 1 of the two-step workflow: detect beads and mark them."""
        try:
            layer, volume = self._current_layer_volume()
            bounds, axis = self._split_params(volume)
            sigma = self.beadSigmaSpin.value()
            min_dist = self.beadMinDistSpin.value()
            threshold = self.beadThresholdSpin.value()
            coords = detect_beads(
                volume,
                bounds,
                axis=axis,
                sigma=sigma,
                min_distance=min_dist,
                threshold_rel=threshold,
            )
        except Exception as exc:
            self.summaryLabel.setText(str(exc))
            return

        self._bead_state = {
            "coords": coords,
            "bounds": list(bounds),
            "axis": axis,
            "detect_params": (float(sigma), int(min_dist), float(threshold)),
            "time_index": self.timeSpin.value(),
            "layer_name": getattr(layer, "name", None),
        }
        self._show_bead_preview(coords, bounds, axis, min_dist)

        counts = [len(channel_coords) for channel_coords in coords]
        per_channel = ", ".join(f"ch{i}={n}" for i, n in enumerate(counts))
        self.resultsText.setPlainText(
            f"Beads found: {sum(counts)} total ({per_channel})\n"
            "Run Register to compute the alignment from these beads."
        )
        self.summaryLabel.setText(
            f"Found {sum(counts)} bead(s) across {len(counts)} channel(s)."
        )

    def register(self) -> None:
        layer = self._active_image_layer()
        if layer is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        try:
            layer, volume = self._current_layer_volume()
            bounds, axis = self._split_params(volume)
            mode = self.modeCombo.currentText()
            bead_coords = (
                self._reusable_bead_coords(layer, bounds, axis)
                if mode == "descriptor_3d"
                else None
            )
            alignment = extract_alignment(
                volume,
                bounds,
                mode=mode,
                reference_channel=self.referenceSpin.value(),
                split_axis=axis,
                bead_sigma=self.beadSigmaSpin.value(),
                bead_min_dist=self.beadMinDistSpin.value(),
                bead_thr_rel=self.beadThresholdSpin.value(),
                match_max_dist=self.matchMaxDistSpin.value(),
                ransac_n_iter=self.ransacIterSpin.value(),
                ransac_inlier_px=self.ransacInlierSpin.value(),
                bead_coords=bead_coords,
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
            summary = alignment_summary(alignment)
            if bead_coords is not None:
                summary = f"{summary}  (reused found beads)"
            self.summaryLabel.setText(summary)
            self.resultsText.setPlainText(transform_details(alignment))
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
            self.resultsText.setPlainText(transform_details(self._alignment))
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

    def _on_slices_changed(self, n_slices: int) -> None:
        self.referenceSpin.setRange(0, max(0, int(n_slices) - 1))
        self._refresh_split_preview()

    def _refresh_split_preview(self, *_args) -> None:
        if self.previewSplitCheck.isChecked():
            self._update_split_preview()

    def _update_split_preview(self, *_args) -> None:
        self._remove_layer(self._SPLIT_PREVIEW_LAYER)
        if not self.previewSplitCheck.isChecked():
            return
        try:
            _layer, volume = self._current_layer_volume()
            bounds, axis = self._split_params(volume)
        except Exception as exc:
            self.summaryLabel.setText(str(exc))
            return
        if axis == "Z":
            self.summaryLabel.setText(
                f"Split along Z at z={bounds}: no in-plane preview in the YX view; "
                "step the Z slider to the boundary planes to inspect them."
            )
            return
        n_slices = len(bounds) - 1
        roi_width = min(bounds[i + 1] - bounds[i] for i in range(n_slices))
        _z_size, y_size, x_size = volume.shape
        rectangles = []
        colors = []
        for i in range(n_slices):
            start, stop = bounds[i], bounds[i] + roi_width
            if axis == "X":
                corners = [[0, start], [0, stop], [y_size, stop], [y_size, start]]
            else:  # Y
                corners = [[start, 0], [stop, 0], [stop, x_size], [start, x_size]]
            rectangles.append(np.asarray(corners, dtype=float))
            colors.append(self._CHANNEL_COLORS[i % len(self._CHANNEL_COLORS)])
        try:
            self._viewer.add_shapes(
                rectangles,
                shape_type="rectangle",
                name=self._SPLIT_PREVIEW_LAYER,
                edge_color=colors,
                face_color="transparent",
                edge_width=2,
                opacity=0.9,
            )
            self.summaryLabel.setText(
                f"Split preview: {n_slices} slice(s) along {axis}, "
                f"bounds={bounds}, width={roi_width}."
            )
        except Exception as exc:
            self.summaryLabel.setText(f"Could not draw split preview: {exc}")

    def _show_bead_preview(self, coords, bounds, axis, min_dist) -> None:
        self._remove_layer(self._BEAD_PREVIEW_LAYER)
        points = []
        colors = []
        for channel, channel_coords in enumerate(beads_to_global(coords, bounds, axis)):
            color = self._CHANNEL_COLORS[channel % len(self._CHANNEL_COLORS)]
            for point in channel_coords:
                points.append(point)
                colors.append(color)
        if not points:
            return
        kwargs = dict(
            name=self._BEAD_PREVIEW_LAYER,
            symbol="square",
            size=max(4, 2 * int(min_dist)),
            face_color="transparent",
            opacity=0.9,
        )
        try:
            self._viewer.add_points(
                np.asarray(points),
                border_color=colors,
                out_of_slice_display=True,
                **kwargs,
            )
        except TypeError:
            # Older napari: Points borders are 'edge_color'.
            try:
                self._viewer.add_points(np.asarray(points), edge_color=colors, **kwargs)
            except Exception as exc:
                self.summaryLabel.setText(f"Could not draw bead preview: {exc}")
        except Exception as exc:
            self.summaryLabel.setText(f"Could not draw bead preview: {exc}")

    def _reusable_bead_coords(self, layer, bounds, axis):
        """Return cached Find Beads coordinates if they match the current setup."""
        state = self._bead_state
        if not state:
            return None
        detect_params = (
            float(self.beadSigmaSpin.value()),
            int(self.beadMinDistSpin.value()),
            float(self.beadThresholdSpin.value()),
        )
        if (
            state["bounds"] == list(bounds)
            and state["axis"] == axis
            and state["layer_name"] == getattr(layer, "name", None)
            and state["time_index"] == self.timeSpin.value()
            and state["detect_params"] == detect_params
        ):
            return state["coords"]
        return None

    def _current_layer_volume(self):
        layer = self._active_image_layer()
        if layer is None:
            raise ValueError("No image layer selected.")
        data = np.asarray(layer.data)
        labels = self._axis_labels(layer, data)
        volume = extract_calibration_volume(data, labels, self.timeSpin.value())
        return layer, volume

    def _split_params(self, volume: np.ndarray):
        axis = self.axisCombo.currentText()
        size = volume.shape[split_axis_index(axis)]
        bounds = parse_bounds(self.boundsEdit.text(), size, self.slicesSpin.value())
        return bounds, axis

    def _remove_layer(self, name: str) -> None:
        try:
            if name in self._viewer.layers:
                self._viewer.layers.remove(name)
        except Exception:
            pass

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
