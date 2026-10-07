import numpy as np

from ..basecontrollers import ImConWidgetController
from imswitch.imcontrol.model.ulenses_localizer import localize_grid, localizer


class ULensesController(ImConWidgetController):
    """Linked to ULensesWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._gridLocalizationResult = None

        # Register the grid overlay with the image viewbox (like the other
        # overlays) so it is tracked in the viewbox's overlay list and receives
        # the detector pixel-scale broadcast, keeping the grid aligned to the
        # image napari draws scaled by the detector pixel size.
        self._commChannel.sigAddItemToVb.emit(self._widget.getPlotGraphicsItem())

        # Connect ULensesWidget signals
        self._widget.sigULensesClicked.connect(self.updateGrid)
        self._widget.sigUShowLensesChanged.connect(self.toggleULenses)
        self._widget.sigLocalizeBtnClicked.connect(self.localize)
        self._widget.sigLocalizationMethodChanged.connect(self._localizationMethodChanged)
        self._commChannel.sigDetectorSwitched.connect(self._detectorSwitched)

        self._syncPixelSizeFromDetector()

    def _detectorSwitched(self, *_args):
        # Grid geometry belongs to the image/detector it was localized on. Do
        # not carry a finite tilted grid onto a newly selected detector.
        self._gridLocalizationResult = None
        self._syncPixelSizeFromDetector()
        if self._widget.getLocalizationMethod() == 'grid':
            self._widget.setData(x=np.asarray([]), y=np.asarray([]))
            self._widget.setLocalizationStatus(
                'Detector changed — run Auto Localize for this image.'
            )

    def _detectorPixelSizeNm(self):
        """Return effective current-frame pixel size in nm when representable.

        DetectorManager.pixelSizeUm is the unbinned sample-plane calibration.
        Camera binning changes the physical size represented by one delivered
        array pixel, so it is applied here. The historical uLenses UI has one
        scalar pixel-size field; anisotropic detector pixels therefore remain a
        manual-fallback case rather than being silently collapsed.
        """
        detector_name = self._master.detectorsManager.getCurrentDetectorName()

        def read(detector):
            sizes = list(detector.pixelSizeUm)
            if len(sizes) >= 2:
                size_y_um, size_x_um = float(sizes[-2]), float(sizes[-1])
            elif len(sizes) == 1:
                size_y_um = size_x_um = float(sizes[0])
            else:
                raise ValueError('detector pixelSizeUm is empty')

            binning = 1
            if not bool(getattr(detector, 'isScanDriven', False)):
                try:
                    binning = max(1, int(getattr(detector, 'binning', 1) or 1))
                except (TypeError, ValueError):
                    binning = 1
            size_y_um *= binning
            size_x_um *= binning

            source = None
            try:
                source_parameter = detector.parameters.get('Camera pixel size source')
                if source_parameter is not None:
                    source = str(source_parameter.value)
            except Exception:
                pass
            return size_y_um, size_x_um, source

        size_y_um, size_x_um, source = self._master.detectorsManager.execOnCurrent(read)
        if source == 'assumed default':
            raise ValueError(
                'current detector only has the assumed default pixel size; '
                'configure Camera pixel size or enter it manually here'
            )
        if not np.isfinite(size_y_um) or not np.isfinite(size_x_um):
            raise ValueError('detector pixel size is not finite')
        if size_y_um <= 0 or size_x_um <= 0:
            raise ValueError('detector pixel size must be positive')

        scale = max(abs(size_y_um), abs(size_x_um), 1e-12)
        if abs(size_y_um - size_x_um) / scale > 1e-3:
            raise ValueError(
                'current detector has anisotropic pixels '
                f'({size_y_um:.6g} x {size_x_um:.6g} um); '
                'the uLenses tool currently uses one scalar pixel size'
            )
        return 1000.0 * 0.5 * (size_y_um + size_x_um), detector_name, source

    def _syncPixelSizeFromDetector(self):
        """Refresh uLenses pixel size from the currently displayed detector."""
        try:
            pixel_size_nm, detector_name, source = self._detectorPixelSizeNm()
        except Exception as error:
            # Retain the existing field value so old setups still work. Manual
            # entry is only a fallback when detector metadata cannot represent
            # the current image pixel size.
            if hasattr(self._widget, 'setPixelSizeManualFallback'):
                self._widget.setPixelSizeManualFallback(str(error))
            return None

        if hasattr(self._widget, 'setPixelSizeFromDetector'):
            self._widget.setPixelSizeFromDetector(pixel_size_nm, detector_name, source)
        else:
            self._widget.setParameters(px=pixel_size_nm)
        return float(pixel_size_nm)

    def localize(self):
        """Localize the uLens grid in the current detector image."""
        try:
            self._syncPixelSizeFromDetector()
            _, _, px, upx, upy = self._widget.getParameters()
            if px <= 0:
                raise ValueError('Pixel size must be positive')

            img_frame = self._commChannel.get_image()
            method = self._widget.getLocalizationMethod()
            if method == 'legacy':
                loc_res = localizer(
                    img_frame,
                    xp_guess=upx / px,
                    yp_guess=upy / px,
                )
                x_period = round(loc_res.xp * px, 2)
                x_offset = round(loc_res.xo, 2)
                y_period = round(loc_res.yp * px, 2)
                y_offset = round(loc_res.yo, 2)

                self._widget.setParameters(
                    x=x_offset,
                    y=y_offset,
                    px=px,
                    upx=x_period,
                    upy=y_period,
                )
                self._widget.setLocalizationStatus(
                    f'Legacy | X {x_period:g} nm | Y {y_period:g} nm'
                )
            elif method == 'grid':
                n_cols, n_rows = self._widget.getGridSize()
                settings = (
                    self._widget.getGridLocalizationSettings()
                    if hasattr(self._widget, 'getGridLocalizationSettings')
                    else {}
                )
                grid = localize_grid(
                    img_frame,
                    xp_guess=upx / px,
                    yp_guess=upy / px,
                    n_cols=n_cols,
                    n_rows=n_rows,
                    **settings,
                )
                self._gridLocalizationResult = grid
                x_period = round(grid.x_period_px * px, 2)
                y_period = round(grid.y_period_px * px, 2)
                self._widget.setParameters(
                    px=px,
                    upx=x_period,
                    upy=y_period,
                )
                stagger = (
                    'no stagger'
                    if grid.stagger_axis is None
                    else f'{grid.stagger_axis} stagger {grid.stagger_fraction:.2f}'
                )
                rms = grid.diagnostics.get('fit_rms_px')
                rms_text = '' if rms is None else f' | RMS {float(rms):.2f} px'
                measured_text = ''
                if grid.missing_count:
                    measured_text = (
                        f' | {grid.matched_count}/{grid.n_cols * grid.n_rows} detected'
                    )
                self._widget.setLocalizationStatus(
                    f'Grid {grid.n_cols}x{grid.n_rows} | tilt {grid.angle_deg:.1f} deg | '
                    f'{stagger}{measured_text}{rms_text}'
                )
            else:
                raise ValueError(f'Unknown localization method: {method}')
        except Exception as e:
            method = getattr(self._widget, 'getLocalizationMethod', lambda: 'legacy')()
            label = 'Grid localization' if method == 'grid' else 'Auto localization'
            message = f'{label} failed: {e}'
            self._logger.warning(message)
            if hasattr(self._widget, 'setLocalizationStatus'):
                self._widget.setLocalizationStatus(message, error=True)
            # Keep the previous successful overlay/result visible for diagnosis.
            return

        self.updateGrid()

    def _localizationMethodChanged(self, method):
        """Switch displayed overlay without discarding either method's state."""
        if method == 'grid' and self._gridLocalizationResult is None:
            self._widget.setData(x=np.asarray([]), y=np.asarray([]))
            self._widget.setLocalizationStatus(
                'Run Auto Localize to establish tilted/staggered grid geometry.'
            )
            return
        self.updateGrid()

    def updateGrid(self):
        """Update the overlay using the selected localization representation."""
        self._syncPixelSizeFromDetector()
        method = self._widget.getLocalizationMethod()
        if method == 'grid':
            grid_result = self._gridLocalizationResult
            if grid_result is None:
                self._widget.setData(x=np.asarray([]), y=np.asarray([]))
                self._widget.setLocalizationStatus(
                    'Run Auto Localize to establish tilted/staggered grid geometry.'
                )
                return
            points = np.asarray(grid_result.grid_points_rc, dtype=np.float64)
            self._widget.setData(x=points[:, 1], y=points[:, 0])
            return

        # Historical axis-aligned behavior remains unchanged.
        x, y, px, upx, upy = self._widget.getParameters()
        size_x, size_y = self._master.detectorsManager.execOnCurrent(lambda c: c.shape)
        pattern_x = np.arange(x, size_x, upx / px)
        pattern_y = np.arange(y, size_y, upy / px)
        grid = np.array(np.meshgrid(pattern_x, pattern_y)).T.reshape(-1, 2)
        self._widget.setData(x=grid[:, 0], y=grid[:, 1])

    def toggleULenses(self, show):
        """Show or hide grid."""
        self._widget.setULensesVisible(show)


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
