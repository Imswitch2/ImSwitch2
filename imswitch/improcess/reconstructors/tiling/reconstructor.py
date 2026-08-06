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

import json
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import tifffile as tiff
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
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import (
    Reconstructor,
    ReconstructionContext,
    ResourceEstimate,
    SourceChoice,
    SourceInspection,
)

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class TilingMosaicResult(ProcessingResult):
    """A stitched mosaic, 2-D or volumetric."""

    def __init__(self, *args, provenance: PayloadProvenance | None = None,
                 output_origin_yx=(0, 0), **kwargs):
        super().__init__(*args, **kwargs)
        self.provenance = provenance
        self.output_origin_yx = tuple(output_origin_yx)

    def save(self, path: Path, fmt: str = "tiff") -> None:
        path = Path(path)
        if fmt != "tiff":
            raise ValueError(
                f'TilingMosaicResult only supports fmt="tiff", got "{fmt}"'
            )
        data = np.asarray(self.data)
        metadata = {"axes": "".join(self.axis_labels)}
        if self.provenance is not None:
            metadata["Description"] = json.dumps(
                self.provenance_summary(), sort_keys=True
            )
        scales = self.axis_scales or []
        by_label = dict(zip(self.axis_labels, scales))
        for label, key in (("X", "PhysicalSizeX"), ("Y", "PhysicalSizeY"),
                           ("Z", "PhysicalSizeZ")):
            if label in by_label:
                metadata[key] = float(by_label[label])
                metadata[f"{key}Unit"] = "µm"
        tiff.imwrite(str(path), data, ome=True, metadata=metadata)

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
            "kind": "imswitch-tiling-mosaic-provenance/1",
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
            "max_shift_px": maxShift if maxShift > 0 else None,
            "project": self.projectCheck.isChecked(),
            "stage_positions": self.stageCheck.isChecked(),
            "detector": self.detectorCombo.currentData() or None,
            "alignment_diagnostic": (
                self.detectorCombo.currentData() == "__alignment__"
            ),
            "channel": self.channelCombo.currentData(),
            "project_z": self.projectCheck.isChecked(),
        }

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

    name = "Tiling mosaic"
    id = "tiling-mosaic"
    file_extensions = ["json", "tiff", "tif", "ome.tiff", "hdf5", "h5", "zarr"]
    description = "Reassemble a saved tiling run into a stitched mosaic"
    default_save_subdir = "mosaic"
    accepted_source_kinds = ("image", "tiling-manifest")
    execution_policy = "worker"

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
        return SourceInspection(
            source_kind=getattr(data_obj, "sourceKind", "image"),
            choices={"output": tuple(choices)},
            metadata={
                "manifest": str(index.manifest),
                "refinement_available": refinement_available,
                "refinement_reason": refinement_reason,
                "alignment_complete": int(alignment_present),
                "total_tiles": total,
            },
            warning=" ".join(warnings) or None,
        )

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
        )
