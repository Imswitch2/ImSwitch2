"""WidefieldSTARSS ImProcess reconstructor."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
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

    def process(
        self, data_obj: "DataObj", params: dict, context=None
    ) -> WidefieldStarssResult:
        current_path = Path(data_obj.dataPath) if data_obj.dataPath else None
        current_role, counterpart_path = self._resolve_pair_paths(
            current_path, params, recorded_role=self._recorded_role(data_obj)
        )

        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            current_stack = np.asarray(data_obj.data)
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()

        counterpart_obj = self._open_counterpart(data_obj, counterpart_path)
        counterpart_preloaded = counterpart_obj.dataLoaded
        try:
            counterpart_obj.checkAndLoadData()
            counterpart_stack = np.asarray(counterpart_obj.data)
        finally:
            if not counterpart_preloaded:
                counterpart_obj.checkAndUnloadData()

        pairing = self._validate_pair(data_obj, counterpart_obj, current_stack, counterpart_stack)
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
            anisotropy_mode=params.get("anisotropy_mode", "stokes"),
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
        result_params.update(pairing)

        name_base = current_path.stem if current_path is not None else data_obj.name
        return WidefieldStarssResult(
            name=f"{name_base}_wfs",
            analysis=analysis,
            params=result_params,
        )

    @staticmethod
    def _open_counterpart(data_obj: "DataObj", counterpart_path: Path) -> "DataObj":
        """Open the other polarization through the shared source pipeline.

        Reading it straight off disk with tifffile skipped the source resolver
        entirely, so the counterpart arrived with no axis labels, no
        calibration and no acquisition layout, and nothing could compare the
        two halves of the pair before analyzing them together.
        """
        from imswitch.improcess.model import DataObj

        return DataObj(
            counterpart_path.name,
            data_obj.datasetName,
            path=str(counterpart_path),
        )

    def _validate_pair(
        self,
        current: "DataObj",
        counterpart: "DataObj",
        current_stack: np.ndarray,
        counterpart_stack: np.ndarray,
    ) -> dict:
        """Reject a pair that cannot describe one acquisition, and say why."""
        if current_stack.shape != counterpart_stack.shape:
            raise ValueError(
                f"WidefieldSTARSS needs two stacks of the same shape, but this "
                f"pair is {current_stack.shape} and {counterpart_stack.shape}. "
                f"Anisotropy is computed pixel by pixel across the pair."
            )

        pairing: dict = {}
        current_scales = list(getattr(current, "axis_scales", None) or [])
        counterpart_scales = list(getattr(counterpart, "axis_scales", None) or [])
        current_unit = getattr(current, "scale_unit", None)
        counterpart_unit = getattr(counterpart, "scale_unit", None)
        if (
            current_scales
            and counterpart_scales
            and (
                current_scales != counterpart_scales
                or current_unit != counterpart_unit
            )
        ):
            raise ValueError(
                f"The two polarization stacks declare different calibrations "
                f"({current_scales} {current_unit} against {counterpart_scales} "
                f"{counterpart_unit}), so their pixels are not comparable."
            )
        if current_scales:
            pairing["pixel_calibration"] = (tuple(current_scales), current_unit)

        current_layout = getattr(getattr(current, "acquisition_layout", None), "layout", None)
        other_layout = getattr(getattr(counterpart, "acquisition_layout", None), "layout", None)
        recorded = [
            layout
            for layout in (current_layout, other_layout)
            if layout is not None and layout.provenance in ("recorded", "user-override")
        ]
        if len(recorded) == 2:
            current_kinds = [loop.kind for loop in recorded[0].event_loops]
            other_kinds = [loop.kind for loop in recorded[1].event_loops]
            if current_kinds != other_kinds:
                raise ValueError(
                    f"The two polarization stacks were acquired with different "
                    f"loop structures ({current_kinds} against {other_kinds}), "
                    f"so their frames do not correspond."
                )
            pairing["acquisition_loops"] = tuple(current_kinds)
        elif recorded:
            self._logger.warning(
                "Only one half of the WidefieldSTARSS pair carries a recorded "
                "acquisition layout; the pair could not be cross-checked."
            )
        return pairing

    @staticmethod
    def _recorded_role(data_obj: "DataObj") -> str | None:
        """The polarization role the acquisition wrote, if it wrote one."""
        attrs = getattr(data_obj, "attrs", None) or {}
        for key in (
            "WidefieldStarss:polarization_role",
            "ome:WidefieldStarss:polarization_role",
        ):
            value = attrs.get(key)
            if value is None:
                continue
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            role = str(value).strip().upper()
            if role in ("H", "V"):
                return role
        return None

    def _resolve_pair_paths(
        self,
        current_path: Path | None,
        params: dict,
        *,
        recorded_role: str | None = None,
    ) -> tuple[str, Path]:
        explicit = params.get("counterpart_path")
        role = params.get("current_role", "Auto")
        h_suffix = str(params.get("h_suffix") or "_h")
        v_suffix = str(params.get("v_suffix") or "_v")

        if current_path is None:
            if not explicit:
                raise ValueError("WidefieldSTARSS needs a current file path or counterpart path")
            if role == "Auto":
                raise ValueError("Set current file role to H or V when current path is unavailable")
            return role, Path(explicit)

        inferred_role, inferred_counterpart = self._infer_counterpart(
            current_path, h_suffix, v_suffix
        )
        if role == "Auto":
            if recorded_role is not None:
                role = recorded_role
            elif inferred_role is not None:
                # Filename parsing is a legacy fallback, not a statement by the
                # recording. Say so: getting H and V the wrong way round
                # inverts the anisotropy rather than failing visibly.
                self._logger.warning(
                    "WidefieldSTARSS role %s was inferred from the filename "
                    "suffix, not from recorded metadata.", inferred_role
                )
                role = inferred_role
            else:
                raise ValueError(
                    f"Could not determine whether {current_path.name!r} is the "
                    f"H or V polarization: it carries no recorded role and its "
                    f"name does not end in {h_suffix}/{v_suffix}. Set the "
                    f"current file role explicitly."
                )
        if role not in ("H", "V"):
            raise ValueError(f"Unsupported current file role: {role!r}")

        counterpart = Path(explicit) if explicit else inferred_counterpart
        if counterpart is None:
            raise ValueError(
                f"Could not infer H/V counterpart. Use a filename ending in "
                f"{h_suffix}/{v_suffix} (configurable under Pairing) or set "
                f"Counterpart path."
            )
        if not counterpart.exists():
            raise FileNotFoundError(f"WidefieldSTARSS counterpart file not found: {counterpart}")
        return role, counterpart

    @staticmethod
    def _infer_counterpart(
        path: Path,
        h_suffix: str = "_h",
        v_suffix: str = "_v",
    ) -> tuple[str | None, Path | None]:
        stem = path.stem
        suffix = path.suffix
        lower = stem.lower()
        # If both suffixes match (e.g. 'h' and '_h'), the longer one wins so
        # the more specific suffix cannot be shadowed by the shorter.
        matches = [
            (role, own, other)
            for role, own, other in (("H", h_suffix, v_suffix), ("V", v_suffix, h_suffix))
            if own and lower.endswith(own.lower())
        ]
        if not matches:
            # No suffix matched, so the role is unknown. Returning "H" made an
            # unlabelled file silently the H half, which inverts the anisotropy
            # when it was really V.
            return None, None
        role, own, other = max(matches, key=lambda item: len(item[1]))
        return role, path.with_name(f"{stem[:-len(own)]}{other}{suffix}")
