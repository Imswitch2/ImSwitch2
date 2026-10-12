"""Dispatch CGH-session window actions to focused Qt feedback coordinators."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any,Mapping,TYPE_CHECKING

from ...cgh.session_window import MeasurementsAction

if TYPE_CHECKING:
    from .adaptation import FeedbackAdaptationCoordinator
    from .automatic import AutomaticFeedbackCoordinator
    from .localization import LocalizationCoordinator
    from .measurement import MeasurementCoordinator
    from .references import PositionReferenceCoordinator


class FeedbackActionRouter:
    """Translate window action identifiers into focused coordinator operations."""

    def __init__(
        self,
        *,
        measurement: "MeasurementCoordinator",
        localization: "LocalizationCoordinator",
        adaptation: "FeedbackAdaptationCoordinator",
        references: "PositionReferenceCoordinator",
        automatic: "AutomaticFeedbackCoordinator",
        editor_writes_allowed: Callable[[],bool],
        compute_adapted_cgh: Callable[[str],None],
        show_inspection: Callable[[str],None],
        report_error: Callable[[str,Exception],None],
    ) -> None:
        self._measurement = measurement
        self._localization = localization
        self._adaptation = adaptation
        self._references = references
        self._automatic = automatic
        self._editor_writes_allowed = editor_writes_allowed
        self._compute_adapted_cgh = compute_adapted_cgh
        self._show_inspection = show_inspection
        self._report_error = report_error

    def dispatch(
        self,section_key: str,action: str,options: Mapping[str,Any],
    ) -> None:
        if not self._editor_writes_allowed():
            return
        try:
            request = MeasurementsAction(str(action))
        except Exception:
            self._report_error(
                "Unknown measurements action",
                RuntimeError("Unsupported measurements action: %r" % action),
            )
            return

        values = dict(options or {})
        if request is MeasurementsAction.AUTOMATIC_STOP:
            self._automatic.stop()
            return
        if request is MeasurementsAction.OBSERVATION_PLANE_SET:
            self._measurement.select_observation_plane(
                section_key,values.get("plane"),
            )
            return
        if self._automatic.operation_active:
            return

        if request is MeasurementsAction.ACQUIRE:
            source = str(values.get("detector") or "")
            self._measurement.set_source_override(section_key,source or None)
            self._measurement.acquire(
                section_key,source,
                reuse_previous_localization=bool(
                    values.get("reuse_previous_localization",False)
                ),
            )
        elif request is MeasurementsAction.VISUAL_BACKGROUND_ACQUIRE:
            source = str(values.get("detector") or "")
            self._measurement.capture_visual_background(section_key,source)
        elif request is MeasurementsAction.LOAD:
            self._measurement.load(
                section_key,
                reuse_previous_localization=bool(
                    values.get("reuse_previous_localization",False)
                ),
            )
        elif request is MeasurementsAction.LOCALIZATION_RUN:
            self._localization.run_localization_candidate(
                section_key,values.get("parameters",{}),
            )
        elif request is MeasurementsAction.LOCALIZATION_INFER_MISSING:
            self._localization.infer_missing_localization_candidate(
                section_key,values.get("localization"),
                values.get("parameters",{}),
            )
        elif request is MeasurementsAction.LOCALIZATION_ACCEPT:
            self._localization.accept_localization(
                section_key,values.get("localization"),
                values.get("parameters",{}),
            )
        elif request is MeasurementsAction.LOCALIZATION_REUSE:
            self._localization.reuse_localization(section_key)
        elif request is MeasurementsAction.FEEDBACK_PARAMETERS:
            self._localization.update_parameters(
                section_key,str(values.get("group") or ""),
                values.get("changes",{}),
                localization=values.get("localization"),
                localization_parameters=values.get(
                    "localization_parameters",{}
                ),
            )
        elif request is MeasurementsAction.INTENSITY_APPLY:
            self._adaptation.apply_intensity_feedback(section_key)
        elif request is MeasurementsAction.INTENSITY_RESET:
            self._adaptation.reset_intensity_feedback(section_key)
        elif request is MeasurementsAction.POSITION_APPLY:
            self._adaptation.apply_position_correction(section_key)
        elif request is MeasurementsAction.POSITION_REFERENCE_SET:
            kwargs = {
                "mode":str(values.get("mode") or "global_fit"),
                "reference_name":values.get("reference_name"),
                "editable_geometry":values.get("editable_geometry"),
                "editable_locks":values.get("editable_locks"),
                "correspondence":values.get("correspondence"),
                "correction_mode":values.get("correction_mode"),
                "polynomial_degree":values.get("polynomial_degree"),
            }
            if "affine_transform" in values:
                kwargs["affine_transform"] = values.get("affine_transform")
            if "manual_transform" in values:
                kwargs["manual_transform"] = values.get("manual_transform")
            self._references.set_position_reference(
                section_key,**kwargs,lightweight=bool(values.get("lightweight",False)),
            )
        elif request is MeasurementsAction.POSITION_REFERENCE_FIT_CENTER:
            self._references.fit_position_reference_center(section_key)
        elif request is MeasurementsAction.POSITION_REFERENCE_ADD:
            self._references.add_position_reference(
                section_key,str(values.get("name") or "").strip(),
            )
        elif request is MeasurementsAction.POSITION_REFERENCE_PERSIST:
            self._references.persist_position_reference(
                section_key,str(values.get("name") or "").strip(),
            )
        elif request is MeasurementsAction.POSITION_REFERENCE_DELETE:
            self._references.delete_position_reference(
                section_key,str(values.get("name") or "").strip(),
            )
        elif request is MeasurementsAction.POSITION_SET_ACTIVE:
            ok,transition = self._adaptation.set_position_active(
                section_key,bool(values.get("active",False)),
            )
            if ok and transition is not None:
                self._compute_adapted_cgh(section_key)
        elif request is MeasurementsAction.POSITION_CLEAR:
            ok,transition = self._adaptation.clear_position_correction(section_key)
            if ok and transition is not None:
                self._compute_adapted_cgh(section_key)
        elif request is MeasurementsAction.COMPUTE_ADAPTED:
            self._compute_adapted_cgh(section_key)
        elif request is MeasurementsAction.RESET_TO_ROUND:
            self._adaptation.reset_to_round(
                section_key,int(values.get("round_index")),
            )
        elif request is MeasurementsAction.PROPAGATE_SELECTED:
            self._adaptation.propagate_round(
                section_key,
                int(values.get("round_index")),
                position_context=str(
                    values.get("position_context") or "corrected"
                ),
                pad_size=values.get("pad_size",1024),
            )
        elif request is MeasurementsAction.INSPECT:
            self._show_inspection(section_key)
        elif request is MeasurementsAction.AUTOMATIC_START:
            self._automatic.start(
                section_key,
                rounds=int(values.get("rounds",1)),
                source=str(values.get("detector") or ""),
                reuse_previous_localization=bool(
                    values.get("reuse_previous_localization",False)
                ),
            )
