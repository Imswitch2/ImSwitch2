from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping,TYPE_CHECKING

from ..state import ParamPath

if TYPE_CHECKING:
    from ...calibration import SLMSectionCalibration
    from ...cgh.execution.status import CGHStatus

@dataclass(frozen=True)
class SectionUpdate:
    """Result of a successfully committed section-runtime transaction.

    ``values`` contains the complete authoritative serialized section state.

    ``normalized_values`` contains the normalized value produced for every
    parameter path explicitly included in the patch.

    ``cgh_changed_values`` contains additional authoritative changes made by
    CGH target canonicalization after the requested values were applied.

    ``base_revision`` identifies the snapshot this update advances from, while
    ``calibration`` carries the final detached section calibration.

    ``target_definition_changed`` reports whether the finalized base-target
    identity changed, independently of target configuration/policy edits.

    ``frame_changed`` reports whether the effective eight-bit section frame
    changed, independent of state/status changes.
    """

    base_revision: int
    revision: int
    values: dict
    normalized_values: Mapping[ParamPath,Any]
    cgh_changed_values: Mapping[ParamPath,Any]
    changed_paths: tuple[ParamPath, ...]
    cgh_status: CGHStatus
    calibration: SLMSectionCalibration | None
    warnings: tuple[str, ...] = ()
    target_definition_changed: bool = False
    frame_changed: bool = False

    @property
    def applied_values(self) -> dict[ParamPath, Any]:
        values = dict(self.normalized_values)
        values.update(self.cgh_changed_values)
        return values

    @classmethod
    def from_transition(
        cls,
        previous_state,
        current_state,
        base_revision: int,
        revision: int,
        normalized_values: Mapping[ParamPath,Any],
        cgh_changed_values: Mapping[ParamPath,Any],
        cgh_status: CGHStatus,
        calibration: SLMSectionCalibration | None,
        warnings: tuple[str, ...] = (),
        target_definition_changed: bool = False,
        frame_changed: bool = False,
    ) -> "SectionUpdate":
        applied_values = dict(normalized_values)
        applied_values.update(cgh_changed_values)

        for path,value in applied_values.items():
            final_value = current_state.get_parameter(path)
            if final_value != value:
                raise RuntimeError(
                    f"Update value for {path} does not match final state: "
                    f"{value!r} != {final_value!r}"
                )

        changed_paths = tuple(
            path for path,value in applied_values.items()
            if previous_state.get_parameter(path) != value
        )

        if int(revision) <= int(base_revision):
            raise ValueError(
                "Section update revision must advance beyond its base revision"
            )

        return cls(
            base_revision=int(base_revision),
            revision=int(revision),
            values=current_state.to_dict(),
            normalized_values=dict(normalized_values),
            cgh_changed_values=dict(cgh_changed_values),
            changed_paths=changed_paths,
            cgh_status=cgh_status,
            calibration=(
                None if calibration is None else calibration.copy()
            ),
            target_definition_changed=bool(target_definition_changed),
            frame_changed=bool(frame_changed),
            warnings=tuple(warnings),
        )
