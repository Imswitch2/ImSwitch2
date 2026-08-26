from __future__ import annotations

from dataclasses import dataclass,field


from ...calibration import SLMSectionCalibration
from ...cgh import CGHStatus
from ..state import GroupStateModel
from .geometry import SectionGeometry
from .model import SLMSectionState
from .presentation import SectionPresentation
from .update import SectionUpdate


@dataclass(frozen=True)
class SectionGroupSnapshot:
    """One group inside a detached section snapshot.

    ``state_key`` is the canonical first segment used by parameter paths.
    ``group_key`` is the stable public key exposed to config and UI consumers.
    """

    state_key: str
    group_key: str
    state: GroupStateModel


@dataclass(frozen=True)
class SLMSectionSnapshot:
    """Detached committed section state returned to backend consumers."""

    revision: int
    geometry: SectionGeometry
    state: SLMSectionState
    calibration: SLMSectionCalibration | None
    cgh_status: CGHStatus
    presentation: SectionPresentation = field(
        default_factory=SectionPresentation,
    )

    def group_entries(self) -> tuple[SectionGroupSnapshot, ...]:
        """Return groups in authoritative section order."""
        entries = []
        for state_key,group in self.state.child_states().items():
            group_key = getattr(group,"GROUP_KEY",None)
            if group_key is None:
                continue
            entries.append(SectionGroupSnapshot(
                state_key=state_key,group_key=group_key,state=group,
            ))
        return tuple(entries)

    def group_entry(self,key: str) -> SectionGroupSnapshot:
        """Return one group entry by its stable public key."""
        for entry in self.group_entries():
            if entry.group_key == key:
                return entry
        raise KeyError(f"Unknown section group '{key}'")

    def group_state(self,key: str) -> GroupStateModel:
        """Return one detached authoritative group state by its public key."""
        return self.group_entry(key).state

    def group_values(self,key: str):
        """Return serialized final values for one group."""
        return self.group_state(key).to_dict()

    def apply_update(self,update: SectionUpdate) -> "SLMSectionSnapshot":
        """Return the committed snapshot produced by one ordinary update.

        The complete serialized state carried by ``SectionUpdate`` is rebuilt
        rather than mutating this detached snapshot in place. The revision
        check rejects stale or out-of-order UI updates explicitly.
        """
        if update.base_revision != self.revision:
            raise RuntimeError(
                "Section update revision mismatch: "
                f"snapshot={self.revision}, update base={update.base_revision}"
            )
        if update.revision <= update.base_revision:
            raise RuntimeError(
                "Section update revision must advance beyond its base revision"
            )

        state,warnings = SLMSectionState.from_dict(
            self.state.registries,update.values,
        )
        if warnings:
            raise RuntimeError(
                f"Unexpected warnings while applying section update: {warnings}"
            )

        calibration = (
            None if update.calibration is None else update.calibration.copy()
        )
        return SLMSectionSnapshot(
            revision=update.revision,
            geometry=self.geometry,
            state=state,
            calibration=calibration,
            cgh_status=update.cgh_status,
            presentation=self.presentation.copy(),
        )
