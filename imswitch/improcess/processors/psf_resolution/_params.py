"""Parameter fields shared by ``psf-resolution`` and ``psf-bead-select``.

The widgets are built from the declared :class:`ParamField` spec, so the
form and ``param_spec()`` cannot drift apart (the widget/spec contract test
holds every built-in to that).
"""

from __future__ import annotations

from typing import Iterable

from qtpy import QtCore, QtWidgets

from imswitch.improcess.analysis.bead_psf import Selection
from imswitch.improcess.model.param_spec import ParamField

#: The group of the pixel-size / z-step overrides, collapsed in the panel:
#: the calibration normally comes with the data.
CALIBRATION_GROUP = "Calibration override"

_RANGE_HELP = "In the data's length unit (nm when calibrated, else px); 0 = automatic (median +- k MAD)."

SELECTION_FIELDS: tuple[ParamField, ...] = (
    ParamField("fwhm_lat_min", "float", 0.0, label="Lateral FWHM min", help=_RANGE_HELP,
               min=0.0, max=1e6, decimals=2, group="Selection"),
    ParamField("fwhm_lat_max", "float", 0.0, label="Lateral FWHM max", help=_RANGE_HELP,
               min=0.0, max=1e6, decimals=2, group="Selection"),
    ParamField("fwhm_z_min", "float", 0.0, label="Axial FWHM min", help=_RANGE_HELP + " 3-D only.",
               min=0.0, max=1e6, decimals=2, group="Selection"),
    ParamField("fwhm_z_max", "float", 0.0, label="Axial FWHM max", help=_RANGE_HELP + " 3-D only.",
               min=0.0, max=1e6, decimals=2, group="Selection"),
    ParamField("range_mad", "float", 3.0, label="Automatic range (k x MAD)",
               help="Half-width of the automatic ranges around the median, in MADs.",
               min=0.5, max=20.0, decimals=1, group="Selection"),
    ParamField("max_ellipticity", "float", 0.0, label="Max ellipticity",
               help="Largest accepted ratio of the lateral FWHMs; rejects unresolved doublets. "
                    "0 = automatic: median + k MAD of the beads, at least 1.3, so an astigmatic "
                    "system keeps its (uniformly elliptical) beads.",
               min=0.0, max=10.0, decimals=2, group="Selection"),
    ParamField("min_r2", "float", 0.8, label="Min fit R²",
               help="Gaussian fit quality; aberrated beads fit a Gaussian less well.",
               min=0.0, max=1.0, decimals=2, group="Selection"),
)


def selection_from_params(params: dict) -> Selection:
    """Build a :class:`Selection`; a range with both bounds at 0 is automatic."""

    def span(lo_key: str, hi_key: str):
        lo = float(params.get(lo_key, 0.0) or 0.0)
        hi = float(params.get(hi_key, 0.0) or 0.0)
        if lo <= 0 and hi <= 0:
            return None
        return (lo, hi if hi > 0 else float("inf"))

    ellipticity = float(params.get("max_ellipticity", 0.0) or 0.0)
    return Selection(
        fwhm_lat_range=span("fwhm_lat_min", "fwhm_lat_max"),
        fwhm_z_range=span("fwhm_z_min", "fwhm_z_max"),
        range_mad=float(params.get("range_mad", 3.0)),
        max_ellipticity=ellipticity if ellipticity > 0 else None,
        min_r2=float(params.get("min_r2", 0.8)),
    )


class CollapsibleSection(QtWidgets.QWidget):
    """A titled section whose body a header button shows and hides."""

    def __init__(self, title: str, collapsed: bool = False, parent=None):
        super().__init__(parent)
        self._title = title
        self.toggle = QtWidgets.QToolButton(self)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(not collapsed)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.body = QtWidgets.QWidget(self)
        self.form = QtWidgets.QFormLayout(self.body)
        self.form.setContentsMargins(12, 0, 0, 4)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toggle)
        layout.addWidget(self.body)
        self.toggle.toggled.connect(self.setExpanded)
        self.setExpanded(not collapsed)

    def setExpanded(self, expanded: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(bool(expanded))
        self.toggle.blockSignals(False)
        self.toggle.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.toggle.setText(self._title)
        self.body.setVisible(bool(expanded))

    def isExpanded(self) -> bool:
        return self.toggle.isChecked()

    def setTitle(self, title: str) -> None:
        self._title = title
        self.toggle.setText(title)


def build_form(parent: QtWidgets.QWidget, fields: Iterable[ParamField],
               collapsed: Iterable[str] = ("Advanced",)) -> QtWidgets.QWidget:
    """A form with one control per field, in one collapsible section per
    ``field.group``.

    Advanced fields go into a trailing "Advanced" section; sections named in
    ``collapsed`` start closed. The returned widget has ``get_values()``,
    ``set_values(dict)``, a ``controls`` dict and a ``sections`` dict.
    """
    fields = list(fields)
    collapsed = set(collapsed)
    widget = QtWidgets.QWidget(parent)
    outer = QtWidgets.QVBoxLayout(widget)
    outer.setContentsMargins(0, 0, 0, 0)
    controls: dict[str, tuple[ParamField, QtWidgets.QWidget]] = {}
    sections: dict[str, CollapsibleSection] = {}

    def layout_for(name: str) -> QtWidgets.QFormLayout:
        if name not in sections:
            section = CollapsibleSection(name or "Parameters", collapsed=name in collapsed, parent=widget)
            sections[name] = section
            outer.addWidget(section)
        return sections[name].form

    ordered = [f for f in fields if not f.advanced] + [f for f in fields if f.advanced]
    for f in ordered:
        control = _control_for(f)
        if f.help:
            control.setToolTip(f.help)
        layout_for("Advanced" if f.advanced else f.group).addRow(f.label or f.key, control)
        controls[f.key] = (f, control)
    outer.addStretch()

    def get_values() -> dict:
        return {key: _read(f, control) for key, (f, control) in controls.items()}

    def set_values(values: dict) -> None:
        for key, value in (values or {}).items():
            if key in controls:
                _write(*controls[key], value)

    widget.get_values = get_values
    widget.set_values = set_values
    widget.controls = {key: control for key, (_f, control) in controls.items()}
    widget.sections = sections
    return widget


def _control_for(f: ParamField) -> QtWidgets.QWidget:
    if f.type == "bool":
        control = QtWidgets.QCheckBox()
        control.setChecked(bool(f.default))
    elif f.type == "select":
        control = QtWidgets.QComboBox()
        labels = OPTION_LABELS.get(f.key, {})
        for option in f.options:
            control.addItem(labels.get(option, str(option)), option)
        control.setCurrentIndex(list(f.options).index(f.default))
    elif f.type == "int":
        control = QtWidgets.QSpinBox()
        control.setRange(int(f.min if f.min is not None else -2**31), int(f.max if f.max is not None else 2**31 - 1))
        control.setValue(int(f.default))
        if f.suffix:
            control.setSuffix(f" {f.suffix}")
    elif f.type == "float":
        control = QtWidgets.QDoubleSpinBox()
        control.setDecimals(f.decimals if f.decimals is not None else 3)
        control.setRange(float(f.min if f.min is not None else -1e12), float(f.max if f.max is not None else 1e12))
        if f.step is not None:
            control.setSingleStep(float(f.step))
        control.setValue(float(f.default))
        if f.suffix:
            control.setSuffix(f" {f.suffix}")
        if f.min == 0.0 and f.default == 0.0 and "0 = " in (f.help or ""):
            control.setSpecialValueText(_zero_meaning(f.help))
    else:
        raise ValueError(f"{f.key}: unsupported field type {f.type!r} for this form")
    return control


def _zero_meaning(help_text: str) -> str:
    """What a 0 stands for, from a help text saying ``0 = <meaning>.``"""
    meaning = help_text.split("0 = ", 1)[1]
    for stop in (".", ":", "(", ";", ","):
        meaning = meaning.split(stop, 1)[0]
    meaning = meaning.strip()
    return meaning if 0 < len(meaning) <= 24 else "auto"


def _read(f: ParamField, control):
    if f.type == "bool":
        return bool(control.isChecked())
    if f.type == "select":
        return control.currentData()
    if f.type == "int":
        return int(control.value())
    return float(control.value())


def _write(f: ParamField, control, value) -> None:
    if f.type == "bool":
        control.setChecked(bool(value))
    elif f.type == "select":
        index = control.findData(value)
        if index >= 0:
            control.setCurrentIndex(index)
    else:
        control.setValue(value)


#: Display labels for select options (the stored values stay the stable slugs).
OPTION_LABELS: dict[str, dict[str, str]] = {
    "source": {"auto": "Auto-detect beads", "rois": "ROI Manager", "full_image": "Whole image (one PSF)"},
    "bead_labeling": {"volume": "Volume labelled", "shell": "Shell labelled"},
    "fit_mode_3d": {"separable": "Separable (axial + lateral)", "full": "Full 3-D Gaussian"},
    "illumination": {"widefield": "Widefield / confocal", "light_sheet": "Light sheet (fit sheet)"},
}
