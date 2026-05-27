"""Reusable watershed-based cell segmentation.

Provides the Segmenter class for 2-D float image segmentation via:
- Gaussian blur → threshold → remove_small_objects → distance-transform watershed
- Region property extraction via skimage.measure.regionprops_table

Returns dict schema:
    {
        "label": array of int (1..N),
        "area": array of float (pixels²),
        "centroid_row": array of float (pixels, top=0),
        "centroid_col": array of float (pixels, left=0),
        "centroid_x_um": array of float (µm),
        "centroid_y_um": array of float (µm),
        "area_um2": array of float (µm²),
        "mean_intensity": array of float,
        "max_intensity": array of float,
        "eccentricity": array of float (0=circle, 1=line),
        "bbox": array of int (4×N: min_row, min_col, max_row, max_col),
        "height_um": array of float (µm),
        "width_um": array of float (µm),
    }

All arrays are numpy arrays of length N (number of detected objects).
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
from scipy import ndimage as ndi
from scipy.ndimage import gaussian_filter
from skimage.feature import peak_local_max
from skimage.filters import threshold_otsu
from skimage.measure import regionprops_table
from skimage.morphology import remove_small_objects
from skimage.segmentation import watershed


def _get_config_dir() -> Path:
    """Return the ImSwitch user config directory."""
    if os.name == 'nt':
        try:
            import ctypes.wintypes
            CSIDL_PERSONAL = 5
            SHGFP_TYPE_CURRENT = 0
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(0, CSIDL_PERSONAL, 0, SHGFP_TYPE_CURRENT, buf)
            base = buf.value
        except ImportError:
            base = os.path.expanduser('~')
    else:
        base = os.path.expanduser('~')
    return Path(base) / 'ImSwitchConfig'


class Segmenter:
    """Watershed-based cell segmenter for 2-D float images.
    
    Args:
        blur_sigma_px: Gaussian blur sigma (pixels).
        threshold: Intensity threshold (0-1 range). If None, auto-computed via Otsu.
        min_area_um2: Minimum object area (µm²) for filtering small objects.
        peak_min_dist_um: Minimum distance (µm) between peaks for watershed splitting.
        min_area_px: Override pixel-based min area (auto-computed from min_area_um2 if None).
        peak_min_dist_px: Override pixel-based peak distance (auto-computed from peak_min_dist_um if None).
    """
    
    def __init__(
        self,
        blur_sigma_px: float = 5.0,
        threshold: Optional[float] = None,
        min_area_um2: float = 30.0,
        peak_min_dist_um: float = 8.0,
        min_area_px: Optional[int] = None,
        peak_min_dist_px: Optional[int] = None,
    ):
        self.blur_sigma_px = blur_sigma_px
        self.threshold = threshold
        self.min_area_um2 = min_area_um2
        self.peak_min_dist_um = peak_min_dist_um
        self._min_area_px_override = min_area_px
        self._peak_min_dist_px_override = peak_min_dist_px
    
    def segment(self, img: np.ndarray, pixel_size_um: float) -> Dict[str, np.ndarray]:
        """Segment cells in a 2-D image.
        
        Args:
            img: 2-D float array (normalized 0-1 or arbitrary range).
            pixel_size_um: Pixel size in µm.
        
        Returns:
            Dict of numpy arrays with keys:
                label, area, centroid_row, centroid_col, centroid_x_um, centroid_y_um,
                area_um2, mean_intensity, max_intensity, eccentricity, bbox,
                height_um, width_um.
            Empty dict if no objects found.
        """
        # Auto-compute pixel-based thresholds from physical units
        if self._min_area_px_override is not None:
            min_area_px = self._min_area_px_override
        else:
            min_area_px = max(10, int(self.min_area_um2 / pixel_size_um ** 2))
        
        if self._peak_min_dist_px_override is not None:
            peak_min_dist_px = self._peak_min_dist_px_override
        else:
            peak_min_dist_px = max(3, int(self.peak_min_dist_um / pixel_size_um))
        
        # 1) Blur and normalize
        img_blur = gaussian_filter(img, sigma=float(self.blur_sigma_px))
        img_blur = img_blur - np.min(img_blur)
        mx = np.max(img_blur)
        if mx > 0:
            img_blur = img_blur / mx
        
        # 2) Threshold (auto-Otsu if None)
        if self.threshold is None:
            thresh_val = threshold_otsu(img_blur)
        else:
            thresh_val = float(self.threshold)
        
        mask = img_blur > thresh_val
        
        # Handle scikit-image API drift (0.26+ renamed min_size → max_size)
        remove_small_objects_params = inspect.signature(remove_small_objects).parameters
        if "max_size" in remove_small_objects_params:
            mask = remove_small_objects(mask, max_size=min_area_px - 1)
        else:
            mask = remove_small_objects(mask, min_size=min_area_px)
        
        if not np.any(mask):
            return {}
        
        # 3) Watershed splitting via distance transform
        distance = ndi.distance_transform_edt(mask)
        
        coords = peak_local_max(
            distance,
            min_distance=int(peak_min_dist_px),
            labels=mask.astype(int)
        )
        
        markers = np.zeros_like(distance, dtype=np.int32)
        if len(coords) > 0:
            # Randomize marker IDs to avoid bias
            tmp_counter = np.arange(len(coords)) + 1
            tmp_counter = tmp_counter[np.random.permutation(len(tmp_counter))]
            for i, (r, c) in enumerate(coords):
                markers[r, c] = tmp_counter[i]
        
        if markers.max() == 0:
            # Fallback: one marker for entire mask
            markers[mask] = 1
        
        labels = watershed(-distance, markers, mask=mask)
        
        if labels.max() == 0:
            return {}
        
        # 4) Extract region properties
        props = regionprops_table(
            labels,
            intensity_image=img,
            properties=(
                "label",
                "area",
                "centroid",
                "bbox",
                "eccentricity",
                "mean_intensity",
                "max_intensity",
            ),
        )
        
        # 5) Derive additional properties
        minr = props["bbox-0"]
        minc = props["bbox-1"]
        maxr = props["bbox-2"]
        maxc = props["bbox-3"]
        
        height_px = maxr - minr
        width_px = maxc - minc
        
        # Physical-unit conversions
        centroid_row = props["centroid-0"]
        centroid_col = props["centroid-1"]
        area_um2 = props["area"] * (pixel_size_um ** 2)
        centroid_x_um = centroid_col * pixel_size_um
        centroid_y_um = centroid_row * pixel_size_um
        height_um = height_px * pixel_size_um
        width_um = width_px * pixel_size_um
        
        return {
            "label": props["label"],
            "area": props["area"],
            "centroid_row": centroid_row,
            "centroid_col": centroid_col,
            "centroid_x_um": centroid_x_um,
            "centroid_y_um": centroid_y_um,
            "area_um2": area_um2,
            "mean_intensity": props["mean_intensity"],
            "max_intensity": props["max_intensity"],
            "eccentricity": props["eccentricity"],
            "bbox": np.column_stack([minr, minc, maxr, maxc]),
            "height_um": height_um,
            "width_um": width_um,
        }
    
    @staticmethod
    def apply_filters(props: Dict[str, np.ndarray], filt_dict: Dict[str, Any]) -> np.ndarray:
        """Apply filter criteria to segmentation results.
        
        Args:
            props: Dict of numpy arrays from segment().
            filt_dict: Filter specification with keys:
                area_enabled (bool): Enable area filtering.
                area_um2_min (float): Min area (µm²).
                area_um2_max (float): Max area (µm²).
                mean_intensity_enabled (bool): Enable mean intensity filtering.
                mean_intensity (float): Min mean intensity.
                eccentricity_enabled (bool): Enable eccentricity filtering.
                eccentricity (float): Max eccentricity.
                max_intensity_enabled (bool): Enable max intensity filtering.
                max_intensity (float): Min max intensity.
        
        Returns:
            Boolean mask (numpy array) indicating which objects to keep.
        """
        if not props:
            return np.array([], dtype=bool)
        
        n_objects = len(props["label"])
        keep = np.ones(n_objects, dtype=bool)
        
        if not filt_dict:
            return keep
        
        area_um2 = props["area_um2"]
        mean_intensity = props["mean_intensity"]
        eccentricity = props["eccentricity"]
        max_intensity = props["max_intensity"]
        
        if filt_dict.get("area_enabled", False):
            keep &= (
                (area_um2 >= filt_dict.get("area_um2_min", 0.0))
                & (area_um2 <= filt_dict.get("area_um2_max", np.inf))
            )
        
        if filt_dict.get("mean_intensity_enabled", False):
            keep &= mean_intensity > filt_dict.get("mean_intensity", 0.0)
        
        if filt_dict.get("eccentricity_enabled", False):
            keep &= eccentricity < filt_dict.get("eccentricity", 1.0)
        
        if filt_dict.get("max_intensity_enabled", False):
            keep &= max_intensity > filt_dict.get("max_intensity", 0.0)
        
        return keep


def segment(img: np.ndarray, pixel_size_um: float, **params) -> Dict[str, np.ndarray]:
    """Convenience function for one-shot segmentation.
    
    Args:
        img: 2-D float array.
        pixel_size_um: Pixel size in µm.
        **params: Forwarded to Segmenter constructor (blur_sigma_px, threshold, etc.).
    
    Returns:
        Dict of numpy arrays (see module docstring for schema).
    """
    segmenter = Segmenter(**params)
    return segmenter.segment(img, pixel_size_um)


def estimate_otsu_threshold(img: np.ndarray, blur_sigma_px: float = 5.0) -> float:
    """Estimate Otsu threshold for an image.
    
    Args:
        img: 2-D float array.
        blur_sigma_px: Gaussian blur sigma before thresholding.
    
    Returns:
        Otsu threshold value (in same range as input image).
    """
    img_blur = gaussian_filter(img, sigma=float(blur_sigma_px))
    img_blur = img_blur - np.min(img_blur)
    mx = np.max(img_blur)
    if mx > 0:
        img_blur = img_blur / mx
    return threshold_otsu(img_blur)


def load_params(path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load segmentation parameters from JSON.
    
    Args:
        path: JSON file path. If None, uses default config location.
    
    Returns:
        Dict of parameters (empty if file not found).
    """
    if path is None:
        path = _get_config_dir() / 'imswitch_segmentation_parameters.json'
    else:
        path = Path(path)
    
    if not path.exists():
        return {}
    
    with open(path, 'r') as f:
        return json.load(f)


def save_params(params: Dict[str, Any], path: Optional[str | Path] = None) -> None:
    """Save segmentation parameters to JSON.
    
    Args:
        params: Dict of parameters to save.
        path: JSON file path. If None, uses default config location.
    """
    if path is None:
        config_dir = _get_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / 'imswitch_segmentation_parameters.json'
    else:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(path, 'w') as f:
        json.dump(params, f, indent=2)
