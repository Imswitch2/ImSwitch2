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
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.tile_mosaic import (
    LayoutOptions,
    MosaicLayout,
    PayloadAssemblyOptions,
    PayloadEstimate,
    PayloadProvenance,
    PayloadSelection,
    SkippedPayload,
    TilingDatasetIndex,
    apply_layout,
    assemble,
    assemble_payload,
    estimate_payload_selection,
    inspect_dataset,
    load_dataset,
    manifest_fingerprint,
    refine_layout,
    solve_layout,
)
from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.core_dims import WHOLE_INPUT
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.reconstructors.base import (
    Reconstructor,
    ReconstructionContext,
    ResourceEstimate,
    SourceChoice,
    SourceInspection,
)

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


TIME_ALIGNMENTS = ('stage', 'shared', 'shared+drift', 'independent')
TIME_ALIGNMENT_HELP = (
    "How the tiles of a timelapse of tilings are placed across time.\n\n"
    "Stage positions only: the positions the manifests record, no registration; every\n"
    "timepoint has identical geometry.\n"
    "One shared layout (default): tile-to-tile offsets are estimated from all timepoints\n"
    "together (their correlations are added before the peak is found, so a faint sample\n"
    "that no single timepoint can align is aligned by the series) and the same layout\n"
    "is used for every timepoint.\n"
    "Shared layout + drift: the shared layout plus one rigid XY shift per timepoint\n"
    "(the sample moving between timepoints), measured against the reference timepoint.\n"
    "Each timepoint on its own: today's registration for every timepoint separately;\n"
    "tiles may jitter between timepoints. They are still placed on one common canvas.\n\n"
    "Every mode gives all timepoints the same canvas: nothing needs cropping or shifting."
)


class TilingMosaicResult(ProcessingResult):
    """A stitched mosaic, 2-D or volumetric."""

    #: A timelapse's per-timepoint drift is a small table in the Results list.
    publishes_table_rows = True

    def __init__(self, *args, provenance: PayloadProvenance | None = None,
                 output_origin_yx=(0, 0), manifest=None,
                 timelapse: dict | None = None, drift_yx=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.provenance = provenance
        #: For a timelapse: the record of what was used (manifests with
        #: fingerprints, alignment mode, layout, drift), JSON-safe.
        self.timelapse = timelapse
        if timelapse is not None:
            # A stack along T goes through the shared writer in every container.
            self.supported_formats = ("tiff", "hdf5", "zarr")
        #: ``(T, 2)`` drift correction (row, col pixels) per plane, or ``None``.
        self.drift_yx = None if drift_yx is None else np.asarray(drift_yx, dtype=float)
        self.output_origin_yx = tuple(output_origin_yx)
        #: The run's ``tiles.json``, so a processor can go back to the tiles
        #: (and their stage positions) the mosaic was assembled from.
        if manifest is None:
            manifest = getattr(provenance, "manifest", None)
        self.manifest = str(manifest) if manifest is not None else None


    supported_formats = ("tiff",)

    def write_files(self, plan, document) -> None:
        if plan.fmt not in self.supported_formats:
            raise ValueError(
                f'TilingMosaicResult only supports fmt="tiff", got "{plan.fmt}"'
            )
        from imswitch.improcess.model.result_io import save_image_result

        # The mosaic's own provenance summary keeps its top-level keys in the
        # OME Description, as every mosaic written so far has had.
        extra = dict(self.provenance_summary() or {})
        if self.timelapse is not None:
            extra["timelapse"] = self.timelapse
        save_image_result(self, plan.primary, plan.fmt, extra=extra, document=document)

    def table_columns(self) -> list[str]:
        if self.timelapse is None:
            return []
        return ["timepoint", "time_s", "drift_y_px", "drift_x_px", "note"]

    def table_records(self) -> list[dict]:
        """One row per plane: its time and the drift correction applied to it."""
        if self.timelapse is None:
            return []
        planes = self.timelapse.get("planes", [])
        rows = []
        for number, plane in enumerate(planes):
            drift = (self.drift_yx[number] if self.drift_yx is not None and number < len(self.drift_yx)
                     else (None, None))
            rows.append({
                "timepoint": plane.get("index"), "time_s": plane.get("time_s"),
                "drift_y_px": None if drift[0] is None else round(float(drift[0]), 3),
                "drift_x_px": None if drift[1] is None else round(float(drift[1]), 3),
                "note": plane.get("note") or "",
            })
        return rows

    def plot_payloads(self) -> list:
        """The drift trace, as drift correction's own result draws it."""
        if self.drift_yx is None or not len(self.drift_yx):
            return []
        from imswitch.improcess.model.plotting import PlotPayload, PlotSeries

        planes = self.timelapse.get("planes", []) if self.timelapse else []
        x = np.asarray([p.get("index", n) for n, p in enumerate(planes)] or np.arange(len(self.drift_yx)))
        return [PlotPayload(
            title="Drift between timepoints", x_label="Timepoint", y_label="Correction (px)",
            series=[
                PlotSeries(name="Y shift", x=x, y=self.drift_yx[:, 0], kind="line"),
                PlotSeries(name="X shift", x=x, y=self.drift_yx[:, 1], kind="line"),
            ],
        )]

    def provenance_summary(self) -> dict | None:
        """Return a serializable record suitable for save metadata and UI."""
        provenance = self.provenance
        if provenance is None:
            return None

        def serializable(value):
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, dict):
                return {str(key): serializable(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [serializable(item) for item in value]
            if isinstance(value, np.generic):
                return value.item()
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            return repr(value)

        report = provenance.refinement_report
        return {
            "kind": (
                "imswitch-tiling-mosaic-provenance/1" if self.timelapse is None
                else "imswitch-tiling-timelapse-provenance/1"
            ),
            "manifest": str(provenance.manifest),
            "detector": provenance.detector,
            "channel": provenance.channel,
            "z_projection": provenance.z_projection,
            "skipped": [
                {"tile_id": item.tile_id, "reason": item.reason}
                for item in provenance.skipped
            ],
            "layout_cache_key": serializable(provenance.layout_cache_key),
            "layout": {
                "placement_path": provenance.placement_path,
                "transform_source": provenance.transform_source,
                "transform": serializable(dict(provenance.transform)),
                "output_origin_yx": list(self.output_origin_yx),
                "refinement": {
                    "summary": report.summary(),
                    "moved": report.moved,
                    "tiles": report.tiles,
                    "components": list(report.components),
                    "accepted_links": len(report.accepted),
                    "total_links": len(report.links),
                },
            },
        }


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

        self.shadingCheck = QtWidgets.QCheckBox("Correct vignetting")
        self.shadingCheck.setChecked(False)
        self.shadingCheck.setToolTip(
            "Estimate the illumination profile from the run itself and divide\n"
            "it out of every tile.\n\n"
            "Shading is fixed to the detector — the same corner is dim in\n"
            "every tile — while the sample is not, so averaging the tiles in\n"
            "detector coordinates lets the specimen cancel and leaves the\n"
            "illumination. No shape is assumed, so one-sided falloff is\n"
            "handled as readily as a radial one.\n\n"
            "Needs enough tiles for the sample to average out, and is skipped\n"
            "with a reason in the log when the estimate cannot be trusted.\n"
            "Costs one extra read pass over the tiles."
        )
        layout.addWidget(self.shadingCheck)

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
        detectorRow.addWidget(QtWidgets.QLabel("Output source:"))
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

        channelRow = QtWidgets.QHBoxLayout()
        self.channelLabel = QtWidgets.QLabel("Channel:")
        channelRow.addWidget(self.channelLabel)
        self.channelCombo = QtWidgets.QComboBox()
        channelRow.addWidget(self.channelCombo)
        channelRow.addStretch()
        layout.addLayout(channelRow)

        self.projectCheck = QtWidgets.QCheckBox("Project volumes to 2D")
        self.projectCheck.setToolTip(
            "Maximum-project a volumetric mosaic instead of keeping every\n"
            "plane. Useful for a quick look at a large 3D run."
        )
        layout.addWidget(self.projectCheck)

        # A timelapse of tilings. Which timepoints (this one, all, a range) is
        # the panel's own Series box; what is specific to tiling is how the
        # tiles are aligned across time.
        self.timelapseBox = QtWidgets.QGroupBox("Timelapse")
        form = QtWidgets.QFormLayout(self.timelapseBox)
        self.alignmentCombo = QtWidgets.QComboBox()
        for label, value in (
            ("Stage positions only", "stage"), ("One shared layout", "shared"),
            ("Shared layout + drift", "shared+drift"), ("Each timepoint on its own", "independent"),
        ):
            self.alignmentCombo.addItem(label, value)
        self.alignmentCombo.setCurrentIndex(self.alignmentCombo.findData("shared"))
        self.alignmentCombo.setToolTip(TIME_ALIGNMENT_HELP)
        form.addRow("Align across time:", self.alignmentCombo)
        self.layoutSourceCombo = QtWidgets.QComboBox()
        self.layoutSourceCombo.addItem("All timepoints", "all")
        self.layoutSourceCombo.addItem("Reference timepoint only", "reference")
        self.layoutSourceCombo.setToolTip(
            "Which timepoints the shared layout is estimated from. All adds their\n"
            "correlations; the reference alone is a single-timepoint registration."
        )
        form.addRow("Layout from:", self.layoutSourceCombo)
        self.referenceSpin = QtWidgets.QSpinBox()
        self.referenceSpin.setRange(-1, 100000)
        self.referenceSpin.setValue(-1)
        self.referenceSpin.setSpecialValueText("first")
        self.referenceSpin.setToolTip("The timepoint drift is measured against.")
        form.addRow("Reference timepoint:", self.referenceSpin)
        self.driftSmoothSpin = QtWidgets.QSpinBox()
        self.driftSmoothSpin.setRange(0, 99)
        self.driftSmoothSpin.setSpecialValueText("off")
        self.driftSmoothSpin.setSuffix(" timepoints")
        self.driftSmoothSpin.setToolTip("Running median over this many timepoints before drift is applied.")
        form.addRow("Smooth drift:", self.driftSmoothSpin)
        self.maxDriftSpin = QtWidgets.QDoubleSpinBox()
        self.maxDriftSpin.setRange(0.0, 100000.0)
        self.maxDriftSpin.setDecimals(0)
        self.maxDriftSpin.setSpecialValueText("auto")
        self.maxDriftSpin.setToolTip("Reject drift estimates larger than this (pixels).")
        form.addRow("Max drift (px):", self.maxDriftSpin)
        layout.addWidget(self.timelapseBox)
        # Only meaningful for a source that is part of a series; set_source_inspection shows it.
        self.timelapseBox.setVisible(False)
        self.alignmentCombo.currentIndexChanged.connect(self._refreshTimelapseEnabled)
        self._refreshTimelapseEnabled()

        self.sourceStatus = QtWidgets.QLabel()
        self.sourceStatus.setWordWrap(True)
        layout.addWidget(self.sourceStatus)

        self._outputMetadata = {}
        self.detectorCombo.currentIndexChanged.connect(
            self._refreshSelectionMetadata
        )
        self.channelCombo.currentIndexChanged.connect(self._refreshEstimate)
        self.projectCheck.toggled.connect(self._refreshEstimate)

        layout.addStretch()

    def get_values(self) -> dict:
        maxShift = self.maxShiftSpin.value()
        return {
            "refine": self.refineCheck.isChecked(),
            "blend": self.blendCheck.isChecked(),
            "shading_correction": self.shadingCheck.isChecked(),
            "max_shift_px": maxShift if maxShift > 0 else None,
            "project": self.projectCheck.isChecked(),
            "stage_positions": self.stageCheck.isChecked(),
            "detector": self.detectorCombo.currentData() or None,
            "alignment_diagnostic": (
                self.detectorCombo.currentData() == "__alignment__"
            ),
            "channel": self.channelCombo.currentData(),
            "project_z": self.projectCheck.isChecked(),
            "time_alignment": self.alignmentCombo.currentData(),
            "layout_source": self.layoutSourceCombo.currentData(),
            "reference_timepoint": (
                self.referenceSpin.value() if self.referenceSpin.value() >= 0 else None
            ),
            "drift_smoothing": self.driftSmoothSpin.value(),
            "max_drift_px": (
                self.maxDriftSpin.value() if self.maxDriftSpin.value() > 0 else None
            ),
        }

    def _refreshTimelapseEnabled(self, *_args) -> None:
        mode = self.alignmentCombo.currentData()
        self.layoutSourceCombo.setEnabled(mode in ("shared", "shared+drift"))
        drift = mode == "shared+drift"
        for control in (self.driftSmoothSpin, self.maxDriftSpin):
            control.setEnabled(drift)
        self.referenceSpin.setEnabled(mode in ("shared", "shared+drift"))

    @staticmethod
    def _estimateKey(channel, projection) -> str:
        channel_key = "all" if channel is None else str(int(channel))
        return f"{channel_key}:{projection}"

    @staticmethod
    def _formatBytes(value) -> str:
        value = int(value or 0)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024

    def set_source_inspection(self, inspection: SourceInspection | None) -> None:
        previous = self.detectorCombo.currentData()
        self.detectorCombo.blockSignals(True)
        self.detectorCombo.clear()
        self._outputMetadata = {}
        choices = inspection.choices.get("output", ()) if inspection else ()
        default_index = 0
        for position, choice in enumerate(choices):
            self.detectorCombo.addItem(choice.label, choice.value)
            self._outputMetadata[choice.value] = dict(choice.metadata)
            if choice.metadata.get("default"):
                default_index = position
        previous_index = self.detectorCombo.findData(previous)
        self.detectorCombo.setCurrentIndex(
            previous_index if previous_index >= 0 else default_index
        )
        self.detectorCombo.blockSignals(False)

        metadata = inspection.metadata if inspection else {}
        available = bool(metadata.get("refinement_available", False))
        was_source_disabled = not self.refineCheck.isEnabled()
        self.refineCheck.setEnabled(available)
        if not available:
            self.refineCheck.setChecked(False)
        elif was_source_disabled:
            self.refineCheck.setChecked(True)
        reason = metadata.get("refinement_reason", "")
        if reason:
            self.refineCheck.setToolTip(reason)
        self._sourceWarning = inspection.warning if inspection else None
        self._series = metadata.get("series")
        self.timelapseBox.setVisible(bool(self._series))
        self._refreshSelectionMetadata()

    def _refreshSelectionMetadata(self, *_args) -> None:
        metadata = self._outputMetadata.get(
            self.detectorCombo.currentData(), {}
        )
        channels = tuple(metadata.get("channels", ()))
        self.channelCombo.blockSignals(True)
        self.channelCombo.clear()
        if channels:
            self.channelCombo.addItem("All", None)
            for channel in channels:
                self.channelCombo.addItem(str(channel), int(channel))
        self.channelCombo.blockSignals(False)
        self.channelLabel.setVisible(bool(channels))
        self.channelCombo.setVisible(bool(channels))

        has_z = bool(metadata.get("has_z", False))
        self.projectCheck.setVisible(has_z)
        self.projectCheck.setEnabled(has_z)
        if not has_z:
            self.projectCheck.setChecked(False)
        self._refreshEstimate()

    def _refreshEstimate(self, *_args) -> None:
        metadata = self._outputMetadata.get(
            self.detectorCombo.currentData(), {}
        )
        projection = "max" if self.projectCheck.isChecked() else "keep"
        key = self._estimateKey(self.channelCombo.currentData(), projection)
        estimate = metadata.get("estimates", {}).get(key)
        axes = metadata.get("axes", "")
        shape = metadata.get("shape", ())
        complete = metadata.get("complete", 0)
        total = metadata.get("total", 0)
        parts = [
            f"Source: {axes or 'unknown axes'} "
            f"{'×'.join(str(size) for size in shape) if shape else 'unknown shape'}; "
            f"{complete}/{total} complete."
        ]
        if estimate:
            out_shape = "×".join(str(size) for size in estimate["shape"])
            footprint = estimate["canvas_bytes"] + estimate["weight_bytes"]
            parts.append(
                f"Estimated output {estimate['axes']} {out_shape}; "
                f"canvas + weights {self._formatBytes(footprint)}."
            )
        series = getattr(self, "_series", None)
        if series:
            parts.append(f"Timelapse: {series['describe']}.")
        warning = metadata.get("warning") or getattr(self, '_sourceWarning', None)
        if warning:
            parts.append(str(warning))
        self.sourceStatus.setText(" ".join(parts))

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

    #: A timelapse of tilings gets one layout and one canvas for every
    #: timepoint (:meth:`process_series`), not a stack of independent mosaics.
    series_policy = "own"
    series_default = "all"

    name = "Tiling mosaic"
    id = "tiling-mosaic"

    #: Reads the tiling run's manifest and tile files, not one array.
    core_dims_in = WHOLE_INPUT
    reads_recording_file = True
    output_kind = "image"
    file_extensions = ["json", "tiff", "tif", "ome.tiff", "hdf5", "h5", "zarr"]
    description = "Reassemble a saved tiling run into a stitched mosaic"
    default_save_subdir = "mosaic"
    accepted_source_kinds = ("image", "tiling-manifest")
    execution_policy = "worker"

    @classmethod
    def default_params(cls) -> dict:
        return {   'refine': True,
        'blend': True,
        'shading_correction': False,
        'max_shift_px': None,
        'project': False,
        'stage_positions': True,
        'detector': None,
        'alignment_diagnostic': False,
        'channel': None,
        'project_z': False,
        'time_alignment': 'shared',
        'layout_source': 'all',
        'reference_timepoint': None,
        'drift_smoothing': 0,
        'max_drift_px': None}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                'refine', 'bool', True, label='Refine alignment',
                help="Cross-correlate every overlapping pair of tiles and solve for\nall positions at once, instead of trusting the recorded stage\npositions. Slower than the live preview's one-neighbour pass,\nbut a single bad match cannot displace the rest of the run.",
            ),
            ParamField(
                'blend', 'bool', True, label='Mean overlaps',
                help='Average overlapping pixels. Unchecked, later tiles overwrite\nearlier ones, which leaves visible seams but no ghosting.',
            ),
            ParamField(
                'shading_correction', 'bool', False, label='Correct vignetting',
                help='Estimate the illumination profile from the run itself and divide\nit out of every tile.\n\nShading is fixed to the detector — the same corner is dim in\nevery tile — while the sample is not, so averaging the tiles in\ndetector coordinates lets the specimen cancel and leaves the\nillumination. No shape is assumed, so one-sided falloff is\nhandled as readily as a radial one.\n\nNeeds enough tiles for the sample to average out, and is skipped\nwith a reason in the log when the estimate cannot be trusted.\nCosts one extra read pass over the tiles.',
            ),
            ParamField(
                'max_shift_px', 'float', None, label='Max shift',
                help='Reject corrections larger than this many pixels, which are usually false matches on repeating structure; empty or 0 derives a limit from the tile size',
                min=0.0, max=100000.0, nullable=True,
            ),
            ParamField(
                'project', 'bool', False, label='Max-project Z',
                help='Collapse a volumetric run to one plane per tile before stitching',
            ),
            ParamField(
                'stage_positions', 'bool', True, label='Start from stage positions',
                help="Lay the tiles out from the stage coordinates the run commanded,\nrather than the pixel positions it saved. The saved positions\ninclude whatever the live 'Align tiles' pass did during\nacquisition, and a bad live correction cannot be undone here:\nthe tiles are correlated where those positions say they overlap.\nUncheck to reassemble exactly the layout that was saved.",
            ),
            ParamField(
                'detector', 'text', None, label='Output source',
                help="Detector whose tiles make the mosaic; empty uses the run's alignment detector",
                nullable=True,
            ),
            ParamField(
                'alignment_diagnostic', 'bool', False, label='Alignment diagnostic',
                help='Also produce the alignment diagnostic result', advanced=True,
            ),
            ParamField(
                'channel', 'text', None, label='Channel',
                help="Index of the one channel to keep; empty or 'all' keeps every channel",
                nullable=True,
            ),
            ParamField(
                'project_z', 'bool', False, label='Max-project Z (recorded spelling)',
                help='The same switch under the name older runs recorded; either one turns it on',
                advanced=True,
            ),
            ParamField(
                'time_alignment', 'select', 'shared', label='Align across time',
                group='Timelapse',
                options=TIME_ALIGNMENTS,
                option_labels=(
                    'Stage positions only', 'One shared layout',
                    'Shared layout + drift', 'Each timepoint on its own',
                ),
                help=TIME_ALIGNMENT_HELP,
            ),
            ParamField(
                'layout_source', 'select', 'all', label='Layout from', group='Timelapse',
                options=('all', 'reference'),
                option_labels=('All timepoints', 'Reference timepoint only'),
                help='Which timepoints the shared layout is estimated from. All adds their '
                     'correlations (best signal); the reference alone is the single-timepoint '
                     'registration applied to every timepoint.',
            ),
            ParamField(
                'reference_timepoint', 'int', None, label='Reference timepoint', group='Timelapse',
                min=-1, max=100000, nullable=True,
                help='The timepoint drift is measured against, and the one used when the layout is '
                     'taken from the reference alone. Empty (or -1): the first one processed.',
            ),
            ParamField(
                'drift_smoothing', 'int', 0, label='Smooth drift over', group='Timelapse',
                min=0, max=99, suffix='timepoints',
                help='Running median over this many timepoints before the drift is applied; '
                     '0 or 1 applies each timepoint\'s own estimate.',
            ),
            ParamField(
                'max_drift_px', 'float', None, label='Max drift (px)', group='Timelapse',
                min=0.0, max=100000.0, decimals=0, nullable=True,
                help='Reject drift estimates larger than this; empty derives a limit from the tile size.',
            ),
        )

    def __init__(self):
        super().__init__()
        self._logger = initLogger(self)
        # Geometry only. Payload/alignment arrays are intentionally never held
        # here, so changing detector, projection or blending reuses placement
        # without retaining a second dataset in memory.
        self._layout_cache: dict[tuple, MosaicLayout] = {}

    @staticmethod
    def _layout_cache_key(index: TilingDatasetIndex, params: dict) -> tuple:
        def identity(path: Path):
            try:
                stat = path.stat()
            except OSError:
                return str(path.resolve()), None, None
            if path.is_dir():
                members = []
                try:
                    for child in sorted(path.rglob('*')):
                        if not child.is_file():
                            continue
                        child_stat = child.stat()
                        members.append((
                            str(child.relative_to(path)),
                            int(child_stat.st_size),
                            int(child_stat.st_mtime_ns),
                        ))
                except OSError:
                    # A store changing while it is fingerprinted cannot reuse
                    # an earlier layout; a unique failure marker forces a miss.
                    return str(path.resolve()), 'changing', object()
                return (
                    str(path.resolve()), 'directory', int(stat.st_mtime_ns),
                    tuple(members),
                )
            return str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns)

        return (
            identity(index.manifest),
            tuple(identity(tile.alignment.path) for tile in index.tiles),
            bool(params.get("stage_positions", True)),
            bool(params.get("refine", True)),
            params.get("max_shift_px"),
        )

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return _TilingParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget):
        # The geometry is in the dataset's own manifest; nothing to ask for.
        return None

    @staticmethod
    def _estimateMetadata(estimate: PayloadEstimate) -> dict:
        return {
            "axes": estimate.axes,
            "shape": tuple(estimate.shape),
            "canvas_bytes": estimate.canvas_bytes,
            "weight_bytes": estimate.weight_bytes,
            "placement_path": estimate.placement_path,
            "tiles": estimate.tiles,
        }

    @staticmethod
    def _alignmentEstimate(index: TilingDatasetIndex, project_z=False):
        """Cheap diagnostic estimate from alignment descriptors and saved positions."""
        selected = []
        for tile in index.tiles:
            if not tile.alignment.path.exists():
                continue
            axes = tile.alignment.axes
            shape = list(tile.alignment.shape)
            if project_z and "Z" in axes:
                position = axes.index("Z")
                axes = axes[:position] + axes[position + 1:]
                shape.pop(position)
            selected.append((tile, axes, tuple(shape)))
        if not selected:
            return None
        axes_set = {axes for _tile, axes, _shape in selected}
        if len(axes_set) != 1:
            return None
        leading_shapes = [shape[:-2] for _tile, _axes, shape in selected]
        leading = tuple(
            max(shape[axis] for shape in leading_shapes)
            for axis in range(len(leading_shapes[0]))
        )
        bounds = []
        for tile, _axes, shape in selected:
            row, column = tile.saved_position_yx
            row0, column0 = int(np.floor(row)), int(np.floor(column))
            bounds.append((
                row0,
                column0,
                row0 + int(shape[-2]),
                column0 + int(shape[-1]),
            ))
        spatial = (
            max(item[2] for item in bounds) - min(item[0] for item in bounds),
            max(item[3] for item in bounds) - min(item[1] for item in bounds),
        )
        shape = leading + spatial
        return {
            "axes": selected[0][1],
            "shape": shape,
            "canvas_bytes": np.dtype(np.float32).itemsize * int(np.prod(shape)),
            "weight_bytes": np.dtype(np.uint16).itemsize * int(np.prod(spatial)),
            "placement_path": "identity-integer",
            "tiles": len(selected),
        }

    def inspect_source(self, data_obj: "DataObj") -> SourceInspection:
        """Populate selection controls using only the parsed manifest index."""
        source = getattr(data_obj, "dataPath", None) or getattr(
            data_obj, "name", None
        )
        index = getattr(data_obj, "sourceMetadata", None)
        completeness = getattr(data_obj, "sourceSummary", None)
        if not isinstance(index, TilingDatasetIndex):
            index, completeness = inspect_dataset(Path(source))

        total = len(index.tiles)
        choices = []
        warnings = []
        if completeness is not None and completeness.missing_paths:
            warnings.append(
                f"{len(completeness.missing_paths)} declared artifact(s) are missing."
            )

        ordered_detectors = list(index.detectors)
        if index.alignment_detector in ordered_detectors:
            ordered_detectors.remove(index.alignment_detector)
            ordered_detectors.insert(0, index.alignment_detector)
        for detector in ordered_detectors:
            refs = [
                tile.payloads[detector]
                for tile in index.tiles
                if detector in tile.payloads
            ]
            complete_refs = [ref for ref in refs if ref.complete]
            representative = complete_refs[0] if complete_refs else (refs[0] if refs else None)
            if representative is None:
                continue
            axes = representative.axes
            shape = representative.shape
            channels = ()
            if "C" in axes and complete_refs:
                axis = axes.index("C")
                channel_count = min(ref.shape[axis] for ref in complete_refs)
                channels = tuple(range(channel_count))
            has_z = "Z" in axes
            estimates = {}
            channel_values = (None, *channels) if channels else (None,)
            projections = ("keep", "max") if has_z else ("keep",)
            estimate_warning = None
            for channel in channel_values:
                for projection in projections:
                    key = _TilingParamsWidget._estimateKey(channel, projection)
                    try:
                        estimate = estimate_payload_selection(
                            index,
                            PayloadSelection(
                                detector=detector,
                                channel=channel,
                                z_projection=projection,
                            ),
                        )
                        estimates[key] = self._estimateMetadata(estimate)
                    except Exception as exc:
                        estimate_warning = f"Estimate unavailable: {exc}"
            complete = (
                completeness.payloads_complete.get(detector, 0)
                if completeness is not None
                else sum(ref.complete for ref in refs)
            )
            choices.append(SourceChoice(
                value=detector,
                label=(
                    f"{detector} (alignment detector)"
                    if detector == index.alignment_detector else detector
                ),
                metadata={
                    "axes": axes,
                    "shape": tuple(shape),
                    "channels": channels,
                    "has_z": has_z,
                    "complete": int(complete),
                    "total": total,
                    "estimates": estimates,
                    "warning": estimate_warning,
                    "default": detector == index.alignment_detector,
                },
            ))

        alignment_present = (
            completeness.alignment_files_present
            if completeness is not None
            else sum(tile.alignment.path.exists() for tile in index.tiles)
        )
        alignment = index.tiles[0].alignment if index.tiles else None
        alignment_estimates = {}
        if alignment is not None:
            keep = self._alignmentEstimate(index, project_z=False)
            projected = self._alignmentEstimate(index, project_z=True)
            if keep is not None:
                alignment_estimates[
                    _TilingParamsWidget._estimateKey(None, "keep")
                ] = keep
            if "Z" in alignment.axes and projected is not None:
                alignment_estimates[
                    _TilingParamsWidget._estimateKey(None, "max")
                ] = projected
            choices.append(SourceChoice(
                value="__alignment__",
                label="Alignment images (diagnostic)",
                metadata={
                    "axes": alignment.axes,
                    "shape": tuple(alignment.shape),
                    "channels": (),
                    "has_z": "Z" in alignment.axes,
                    "complete": int(alignment_present),
                    "total": total,
                    "estimates": alignment_estimates,
                    "default": not bool(index.detectors),
                    "warning": (
                        "No alignment images are readable."
                        if alignment_present == 0 else None
                    ),
                },
            ))

        refinement_available = alignment_present > 0
        refinement_reason = ""
        if not refinement_available:
            refinement_reason = (
                "Refinement is unavailable because no alignment artifacts "
                "are present. Disable refinement to assemble at nominal positions."
            )
        elif alignment_present < total:
            warnings.append(
                f"Refinement will use {alignment_present}/{total} alignment images; "
                "the others retain nominal positions."
            )
        metadata = {
            "manifest": str(index.manifest),
            "refinement_available": refinement_available,
            "refinement_reason": refinement_reason,
            "alignment_complete": int(alignment_present),
            "total_tiles": total,
        }
        series = self._series_inspection(data_obj)
        if series is not None:
            metadata["series"] = series
            if series["incompatible"]:
                warnings.append(
                    "Timepoint(s) " + ", ".join(str(t) for t in series["incompatible"][:8])
                    + " have a different grid and are left out of a timelapse reconstruction."
                )
        return SourceInspection(
            source_kind=getattr(data_obj, "sourceKind", "image"),
            choices={"output": tuple(choices)},
            metadata=metadata,
            warning=" ".join(warnings) or None,
        )

    @staticmethod
    def _series_inspection(data_obj) -> dict | None:
        """What the opened tiling says about being one timepoint of a timelapse.

        Manifests only (no pixels). The grid check parses the first, the opened
        and the last complete timepoint, not every one: that is what a form
        needs, and a full check follows when the series is reconstructed.
        """
        from imswitch.imcommon.algorithms import tile_timelapse as ttl
        from imswitch.improcess.model.series import KIND_TILING_TIMELAPSE, discover_series

        info = discover_series(data_obj)
        if info is None or info.kind != KIND_TILING_TIMELAPSE:
            return None
        counts = info.counts()
        complete = [m.index for m in info.members if m.state == "complete"]
        sample = sorted({*complete[:1], *complete[-1:], *([info.anchor] if info.anchor in complete else [])})
        incompatible: list[int] = []
        reasons: dict[int, list[str]] = {}
        series = ttl.read_timelapse(info.manifest) if info.manifest else None
        if series is not None and len(sample) > 1:
            verdict, _parsed = ttl.compatibility(series, indices=sample)
            incompatible = sorted(verdict.incompatible)
            reasons = {t: list(r) for t, r in verdict.incompatible.items()}
        return {
            "manifest": info.manifest,
            "planned": info.planned,
            "counts": counts,
            "times_s": info.times_s(),
            "states": [m.state for m in info.members],
            "anchor": info.anchor,
            "opened_as_series": info.opened_as_series,
            "compatible": not incompatible,
            "incompatible": incompatible,
            "reasons": reasons,
            "sampled": sample,
            "describe": info.describe(),
        }

    @staticmethod
    def _indexForProcess(data_obj: "DataObj", source):
        cached = getattr(data_obj, "sourceMetadata", None)
        if not isinstance(cached, TilingDatasetIndex):
            return inspect_dataset(Path(source))[0]
        expected = getattr(data_obj, "sourceFingerprint", None)
        current = manifest_fingerprint(cached.manifest)
        if expected is not None and current != expected:
            raise ValueError(
                "The tiling manifest changed after it was opened. Reopen the "
                "run so detector choices, estimates and reconstruction use the "
                "same manifest version."
            )
        return cached

    def estimate_resources(
        self, data_obj: "DataObj", params: dict
    ) -> ResourceEstimate | None:
        """The mosaic's footprint; for a timelapse, T of them.

        The T output planes are all held (the result is one array), so the
        canvas bytes scale with the number of planes; the weight canvas is
        reused from plane to plane and does not. Approximate for the modes that
        move tiles between timepoints: the union canvas can be a little larger.
        """
        estimate = self._estimate_single(data_obj, params)
        if estimate is None:
            return None
        from imswitch.improcess.model.series import SeriesError
        from imswitch.improcess.reconstructors import series_run

        try:
            selection = series_run.plan_series(self, data_obj, params)
        except SeriesError:
            return estimate
        if selection is None:
            return estimate
        planes = len(selection.positions)
        suggestions = (*estimate.suggestions, "reconstruct a range or every n-th timepoint")
        return ResourceEstimate(
            output_shape=(planes, *estimate.output_shape),
            canvas_bytes=int(estimate.canvas_bytes) * planes,
            weight_bytes=int(estimate.weight_bytes),
            description=f"{planes}-timepoint {estimate.description}",
            suggestions=suggestions,
        )

    def _estimate_single(
        self, data_obj: "DataObj", params: dict
    ) -> ResourceEstimate | None:
        source = getattr(data_obj, "dataPath", None) or getattr(
            data_obj, "name", None
        )
        if not source:
            return None
        index = self._indexForProcess(data_obj, source)
        if index.format != "imswitch-tiling/2":
            return None
        requested_detector = params.get("detector")
        diagnostic = bool(
            params.get("alignment_diagnostic", False)
            or requested_detector == "__alignment__"
            or not index.detectors
        )
        if diagnostic:
            estimate = self._alignmentEstimate(
                index,
                project_z=bool(
                    params.get("project_z", params.get("project", False))
                ),
            )
            if estimate is None:
                return None
            return ResourceEstimate(
                output_shape=tuple(estimate["shape"]),
                canvas_bytes=int(estimate["canvas_bytes"]),
                weight_bytes=int(estimate["weight_bytes"]),
                description="alignment diagnostic mosaic",
            )

        detector = requested_detector or (
            index.alignment_detector
            if index.alignment_detector in index.detectors
            else index.detectors[0]
        )
        raw_channel = params.get("channel")
        channel = (
            None if raw_channel in (None, "", "all", "All")
            else int(raw_channel)
        )
        projection = (
            "max" if params.get(
                "project_z", params.get("project", False)
            ) else "keep"
        )
        estimate = estimate_payload_selection(
            index,
            PayloadSelection(
                detector=detector,
                channel=channel,
                z_projection=projection,
            ),
            stage_positions=params.get("stage_positions", True),
            transform_resolver=params.get("transform_resolver"),
        )
        suggestions = []
        if channel is None and "C" in estimate.axes:
            suggestions.append("select one channel instead of All")
        if projection == "keep" and "Z" in estimate.axes:
            suggestions.append("max-project Z")
        return ResourceEstimate(
            output_shape=tuple(estimate.shape),
            canvas_bytes=estimate.canvas_bytes,
            weight_bytes=estimate.weight_bytes,
            description=f"{detector} payload mosaic",
            suggestions=tuple(suggestions),
        )

    # ------------------------------------------------------------------
    # A timelapse of tilings
    # ------------------------------------------------------------------

    def process_series(self, selection, data_obj, params, context=None, *,
                       progress=None, cancel=None):
        """Reconstruct a timelapse of tilings as one ``T, ..., Y, X`` result.

        Every timepoint is drawn on the *same canvas* (one origin, one shape),
        so the stack needs no cropping or shifting; the layout across time
        follows ``time_alignment`` (see :data:`TIME_ALIGNMENT_HELP`). Planes
        are assembled one timepoint at a time straight into the output, so only
        one timepoint's tiles are in memory; the output itself is the memory
        ceiling, checked against the budget before anything is read.
        """
        from dataclasses import replace

        from imswitch.imcommon.algorithms import tile_timelapse as ttl
        from imswitch.improcess.model import iteration
        from imswitch.improcess.model import series as sm
        from imswitch.improcess.reconstructors import series_run

        info = selection.info
        if info.kind != sm.KIND_TILING_TIMELAPSE:
            raise ValueError(
                f"The tiling mosaic reconstructs a timelapse of tilings; {info.name} is a "
                f"{info.kind}. Open a tiling folder or the timelapse manifest."
            )
        if params.get("alignment_diagnostic") or params.get("detector") == "__alignment__":
            raise ValueError(
                "The alignment diagnostic mosaic is available for one timepoint at a time: "
                "choose 'This timepoint only'."
            )
        mode = str(params.get("time_alignment", "shared"))
        if mode not in TIME_ALIGNMENTS:
            raise ValueError(f"time_alignment must be one of {list(TIME_ALIGNMENTS)}, got {mode!r}")
        if mode == "shared+drift" and params.get("layout_source", "all") not in ("all", "reference"):
            raise ValueError("layout_source must be 'all' or 'reference'")

        def check_cancelled():
            iteration.check_cancelled(cancel, context)

        def phase_progress(phase, completed, total, message):
            if context is not None:
                context.report(phase, completed, total, message)

        log = self._logger.info
        phase_progress("inspect", 0, 1, "Reading the timepoints' manifests")
        members = {info.members[p].index: p for p in selection.process}
        parsed: dict[int, TilingDatasetIndex] = {}
        for number, (timepoint, position) in enumerate(sorted(members.items()), start=1):
            check_cancelled()
            index, _completeness = inspect_dataset(Path(info.members[position].path))
            if index.format != "imswitch-tiling/2":
                raise ValueError(
                    f"Timepoint {timepoint} is a {index.format or 'legacy'} tiling manifest; "
                    "a timelapse reconstruction needs imswitch-tiling/2."
                )
            parsed[timepoint] = index
            phase_progress("inspect", number, len(members), f"Read manifest of timepoint {timepoint}")
        verdict = ttl.compare_grids(parsed)
        if not verdict.compatible:
            raise ValueError("No timepoint has a usable grid: " + "; ".join(
                f"{t}: {', '.join(r)}" for t, r in verdict.incompatible.items()))
        if verdict.incompatible:
            process = tuple(p for p in selection.process if info.members[p].index in verdict.compatible)
            dropped = {members_pos: "different grid: " + "; ".join(verdict.incompatible[tp])
                       for tp, members_pos in members.items() if tp in verdict.incompatible}
            filled = set(selection.filled)
            if selection.request.missing != "skip":
                filled |= set(dropped)
            skipped = tuple(selection.skipped) + tuple(
                (pos, why + (f" (filled with {selection.request.missing})"
                             if selection.request.missing != "skip" else ""))
                for pos, why in dropped.items())
            selection = replace(selection, process=process, filled=tuple(sorted(filled)), skipped=skipped)
            for tp in verdict.incompatible:
                parsed.pop(tp, None)
            log(f"Left out {len(dropped)} timepoint(s) with a different grid.")
        use_all = sorted(parsed)
        reference_tp = params.get("reference_timepoint")
        if reference_tp is None or int(reference_tp) < 0:
            reference_tp = use_all[0]
        elif int(reference_tp) not in parsed:
            raise ValueError(
                f"Reference timepoint {reference_tp} is not among the timepoints reconstructed {use_all}"
            )
        reference_tp = int(reference_tp)
        reference_index = parsed[reference_tp]

        requested_detector = params.get("detector")
        detector = requested_detector or (
            reference_index.alignment_detector
            if reference_index.alignment_detector in reference_index.detectors
            else (reference_index.detectors[0] if reference_index.detectors else None)
        )
        if not detector or detector not in reference_index.detectors:
            raise ValueError(f"No payload for detector {detector!r} in this timelapse")
        raw_channel = params.get("channel")
        channel = None if raw_channel in (None, "", "all", "All") else int(raw_channel)
        projection = "max" if params.get("project_z", params.get("project", False)) else "keep"
        payload_selection = PayloadSelection(detector=detector, channel=channel, z_projection=projection)

        # -- one layout (or one per timepoint) ------------------------------
        refine = bool(params.get("refine", True)) and mode != "stage"
        if refine and not any(
            tile.alignment.path.exists() for index in parsed.values() for tile in index.tiles
        ):
            raise ValueError(
                "Alignment refinement was requested, but no alignment artifacts are present. "
                "Choose 'Stage positions only' or disable refinement."
            )
        options = LayoutOptions(
            stage_positions=params.get("stage_positions", True), refine=refine,
            max_shift_px=params.get("max_shift_px"),
        )
        layouts: dict[int, MosaicLayout] = {}
        shared_report = None
        layout_timepoints: list[int] = []
        if mode == "independent":
            for number, timepoint in enumerate(use_all, start=1):
                check_cancelled()
                layouts[timepoint] = solve_layout(
                    parsed[timepoint], options, progress=log, check_cancelled=check_cancelled,
                    phase_progress=phase_progress,
                )
            layout_timepoints = list(use_all)
        else:
            layout_timepoints = (
                [reference_tp] if params.get("layout_source", "all") == "reference" else list(use_all)
            )
            if params.get("layout_source", "all") == "reference":
                # One timepoint's own registration, exactly as a single tiling
                # gets it, applied to every timepoint.
                shared_layout = solve_layout(
                    parsed[reference_tp], options, progress=log, check_cancelled=check_cancelled,
                    phase_progress=phase_progress,
                )
            else:
                shared_layout, shared_report = ttl.solve_shared_layout(
                    parsed, layout_timepoints, options, progress=log,
                    check_cancelled=check_cancelled, phase_progress=phase_progress,
                )
            layouts = {timepoint: shared_layout for timepoint in use_all}
        phase_progress("align", 1, 1, "Layout ready")
        check_cancelled()

        drift = None
        shifts = {timepoint: (0.0, 0.0) for timepoint in use_all}
        if mode == "shared+drift":
            drift = ttl.estimate_drift(
                parsed, use_all, reference_tp, max_drift_px=params.get("max_drift_px"),
                smoothing=int(params.get("drift_smoothing") or 0),
                check_cancelled=check_cancelled, phase_progress=phase_progress,
            )
            shifts = {t: tuple(shift) for t, shift in zip(drift.timepoints, drift.shift_yx)}
            log("Drift (px, row/col): " + ", ".join(
                f"t{t}: ({dy:+.1f}, {dx:+.1f})" for t, (dy, dx) in shifts.items()))

        # -- one canvas -----------------------------------------------------
        resolver = params.get("transform_resolver")
        bounds = [
            ttl.placement_bounds(
                parsed[t], layouts[t].positions_yx, payload_selection,
                shift_yx=shifts[t], transform_resolver=resolver,
            )
            for t in use_all
        ]
        canvas = ttl.common_canvas(bounds)
        axes, leading = ttl.payload_axes_leading(list(parsed.values()), payload_selection)
        plane_shape = tuple(leading) + (canvas[2], canvas[3])
        positions = selection.positions
        planes = len(positions)
        plane_bytes = np.dtype(np.float32).itemsize * int(np.prod(plane_shape))
        weight_bytes = np.dtype(np.float32).itemsize * int(canvas[2] * canvas[3])
        required = plane_bytes * planes + weight_bytes
        budget = context.memory_budget_bytes if context else None
        if budget is not None and required > int(budget) and not (context and context.confirmed_over_budget):
            raise MemoryError(
                f"The timelapse mosaic is {planes} planes of {'x'.join(str(n) for n in plane_shape)} "
                f"(float32, {axes}): {required} bytes, above the {int(budget)}-byte reconstruction "
                "budget. Reconstruct a range or every n-th timepoint, select one channel, or "
                "max-project Z."
            )
        out = np.zeros((planes, *plane_shape), dtype=np.float32)
        if selection.request.missing == "nan":
            for k, position in enumerate(positions):
                if position in selection.filled:
                    out[k] = np.nan

        # -- timepoint by timepoint -----------------------------------------
        payload_provenance = {}
        scales = None
        # Alignment is already reported on the outer context; the timepoints
        # share what is left of the bar.
        base = float(getattr(context, "_last_fraction", 0.0) or 0.0)
        for k, position in enumerate(positions):
            if position in selection.filled and position not in selection.process:
                continue
            check_cancelled()
            timepoint = info.members[position].index
            member_context = series_run._member_context(context, k, planes, base)

            def member_phase(phase, completed, total, message, _ctx=member_context):
                if _ctx is not None:
                    _ctx.report(phase, completed, total, message)

            layout = layouts[timepoint]
            shift = shifts[timepoint]
            if shift != (0.0, 0.0):
                # Drift: the same layout, moved as a whole by this timepoint's correction.
                layout = MosaicLayout(
                    {i: (y + shift[0], x + shift[1]) for i, (y, x) in layout.positions_yx.items()},
                    layout.source, layout.report,
                )
            result = assemble_payload(
                parsed[timepoint], layout, payload_selection,
                PayloadAssemblyOptions(
                    blend=params.get("blend", True),
                    shading_correction=params.get("shading_correction", False),
                    transform_resolver=resolver,
                    layout_cache_key=("timelapse", timepoint, mode, shift),
                    progress=log, check_cancelled=check_cancelled,
                    phase_progress=member_phase if member_context is not None else None,
                    canvas=canvas, out=out[k],
                ),
            )
            payload_provenance[timepoint] = result.provenance
            scales = scales or result.scales
            if progress is not None:
                progress(k + 1, planes, f"T={k + 1}/{planes}")
            series_run.report_timepoint(context, k, planes, f"Timepoint {timepoint} assembled", base)
        check_cancelled()

        # -- the result -----------------------------------------------------
        stamps = selection.times_s()
        axis = sm.time_axis(stamps, positions, info.interval_s)
        first_tp = next(iter(payload_provenance))
        first_provenance = payload_provenance[reference_tp if reference_tp in payload_provenance else first_tp]
        drift_rows = np.zeros((planes, 2), dtype=float)
        plane_records = []
        for k, position in enumerate(positions):
            member = info.members[position]
            used = position in selection.process
            note = None if used else dict(selection.skipped).get(position, member.state)
            if used and member.index in shifts:
                drift_rows[k] = shifts[member.index]
            plane_records.append({"index": member.index, "time_s": member.time_s,
                                  "used": used, "note": note})
        manifests = []
        for timepoint in use_all:
            fingerprint = manifest_fingerprint(parsed[timepoint].manifest)
            manifests.append({
                "index": timepoint, "manifest": str(parsed[timepoint].manifest),
                "fingerprint": list(fingerprint),
                "skipped_tiles": [
                    {"tile_id": item.tile_id, "reason": item.reason}
                    for item in payload_provenance[timepoint].skipped
                ] if timepoint in payload_provenance else [],
            })
        timelapse_manifest = Path(info.manifest) if info.manifest else None
        shared_summary = None
        if shared_report is not None:
            shared_summary = {
                "timepoints": list(shared_report.timepoints), "pairs": shared_report.pairs,
                "pairs_measured": shared_report.pairs_measured, "passes": shared_report.passes,
                "method": "correlation surfaces summed over timepoints, then robust global solve",
            }
        sample_layout = layouts[reference_tp]
        record = {
            "kind": "imswitch-tiling-timelapse/1",
            "timelapse_manifest": str(timelapse_manifest) if timelapse_manifest else None,
            "timelapse_manifest_fingerprint": sm._fingerprint(str(timelapse_manifest)) if timelapse_manifest else None,
            "alignment_mode": mode,
            "reference_timepoint": reference_tp,
            "layout_source": params.get("layout_source", "all") if mode != "independent" else "own",
            "layout_timepoints": layout_timepoints,
            "refine": refine,
            "canvas": {"origin_yx": [canvas[0], canvas[1]], "shape_yx": [canvas[2], canvas[3]]},
            "manifests": manifests,
            "layout": {
                "source": sample_layout.source,
                "positions_yx": (
                    {str(tile_id): list(pos) for tile_id, pos in sample_layout.positions_yx.items()}
                    if mode != "independent" else
                    {str(t): {str(i): list(pos) for i, pos in layouts[t].positions_yx.items()}
                     for t in use_all}
                ),
                "shared": shared_summary,
                "refinement": sample_layout.report.summary(),
            },
            "drift": drift.as_dict() if drift is not None else None,
            "planes": plane_records,
            "left_out": [{"index": info.members[p].index, "why": why}
                         for p, why in selection.skipped if p not in selection.filled],
            "filled": [info.members[p].index for p in selection.filled],
            "filled_with": selection.request.missing if selection.filled else None,
            "memory": {"output_bytes": int(plane_bytes * planes), "budget_bytes": budget},
        }
        suffix = (" (all channels)" if channel is None and "C" in axes
                  else (f" (channel {channel})" if channel is not None else ""))
        name = f"{info.name} {detector}{suffix} timelapse mosaic ({planes} timepoints, {mode})"
        phase_progress("finalize", 1, 1, "Timelapse mosaic complete")
        ndim = len(axes) + 1
        result = TilingMosaicResult(
            name=name, data=out, axis_labels=["T", *axes],
            view_modes=[ViewMode("Standard", tuple(range(ndim)))],
            axis_scales=[axis["scale"], *scales], scale_unit="µm",
            provenance=first_provenance, output_origin_yx=(canvas[0], canvas[1]),
            manifest=reference_index.manifest, timelapse=record,
            drift_yx=drift_rows if mode == "shared+drift" else None,
        )
        result.metadata = {**dict(getattr(result, "metadata", None) or {}),
                           sm.METADATA_KEY: sm.series_record(selection, axis, extra={
                               "alignment_mode": mode, "canvas_origin_yx": [canvas[0], canvas[1]],
                               "canvas_shape_yx": [canvas[2], canvas[3]]})}
        return result

    def process(
        self,
        data_obj: "DataObj",
        params: dict,
        context: ReconstructionContext | None = None,
    ) -> TilingMosaicResult:
        source = getattr(data_obj, "dataPath", None) or getattr(
            data_obj, "name", None
        )
        if not source:
            raise ValueError(
                "The tiling reconstructor needs a file on disk: open a file "
                "from the tiling run's folder."
            )

        # A hundred multi-megapixel tiles take tens of seconds, so every stage
        # reports as it goes even though ImProcess normally dispatches this
        # reconstructor to a worker. The refinement
        # summary doubles as the diagnosis when a mosaic still looks wrong —
        # how many links held, how far they disagree, and whether the run fell
        # into groups that could not be tied to each other.
        progress = self._logger.info

        def check_cancelled():
            if context is not None:
                context.check_cancelled()

        def phase_progress(phase, completed, total, message):
            if context is not None:
                context.report(phase, completed, total, message)

        phase_progress("inspect", 0, 1, "Validating tiling source")
        check_cancelled()
        index = self._indexForProcess(data_obj, source)
        phase_progress(
            "inspect",
            1,
            1,
            f"Validated {len(index.tiles)}-tile manifest",
        )
        result_provenance = None
        result_origin_yx = (0, 0)
        diagnostic_name = None
        if index.format == 'imswitch-tiling/2':
            alignment_present = sum(
                tile.alignment.path.exists() for tile in index.tiles
            )
            if params.get("refine", True) and alignment_present == 0:
                raise ValueError(
                    "Alignment refinement was requested, but no alignment "
                    "artifacts are present. Disable refinement to assemble "
                    "the payloads at their nominal positions."
                )
            cache_key = self._layout_cache_key(index, params)
            layout = self._layout_cache.get(cache_key)
            if layout is None:
                layout = solve_layout(
                    index,
                    LayoutOptions(
                        stage_positions=params.get("stage_positions", True),
                        refine=params.get("refine", True),
                        max_shift_px=params.get("max_shift_px"),
                    ),
                    progress=progress,
                    check_cancelled=check_cancelled,
                    phase_progress=phase_progress,
                )
                # A one-entry cache invalidates eagerly when any geometry input
                # changes and cannot grow across many opened runs.
                self._layout_cache = {cache_key: layout}
            else:
                progress('Reusing solved alignment layout.')
            phase_progress(
                "align",
                1,
                1,
                "Alignment layout ready",
            )
            check_cancelled()

            requested_detector = params.get("detector")
            diagnostic = bool(
                params.get("alignment_diagnostic", False)
                or requested_detector == '__alignment__'
                or not index.detectors
            )
            if not diagnostic:
                detector = requested_detector or (
                    index.alignment_detector
                    if index.alignment_detector in index.detectors
                    else index.detectors[0]
                )
                raw_channel = params.get("channel")
                channel = (
                    None if raw_channel in (None, '', 'all', 'All')
                    else int(raw_channel)
                )
                payload_result = assemble_payload(
                    index,
                    layout,
                    PayloadSelection(
                        detector=detector,
                        channel=channel,
                        z_projection=(
                            'max' if params.get(
                                "project_z", params.get("project", False)
                            ) else 'keep'
                        ),
                    ),
                    PayloadAssemblyOptions(
                        blend=params.get("blend", True),
                        shading_correction=params.get(
                            "shading_correction", False
                        ),
                        transform_resolver=params.get("transform_resolver"),
                        layout_cache_key=cache_key,
                        progress=progress,
                        check_cancelled=check_cancelled,
                        phase_progress=phase_progress,
                        memory_budget_bytes=(
                            context.memory_budget_bytes if context else None
                        ),
                        confirmed_over_budget=(
                            context.confirmed_over_budget if context else False
                        ),
                    ),
                )
                mosaic = payload_result.data
                axes = payload_result.axes
                axis_scales = list(payload_result.scales)
                if payload_result.provenance.transform_source == 'assumed':
                    # The pixels are placed either way; what the operator needs
                    # to know is that their alignment to the reference detector
                    # is a default nobody stated, not a measured fact.
                    progress(
                        f'Nothing declares how {detector} relates to '
                        f'{index.alignment_detector}; placing it as if the two '
                        'were identical. Overlays between them are only as '
                        'good as that assumption.'
                    )
                channel_suffix = (
                    ' (all channels)' if channel is None and 'C' in axes
                    else (f' (channel {channel})' if channel is not None else '')
                )
                name = (
                    f'{index.manifest.parent.name} {detector}{channel_suffix} '
                    'mosaic'
                )
                if layout.report.moved:
                    name += (
                        f' (refined {layout.report.moved}/{len(index.tiles)})'
                    )
                phase_progress(
                    "finalize", 0, 1, "Finalizing payload mosaic metadata"
                )
                check_cancelled()
                phase_progress("finalize", 1, 1, "Payload mosaic complete")
                return TilingMosaicResult(
                    name=name,
                    data=mosaic,
                    axis_labels=list(axes),
                    view_modes=[
                        ViewMode("Standard", tuple(range(mosaic.ndim)))
                    ],
                    axis_scales=axis_scales,
                    scale_unit="µm",
                    provenance=payload_result.provenance,
                    output_origin_yx=payload_result.origin_yx,
                )

            # Explicit diagnostic compatibility: reconstruct the singular
            # alignment images rather than any detector's full payload.
            dataset = load_dataset(
                Path(source), progress=progress,
                prefer_stage_positions=params.get("stage_positions", True),
                detector=None,
                check_cancelled=check_cancelled,
                phase_progress=phase_progress,
            )
            apply_layout(dataset, layout)
            moved = layout.report.moved
            result_provenance = PayloadProvenance(
                detector="__alignment__",
                channel=None,
                z_projection=(
                    "max" if params.get(
                        "project_z", params.get("project", False)
                    ) else "keep"
                ),
                skipped=tuple(
                    SkippedPayload(tile.tile_id, "alignment artifact missing")
                    for tile in index.tiles
                    if not tile.alignment.path.exists()
                ),
                manifest=index.manifest,
                layout_cache_key=cache_key,
                refinement_report=layout.report,
                transform_source="alignment-grid",
                transform={"kind": "identity"},
                placement_path="alignment-diagnostic",
            )
            if layout.positions_yx:
                result_origin_yx = tuple(
                    int(np.floor(min(position[axis] for position in layout.positions_yx.values())))
                    for axis in (0, 1)
                )
            diagnostic_name = (
                f"{index.manifest.parent.name} alignment images (diagnostic) mosaic"
            )
        else:
            # Version-1/interim datasets have no singular, exact alignment
            # contract. Preserve their previous load-and-refine behavior.
            dataset = load_dataset(
                Path(source), progress=progress,
                prefer_stage_positions=params.get("stage_positions", True),
                detector=params.get("detector"),
                check_cancelled=check_cancelled,
                phase_progress=phase_progress,
            )
            moved = 0
            if params.get("refine", True):
                report = refine_layout(
                    dataset,
                    params.get("max_shift_px"),
                    progress=progress,
                    check_cancelled=check_cancelled,
                    phase_progress=phase_progress,
                )
                moved = report.moved

        mosaic = assemble(
            dataset,
            blend=params.get("blend", True),
            progress=progress,
            shading_correction=params.get("shading_correction", False),
            check_cancelled=check_cancelled,
            phase_progress=phase_progress,
            memory_budget_bytes=(
                context.memory_budget_bytes if context else None
            ),
            confirmed_over_budget=(
                context.confirmed_over_budget if context else False
            ),
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

        name = diagnostic_name or f"{Path(source).parent.name} mosaic"
        if moved:
            name = f"{name} (refined {moved}/{len(dataset.tiles)})"

        phase_progress("finalize", 0, 1, "Finalizing mosaic metadata")
        check_cancelled()
        phase_progress("finalize", 1, 1, "Mosaic complete")
        return TilingMosaicResult(
            name=name,
            data=mosaic,
            axis_labels=axis_labels,
            view_modes=[ViewMode("Standard", tuple(range(mosaic.ndim)))],
            axis_scales=axis_scales,
            scale_unit="µm",
            provenance=result_provenance,
            output_origin_yx=result_origin_yx,
            manifest=index.manifest,
        )
