"""WidefieldSTARSS ImProcess reconstructor."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import tifffile as tiff
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import Reconstructor

from .analysis import WidefieldStarssParams, analyze_widefield_starss_pair
from .params_widget import WidefieldStarssParamsWidget
from .result import WidefieldStarssResult

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class WidefieldStarssReconstructor(Reconstructor):
    """Analyze a WidefieldSTARSS H/V acquisition pair."""

    name = "WidefieldSTARSS analysis"
    id = "widefield-starss"
    file_extensions = ["tiff", "tif"]
    description = "Polarization demux, anisotropy maps, and per-region WFS metrics"

    def __init__(self):
        self._logger = initLogger("WidefieldStarssReconstructor")

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return WidefieldStarssParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        return None

    def process(self, data_obj: "DataObj", params: dict) -> WidefieldStarssResult:
        current_path = Path(data_obj.dataPath) if data_obj.dataPath else None
        current_role, counterpart_path = self._resolve_pair_paths(current_path, params)

        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            current_stack = np.asarray(data_obj.data)
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()

        counterpart_stack = tiff.imread(str(counterpart_path))
        if current_role == "H":
            stack_h = current_stack
            stack_v = counterpart_stack
        else:
            stack_h = counterpart_stack
            stack_v = current_stack

        analysis_params = WidefieldStarssParams(
            convention=params.get("convention", "alternating"),
            start_frame=int(params.get("start_frame", 0)),
            n_dark=int(params.get("n_dark", 0)),
            n_off=int(params.get("n_off", 0)),
            sum_stacks=bool(params.get("sum_stacks", False)),
            split_detection=bool(params.get("split_detection", False)),
            split_y=params.get("split_y"),
            segmentation_mode=params.get("segmentation_mode", "none"),
            segmentation_sigma=float(params.get("segmentation_sigma", 2.0)),
            min_size=int(params.get("min_size", 200)),
            hole_size=int(params.get("hole_size", 200)),
            threshold_scale=float(params.get("threshold_scale", 1.0)),
            psf_sigma=float(params.get("psf_sigma", 2.0)),
            psf_min_distance=int(params.get("psf_min_distance", 5)),
            psf_threshold_rel=float(params.get("psf_threshold_rel", 0.1)),
            psf_radius=int(params.get("psf_radius", 3)),
            smooth_sigma=float(params.get("smooth_sigma", 2.0)),
            intensity_threshold=params.get("intensity_threshold"),
        )
        analysis = analyze_widefield_starss_pair(stack_h, stack_v, analysis_params)
        result_params = dict(params)
        result_params["current_role"] = current_role
        result_params["counterpart_path"] = str(counterpart_path)
        result_params["source_h_path"] = str(current_path if current_role == "H" else counterpart_path)
        result_params["source_v_path"] = str(counterpart_path if current_role == "H" else current_path)

        name_base = current_path.stem if current_path is not None else data_obj.name
        return WidefieldStarssResult(
            name=f"{name_base}_wfs",
            analysis=analysis,
            params=result_params,
        )

    def _resolve_pair_paths(
        self,
        current_path: Path | None,
        params: dict,
    ) -> tuple[str, Path]:
        explicit = params.get("counterpart_path")
        role = params.get("current_role", "Auto")

        if current_path is None:
            if not explicit:
                raise ValueError("WidefieldSTARSS needs a current file path or counterpart path")
            if role == "Auto":
                raise ValueError("Set current file role to H or V when current path is unavailable")
            return role, Path(explicit)

        inferred_role, inferred_counterpart = self._infer_counterpart(current_path)
        if role == "Auto":
            role = inferred_role
        if role not in ("H", "V"):
            raise ValueError(f"Unsupported current file role: {role!r}")

        counterpart = Path(explicit) if explicit else inferred_counterpart
        if counterpart is None:
            raise ValueError(
                "Could not infer H/V counterpart. Use a filename ending in _h/_v "
                "or set Counterpart path."
            )
        if not counterpart.exists():
            raise FileNotFoundError(f"WidefieldSTARSS counterpart file not found: {counterpart}")
        return role, counterpart

    @staticmethod
    def _infer_counterpart(path: Path) -> tuple[str, Path | None]:
        stem = path.stem
        suffix = path.suffix
        lower = stem.lower()
        if lower.endswith("_h"):
            return "H", path.with_name(f"{stem[:-2]}_v{suffix}")
        if lower.endswith("_v"):
            return "V", path.with_name(f"{stem[:-2]}_h{suffix}")
        return "H", None
