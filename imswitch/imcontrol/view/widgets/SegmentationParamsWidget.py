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

"""Interactive segmentation parameter tuning dialog.

Provides a QDialog for tuning cell segmentation thresholds and filters with
live preview. Operates on a 2-D float image and an optional Segmenter instance.
"""

import numpy as np
from qtpy import QtCore, QtWidgets, QtGui


class SegmentationParamsWidget(QtWidgets.QDialog):
    """Dialog for interactive segmentation parameter tuning.
    
    Allows users to adjust threshold, area, intensity, and eccentricity filters
    with live preview of segmentation results. Emits accepted parameters as a
    dict when the user clicks Accept & Save.
    
    Parameters
    ----------
    image : np.ndarray
        2-D float array to segment
    pixel_size_um : float
        Pixel size in micrometers for area calculations
    segmenter : object, optional
        Segmenter instance with segment() and apply_filters() methods.
        If None, uses a fallback stub segmenter.
    initial_params : dict, optional
        Initial parameter values. Expected keys:
        - threshold (float)
        - blur_sigma_px (float)
        - area_um2_min (float)
        - area_um2_max (float)
        - mean_intensity (float)
        - eccentricity (float)
    parent : QWidget, optional
        Parent widget
    
    Signals
    -------
    sigParamsAccepted : QtCore.Signal(dict)
        Emitted when user accepts parameters. Dict contains:
        - threshold
        - blur_sigma_px
        - area_um2_min
        - area_um2_max
        - mean_intensity
        - eccentricity
        - area_enabled (bool)
        - mean_intensity_enabled (bool)
        - eccentricity_enabled (bool)
    """
    
    sigParamsAccepted = QtCore.Signal(dict)
    
    _PREVIEW_PX = 512  # max dimension for preview images
    
    def __init__(
        self,
        image,
        pixel_size_um,
        segmenter=None,
        initial_params=None,
        parent=None
    ):
        super().__init__(parent)
        
        self._image = image
        self._pixel_size_um = pixel_size_um
        self._segmenter = segmenter
        
        # Initialize segmenter (try lazy import, fallback to stub)
        if self._segmenter is None:
            try:
                from imswitch.imcontrol.model.workflows.segmentation import Segmenter
                self._segmenter = Segmenter()
            except ImportError:
                self._segmenter = self._StubSegmenter()
        
        # Default parameters
        defaults = {
            'threshold': 0.3,
            'blur_sigma_px': 3.0,
            'area_um2_min': 10.0,
            'area_um2_max': 500.0,
            'mean_intensity': 0.1,
            'eccentricity': 0.95,
        }
        if initial_params:
            defaults.update(initial_params)
        
        self._threshold = defaults['threshold']
        self._blur_sigma_px = defaults['blur_sigma_px']
        self._min_area = defaults['area_um2_min']
        self._max_area = defaults['area_um2_max']
        self._intensity = defaults['mean_intensity']
        self._eccentricity = defaults['eccentricity']
        
        # Cache segmentation results
        self._labels = None
        self._mask = None
        self._img_blur = None
        self._props = None
        
        self._buildUI()
        self._connectSignals()
        
    def _buildUI(self):
        """Build the dialog UI."""
        self.setWindowTitle("Segmentation Parameters")
        self.setMinimumSize(900, 700)
        self.setSizeGripEnabled(True)
        
        root = QtWidgets.QVBoxLayout(self)
        
        # Preview row
        img_row = QtWidgets.QHBoxLayout()
        self._lbl_left = QtWidgets.QLabel()
        self._lbl_right = QtWidgets.QLabel()
        
        for lbl, title in (
            (self._lbl_left, "Blurred image + segmentation mask"),
            (self._lbl_right, "Watershed cell labels"),
        ):
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            lbl.setMinimumSize(400, 400)
            lbl.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Expanding,
            )
            lbl.setText(title)
            img_row.addWidget(lbl)
        root.addLayout(img_row)
        
        # Slider grid
        form = QtWidgets.QFormLayout()
        
        def _make_row(lo, hi, init):
            sl = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            sl.setMinimum(0)
            sl.setMaximum(1000)
            sl.setValue(int((init - lo) / (hi - lo) * 1000))
            val_lbl = QtWidgets.QLabel(f"{init:.3f}")
            val_lbl.setMinimumWidth(70)
            return sl, val_lbl, lo, hi
        
        self._sl_thr, self._v_thr, self._lo_thr, self._hi_thr = _make_row(
            0.0, 1.0, self._threshold
        )
        self._sl_amin, self._v_amin, self._lo_amin, self._hi_amin = _make_row(
            0.0, 2000.0, self._min_area
        )
        self._sl_amax, self._v_amax, self._lo_amax, self._hi_amax = _make_row(
            0.0, 2000.0, self._max_area
        )
        self._sl_inte, self._v_inte, self._lo_inte, self._hi_inte = _make_row(
            0.0, 1.0, self._intensity
        )
        self._sl_ecce, self._v_ecce, self._lo_ecce, self._hi_ecce = _make_row(
            0.0, 1.0, self._eccentricity
        )
        
        for label, sl, vl in (
            ("Threshold", self._sl_thr, self._v_thr),
            ("Min area [µm²]", self._sl_amin, self._v_amin),
            ("Max area [µm²]", self._sl_amax, self._v_amax),
            ("Min intensity", self._sl_inte, self._v_inte),
            ("Max eccentricity", self._sl_ecce, self._v_ecce),
        ):
            row_w = QtWidgets.QWidget()
            row_l = QtWidgets.QHBoxLayout(row_w)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.addWidget(sl)
            row_l.addWidget(vl)
            form.addRow(label, row_w)
        root.addLayout(form)
        
        # Status + buttons
        bottom = QtWidgets.QHBoxLayout()
        self._lbl_count = QtWidgets.QLabel("Computing…")
        btn_accept = QtWidgets.QPushButton("Accept && Save Parameters")
        btn_accept.setDefault(True)
        bottom.addWidget(self._lbl_count)
        bottom.addStretch()
        bottom.addWidget(btn_accept)
        root.addLayout(bottom)
        
        self._btn_accept = btn_accept
        
    def _connectSignals(self):
        """Connect slider and button signals."""
        self._sl_thr.valueChanged.connect(lambda v: self._on_slider(v, "thr"))
        self._sl_amin.valueChanged.connect(lambda v: self._on_slider(v, "amin"))
        self._sl_amax.valueChanged.connect(lambda v: self._on_slider(v, "amax"))
        self._sl_inte.valueChanged.connect(lambda v: self._on_slider(v, "inte"))
        self._sl_ecce.valueChanged.connect(lambda v: self._on_slider(v, "ecce"))
        self._btn_accept.clicked.connect(self._accept)
        
    def _decode(self, raw, lo, hi):
        """Convert slider position (0-1000) to parameter value."""
        return lo + raw / 1000.0 * (hi - lo)
    
    def _on_slider(self, raw, which):
        """Handle slider value changes."""
        reseg = False
        if which == "thr":
            self._threshold = self._decode(raw, self._lo_thr, self._hi_thr)
            self._v_thr.setText(f"{self._threshold:.3f}")
            reseg = True
        elif which == "amin":
            v = self._decode(raw, self._lo_amin, self._hi_amin)
            if v < self._max_area:
                self._min_area = v
            self._v_amin.setText(f"{self._min_area:.0f}")
        elif which == "amax":
            v = self._decode(raw, self._lo_amax, self._hi_amax)
            if v > self._min_area:
                self._max_area = v
            self._v_amax.setText(f"{self._max_area:.0f}")
        elif which == "inte":
            self._intensity = self._decode(raw, self._lo_inte, self._hi_inte)
            self._v_inte.setText(f"{self._intensity:.3f}")
        elif which == "ecce":
            self._eccentricity = self._decode(raw, self._lo_ecce, self._hi_ecce)
            self._v_ecce.setText(f"{self._eccentricity:.3f}")
        
        self._refresh(reseg)
    
    def _refresh(self, reseg=False):
        """Re-segment and/or re-filter the image, update previews."""
        if reseg or self._labels is None:
            # Call segmenter - need to create new instance with updated params
            if hasattr(self._segmenter, '__class__') and self._segmenter.__class__.__name__ == '_StubSegmenter':
                # Stub segmenter returns extended result dict
                result = self._segmenter.segment(
                    self._image,
                    self._pixel_size_um,
                    threshold=self._threshold,
                    blur_sigma_px=self._blur_sigma_px,
                )
                self._labels = result.get('labels')
                self._mask = result.get('mask')
                self._img_blur = result.get('img_blur')
                self._props = result.get('props')
            else:
                # Real Segmenter: create new instance with current params
                from imswitch.imcontrol.model.workflows.segmentation import Segmenter
                seg = Segmenter(
                    blur_sigma_px=self._blur_sigma_px,
                    threshold=self._threshold,
                )
                self._props = seg.segment(self._image, self._pixel_size_um)
                
                # Compute mask and labels for visualization
                if self._props:
                    from scipy.ndimage import gaussian_filter
                    self._img_blur = gaussian_filter(self._image, sigma=self._blur_sigma_px)
                    img_blur_norm = self._img_blur - np.min(self._img_blur)
                    mx = np.max(img_blur_norm)
                    if mx > 0:
                        img_blur_norm = img_blur_norm / mx
                    self._mask = img_blur_norm > self._threshold
                    
                    # Create labels image from props
                    self._labels = np.zeros(self._image.shape, dtype=np.int32)
                    for i, bbox in enumerate(self._props['bbox']):
                        minr, minc, maxr, maxc = bbox
                        self._labels[minr:maxr, minc:maxc] = self._props['label'][i]
                else:
                    self._mask = np.zeros(self._image.shape, dtype=bool)
                    self._labels = np.zeros(self._image.shape, dtype=np.int32)
                    self._img_blur = self._image
        
        # Apply filters
        if self._props and len(self._props.get("area_um2", [])) > 0:
            filt = {
                "area_um2_min": self._min_area,
                "area_um2_max": self._max_area,
                "mean_intensity": self._intensity,
                "eccentricity": self._eccentricity,
            }
            idx = self._apply_filters(self._props, filt)
            
            self._lbl_count.setText(
                f"Total cells: {len(self._props['area_um2'])}   |   "
                f"After filter: {len(idx)}"
            )
            valid_x = self._props["centroid_x_um"][idx] if len(idx) else None
            valid_y = self._props["centroid_y_um"][idx] if len(idx) else None
        else:
            self._lbl_count.setText("No cells found")
            valid_x = valid_y = None
        
        self._update_previews(valid_x, valid_y)
    
    def _apply_filters(self, props, filt):
        """Apply area/intensity/eccentricity filters to cell properties."""
        area_um2 = np.array(props["area_um2"])
        mean_intensity = np.array(props["mean_intensity"])
        eccentricity = np.array(props["eccentricity"])
        
        idx = np.where(
            (area_um2 >= filt["area_um2_min"])
            & (area_um2 <= filt["area_um2_max"])
            & (mean_intensity > filt["mean_intensity"])
            & (eccentricity < filt["eccentricity"])
        )[0]
        return idx
    
    def _update_previews(self, valid_x, valid_y):
        """Render preview images as QPixmaps."""
        if self._img_blur is None or self._mask is None or self._labels is None:
            return
        
        vmax = float(np.percentile(self._image, 99.5))
        
        px_left = self._to_pixmap(
            self._img_blur,
            vmax=vmax,
            mask=self._mask,
            scatter_x=valid_x,
            scatter_y=valid_y,
            pixel_size_um=self._pixel_size_um,
        )
        px_right = self._to_pixmap(
            self._image,
            vmax=vmax,
            labels=self._labels,
        )
        
        size = self._lbl_left.size()
        self._lbl_left.setPixmap(
            px_left.scaled(size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        )
        self._lbl_right.setPixmap(
            px_right.scaled(size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        )
    
    @staticmethod
    def _to_pixmap(
        arr,
        vmax=None,
        mask=None,
        labels=None,
        scatter_x=None,
        scatter_y=None,
        pixel_size_um=1.0,
    ):
        """Convert a 2-D float array to a QPixmap with optional overlays."""
        # Downsample for display
        h, w = arr.shape
        scale = min(1.0, SegmentationParamsWidget._PREVIEW_PX / max(h, w))
        if scale < 1.0:
            from scipy.ndimage import zoom as nd_zoom
            arr = nd_zoom(arr, scale, order=1)
            if mask is not None:
                mask = nd_zoom(mask.astype(np.float32), scale, order=0).astype(bool)
            if labels is not None:
                labels = nd_zoom(labels.astype(np.float32), scale, order=0).astype(np.int32)
        
        # Normalize to [0, 255]
        arr = arr.astype(np.float32)
        arr -= arr.min()
        clip = vmax if vmax is not None else arr.max()
        arr = np.clip(arr / (clip + 1e-9), 0.0, 1.0)
        u8 = (arr * 255).astype(np.uint8)
        h, w = u8.shape
        
        # Build RGB
        rgb = np.stack([u8, u8, u8], axis=-1)
        
        if mask is not None:
            # Red-tinted overlay for segmentation mask
            rgb[mask, 0] = np.clip(rgb[mask, 0].astype(np.int16) + 90, 0, 255).astype(np.uint8)
            rgb[mask, 1] = (rgb[mask, 1].astype(np.float32) * 0.4).astype(np.uint8)
            rgb[mask, 2] = (rgb[mask, 2].astype(np.float32) * 0.4).astype(np.uint8)
        
        if labels is not None:
            # Color-coded label overlay (HSV wheel, label 0 = background)
            nz = labels > 0
            hue = ((labels[nz] * 37) % 256).astype(np.uint8)
            rgb[nz, 0] = (rgb[nz, 0].astype(np.float32) * 0.3 + hue.astype(np.float32) * 0.7).clip(0, 255).astype(np.uint8)
            rgb[nz, 1] = (rgb[nz, 1].astype(np.float32) * 0.3).astype(np.uint8)
            rgb[nz, 2] = (rgb[nz, 2].astype(np.float32) * 0.3 + (255 - hue).astype(np.float32) * 0.7).clip(0, 255).astype(np.uint8)
        
        if scatter_x is not None and scatter_y is not None and len(scatter_x):
            # Draw small cross markers for accepted cells
            # Convert µm coords to pixel coords (approximate linear mapping)
            img_extent_x_um = w * pixel_size_um * scale
            img_extent_y_um = h * pixel_size_um * scale
            x_px = (scatter_x / img_extent_x_um * w).astype(int)
            y_px = (scatter_y / img_extent_y_um * h).astype(int)
            for xi, yi in zip(x_px, y_px):
                for d in range(-4, 5):
                    if 0 <= yi + d < h and 0 <= xi < w:
                        rgb[yi + d, xi, :] = [255, 255, 0]
                    if 0 <= xi + d < w and 0 <= yi < h:
                        rgb[yi, xi + d, :] = [255, 255, 0]
        
        rgb = np.ascontiguousarray(rgb)
        qimg = QtGui.QImage(rgb.data, w, h, w * 3, QtGui.QImage.Format_RGB888)
        # Keep reference to data to prevent garbage collection
        qimg._data_ref = rgb
        return QtGui.QPixmap.fromImage(qimg)
    
    def _accept(self):
        """Handle Accept button click - emit params and close."""
        params = {
            "threshold": self._threshold,
            "blur_sigma_px": self._blur_sigma_px,
            "area_um2_min": self._min_area,
            "area_um2_max": self._max_area,
            "mean_intensity": self._intensity,
            "eccentricity": self._eccentricity,
            "area_enabled": True,
            "mean_intensity_enabled": True,
            "eccentricity_enabled": True,
        }
        self.sigParamsAccepted.emit(params)
        self.accept()
    
    def showEvent(self, event):
        """On show, run initial segmentation and update previews."""
        super().showEvent(event)
        if self._labels is None:
            self._refresh(reseg=True)
    
    class _StubSegmenter:
        """Fallback segmenter when real Segmenter is not available."""
        
        def segment(self, image, pixel_size_um, threshold=0.3, blur_sigma_px=3.0):
            """Stub segmentation: blur + threshold + label."""
            from scipy import ndimage
            from skimage import measure, morphology
            
            # Gaussian blur
            img_blur = ndimage.gaussian_filter(image, sigma=blur_sigma_px)
            
            # Threshold
            thr_val = np.percentile(img_blur, threshold * 100)
            mask = img_blur > thr_val
            
            # Label connected components
            labels = measure.label(mask)
            labels = morphology.remove_small_objects(labels, min_size=10)
            
            # Compute region properties
            props_raw = measure.regionprops(labels, intensity_image=image)
            
            # Build props dict
            props = {
                "area_um2": np.array([p.area * pixel_size_um**2 for p in props_raw]),
                "mean_intensity": np.array([p.mean_intensity for p in props_raw]),
                "eccentricity": np.array([p.eccentricity for p in props_raw]),
                "centroid_x_um": np.array([p.centroid[1] * pixel_size_um for p in props_raw]),
                "centroid_y_um": np.array([p.centroid[0] * pixel_size_um for p in props_raw]),
            }
            
            return {
                'labels': labels,
                'mask': mask,
                'img_blur': img_blur,
                'props': props,
            }


if __name__ == "__main__":
    """Test harness for SegmentationParamsWidget."""
    import sys
    
    app = QtWidgets.QApplication(sys.argv)
    
    # Create a synthetic test image with some blobs
    from scipy import ndimage
    
    img = np.random.rand(512, 512) * 0.1
    centers = [
        (100, 100), (200, 150), (350, 300), (450, 450),
        (120, 400), (300, 100), (400, 200)
    ]
    for cy, cx in centers:
        y, x = np.ogrid[:512, :512]
        blob = np.exp(-((x - cx)**2 + (y - cy)**2) / (30**2))
        img += blob * (0.5 + np.random.rand() * 0.5)
    
    img = ndimage.gaussian_filter(img, sigma=2.0)
    img = img.astype(np.float32)
    
    # Create dialog
    dlg = SegmentationParamsWidget(
        image=img,
        pixel_size_um=0.5,
        initial_params={
            'threshold': 0.35,
            'blur_sigma_px': 3.0,
            'area_um2_min': 50.0,
            'area_um2_max': 1000.0,
            'mean_intensity': 0.2,
            'eccentricity': 0.9,
        }
    )
    
    def on_accepted(params):
        print("Accepted parameters:")
        for k, v in params.items():
            print(f"  {k}: {v}")
    
    dlg.sigParamsAccepted.connect(on_accepted)
    dlg.show()
    
    sys.exit(app.exec_())
