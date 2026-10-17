"""Detector-neutral workflows for time-resolved scan products.

These workflows sit above :class:`TimeResolvedDetectorFacade`, so they can use
the Swabian Time Tagger today and any future time-tagger manager that implements
the same small contract.
"""

from __future__ import annotations

from contextlib import nullcontext
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, TYPE_CHECKING, Optional

import numpy as np
import tifffile as tf

from imswitch.imcontrol.model.timeresolved.io import (
    _metadata_json as _io_metadata_json,
    safe_label,
    save_h5,
    save_npz,
    save_tiffs,
)
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

        leaseFactory = getattr(detector, "acquisition_lease", None)
        leaseContext = leaseFactory() if callable(leaseFactory) else nullcontext()
        with leaseContext:
            params = self.params
            if measurement_name is not None:
                params = replace(params, measurement_name=measurement_name)

            # This run owns the detector's product session: a second run
            # that overlaps is refused at configure, before it touches the
            # scan, and its cleanup cannot erase this run's state.
            token = f"{params.measurement_name}-{uuid.uuid4().hex[:8]}"
            detector.clear()  # an unowned leftover; an owned one stays put
            detector.configure(params.to_scan_config(), owner=token)

            acquire = acquisition or self.acquisition
            if acquire is None:
                scan = getattr(self.facade, "scan", None)
                if scan is not None and callable(getattr(scan, "run_once", None)):
                    acquire = lambda: scan.run_once(timeout_s=params.timeout_s)
            try:
                if acquire is not None:
                    acquire()

                products = detector.wait_for_final(
                    timeout_s=params.timeout_s, owner=token
                )
            finally:
                # Product capture is armed for this run only. Left armed, every
                # later scan would copy products and compute gates, and the
                # Swabian backend would reject any later z/t scan as an
                # unsupported outer axis until something cleared it.
                detector.clear(owner=token)
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
        return save_products(products, params)

    @staticmethod
    def _resolve_save_folder(params: TimeResolvedWorkflowParams) -> Path:
        return resolve_save_folder(params)


def resolve_save_folder(params: TimeResolvedWorkflowParams) -> Path:
    """``params.save_folder``, else today's folder under the measurements root."""
    if params.save_folder is not None:
        return Path(params.save_folder)
    root = resolve_measurements_root(params.measurements_root)
    return root / time.strftime("%Y_%m_%d")


def save_products(
    products: TimeResolvedScanProducts,
    params: TimeResolvedWorkflowParams,
) -> dict[str, Path]:
    """Write the products the way a workflow run does (HDF5, NPZ, TIFFs as
    ``params`` select), so the Lifetime widget's Save and a script produce
    identical files. Returns ``{kind: path}``."""
    output_paths: dict[str, Path] = {}
    if not (params.save_h5 or params.save_npz or params.save_tiff):
        return output_paths
    folder = resolve_save_folder(params)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    prefix = f"{params.measurement_name}_{stamp}"
    if params.save_h5:
        output_paths["h5"] = save_h5(
            products,
            folder / f"{prefix}.h5",
            workflow_name=params.measurement_name,
            gates=params.gates,
        )
    if params.save_npz:
        output_paths["npz"] = save_npz(products, folder / f"{prefix}.npz")
    if params.save_tiff:
        output_paths.update(save_tiffs(products, folder, prefix))
    return output_paths


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


# The writers live in timeresolved.io (shared with the Lifetime widget);
# the old private names stay importable.
_save_h5 = save_h5
_save_npz = save_npz
_save_tiffs = save_tiffs
_metadata_json = _io_metadata_json
_safe_label = safe_label
