"""Parameter fields shared by ``psf-resolution`` and ``psf-bead-select``.

The widgets are built from the declared :class:`ParamField` spec, so the
form and ``param_spec()`` cannot drift apart (the widget/spec contract test
holds every built-in to that).
"""

from __future__ import annotations

from typing import Iterable

from qtpy import QtWidgets

from imswitch.improcess.analysis.bead_psf import Selection
from imswitch.improcess.model.param_spec import ParamField

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
               help="Half-width of the automatic FWHM range around the median, in MADs.",
               min=0.5, max=20.0, decimals=1, group="Selection"),
    ParamField("max_ellipticity", "float", 1.3, label="Max ellipticity",
               help="Largest accepted ratio of the lateral FWHMs; rejects unresolved doublets "
                    "and astigmatic failures.",
               min=1.0, max=10.0, decimals=2, group="Selection"),
    ParamField("min_r2", "float", 0.9, label="Min fit R²", min=0.0, max=1.0, decimals=2,
               group="Selection"),
)


def selection_from_params(params: dict) -> Selection:
    """Build a :class:`Selection`; a range with both bounds at 0 is automatic."""

    def span(lo_key: str, hi_key: str):
        lo = float(params.get(lo_key, 0.0) or 0.0)
        hi = float(params.get(hi_key, 0.0) or 0.0)
        if lo <= 0 and hi <= 0:
            return None
        return (lo, hi if hi > 0 else float("inf"))

    return Selection(
        fwhm_lat_range=span("fwhm_lat_min", "fwhm_lat_max"),
        fwhm_z_range=span("fwhm_z_min", "fwhm_z_max"),
        range_mad=float(params.get("range_mad", 3.0)),
        max_ellipticity=float(params.get("max_ellipticity", 1.3)),
        min_r2=float(params.get("min_r2", 0.9)),
    )


def build_form(parent: QtWidgets.QWidget, fields: Iterable[ParamField]) -> QtWidgets.QWidget:
    """A form with one control per field, grouped by ``field.group``.

    Advanced fields go into a trailing "Advanced" group. The returned widget
    has ``get_values()`` and ``set_values(dict)``, and a ``controls`` dict.
    """
    fields = list(fields)
    widget = QtWidgets.QWidget(parent)
    outer = QtWidgets.QVBoxLayout(widget)
    outer.setContentsMargins(0, 0, 0, 0)
    controls: dict[str, tuple[ParamField, QtWidgets.QWidget]] = {}
    groups: dict[str, QtWidgets.QFormLayout] = {}

    def layout_for(name: str) -> QtWidgets.QFormLayout:
        if name not in groups:
            box = QtWidgets.QGroupBox(name or "Parameters", widget)
            groups[name] = QtWidgets.QFormLayout(box)
            outer.addWidget(box)
        return groups[name]

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
    else:
        raise ValueError(f"{f.key}: unsupported field type {f.type!r} for this form")
    return control


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
}
