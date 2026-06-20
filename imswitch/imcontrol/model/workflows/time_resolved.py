"""Detector-neutral workflows for time-resolved scan products.

These workflows sit above :class:`TimeResolvedDetectorFacade`, so they can use
the Swabian Time Tagger today and any future time-tagger manager that implements
the same small contract.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, TYPE_CHECKING, Optional

import numpy as np
import tifffile as tf

from imswitch.imcontrol.model.timeresolved import (
    GateSpec,
    LifetimeFitConfig,
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
)
from imswitch.imcontrol.model.workflows.paths import (
    default_measurements_root,
    resolve_measurements_root,
)

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade

logger = logging.getLogger(__name__)

DEFAULT_MEASUREMENTS_ROOT = default_measurements_root()
AcquisitionCallable = Callable[[], object]


@dataclass
class TimeResolvedWorkflowParams:
    """Common settings for time-resolved detector workflows."""

    capture_cube: bool = False
    gates: tuple[GateSpec, ...] = ()
    fit: LifetimeFitConfig = field(default_factory=LifetimeFitConfig)
    include_live_products: bool = False
    max_retained_products: int = 1
    timeout_s: float = 60.0
    measurements_root: Optional[Path | str] = field(
        default_factory=lambda: DEFAULT_MEASUREMENTS_ROOT
    )
    save_folder: Optional[Path | str] = None
    measurement_name: str = "time_resolved"
    save_h5: bool = True
    save_npz: bool = False
    save_tiff: bool = True

    def __post_init__(self) -> None:
        self.capture_cube = bool(self.capture_cube)
        self.gates = tuple(self.gates or ())
        if self.fit is None:
            self.fit = LifetimeFitConfig()
        self.include_live_products = bool(self.include_live_products)
        self.max_retained_products = max(1, int(self.max_retained_products))
        self.timeout_s = float(self.timeout_s)
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.measurement_name = _safe_label(self.measurement_name or "time_resolved")
        self.save_h5 = bool(self.save_h5)
        self.save_npz = bool(self.save_npz)
        self.save_tiff = bool(self.save_tiff)

    def to_scan_config(self) -> TimeResolvedScanConfig:
        """Return the detector-manager config requested by this workflow."""

        return TimeResolvedScanConfig(
            capture_cube=self.capture_cube,
            gates=self.gates,
            fit=self.fit,
            include_live_products=self.include_live_products,
            max_retained_products=self.max_retained_products,
        )


@dataclass
class BinnedPhotonArrivalParams(TimeResolvedWorkflowParams):
    """Workflow settings for retaining the full per-pixel TCSPC cube."""

    capture_cube: bool = True
    measurement_name: str = "photon_arrivals"


@dataclass
class GatedSTEDParams(TimeResolvedWorkflowParams):
    """Workflow settings for gated-STED image reconstruction."""

    capture_cube: bool = False
    measurement_name: str = "gated_sted"


@dataclass
class TauSTEDParams(TimeResolvedWorkflowParams):
    """Workflow settings for tau-STED lifetime-image reconstruction."""

    capture_cube: bool = False
    measurement_name: str = "tau_sted"


@dataclass
class TimeResolvedWorkflowResult:
    """Return value for a time-resolved workflow run."""

    products: TimeResolvedScanProducts
    save_folder: Path
    output_paths: dict[str, Path]


class TimeResolvedScanWorkflow:
    """Configure a time-resolved detector, run an optional scan, and save products.

    ``acquisition`` is intentionally just a no-argument callable. Existing scan
    controllers, scripts, or future scan facades can be plugged in by passing a
    lambda without coupling this workflow to one scanner implementation.
    """

    def __init__(
        self,
        facade: MicroscopeFacade,
        params: TimeResolvedWorkflowParams,
        acquisition: Optional[AcquisitionCallable] = None,
    ) -> None:
        self.facade = facade
        self.params = params
        self.acquisition = acquisition

    def run(
        self,
        acquisition: Optional[AcquisitionCallable] = None,
        measurement_name: Optional[str] = None,
    ) -> TimeResolvedWorkflowResult:
        detector = getattr(self.facade, "time_resolved", None)
        if detector is None:
            raise RuntimeError("Time-resolved workflow requires facade.time_resolved")

        params = self.params
        if measurement_name is not None:
            params = replace(params, measurement_name=measurement_name)

        detector.clear()
        detector.configure(params.to_scan_config())

        acquire = acquisition or self.acquisition
        if acquire is None:
            scan = getattr(self.facade, "scan", None)
            if scan is not None and callable(getattr(scan, "run_once", None)):
                acquire = lambda: scan.run_once(timeout_s=params.timeout_s)
        if acquire is not None:
            acquire()

        products = detector.wait_for_final(timeout_s=params.timeout_s)
        output_paths = self._save(products, params)
        return TimeResolvedWorkflowResult(
            products=products,
            save_folder=self._resolve_save_folder(params),
            output_paths=output_paths,
        )

    def _save(
        self,
        products: TimeResolvedScanProducts,
        params: TimeResolvedWorkflowParams,
    ) -> dict[str, Path]:
        output_paths: dict[str, Path] = {}
        if not (params.save_h5 or params.save_npz or params.save_tiff):
            return output_paths

        folder = self._resolve_save_folder(params)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        prefix = f"{params.measurement_name}_{stamp}"

        if params.save_h5:
            output_paths["h5"] = _save_h5(
                products,
                folder / f"{prefix}.h5",
                workflow_name=params.measurement_name,
                gates=params.gates,
            )
        if params.save_npz:
            output_paths["npz"] = _save_npz(products, folder / f"{prefix}.npz")
        if params.save_tiff:
            output_paths.update(_save_tiffs(products, folder, prefix))

        return output_paths

    @staticmethod
    def _resolve_save_folder(params: TimeResolvedWorkflowParams) -> Path:
        if params.save_folder is not None:
            return Path(params.save_folder)
        root = resolve_measurements_root(params.measurements_root)
        return root / time.strftime("%Y_%m_%d")


class BinnedPhotonArrivalWorkflow(TimeResolvedScanWorkflow):
    """Opt-in workflow that stores binned photon arrival times per pixel."""

    def __init__(
        self,
        facade: MicroscopeFacade,
        params: Optional[BinnedPhotonArrivalParams] = None,
        acquisition: Optional[AcquisitionCallable] = None,
    ) -> None:
        params = params or BinnedPhotonArrivalParams()
        if not params.capture_cube:
            params = replace(params, capture_cube=True)
        super().__init__(facade, params, acquisition)

    def run(
        self,
        acquisition: Optional[AcquisitionCallable] = None,
        measurement_name: Optional[str] = None,
    ) -> TimeResolvedWorkflowResult:
        result = super().run(acquisition=acquisition, measurement_name=measurement_name)
        if result.products.cube_counts is None:
            raise RuntimeError(
                "Binned photon-arrival workflow did not receive cube_counts. "
                "Check that the detector supports binned cubes and capture_cube=True."
            )
        return result


class GatedSTEDWorkflow(TimeResolvedScanWorkflow):
    """Workflow for software time-gated STED image products."""

    def __init__(
        self,
        facade: MicroscopeFacade,
        params: GatedSTEDParams,
        acquisition: Optional[AcquisitionCallable] = None,
    ) -> None:
        super().__init__(facade, params, acquisition)

    def run(
        self,
        acquisition: Optional[AcquisitionCallable] = None,
        measurement_name: Optional[str] = None,
    ) -> TimeResolvedWorkflowResult:
        if not self.params.gates:
            raise ValueError("GatedSTEDWorkflow requires at least one GateSpec")
        result = super().run(acquisition=acquisition, measurement_name=measurement_name)
        missing = [gate.name for gate in self.params.gates if gate.name not in result.products.gate_images]
        if missing:
            raise RuntimeError("Missing gated-STED images: " + ", ".join(missing))
        return result


class TauSTEDWorkflow(TimeResolvedScanWorkflow):
    """Workflow for tau-STED lifetime products from a time-resolved detector."""

    def __init__(
        self,
        facade: MicroscopeFacade,
        params: Optional[TauSTEDParams] = None,
        acquisition: Optional[AcquisitionCallable] = None,
    ) -> None:
        super().__init__(facade, params or TauSTEDParams(), acquisition)

    def run(
        self,
        acquisition: Optional[AcquisitionCallable] = None,
        measurement_name: Optional[str] = None,
    ) -> TimeResolvedWorkflowResult:
        result = super().run(acquisition=acquisition, measurement_name=measurement_name)
        if result.products.lifetime_ns is None:
            raise RuntimeError("TauSTEDWorkflow did not receive a lifetime_ns image")
        return result


def _save_h5(
    products: TimeResolvedScanProducts,
    path: Path,
    *,
    workflow_name: str,
    gates: tuple[GateSpec, ...] = (),
) -> Path:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError(
            "h5py is required to save time-resolved products as HDF5. "
            "Set save_h5=False and save_npz=True to use NumPy output instead."
        ) from exc

    metadata = products.metadata or {}
    gate_specs = {gate.name: gate for gate in gates}

    with h5py.File(str(path), "w") as h5:
        h5.attrs["created_unix_s"] = time.time()
        h5.attrs["workflow_name"] = workflow_name
        h5.attrs["backend"] = str(metadata.get("backend", ""))
        h5.attrs["detector_name"] = str(metadata.get("detector_name", ""))
        h5.attrs["metadata_json"] = _metadata_json(metadata)

        scan = h5.create_group("scan")
        scan.attrs["metadata_json"] = _metadata_json(metadata.get("scan_info", {}))

        tr = h5.create_group("time_resolved")
        tr.create_dataset("t_axis_ns", data=products.t_axis_ns)
        tr.create_dataset("intensity", data=products.intensity)
        tr.create_dataset("decay_counts", data=products.decay_counts)
        if products.cube_counts is not None:
            ds = tr.create_dataset(
                "cube_counts",
                data=products.cube_counts,
                compression="gzip",
            )
            ds.attrs["axes_json"] = json.dumps(tuple(products.cube_axes))
        if products.lifetime_ns is not None:
            tr.create_dataset("lifetime_ns", data=products.lifetime_ns)

        gates_group = h5.create_group("gates")
        used_gate_keys: set[str] = set()
        for idx, (name, image) in enumerate(products.gate_images.items()):
            key = _safe_label(name)
            if key in used_gate_keys:
                key = f"{idx}_{key}"
            used_gate_keys.add(key)
            ds = gates_group.create_dataset(key, data=image)
            ds.attrs["gate_name"] = name
            if name in gate_specs:
                ds.attrs["start_ns"] = float(gate_specs[name].start_ns)
                ds.attrs["stop_ns"] = float(gate_specs[name].stop_ns)

        fit = h5.create_group("fit")
        fit.attrs["method"] = str(metadata.get("fit_method", ""))
        fit.attrs["min_counts_per_pixel"] = int(metadata.get("min_counts_per_pixel", 0) or 0)
        fit.attrs["laser_rep_rate_mhz"] = float(metadata.get("laser_rep_rate_mhz", 0.0) or 0.0)
        fit.attrs["peak_bin"] = int(metadata.get("peak_bin", 0) or 0)
        fit.attrs["peak_time_ns"] = float(metadata.get("peak_time_ns", 0.0) or 0.0)
        fit.attrs["global_tau_ns"] = float(products.global_tau_ns)

        tr.attrs["cube_axes_json"] = json.dumps(tuple(products.cube_axes))
        tr.attrs["global_tau_ns"] = float(products.global_tau_ns)
        tr.attrs["is_final"] = bool(products.is_final)

    logger.info("Saved time-resolved HDF5 products to %s", path)
    return path


def _save_npz(products: TimeResolvedScanProducts, path: Path) -> Path:
    arrays = {
        "t_axis_ns": products.t_axis_ns,
        "intensity": products.intensity,
        "decay_counts": products.decay_counts,
        "global_tau_ns": np.asarray(products.global_tau_ns, dtype=np.float64),
        "is_final": np.asarray(products.is_final, dtype=bool),
        "cube_axes_json": np.asarray(json.dumps(tuple(products.cube_axes))),
        "metadata_json": np.asarray(_metadata_json(products.metadata)),
    }
    if products.cube_counts is not None:
        arrays["cube_counts"] = products.cube_counts
    if products.lifetime_ns is not None:
        arrays["lifetime_ns"] = products.lifetime_ns

    gate_key_map = {}
    for idx, (name, image) in enumerate(products.gate_images.items()):
        key = f"gate_{idx}_{_safe_label(name)}"
        arrays[key] = image
        gate_key_map[name] = key
    arrays["gate_key_map_json"] = np.asarray(json.dumps(gate_key_map))

    np.savez_compressed(path, **arrays)
    logger.info("Saved time-resolved NumPy products to %s", path)
    return path


def _save_tiffs(
    products: TimeResolvedScanProducts,
    folder: Path,
    prefix: str,
) -> dict[str, Path]:
    paths: dict[str, Path] = {}

    intensity_path = folder / f"{prefix}_intensity.tif"
    tf.imwrite(str(intensity_path), products.intensity.astype(np.float32, copy=False))
    paths["intensity_tiff"] = intensity_path

    if products.lifetime_ns is not None:
        lifetime_path = folder / f"{prefix}_lifetime_ns.tif"
        tf.imwrite(str(lifetime_path), products.lifetime_ns.astype(np.float32, copy=False))
        paths["lifetime_tiff"] = lifetime_path

    for name, image in products.gate_images.items():
        key = f"gate_{_safe_label(name)}_tiff"
        gate_path = folder / f"{prefix}_gate_{_safe_label(name)}.tif"
        tf.imwrite(str(gate_path), image.astype(np.float32, copy=False))
        paths[key] = gate_path

    return paths


def _metadata_json(metadata: dict) -> str:
    return json.dumps(metadata or {}, default=_json_default, sort_keys=True)


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    try:
        return str(value)
    except Exception:
        return repr(value)


def _safe_label(label: str) -> str:
    label = str(label).strip()
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
    label = label.strip("._-")
    return label or "time_resolved"
