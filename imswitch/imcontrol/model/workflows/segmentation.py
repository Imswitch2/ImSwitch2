"""Reusable watershed-based cell segmentation.

Provides the Segmenter class for 2-D float image segmentation via the shared
ImProcess segmentation kernel:
- Gaussian blur -> normalization -> threshold -> distance-transform watershed
- Region property extraction from ``SegmentationRegion`` records

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

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from imswitch.improcess.analysis.segmentation import (
    otsu_threshold,
    prepare_segmentation_image,
    segment_image,
)


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


@dataclass(frozen=True)
class CellTargetResult:
    """Filtered cell-target detection result for tiling workflows."""

    props: Dict[str, np.ndarray]
    keep: np.ndarray
    indices: np.ndarray
    positions: np.ndarray
    segmenter: object

    @property
    def n_total(self) -> int:
        if not self.props:
            return 0
        return int(len(self.props["label"]))

    @property
    def n_valid(self) -> int:
        return int(len(self.indices))

    @property
    def filtered_props(self) -> Dict[str, np.ndarray]:
        if not self.props:
            return {}
        return {key: value[self.indices] for key, value in self.props.items()}


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

        # Populated by segment(); exposed for visualization
        self.mask: Optional[np.ndarray] = None
        self.labels: Optional[np.ndarray] = None
        self.img_blur: Optional[np.ndarray] = None
    
    def segment(self, img: np.ndarray, pixel_size_um: float) -> Dict[str, np.ndarray]:
        """Segment cells in a 2-D image.

        After calling, the intermediate ``mask``, ``labels`` and ``img_blur``
        arrays are also accessible as attributes on the Segmenter instance,
        for visualization.

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
        pixel_size_um = float(pixel_size_um)
        if pixel_size_um <= 0:
            raise ValueError("pixel_size_um must be positive")

        # Auto-compute pixel-based thresholds from physical units
        if self._min_area_px_override is not None:
            min_area_px = self._min_area_px_override
        else:
            min_area_px = max(10, int(self.min_area_um2 / pixel_size_um ** 2))
        
        if self._peak_min_dist_px_override is not None:
            peak_min_dist_px = self._peak_min_dist_px_override
        else:
            peak_min_dist_px = max(3, int(self.peak_min_dist_um / pixel_size_um))
        
        # Tiling historically thresholds in normalized post-blur intensity units.
        if self.threshold is None:
            threshold_method = "otsu"
            threshold_value = None
        else:
            threshold_method = "manual"
            threshold_value = float(self.threshold)

        analysis = segment_image(
            img,
            threshold_method=threshold_method,
            threshold_value=threshold_value,
            min_area=int(min_area_px),
            smooth_sigma=float(self.blur_sigma_px),
            normalize=True,
            label_method="watershed",
            watershed_min_distance=int(peak_min_dist_px),
            pixel_size_um=pixel_size_um,
        )

        self.img_blur = analysis.processed_image
        self.mask = analysis.mask
        self.labels = analysis.labels

        if not analysis.regions:
            return {}

        return {
            "label": np.asarray([region.label for region in analysis.regions], dtype=np.int32),
            "area": np.asarray([region.area_pixels for region in analysis.regions], dtype=float),
            "centroid_row": np.asarray([region.centroid_row for region in analysis.regions], dtype=float),
            "centroid_col": np.asarray([region.centroid_col for region in analysis.regions], dtype=float),
            "centroid_x_um": np.asarray([region.centroid_x_um for region in analysis.regions], dtype=float),
            "centroid_y_um": np.asarray([region.centroid_y_um for region in analysis.regions], dtype=float),
            "area_um2": np.asarray([region.area_um2 for region in analysis.regions], dtype=float),
            "mean_intensity": np.asarray([region.mean_intensity for region in analysis.regions], dtype=float),
            "max_intensity": np.asarray([region.max_intensity for region in analysis.regions], dtype=float),
            "eccentricity": np.asarray([region.eccentricity for region in analysis.regions], dtype=float),
            "bbox": np.asarray([region.bbox for region in analysis.regions], dtype=np.int32),
            "height_um": np.asarray([region.height_um for region in analysis.regions], dtype=float),
            "width_um": np.asarray([region.width_um for region in analysis.regions], dtype=float),
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


def detect_cell_targets(
    img: np.ndarray,
    pixel_size_um: float,
    params: Optional[Dict[str, Any]] = None,
) -> CellTargetResult:
    """Segment and filter cell targets for tiling.

    This keeps the tiling workflow and controller on one code path while
    preserving the legacy `Segmenter` output schema.
    """
    params = params or {}
    segmenter = Segmenter(
        blur_sigma_px=params.get("blur_sigma_px", 3.0),
        threshold=params.get("threshold"),
        min_area_um2=params.get("min_area_um2", 30.0),
        peak_min_dist_um=params.get("peak_min_dist_um", 8.0),
        min_area_px=params.get("min_area_px"),
        peak_min_dist_px=params.get("peak_min_dist_px"),
    )
    props = segmenter.segment(img, pixel_size_um)
    if not props:
        empty = np.array([], dtype=bool)
        return CellTargetResult(
            props={},
            keep=empty,
            indices=np.array([], dtype=int),
            positions=np.empty((0, 2), dtype=float),
            segmenter=segmenter,
        )

    keep = Segmenter.apply_filters(props, params)
    indices = np.where(keep)[0]
    positions = np.column_stack([
        props["centroid_row"][indices],
        props["centroid_col"][indices],
    ])
    return CellTargetResult(
        props=props,
        keep=keep,
        indices=indices,
        positions=positions,
        segmenter=segmenter,
    )


def estimate_otsu_threshold(img: np.ndarray, blur_sigma_px: float = 5.0) -> float:
    """Estimate Otsu threshold for an image.
    
    Args:
        img: 2-D float array.
        blur_sigma_px: Gaussian blur sigma before thresholding.
    
    Returns:
        Otsu threshold value (in same range as input image).
    """
    img_blur = prepare_segmentation_image(
        img,
        smooth_sigma=float(blur_sigma_px),
        normalize=True,
    )
    return otsu_threshold(img_blur)


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
