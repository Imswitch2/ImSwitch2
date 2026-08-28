"""Reusable interactive localization workbench for slmcore hosts.

The widgets in this module are intentionally passive. They own parameter
editing, candidate/stale/busy state and visualization, but never call a camera,
SLM runtime or localization backend directly. A host responds to
``sigRunRequested`` and returns a candidate through :meth:`set_result`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping,Sequence

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore,QtWidgets

from ...core.cgh.localization.parameters import LOCALIZATION_PARAMS
from ...core.measurement import ImageMeasurement
from ...core.engine.parameters.spec import ParamSpec
from ..widgets.fields import ParamForm
from .controls import MeasurementControls


_WARNING_COLOR = "#a66a00"
_OK_COLOR = "#286b2d"
_MUTED_COLOR = "#888"
_ERROR_COLOR = "#a33"


_GEOMETRY_PARAM_KEYS = (
    "pattern_geometry_type",
    "period_prior_mode",
    "expected_period_x_px",
    "expected_period_y_px",
    "stagger_prior_mode",
    "manual_stagger",
    "lattice_size_prior_mode",
    "manual_lattice_count_x",
    "manual_lattice_count_y",
)


@dataclass(frozen=True)
class LocalizationWorkbenchContext:
    """Read-only target/backend hints displayed by the workbench."""

    target_type: str | None=None
    target_geometry_type: str | None=None
    target_spot_count: int | None=None
    target_lattice_count: tuple[int, int] | None=None
    target_stagger: float | None=None
    target_stagger_source: str="unavailable"
    target_expected_period_px: tuple[float, float] | None=None
    target_period_source: str="unavailable"

    @classmethod
    def from_mapping(
        cls,value: Mapping[str, Any] | None,
    ) -> "LocalizationWorkbenchContext":
        data = dict(value or {})
        period = data.get("target_expected_period_px")
        if period is not None:
            period = tuple(float(item) for item in period)
            if len(period) != 2:
                raise ValueError("target_expected_period_px must contain two values")
        count = data.get("target_spot_count")
        lattice_count = data.get("target_lattice_count")
        if lattice_count is not None:
            lattice_count = tuple(int(item) for item in lattice_count)
            if len(lattice_count) != 2:
                raise ValueError("target_lattice_count must contain two values")
        stagger = data.get("target_stagger")
        return cls(
            target_type=(
                None if data.get("target_type") is None
                else str(data.get("target_type"))
            ),
            target_geometry_type=(
                None if data.get("target_geometry_type") is None
                else str(data.get("target_geometry_type"))
            ),
            target_spot_count=None if count is None else int(count),
            target_lattice_count=lattice_count,
            target_stagger=None if stagger is None else float(stagger),
            target_stagger_source=str(
                data.get("target_stagger_source","unavailable")
            ),
            target_expected_period_px=period,
            target_period_source=str(
                data.get("target_period_source","unavailable")
            ),
        )


class LocalizationResultView(QtWidgets.QWidget):
    """Image view with localization overlays and compact diagnostics."""

    def __init__(
        self,parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self._result = None
        self._acquisition_image = None
        self._auxiliary_images: dict[str, np.ndarray] = {}

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(7)

        self.graphics = pg.GraphicsLayoutWidget(self)
        self.view_box = self.graphics.addViewBox(row=0,col=0)
        self.view_box.setAspectLocked(True)
        self.view_box.invertY(True)

        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.view_box.addItem(self.image_item)

        self.detected_item = pg.ScatterPlotItem(
            symbol="o",size=7,pen=pg.mkPen("#7ec8e3",width=1.2),
            brush=pg.mkBrush(0,0,0,0),
        )
        self.expected_item = pg.ScatterPlotItem(
            symbol="+",size=10,pen=pg.mkPen("#dddddd",width=1.2),
        )
        self.measured_item = pg.ScatterPlotItem(
            symbol="x",size=9,pen=pg.mkPen("#55aa55",width=1.6),
        )
        self.missing_item = pg.ScatterPlotItem(
            symbol="x",size=12,pen=pg.mkPen(_WARNING_COLOR,width=2.0),
        )
        self.unmatched_item = pg.ScatterPlotItem(
            symbol="o",size=10,pen=pg.mkPen(_WARNING_COLOR,width=1.8),
            brush=pg.mkBrush(0,0,0,0),
        )
        self.residual_item = pg.PlotDataItem(
            pen=pg.mkPen("#ff6666",width=1.2),connect="finite",
        )
        for item in (
            self.detected_item,self.expected_item,self.measured_item,
            self.missing_item,self.unmatched_item,self.residual_item,
        ):
            self.view_box.addItem(item)

        layout.addWidget(self.graphics,1)

        side = QtWidgets.QWidget(self)
        side.setMinimumWidth(175)
        side.setMaximumWidth(220)
        side_layout = QtWidgets.QVBoxLayout(side)
        side_layout.setContentsMargins(0,0,0,0)
        side_layout.setSpacing(5)

        self.reset_button = QtWidgets.QPushButton("Reset View")
        self.reset_button.setFixedHeight(22)
        self.reset_button.clicked.connect(self.view_box.autoRange)
        side_layout.addWidget(self.reset_button)

        image_row = QtWidgets.QHBoxLayout()
        image_row.setContentsMargins(0,0,0,0)
        image_row.setSpacing(5)
        self.image_mode_label = QtWidgets.QLabel("Image:")
        image_row.addWidget(self.image_mode_label)
        self.image_mode_combo = QtWidgets.QComboBox()
        self.image_mode_combo.addItem("Localization","Localization")
        self.image_mode_combo.currentIndexChanged.connect(
            self._on_image_mode_changed
        )
        image_row.addWidget(self.image_mode_combo,1)
        side_layout.addLayout(image_row)
        side_layout.addSpacing(3)

        self.show_detected = QtWidgets.QCheckBox("Detected")
        self.show_detected.setChecked(True)
        self.show_expected = QtWidgets.QCheckBox("Expected")
        self.show_expected.setChecked(True)
        self.show_measured = QtWidgets.QCheckBox("Matched")
        self.show_measured.setChecked(True)
        self.show_residuals = QtWidgets.QCheckBox("Residuals")
        self.show_residuals.setChecked(True)
        for checkbox in (
            self.show_detected,self.show_expected,
            self.show_measured,self.show_residuals,
        ):
            checkbox.toggled.connect(self._apply_visibility)
            side_layout.addWidget(checkbox)

        side_layout.addSpacing(5)
        self.result_heading = QtWidgets.QLabel("Localization Result")
        result_font = self.result_heading.font()
        result_font.setBold(True)
        self.result_heading.setFont(result_font)
        side_layout.addWidget(self.result_heading)

        self.summary_label = QtWidgets.QLabel("No localization result")
        self.summary_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.summary_label.setWordWrap(True)
        side_layout.addWidget(self.summary_label)

        self.detail_label = QtWidgets.QLabel("")
        self.detail_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.detail_label.setWordWrap(True)
        side_layout.addWidget(self.detail_label)

        self.result_extras = QtWidgets.QWidget()
        self.result_extras_layout = QtWidgets.QVBoxLayout(self.result_extras)
        self.result_extras_layout.setContentsMargins(0,0,0,0)
        self.result_extras_layout.setSpacing(3)
        self.result_extras.setVisible(False)
        side_layout.addWidget(self.result_extras)
        side_layout.addStretch(1)

        layout.addWidget(side,0)
        self._clear_overlays()

    def add_result_widget(self,widget: QtWidgets.QWidget) -> None:
        self.result_extras_layout.addWidget(widget)
        self.result_extras.setVisible(True)

    def set_auxiliary_image(self,name: str,image: Any) -> None:
        """Register an alternate base image sharing localization coordinates."""
        name = str(name or "").strip()
        if not name or name == "Localization":
            raise ValueError("Auxiliary image name must differ from 'Localization'")
        array = np.asarray(image,dtype=np.float64)
        if array.ndim != 2:
            raise ValueError("Auxiliary localization images must be two-dimensional")
        self._auxiliary_images[name] = np.array(array,copy=True)
        self._refresh_image_selector()
        if str(self.image_mode_combo.currentData()) == name:
            self._refresh_base_image(auto_range=False)

    def clear_auxiliary_image(self,name: str) -> None:
        name = str(name or "").strip()
        self._auxiliary_images.pop(name,None)
        self._refresh_image_selector()
        self._refresh_base_image(auto_range=False)

    def clear_auxiliary_images(self) -> None:
        self._auxiliary_images.clear()
        self._refresh_image_selector()
        self._refresh_base_image(auto_range=False)

    def _refresh_image_selector(self) -> None:
        selected = str(self.image_mode_combo.currentData() or "Localization")
        blocker = QtCore.QSignalBlocker(self.image_mode_combo)
        try:
            self.image_mode_combo.clear()
            self.image_mode_combo.addItem("Localization","Localization")
            for name in self._auxiliary_images:
                self.image_mode_combo.addItem(name,name)
            index = self.image_mode_combo.findData(selected)
            self.image_mode_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            del blocker
        # Keep the selector present so the viewing mode has a stable location.
        # Before auxiliary images exist, Localization is simply the only choice.
        self.image_mode_label.setVisible(True)
        self.image_mode_combo.setVisible(True)
        self.image_mode_combo.setEnabled(True)
        self._apply_visibility()

    def _on_image_mode_changed(self,_index: int) -> None:
        self._refresh_base_image(auto_range=False)
        self._apply_visibility()

    def _primary_image(self):
        if self._result is not None:
            return np.asarray(self._result.cropped_image,dtype=np.float64)
        return self._acquisition_image

    def _refresh_base_image(self,*,auto_range: bool) -> None:
        mode = str(self.image_mode_combo.currentData() or "Localization")
        image = (
            self._primary_image()
            if mode == "Localization"
            else self._auxiliary_images.get(mode)
        )
        if image is None:
            self.image_item.clear()
            return
        self.image_item.setImage(np.asarray(image),autoLevels=True)
        if auto_range:
            self.view_box.autoRange()

    def set_acquisition_image(self,image: np.ndarray) -> None:
        array = np.asarray(image,dtype=np.float64)
        if array.ndim != 2:
            raise ValueError("Localization acquisition image must be two-dimensional")
        self._acquisition_image = np.array(array,copy=True)
        if self._result is None:
            self._refresh_base_image(auto_range=True)

    def clear_acquisition_image(self) -> None:
        self._acquisition_image = None
        self._result = None
        self._auxiliary_images.clear()
        self._refresh_image_selector()
        self._clear_overlays()
        self.image_item.clear()
        self.summary_label.setText("No measurement")
        self.summary_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.detail_label.setText("")

    def clear_result(self) -> None:
        self._result = None
        self._auxiliary_images.clear()
        self._refresh_image_selector()
        self._clear_overlays()
        self.summary_label.setText("No localization result")
        self.summary_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.detail_label.setText("")
        self._refresh_base_image(auto_range=True)

    def set_result(self,result: Any) -> None:
        self._result = result
        self._refresh_base_image(auto_range=False)

        diagnostics = dict(getattr(result,"diagnostics",{}) or {})
        expected = _points(getattr(result,"expected_positions_px",None))
        measured = _points(getattr(result,"measured_positions_px",None))
        detected = _points(diagnostics.get("detected_positions_px"))
        matched = _matched_mask(result,diagnostics,expected.shape[1])

        self.expected_item.setData(
            x=expected[0],y=expected[1],
        )
        self.measured_item.setData(
            x=measured[0,matched],y=measured[1,matched],
        )
        self.missing_item.setData(
            x=expected[0,~matched],y=expected[1,~matched],
        )
        self.detected_item.setData(
            x=detected[0],y=detected[1],
        )

        detection_indices = np.asarray(
            diagnostics.get("detection_indices",()),dtype=np.int64,
        )
        used = set(
            int(value) for value in detection_indices[matched]
            if int(value) >= 0
        ) if detection_indices.shape == matched.shape else set()
        unmatched_indices = [
            index for index in range(detected.shape[1]) if index not in used
        ]
        if unmatched_indices:
            unmatched = detected[:,unmatched_indices]
            self.unmatched_item.setData(x=unmatched[0],y=unmatched[1])
        else:
            self.unmatched_item.setData(x=[],y=[])

        rx,ry = _residual_lines(expected,measured,matched)
        self.residual_item.setData(rx,ry)

        total = int(expected.shape[1])
        matched_count = int(np.count_nonzero(matched))
        missing_count = total - matched_count
        extra_count = int(diagnostics.get(
            "unmatched_detection_count",len(unmatched_indices),
        ) or 0)
        rms = _finite_float(diagnostics.get("rms_residual_px"))
        if rms is None:
            residuals = measured[:,matched] - expected[:,matched]
            if residuals.size:
                rms = float(np.sqrt(np.mean(np.sum(residuals*residuals,axis=0))))

        summary = "%d/%d matched" % (matched_count,total)
        if rms is not None:
            summary += " · RMS %.3g px" % rms
        if missing_count:
            summary += " · %d missing" % missing_count
        if extra_count:
            summary += " · %d unmatched detection%s" % (
                extra_count,"" if extra_count == 1 else "s",
            )
        warning = bool(missing_count or extra_count)
        self.summary_label.setText(summary)
        self.summary_label.setStyleSheet(
            "color: %s; font-weight: 600;" % (
                _WARNING_COLOR if warning else _OK_COLOR
            )
        )

        linear = np.asarray(diagnostics.get("affine_linear",()),dtype=np.float64)
        rotation,lattice_angle = _affine_angles(linear)

        affine_details = []
        if rotation is not None:
            affine_details.append("rotation %.3g°" % rotation)
        if lattice_angle is not None:
            affine_details.append("lattice angle %.3g°" % lattice_angle)

        detail_lines = []
        if affine_details:
            detail_lines.append("Affine geometry: " + " · ".join(affine_details))
        if bool(diagnostics.get("reused_exact",False)):
            detail_lines.append("Exact previous localization reused")
        self.detail_label.setText("\n".join(detail_lines))

        self._apply_visibility()
        self.view_box.autoRange()

    def set_message(self,text: str,*,error: bool=False) -> None:
        self.summary_label.setText(str(text))
        self.summary_label.setStyleSheet(
            "color: %s;" % (_ERROR_COLOR if error else _MUTED_COLOR)
        )

    def _clear_overlays(self) -> None:
        for item in (
            self.detected_item,self.expected_item,self.measured_item,
            self.missing_item,self.unmatched_item,
        ):
            item.setData(x=[],y=[])
        self.residual_item.setData([],[])

    def _apply_visibility(self,*_args: Any) -> None:
        localization_mode = (
            str(self.image_mode_combo.currentData() or "Localization")
            == "Localization"
        )
        for checkbox in (
            self.show_detected,self.show_expected,
            self.show_measured,self.show_residuals,
        ):
            checkbox.setEnabled(localization_mode)

        self.detected_item.setVisible(
            localization_mode and self.show_detected.isChecked()
        )
        self.unmatched_item.setVisible(
            localization_mode and self.show_detected.isChecked()
        )
        self.expected_item.setVisible(
            localization_mode and self.show_expected.isChecked()
        )
        self.missing_item.setVisible(
            localization_mode and self.show_expected.isChecked()
        )
        self.measured_item.setVisible(
            localization_mode and self.show_measured.isChecked()
        )
        self.residual_item.setVisible(
            localization_mode and self.show_residuals.isChecked()
        )


class LocalizationWorkbench(QtWidgets.QWidget):
    """Host-neutral measurement + localization inspection workspace.

    The workbench owns one image canvas. Hosts may provide an initial image for
    backward compatibility or feed :class:`ImageMeasurement` instances later
    through :meth:`set_measurement`. Optional source controls only emit user
    intent; they never access detector hardware or files directly.
    """

    sigRunRequested = QtCore.Signal(object)  # complete working parameter dict
    sigCandidateStateChanged = QtCore.Signal(bool)  # currently acceptable
    sigAcquireRequested = QtCore.Signal(str)  # selected detector name
    sigLoadRequested = QtCore.Signal()

    def __init__(
        self,
        *,
        parameters: Mapping[str,Any],
        image: np.ndarray | None=None,
        measurement: ImageMeasurement | None=None,
        context: Mapping[str, Any] | None=None,
        parameter_specs: Mapping[str, ParamSpec] | None=None,
        presentation: str="horizontal",
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        if image is not None and measurement is not None:
            raise ValueError("Provide either image or measurement, not both")
        if measurement is not None and not isinstance(measurement,ImageMeasurement):
            raise TypeError("measurement must be an ImageMeasurement")
        if measurement is None and image is not None:
            measurement = ImageMeasurement(image=image,source="provided")

        presentation = str(presentation or "horizontal").strip().lower()
        if presentation not in ("horizontal","vertical"):
            raise ValueError("presentation must be 'horizontal' or 'vertical'")
        self._presentation = presentation
        self._read_only = False
        self.context = LocalizationWorkbenchContext.from_mapping(context)
        self.parameter_specs = dict(parameter_specs or LOCALIZATION_PARAMS)
        self._measurement: ImageMeasurement | None = None
        self._measurement_revision = 0
        self._candidate = None
        self._candidate_parameters: dict[str, Any] | None = None
        self._committed_result = None
        self._committed_parameters: dict[str, Any] | None = None
        self._candidate_measurement_revision: int | None = None
        self._pending_parameters: dict[str, Any] | None = None
        self._pending_measurement_revision: int | None = None
        self._busy = False
        self._measurement_busy = False

        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.setContentsMargins(0,0,0,0)
        outer_layout.setSpacing(7)

        self.measurement_controls = MeasurementControls(self)
        self.measurement_controls.hide()
        self.measurement_controls.sigAcquireRequested.connect(
            lambda detector:self.sigAcquireRequested.emit(detector)
        )
        self.measurement_controls.sigLoadRequested.connect(
            self.sigLoadRequested.emit
        )
        outer_layout.addWidget(self.measurement_controls)

        layout = (
            QtWidgets.QVBoxLayout()
            if self._presentation == "vertical"
            else QtWidgets.QHBoxLayout()
        )
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(8 if self._presentation == "vertical" else 10)
        outer_layout.addLayout(layout,1)

        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        if self._presentation == "horizontal":
            left_scroll.setMinimumWidth(320)
            left_scroll.setMaximumWidth(430)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        if self._presentation == "vertical":
            left_layout.setContentsMargins(0,0,0,0)
        else:
            left_layout.setContentsMargins(0,0,4,0)
        left_layout.setSpacing(7)
        if self._presentation == "horizontal":
            left_scroll.setWidget(left)

        geometry_specs = {
            key:self.parameter_specs[key]
            for key in _GEOMETRY_PARAM_KEYS
            if key in self.parameter_specs
        }
        option_specs = {
            key:spec
            for key,spec in self.parameter_specs.items()
            if key not in _GEOMETRY_PARAM_KEYS
        }

        # ------------------------------------------------------------------
        # Pattern geometry
        # ------------------------------------------------------------------

        self.geometry_group = QtWidgets.QGroupBox("Pattern Geometry")
        if self._presentation == "vertical":
            geometry_content = QtWidgets.QWidget()
            geometry_grid = QtWidgets.QGridLayout(geometry_content)
            geometry_parent = geometry_content
            geometry_grid.setContentsMargins(8,8,8,8)
        else:
            geometry_content = None
            geometry_grid = QtWidgets.QGridLayout(self.geometry_group)
            geometry_parent = self.geometry_group
            geometry_grid.setContentsMargins(8,10,8,8)
        geometry_grid.setHorizontalSpacing(7)
        geometry_grid.setVerticalSpacing(5)
        geometry_grid.setColumnStretch(2,1)

        self.geometry_form = ParamForm(
            name="localization_geometry",
            definitions=geometry_specs,
            parent=geometry_parent,
            per_row=1,
            editor_width=72 if self._presentation == "vertical" else 105,
            use_subsection=False,
            show_complementary=False,
        )

        row = 0
        # Keep the target readout mounted even when no target guidance is
        # initially available. A modeless host can later make a current CGH
        # available without rebuilding the workbench.
        self.target_label = QtWidgets.QLabel(
            "Target: %s" % self.context.target_type
            if self.context.target_type else "",
            parent=geometry_parent,
        )
        self.target_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.target_label.setVisible(bool(self.context.target_type))
        geometry_grid.addWidget(self.target_label,row,0,1,3)
        row += 1

        geometry_grid.addWidget(QtWidgets.QLabel("Geometry"),row,0)
        geometry_type = self.geometry_form.field("pattern_geometry_type")
        geometry_grid.addWidget(geometry_type.editor,row,1,1,2)
        geometry_type.editor.show()
        row += 1

        self._geometry_value_stacks = {}
        self._geometry_readouts = {}
        self._geometry_manual_widgets = {}

        row = self._add_geometry_source_row(
            geometry_grid,row,
            label="Period",
            source_key="period_prior_mode",
            manual_keys=("expected_period_x_px","expected_period_y_px"),
            manual_labels=("X","Y"),
        )
        row = self._add_geometry_source_row(
            geometry_grid,row,
            label="Stagger",
            source_key="stagger_prior_mode",
            manual_keys=("manual_stagger",),
        )
        row = self._add_geometry_source_row(
            geometry_grid,row,
            label="Lattice size",
            source_key="lattice_size_prior_mode",
            manual_keys=("manual_lattice_count_x","manual_lattice_count_y"),
            manual_labels=("Nx","Ny"),
        )

        # ------------------------------------------------------------------
        # Numerical localization options
        # ------------------------------------------------------------------

        self.options_group = QtWidgets.QGroupBox("Localization Options")
        if self._presentation == "vertical":
            options_content = QtWidgets.QWidget()
            options_grid = QtWidgets.QGridLayout(options_content)
            options_parent = options_content
            options_grid.setContentsMargins(8,8,8,8)
        else:
            options_content = None
            options_grid = QtWidgets.QGridLayout(self.options_group)
            options_parent = self.options_group
            options_grid.setContentsMargins(8,10,8,8)
        options_grid.setHorizontalSpacing(6)
        options_grid.setVerticalSpacing(4)

        self.options_form = ParamForm(
            name="localization_options",
            definitions=option_specs,
            parent=options_parent,
            per_row=1,
            editor_width=80 if self._presentation == "vertical" else 110,
            use_subsection=False,
            show_complementary=False,
        )
        self.options_form.add_to_grid(options_grid,0)
        options_grid.setColumnStretch(options_grid.columnCount(),1)

        if self._presentation == "vertical":
            self._mount_compact_parameter_group(
                self.geometry_group,geometry_content,maximum_height=130,
            )
            self._mount_compact_parameter_group(
                self.options_group,options_content,maximum_height=130,
            )
            parameter_row = QtWidgets.QHBoxLayout()
            parameter_row.setContentsMargins(0,0,0,0)
            parameter_row.setSpacing(10)
            parameter_row.addWidget(self.geometry_group,1)
            parameter_row.addWidget(self.options_group,1)
            left_layout.addLayout(parameter_row)
        else:
            left_layout.addWidget(self.geometry_group)
            left_layout.addWidget(self.options_group)

        self._set_parameter_values(parameters)
        self.geometry_form.sigValueChanged.connect(self._on_parameter_changed)
        self.options_form.sigValueChanged.connect(self._on_parameter_changed)

        self.state_label = QtWidgets.QLabel("")
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("color: %s;" % _MUTED_COLOR)

        self.run_button = QtWidgets.QPushButton("Run Localization")
        self.run_button.setMinimumHeight(23)
        self.run_button.setMinimumWidth(160)
        self.run_button.clicked.connect(self.request_run)

        self.result_view = LocalizationResultView()
        self._action_row = None
        self._action_insert_index = 1
        self._lower_layout: QtWidgets.QVBoxLayout | None = None

        if self._presentation == "horizontal":
            left_layout.addStretch(1)
            left_layout.addWidget(self.state_label)
            left_layout.addWidget(self.run_button)
            self.result_view.setMinimumSize(520,430)
            layout.addWidget(left_scroll,0)
            layout.addWidget(self.result_view,1)
        else:
            self.result_view.setMinimumSize(420,300)
            self.result_view.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Expanding,
            )

            action_widget = QtWidgets.QWidget()
            self._action_row = QtWidgets.QHBoxLayout(action_widget)
            self._action_row.setContentsMargins(0,0,0,0)
            self._action_row.setSpacing(8)
            self._action_row.addWidget(self.run_button,1)
            self._action_row.addWidget(self.state_label,1)

            lower = QtWidgets.QWidget()
            lower_layout = QtWidgets.QVBoxLayout(lower)
            lower_layout.setContentsMargins(0,0,0,0)
            lower_layout.setSpacing(7)
            lower_layout.addWidget(left,1)
            lower_layout.addWidget(action_widget,0)
            self._lower_layout = lower_layout
            lower.setMinimumHeight(0)
            lower.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Minimum,
            )

            self.vertical_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical,self)
            self.vertical_splitter.setChildrenCollapsible(True)
            self.vertical_splitter.addWidget(self.result_view)
            self.vertical_splitter.addWidget(lower)
            self.vertical_splitter.setCollapsible(1,True)
            self.vertical_splitter.setStretchFactor(0,1)
            self.vertical_splitter.setStretchFactor(1,0)
            # Open with the visualization dominant while leaving the controls
            # immediately usable. The matching feedback splitter uses the same
            # lower-pane default.
            self.vertical_splitter.setSizes([700,150])
            layout.addWidget(self.vertical_splitter,1)

        if measurement is not None:
            self.set_measurement(measurement)
        else:
            self.result_view.clear_acquisition_image()
            self.state_label.setText("Acquire or load an image before localization.")
            self._refresh_interaction_state()

        self._refresh_geometry_display()
        self._emit_candidate_state()

    @property
    def measurement(self) -> ImageMeasurement | None:
        return self._measurement

    @property
    def candidate(self):
        return self._candidate

    @property
    def candidate_parameters(self) -> Mapping[str, Any] | None:
        return (
            None if self._candidate_parameters is None
            else dict(self._candidate_parameters)
        )

    @property
    def candidate_is_current(self) -> bool:
        return (
            self._measurement is not None
            and self._candidate is not None
            and self._candidate_parameters is not None
            and self._candidate_measurement_revision == self._measurement_revision
            and self._candidate_parameters == self.parameters()
            and not self._busy
            and not self._measurement_busy
        )

    @staticmethod
    def _mount_compact_parameter_group(
        group: QtWidgets.QGroupBox,
        content: QtWidgets.QWidget,
        *,
        maximum_height: int,
    ) -> None:
        """Mount one fixed parameter frame with vertical scrolling only."""
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setWidget(content)
        scroll.setMinimumHeight(min(60,int(maximum_height)))
        # Ignore content-derived vertical hints so the outer splitter can
        # shrink this pane below the forms' preferred height. The widgets
        # still expand normally when the user gives the pane more space.
        scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Ignored,
        )
        group.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Ignored,
        )
        scroll.setStyleSheet(
            "QScrollArea {"
            "  border: none;"
            "  background: transparent;"
            "}"
            "QScrollArea > QWidget > QWidget {"
            "  background: transparent;"
            "}"
        )
        scroll.viewport().setAutoFillBackground(False)

        group_layout = QtWidgets.QVBoxLayout(group)
        group_layout.setContentsMargins(0,0,0,0)
        group_layout.addWidget(scroll)

    def set_lower_pane_height(self,height: int) -> None:
        """Set the vertical presentation lower pane to an exact height."""
        splitter = getattr(self,"vertical_splitter",None)
        if splitter is None:
            return
        sizes = splitter.sizes()
        total = sum(int(value) for value in sizes)
        if total <= 0:
            total = int(splitter.height())
        if total <= 0:
            return
        lower = max(0,min(int(height),total))
        splitter.setSizes([max(0,total-lower),lower])

    def add_result_widget(self,widget: QtWidgets.QWidget) -> None:
        """Add host-specific information beside localization diagnostics."""
        self.result_view.add_result_widget(widget)

    def add_lower_widget(self,widget: QtWidgets.QWidget) -> None:
        """Add host-specific controls to the collapsible lower work area."""
        if self._lower_layout is None:
            raise RuntimeError(
                "Lower widgets are only supported in vertical presentation"
            )
        # Keep the action row as the final item.
        index = max(0,self._lower_layout.count() - 1)
        self._lower_layout.insertWidget(index,widget,0)

    def add_action_widget(self,widget: QtWidgets.QWidget) -> None:
        """Place a host-specific primary action beside Run Localization."""
        if self._action_row is None:
            raise RuntimeError(
                "Additional actions are only supported in vertical presentation"
            )
        self._action_row.insertWidget(self._action_insert_index,widget,1)
        self._action_insert_index += 1

    def configure_measurement_sources(
        self,
        detectors: Sequence[str]=(),
        *,
        current_detector: str | None=None,
        allow_load: bool=True,
        visible: bool=True,
    ) -> None:
        """Configure the optional host-driven Acquire/Load source row."""
        self.measurement_controls.set_detectors(
            detectors,current_detector=current_detector,
        )
        self.measurement_controls.set_load_available(bool(allow_load))
        self.measurement_controls.setVisible(bool(visible))
        self._refresh_interaction_state()

    def set_context(
        self,context: Mapping[str, Any] | None=None,
    ) -> None:
        """Replace read-only localization guidance exposed by the host.

        Context changes never mutate measurement or candidate state. They only
        update target-derived readouts/availability; the host remains
        responsible for deciding whether a previously computed candidate is
        still semantically usable.
        """
        self.context = LocalizationWorkbenchContext.from_mapping(context)
        if self.target_label is not None:
            self.target_label.setText(
                "Target: %s" % self.context.target_type
                if self.context.target_type else ""
            )
            self.target_label.setVisible(bool(self.context.target_type))
        self._refresh_geometry_display()

    def set_parameters(
        self,parameters: Mapping[str,Any],*,invalidate_candidate: bool=True,
    ) -> None:
        """Synchronize editable localization parameters from a host workflow."""
        before = self.parameters()
        self._set_parameter_values(parameters)
        changed = before != self.parameters()
        self._refresh_geometry_display()
        if changed and invalidate_candidate and self._candidate is not None:
            self._mark_stale()
        self._emit_candidate_state()

    def set_committed_result(
        self,result: Any,parameters: Mapping[str, Any] | None=None,
    ) -> None:
        """Display an already accepted backend localization on this measurement.

        A committed result is inspection state, not an acceptable candidate; the
        containing workflow therefore does not need to fake a new localization
        run merely to restore overlays after reopening or refreshing a window.
        """
        if self._measurement is None:
            raise RuntimeError("Cannot display localization without a measurement")
        self._candidate = None
        self._candidate_parameters = None
        self._candidate_measurement_revision = None
        self._committed_result = result
        self._committed_parameters = dict(
            parameters if parameters is not None
            else getattr(result,"parameters",{}) or {}
        )
        if self._committed_parameters:
            self.set_parameters(
                self._committed_parameters,invalidate_candidate=False,
            )
        self.result_view.set_result(result)
        self.state_label.setText("Accepted localization is current.")
        self.state_label.setStyleSheet("color: %s;" % _OK_COLOR)
        self._refresh_geometry_display()
        self._emit_candidate_state()

    def clear_localization_result(self) -> None:
        """Clear candidate/committed overlays while preserving the measurement."""
        self._candidate = None
        self._candidate_parameters = None
        self._candidate_measurement_revision = None
        self._committed_result = None
        self._committed_parameters = None
        self.result_view.clear_result()
        if self._measurement is not None and not self._busy:
            self.state_label.setText("Measurement ready — run localization.")
            self.state_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self._emit_candidate_state()

    def set_measurement(self,measurement: ImageMeasurement) -> None:
        """Display a new immutable measurement and invalidate old localization."""
        if not isinstance(measurement,ImageMeasurement):
            raise TypeError("measurement must be an ImageMeasurement")

        self._measurement = measurement
        self._measurement_revision += 1
        self._candidate = None
        self._candidate_parameters = None
        self._candidate_measurement_revision = None
        self._committed_result = None
        self._committed_parameters = None

        self.result_view.clear_result()
        self.result_view.set_acquisition_image(measurement.image)
        self.measurement_controls.set_measurement(measurement)
        self._measurement_busy = False

        if not self._busy:
            self.state_label.setText("Measurement ready — run localization.")
            self.state_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self._refresh_geometry_display()
        self._refresh_interaction_state()
        self._emit_candidate_state()

    def clear_measurement(self) -> None:
        """Remove the current measurement and any localization candidate."""
        self._measurement = None
        self._measurement_revision += 1
        self._candidate = None
        self._candidate_parameters = None
        self._candidate_measurement_revision = None
        self._committed_result = None
        self._committed_parameters = None
        self._measurement_busy = False
        self.result_view.clear_acquisition_image()
        self.measurement_controls.set_status("")
        if not self._busy:
            self.state_label.setText("Acquire or load an image before localization.")
            self.state_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self._refresh_geometry_display()
        self._refresh_interaction_state()
        self._emit_candidate_state()

    def set_read_only(self,read_only: bool) -> None:
        """Disable source/parameter actions while retaining image inspection."""
        self._read_only = bool(read_only)
        self.geometry_group.setEnabled(not self._read_only)
        self.options_group.setEnabled(not self._read_only)
        self._refresh_interaction_state()

    def set_measurement_busy(self,busy: bool,text: str="") -> None:
        """Update host-controlled acquisition/loading busy state."""
        self._measurement_busy = bool(busy)
        if text:
            self.measurement_controls.set_status(text)
        if self._measurement_busy and not self._busy:
            self.state_label.setText(text or "Waiting for measurement…")
            self.state_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        elif not self._measurement_busy and not self._busy:
            if self._measurement is None:
                self.state_label.setText(
                    "Acquire or load an image before localization."
                )
            elif self._candidate is None:
                self.state_label.setText("Measurement ready — run localization.")
            self.state_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self._refresh_interaction_state()
        self._emit_candidate_state()

    def set_measurement_error(self,error: Any) -> None:
        """Finish a host measurement request with an error."""
        self._measurement_busy = False
        self.measurement_controls.set_status(str(error),error=True)
        if not self._busy:
            self.state_label.setText("Measurement failed: %s" % str(error))
            self.state_label.setStyleSheet("color: %s;" % _ERROR_COLOR)
        self._refresh_interaction_state()
        self._emit_candidate_state()

    def parameters(self) -> dict[str, Any]:
        values = self.geometry_form.values()
        values.update(self.options_form.values())
        return values

    def request_run(self,*_args: Any) -> None:
        if (
            self._read_only or self._busy or self._measurement_busy
            or self._measurement is None
        ):
            return
        values = self.parameters()
        self._pending_parameters = dict(values)
        self._pending_measurement_revision = self._measurement_revision
        self.set_busy(True)
        self.sigRunRequested.emit(dict(values))

    def set_busy(self,busy: bool) -> None:
        self._busy = bool(busy)
        self.run_button.setText(
            "Localizing..." if self._busy else "Run Localization"
        )
        if self._busy:
            self.state_label.setText("Localization running…")
            self.state_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self._refresh_interaction_state()
        self._emit_candidate_state()

    def set_result(
        self,result: Any,parameters: Mapping[str, Any] | None=None,
    ) -> None:
        source_revision = (
            self._measurement_revision
            if self._pending_measurement_revision is None
            else self._pending_measurement_revision
        )
        used = dict(
            parameters
            if parameters is not None
            else (self._pending_parameters or self.parameters())
        )
        self._pending_parameters = None
        self._pending_measurement_revision = None

        if self._measurement is None or source_revision != self._measurement_revision:
            self.set_busy(False)
            self.state_label.setText(
                "Localization result discarded because the measurement changed."
            )
            self.state_label.setStyleSheet("color: %s;" % _WARNING_COLOR)
            self._emit_candidate_state()
            return

        self._candidate = result
        self._candidate_parameters = used
        self._committed_result = None
        self._committed_parameters = None
        self._candidate_measurement_revision = source_revision
        self.set_busy(False)
        self.result_view.set_result(result)
        self._refresh_geometry_display()

        if self.candidate_is_current:
            self.state_label.setText(
                "Candidate is current. Inspect it, then accept or adjust parameters."
            )
            self.state_label.setStyleSheet("color: %s;" % _OK_COLOR)
        else:
            self._mark_stale()
        self._emit_candidate_state()

    def set_error(self,error: Any) -> None:
        source_revision = self._pending_measurement_revision
        self._pending_parameters = None
        self._pending_measurement_revision = None
        self.set_busy(False)

        if (
            source_revision is not None
            and source_revision != self._measurement_revision
        ):
            self.state_label.setText(
                "Previous localization request ended after the measurement changed."
            )
            self.state_label.setStyleSheet("color: %s;" % _WARNING_COLOR)
            self._emit_candidate_state()
            return

        self.state_label.setText("Localization failed: %s" % str(error))
        self.state_label.setStyleSheet("color: %s;" % _ERROR_COLOR)
        # Keep the last successful candidate visible. It becomes acceptable only
        # if its parameters and measurement still match the current workspace.
        self.result_view.set_message(
            "Localization failed: %s" % str(error),error=True,
        )
        self._emit_candidate_state()

    def _refresh_interaction_state(self) -> None:
        disabled = self._busy or self._measurement_busy
        self.run_button.setEnabled(
            self._measurement is not None
            and not disabled
            and not self._read_only
        )
        self.measurement_controls.set_busy(disabled)
        self.measurement_controls.setEnabled(not self._read_only)

    def _set_parameter_values(self,parameters: Mapping[str,Any]) -> None:
        values = dict(parameters or {})
        self.geometry_form.set_values(values,emit=False)
        self.options_form.set_values(values,emit=False)

    def _on_parameter_changed(self,key: str,_value: Any) -> None:
        if key in (
            "pattern_geometry_type",
            "period_prior_mode",
            "stagger_prior_mode",
            "lattice_size_prior_mode",
        ):
            self._refresh_geometry_display()
        if self._candidate is not None:
            self._mark_stale()
        self._emit_candidate_state()

    def _mark_stale(self) -> None:
        self.state_label.setText(
            "Parameters changed — run localization again before accepting."
        )
        self.state_label.setStyleSheet("color: %s;" % _WARNING_COLOR)

    def _add_geometry_source_row(
        self,
        layout: QtWidgets.QGridLayout,
        row: int,
        *,
        label: str,
        source_key: str,
        manual_keys: tuple[str, ...],
        manual_labels: tuple[str, ...]=(),
    ) -> int:
        layout.addWidget(QtWidgets.QLabel(label),row,0)

        source_field = self.geometry_form.field(source_key)
        layout.addWidget(source_field.editor,row,1)
        source_field.editor.show()

        stack = QtWidgets.QStackedWidget()
        stack.setMinimumHeight(26)
        stack.setMinimumWidth(0)
        stack.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )

        readout = QtWidgets.QLineEdit()
        readout.setReadOnly(True)
        readout.setFocusPolicy(QtCore.Qt.NoFocus)
        readout.setPlaceholderText("—")
        readout.setMinimumWidth(0)
        readout.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        stack.addWidget(readout)

        manual = QtWidgets.QWidget()
        manual_layout = QtWidgets.QGridLayout(manual)
        manual_layout.setContentsMargins(0,0,0,0)
        manual_layout.setHorizontalSpacing(5)
        manual_layout.setVerticalSpacing(3)

        for index,key in enumerate(manual_keys):
            editor = self.geometry_form.field(key).editor
            editor.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Preferred,
            )
            editor.setMinimumWidth(0)

            if index < len(manual_labels):
                manual_layout.addWidget(
                    QtWidgets.QLabel(manual_labels[index]),
                    index,
                    0,
                )
                manual_layout.addWidget(editor,index,1)
            else:
                # Single unlabeled manual value, e.g. manual_stagger.
                manual_layout.addWidget(editor,index,0,1,2)
            editor.show()

        manual_layout.setColumnStretch(1,1)

        stack.addWidget(manual)

        layout.addWidget(stack,row,2)
        self._geometry_value_stacks[source_key] = stack
        self._geometry_readouts[source_key] = readout
        self._geometry_manual_widgets[source_key] = manual
        return row + 1

    def _refresh_geometry_display(self) -> None:
        self._refresh_geometry_source(
            "period_prior_mode",
            self._period_display_value,
        )
        self._refresh_geometry_source(
            "stagger_prior_mode",
            self._stagger_display_value,
        )
        self._refresh_geometry_source(
            "lattice_size_prior_mode",
            self._lattice_size_display_value,
        )

    def _refresh_geometry_source(self,source_key: str,value_getter) -> None:
        try:
            mode = str(self.geometry_form.field(source_key).value())
        except KeyError:
            return

        stack = self._geometry_value_stacks.get(source_key)
        readout = self._geometry_readouts.get(source_key)
        manual = self._geometry_manual_widgets.get(source_key)
        if stack is None or readout is None:
            return

        if mode == "manual":
            stack.setCurrentIndex(1)
            if manual is not None:
                stack.setMinimumHeight(
                    max(26,manual.sizeHint().height())
                )
                return

        stack.setCurrentIndex(0)
        stack.setMinimumHeight(26)
        text,tooltip = value_getter(mode)
        readout.setText(text)
        readout.setToolTip(tooltip)

    def _period_display_value(self,mode: str):
        if mode == "target":
            period = self.context.target_expected_period_px
            if period is None:
                return (
                    "Unavailable",
                    "Target camera-space period is unavailable. A valid "
                    "SLM calibration is required to convert the target period.",
                )
            return (
                "%.4g × %.4g px" % period,
                "Camera-space period derived from the current target geometry "
                "and active SLM calibration.",
            )

        result = self._candidate
        if result is None:
            return "","Auto: measured from the image after localization."
        px = _finite_float(getattr(result,"period_x_px",None))
        py = _finite_float(getattr(result,"period_y_px",None))
        if px is None or py is None:
            return "","Auto period unavailable in the current result."
        return (
            "%.4g × %.4g px" % (px,py),
            "Measured by the most recent localization result.",
        )

    def _stagger_display_value(self,mode: str):
        if mode == "target":
            value = self.context.target_stagger
            if value is None:
                return "Unavailable","Current target does not expose stagger."
            return (
                "%.4g" % value,
                "Structural stagger exposed by the current target geometry.",
            )

        result = self._candidate
        if result is None:
            return "","Auto: inferred from the image after localization."
        diagnostics = dict(getattr(result,"diagnostics",{}) or {})
        value = _finite_float(diagnostics.get("resolved_stagger"))
        if value is None:
            return "","Auto stagger unavailable in the current result."
        return "%.4g" % value,"Resolved by the most recent localization result."

    def _lattice_size_display_value(self,mode: str):
        if mode == "target":
            count = self.context.target_lattice_count
            if count is None:
                return "Unavailable","Current target does not expose lattice dimensions."
            return (
                "%d × %d" % count,
                "Finite lattice dimensions exposed by the current target geometry.",
            )

        result = self._candidate
        if result is None:
            return "","Auto: inferred from the finite lattice after localization."
        diagnostics = dict(getattr(result,"diagnostics",{}) or {})
        count = _result_lattice_count(diagnostics)
        if count is None:
            return "","Auto lattice size unavailable in the current result."
        return "%d × %d" % count,"Resolved by the most recent localization result."

    def _emit_candidate_state(self) -> None:
        self.sigCandidateStateChanged.emit(self.candidate_is_current)


class LocalizationDialog(QtWidgets.QDialog):
    """Thin modal wrapper around :class:`LocalizationWorkbench`.

    Existing hosts may still pass ``image=...``. New workflows can instead
    supply or replace generic :class:`ImageMeasurement` objects and optionally
    expose the host-driven Acquire/Load source row.
    """

    sigRunRequested = QtCore.Signal(object)
    sigAcquireRequested = QtCore.Signal(str)
    sigLoadRequested = QtCore.Signal()

    def __init__(
        self,
        *,
        parameters: Mapping[str,Any],
        image: np.ndarray | None=None,
        measurement: ImageMeasurement | None=None,
        context: Mapping[str, Any] | None=None,
        parameter_specs: Mapping[str, ParamSpec] | None=None,
        detectors: Sequence[str] | None=None,
        current_detector: str | None=None,
        allow_load: bool=False,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Localization")
        self.resize(1050,620)

        layout = QtWidgets.QVBoxLayout(self)
        self.workbench = LocalizationWorkbench(
            image=image,
            measurement=measurement,
            parameters=parameters,
            context=context,
            parameter_specs=parameter_specs,
            parent=self,
        )
        self.workbench.sigRunRequested.connect(
            lambda parameters:self.sigRunRequested.emit(parameters)
        )
        self.workbench.sigAcquireRequested.connect(
            lambda detector:self.sigAcquireRequested.emit(detector)
        )
        self.workbench.sigLoadRequested.connect(self.sigLoadRequested.emit)
        if detectors is not None or allow_load:
            self.workbench.configure_measurement_sources(
                () if detectors is None else detectors,
                current_detector=current_detector,
                allow_load=allow_load,
                visible=True,
            )
        layout.addWidget(self.workbench,1)

        buttons = QtWidgets.QDialogButtonBox()
        self.accept_button = buttons.addButton(
            "Accept Localization Result",QtWidgets.QDialogButtonBox.AcceptRole,
        )
        self.cancel_button = buttons.addButton(
            "Cancel",QtWidgets.QDialogButtonBox.RejectRole,
        )
        self.accept_button.setEnabled(False)
        buttons.accepted.connect(self._accept_if_current)
        buttons.rejected.connect(self.reject)
        self.workbench.sigCandidateStateChanged.connect(
            self.accept_button.setEnabled,
        )
        layout.addWidget(buttons)

    @property
    def measurement(self) -> ImageMeasurement | None:
        return self.workbench.measurement

    @property
    def candidate(self):
        return self.workbench.candidate

    @property
    def accepted_parameters(self) -> Mapping[str, Any] | None:
        if not self.workbench.candidate_is_current:
            return None
        return self.workbench.candidate_parameters

    def configure_measurement_sources(
        self,
        detectors: Sequence[str]=(),
        *,
        current_detector: str | None=None,
        allow_load: bool=True,
        visible: bool=True,
    ) -> None:
        self.workbench.configure_measurement_sources(
            detectors,
            current_detector=current_detector,
            allow_load=allow_load,
            visible=visible,
        )

    def set_context(
        self,context: Mapping[str, Any] | None=None,
    ) -> None:
        self.workbench.set_context(context)

    def set_parameters(
        self,parameters: Mapping[str,Any],*,invalidate_candidate: bool=True,
    ) -> None:
        self.workbench.set_parameters(
            parameters,invalidate_candidate=invalidate_candidate,
        )

    def set_committed_result(
        self,result: Any,parameters: Mapping[str, Any] | None=None,
    ) -> None:
        self.workbench.set_committed_result(result,parameters)

    def clear_localization_result(self) -> None:
        self.workbench.clear_localization_result()

    def set_measurement(self,measurement: ImageMeasurement) -> None:
        self.workbench.set_measurement(measurement)

    def clear_measurement(self) -> None:
        self.workbench.clear_measurement()

    def set_measurement_busy(self,busy: bool,text: str="") -> None:
        self.workbench.set_measurement_busy(busy,text)

    def set_measurement_error(self,error: Any) -> None:
        self.workbench.set_measurement_error(error)

    def set_busy(self,busy: bool) -> None:
        self.workbench.set_busy(busy)

    def set_result(
        self,result: Any,parameters: Mapping[str, Any] | None=None,
    ) -> None:
        self.workbench.set_result(result,parameters)

    def set_error(self,error: Any) -> None:
        self.workbench.set_error(error)

    def _accept_if_current(self) -> None:
        if self.workbench.candidate_is_current:
            self.accept()


def _points(value: Any) -> np.ndarray:
    if value is None:
        return np.empty((2,0),dtype=np.float64)
    array = np.asarray(value,dtype=np.float64)
    if array.size == 0:
        return np.empty((2,0),dtype=np.float64)
    if array.ndim != 2 or array.shape[0] != 2:
        raise ValueError("Localization point arrays must have shape (2, N)")
    return array


def _matched_mask(result: Any,diagnostics: Mapping[str,Any],count: int) -> np.ndarray:
    value = diagnostics.get("matched_mask")
    if value is None:
        return np.ones(count,dtype=bool)
    mask = np.asarray(value,dtype=bool)
    if mask.shape != (count,):
        raise ValueError("Localization matched mask does not match point count")
    return mask


def _residual_lines(expected: np.ndarray,measured: np.ndarray,matched: np.ndarray):
    xs = []
    ys = []
    for index in np.flatnonzero(matched):
        xs.extend((expected[0,index],measured[0,index],np.nan))
        ys.extend((expected[1,index],measured[1,index],np.nan))
    return np.asarray(xs,dtype=np.float64),np.asarray(ys,dtype=np.float64)


def _result_lattice_count(diagnostics: Mapping[str,Any]):
    value = diagnostics.get("resolved_lattice_count")
    if value is not None:
        try:
            count = tuple(int(item) for item in value)
        except Exception:
            count = ()
        if len(count) == 2:
            return count

    x = diagnostics.get("lattice_count_x")
    y = diagnostics.get("lattice_count_y")
    try:
        if x is not None and y is not None:
            return int(x),int(y)
    except Exception:
        pass
    return None


def _affine_angles(linear: np.ndarray):
    if linear.shape != (2,2) or not np.all(np.isfinite(linear)):
        return None,None
    a = linear[:,0]
    b = linear[:,1]
    rotation = float(np.degrees(np.arctan2(a[1],a[0])))
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 0:
        return rotation,None
    angle = float(np.degrees(np.arccos(np.clip(np.dot(a,b)/denom,-1.0,1.0))))
    return rotation,angle


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except Exception:
        return None
    return result if np.isfinite(result) else None
