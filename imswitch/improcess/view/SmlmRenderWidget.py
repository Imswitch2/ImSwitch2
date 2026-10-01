"""Controls for how a localization result is drawn as a point cloud.

The knobs napari-storm's own dock widget offers, in ImProcess's idiom: how wide
each molecule's Gaussian is, whether depth is colour-coded, and which sub-volume
of the dataset is drawn.

Emits intent and owns no viewer state. The reconstruction viewer's controller
owns the renderer and applies these, which is why this panel does not need a
reference to it.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

#: Gaussian sigma (nm) the width spin boxes span. The upper end is generous
#: on purpose: a deliberately over-blurred render is a legitimate way to look
#: at sparse data.
_SIGMA_RANGE_NM = (0.5, 500.0)

#: FWHM = 2*sqrt(2*ln2)*sigma. Shown alongside because SMLM papers quote FWHM
#: while the renderer takes sigma, and the factor is otherwise a mystery.
_FWHM_PER_SIGMA = 2.3548

#: Colormap used when depth is encoded as colour. Depth on a monotonic ramp is
#: indistinguishable from depth on no ramp at all -- it just looks like
#: brightness variation -- so a hue sweep is what makes the encoding readable.
_DEPTH_COLORMAP = "hsv"

#: Colormaps that already sweep hue, and so need no substitution.
_HUE_COLORMAPS = frozenset({"hsv", "twilight", "turbo", "viridis", "inferno", "magma"})


class SmlmRenderWidget(QtWidgets.QWidget):
    """Panel driving the napari-storm point-cloud renderer."""

    sigSettingsChanged = QtCore.Signal(object, object)  # (overrides, renderRange)
    sigAppearanceChanged = QtCore.Signal(object)  # (appearance kwargs)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._emitting = False
        # What the selected result supports. Held rather than re-derived so
        # enablement has one source and cannot drift between the two places
        # that change it (selection change, and mode change).
        self._hasZ = False
        self._hasUncertainty = False
        #: Colormap to put back when depth colouring is switched off again.
        self._restoreColormap = ""
        #: Whether the user has changed anything yet. Until they have, the
        #: width mode follows the data the way the renderer's own default
        #: does, so the panel agrees with what is already on screen instead of
        #: silently forcing "fixed" the first time any control is touched.
        self._touched = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self._status = QtWidgets.QLabel("No localization result selected.")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        layout.addWidget(self._buildGaussianGroup())
        layout.addWidget(self._buildRangeGroup())
        layout.addWidget(self._buildAppearanceGroup())
        layout.addStretch(1)

        self.setEnabled(False)

    # -- construction -----------------------------------------------------

    def _buildGaussianGroup(self) -> QtWidgets.QWidget:
        group = QtWidgets.QGroupBox("Gaussian")
        form = QtWidgets.QFormLayout(group)

        self._mode = QtWidgets.QComboBox()
        self._mode.addItem("Fixed width", 0)
        self._mode.addItem("From uncertainty", 1)
        self._mode.setToolTip(
            "Fixed draws every molecule the same size. From uncertainty scales "
            "each one by its localization precision, which is the physically "
            "meaningful choice when the data carries it."
        )
        form.addRow("Width mode:", self._mode)

        self._sigmaXY = self._sigmaSpin(
            "Gaussian sigma in the lateral plane, in nanometres."
        )
        self._sigmaXYHint = QtWidgets.QLabel()
        self._sigmaXYHint.setStyleSheet("color: gray;")
        lateral = QtWidgets.QHBoxLayout()
        lateral.addWidget(self._sigmaXY)
        lateral.addWidget(self._sigmaXYHint)
        form.addRow("Fixed sigma XY:", lateral)

        self._sigmaZ = self._sigmaSpin("Gaussian sigma along the axial direction.")
        form.addRow("Fixed sigma Z:", self._sigmaZ)

        self._zColor = QtWidgets.QCheckBox("Colour by depth")
        self._zColor.setToolTip(
            "Encode z as colour instead of intensity. Fixed width only, and "
            "only for datasets with a real z coordinate."
        )
        form.addRow("", self._zColor)

        self._mode.currentIndexChanged.connect(self._onModeChanged)
        self._sigmaXY.valueChanged.connect(self._onSigmaChanged)
        self._sigmaZ.valueChanged.connect(self._emitSettings)
        self._zColor.toggled.connect(self._onDepthColourToggled)
        return group

    def _sigmaSpin(self, tooltip: str) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(*_SIGMA_RANGE_NM)
        spin.setDecimals(1)
        spin.setSingleStep(1.0)
        spin.setSuffix(" nm")
        spin.setValue(8.5)
        spin.setToolTip(tooltip)
        spin.setKeyboardTracking(False)
        return spin

    def _buildRangeGroup(self) -> QtWidgets.QWidget:
        group = QtWidgets.QGroupBox("Render range")
        group.setToolTip(
            "Draw only a sub-volume, as a percentage of the dataset's extent "
            "along each axis. Affects what is displayed, never what is saved."
        )
        form = QtWidgets.QFormLayout(group)
        self._range: dict[str, tuple[QtWidgets.QSpinBox, QtWidgets.QSpinBox]] = {}
        for axis in ("x", "y", "z"):
            low, high = QtWidgets.QSpinBox(), QtWidgets.QSpinBox()
            for spin, value in ((low, 0), (high, 100)):
                spin.setRange(0, 100)
                spin.setSuffix(" %")
                spin.setValue(value)
                spin.setKeyboardTracking(False)
                spin.valueChanged.connect(self._emitSettings)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(low)
            row.addWidget(QtWidgets.QLabel("to"))
            row.addWidget(high)
            form.addRow(f"{axis.upper()}:", row)
            self._range[axis] = (low, high)

        reset = QtWidgets.QPushButton("Show all")
        reset.clicked.connect(self.resetRange)
        form.addRow("", reset)
        return group

    def _buildAppearanceGroup(self) -> QtWidgets.QWidget:
        group = QtWidgets.QGroupBox("Appearance")
        form = QtWidgets.QFormLayout(group)

        self._colormap = QtWidgets.QComboBox()
        # Never offer an empty entry: napari resolves "no colormap" to an
        # arbitrary unnamed one, which the instanced backend draws as black.
        self._colormap.addItems(
            ["gray", "red", "green", "blue", "cyan", "magenta", "yellow",
             "viridis", "inferno", "magma", "turbo", "hsv", "twilight"]
        )
        form.addRow("Colormap:", self._colormap)

        self._opacity = QtWidgets.QDoubleSpinBox()
        self._opacity.setRange(0.05, 1.0)
        self._opacity.setSingleStep(0.05)
        self._opacity.setDecimals(2)
        self._opacity.setValue(1.0)
        self._opacity.setKeyboardTracking(False)
        form.addRow("Opacity:", self._opacity)

        self._colormap.currentTextChanged.connect(self._emitAppearance)
        self._opacity.valueChanged.connect(self._emitAppearance)
        return group

    # -- state ------------------------------------------------------------

    def overrides(self) -> dict:
        """The Gaussian settings the user has chosen."""
        values = {
            "mode": int(self._mode.currentData()),
            "fixed_sigma_xy_nm": float(self._sigmaXY.value()),
            "fixed_sigma_z_nm": float(self._sigmaZ.value()),
            "z_color_encoding": bool(self._zColor.isChecked()),
        }
        return values

    def renderRange(self) -> dict:
        """Per-axis fractions of the dataset extent, omitting untouched axes."""
        ranges = {}
        for axis, (low, high) in self._range.items():
            lo, hi = low.value() / 100.0, high.value() / 100.0
            if lo > hi:
                lo, hi = hi, lo
            if lo > 0.0 or hi < 1.0:
                ranges[axis] = (lo, hi)
        return ranges

    def appearance(self) -> dict:
        return {
            "colormap": self._colormap.currentText(),
            "opacity": float(self._opacity.value()),
        }

    def resetRange(self) -> None:
        self._withoutEmitting(self._resetRangeWidgets)
        self._emitSettings()

    def _resetRangeWidgets(self) -> None:
        for low, high in self._range.values():
            low.setValue(0)
            high.setValue(100)

    def setResultContext(
        self,
        *,
        available: bool,
        has_z: bool = False,
        has_uncertainty: bool = False,
        name: str = "",
        count: int = 0,
    ) -> None:
        """Enable and shape the controls for the selected result.

        A control that cannot apply is disabled rather than hidden, so the
        panel does not reshuffle as the selection changes, and each disabled
        control says why in its tooltip.
        """
        self.setEnabled(available)
        if not available:
            self._status.setText(
                "No localization result selected. Select one to control how it "
                "is rendered."
            )
            return

        self._status.setText(
            f"<b>{name}</b> — {count:,} localizations, {'3D' if has_z else '2D'}"
        )
        self._hasZ = bool(has_z)
        self._hasUncertainty = bool(has_uncertainty)

        def shape():
            if not self._touched:
                # Match the renderer's own rule: width from uncertainty when
                # the data carries one. Otherwise the panel would claim
                # "fixed" while the canvas showed variable, and the first
                # touch of any control would quietly change the render.
                wanted = 1 if self._hasUncertainty else 0
                index = self._mode.findData(wanted)
                if index >= 0:
                    self._mode.setCurrentIndex(index)
            self._applyEnablement()

        self._withoutEmitting(shape)

    def _applyEnablement(self) -> None:
        """Derive every control's enabled state from the held context."""
        variable = self._mode.model().item(1)
        variable.setEnabled(self._hasUncertainty)
        if not self._hasUncertainty and int(self._mode.currentData()) == 1:
            self._mode.setCurrentIndex(0)
        self._mode.setToolTip(
            "This dataset carries neither a localization precision nor photon "
            "counts, so only fixed width is available."
            if not self._hasUncertainty else
            "Fixed draws every molecule the same size. From uncertainty scales "
            "each one by its localization precision, which is the physically "
            "meaningful choice when the data carries it."
        )

        fixed = int(self._mode.currentData()) == 0
        self._sigmaXY.setEnabled(fixed)
        self._sigmaZ.setEnabled(fixed and self._hasZ)
        self._zColor.setEnabled(fixed and self._hasZ)
        if not (fixed and self._hasZ):
            self._zColor.setChecked(False)
        self._zColor.setToolTip(
            "This dataset has no z coordinate to colour by." if not self._hasZ
            else "Colour by depth is available in fixed-width mode only."
            if not fixed else
            "Encode z as colour instead of intensity."
        )
        for spin in self._range["z"]:
            spin.setEnabled(self._hasZ)
        self._updateFwhmHint()

    def setValues(self, overrides: dict | None) -> None:
        """Reflect settings decided elsewhere without echoing them back."""
        if not overrides:
            return

        def apply():
            if "mode" in overrides:
                index = self._mode.findData(int(overrides["mode"]))
                if index >= 0:
                    self._mode.setCurrentIndex(index)
            if "fixed_sigma_xy_nm" in overrides:
                self._sigmaXY.setValue(float(overrides["fixed_sigma_xy_nm"]))
            if "fixed_sigma_z_nm" in overrides:
                self._sigmaZ.setValue(float(overrides["fixed_sigma_z_nm"]))
            if "z_color_encoding" in overrides:
                self._zColor.setChecked(bool(overrides["z_color_encoding"]))
            self._updateFwhmHint()

        self._withoutEmitting(apply)

    # -- signalling -------------------------------------------------------

    def _withoutEmitting(self, action) -> None:
        """Run a programmatic update without echoing it back as user intent."""
        previous, self._emitting = self._emitting, True
        try:
            action()
        finally:
            self._emitting = previous

    def _onModeChanged(self) -> None:
        self._withoutEmitting(self._applyEnablement)
        self._emitSettings()

    def _onDepthColourToggled(self, enabled: bool) -> None:
        """Switch to a hue-sweeping colormap, and put the old one back after.

        Depth encoded on a monotonic ramp reads as brightness, not depth, so
        turning the setting on with a grey colormap looks like it did nothing.
        A colormap the user picked deliberately is left alone.
        """
        if enabled:
            current = self._colormap.currentText()
            if current not in _HUE_COLORMAPS:
                self._restoreColormap = current
                index = self._colormap.findText(_DEPTH_COLORMAP)
                if index >= 0:
                    self._colormap.setCurrentIndex(index)
        elif self._restoreColormap:
            index = self._colormap.findText(self._restoreColormap)
            self._restoreColormap = ""
            if index >= 0:
                self._colormap.setCurrentIndex(index)
        self._emitSettings()

    def _onSigmaChanged(self) -> None:
        self._updateFwhmHint()
        self._emitSettings()

    def _updateFwhmHint(self) -> None:
        fwhm = float(self._sigmaXY.value()) * _FWHM_PER_SIGMA
        self._sigmaXYHint.setText(f"≈ {fwhm:.0f} nm FWHM")

    def _emitSettings(self) -> None:
        if self._emitting:
            return
        self._touched = True
        self.sigSettingsChanged.emit(self.overrides(), self.renderRange())

    def _emitAppearance(self) -> None:
        if self._emitting:
            return
        self.sigAppearanceChanged.emit(self.appearance())


__all__ = ["SmlmRenderWidget"]
