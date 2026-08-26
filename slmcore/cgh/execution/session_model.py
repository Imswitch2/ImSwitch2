"""Immutable records owned by the CGH session lifecycle."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ..feedback import (
    FeedbackChangeKind,
    FeedbackMeasurement,
    IntensityAdaptation,
    PositionCorrection,
    RoundEvaluation,
)
from .result import CGHResult
from ..signature import CGHSignature


class CGHWorkingRoundState(str,Enum):
    """Transient compute state for the current desired round."""

    NOT_COMPUTED = "not_computed"
    COMPUTING = "computing"
    FAILED = "failed"


class CGHPreparedPurpose(str,Enum):
    """What a prepared asynchronous CGH computation is allowed to commit."""

    TARGET_REPLACEMENT = "target_replacement"
    WORKING_ROUND = "working_round"
    RECOMPUTE_ROUND = "recompute_round"


@dataclass(frozen=True)
class TargetDefinitionState:
    """Frozen semantic target definition independent from UI presentation."""

    target_type: str
    canonical_params: Mapping[str,Any]
    target_signature: CGHSignature
    context_signature: CGHSignature

    def __post_init__(self) -> None:
        target_type = str(self.target_type or "").strip()
        if not target_type:
            raise ValueError("Target type cannot be empty")
        object.__setattr__(self,"target_type",target_type)
        object.__setattr__(
            self,"canonical_params",_freeze_mapping(self.canonical_params),
        )
        object.__setattr__(
            self,"target_signature",CGHSignature(str(self.target_signature)),
        )
        object.__setattr__(
            self,"context_signature",CGHSignature(str(self.context_signature)),
        )


@dataclass(frozen=True)
class CGHRound:
    """One complete intensity-feedback round with a committed CGH result."""

    index: int
    intensities: np.ndarray
    result: CGHResult
    feedback_target_signature: CGHSignature
    adaptation: IntensityAdaptation | None = None
    evaluation: RoundEvaluation | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        index = int(self.index)
        if index < 0:
            raise ValueError("Round index must be >= 0")
        if not isinstance(self.result,CGHResult):
            raise TypeError("CGHRound.result must be CGHResult")
        if (
            CGHSignature(str(self.feedback_target_signature))
            != self.result.spec.feedback_target_signature
        ):
            raise ValueError(
                "Round effective target signature must match its CGH result"
            )
        adaptation = self.adaptation
        if adaptation is not None:
            if index == 0:
                raise ValueError("Round 0 cannot have an intensity adaptation")
            if not isinstance(adaptation,IntensityAdaptation):
                raise TypeError(
                    "CGHRound.adaptation must be IntensityAdaptation or None"
                )
            expected_source = index - 1
            if index > 0 and adaptation.source_round_index != expected_source:
                raise ValueError(
                    "Intensity adaptation source must be the previous round"
                )
            if not np.array_equal(adaptation.adapted_intensities,self.intensities):
                raise ValueError(
                    "Round intensities must match the adaptation that created it"
                )
        elif index > 0:
            raise ValueError("Rounds after round 0 must have an adaptation")
        evaluation = self.evaluation
        if evaluation is not None:
            if not isinstance(evaluation,RoundEvaluation):
                raise TypeError(
                    "CGHRound.evaluation must be RoundEvaluation or None"
                )
            if evaluation.index != index:
                raise ValueError("Round evaluation index must match round index")
        object.__setattr__(self,"index",index)
        object.__setattr__(
            self,"intensities",
            _freeze_array(self.intensities,"intensities",ndim=1),
        )
        object.__setattr__(
            self,
            "feedback_target_signature",
            CGHSignature(str(self.feedback_target_signature)),
        )
        object.__setattr__(self,"created_at",str(self.created_at or ""))

    def with_result(self,result: CGHResult) -> "CGHRound":
        return type(self)(
            index=self.index,
            intensities=self.intensities,
            result=result,
            feedback_target_signature=self.feedback_target_signature,
            adaptation=self.adaptation,
            evaluation=self.evaluation,
            created_at=self.created_at,
        )

    def with_evaluation(
        self,evaluation: RoundEvaluation | None,
    ) -> "CGHRound":
        return type(self)(
            index=self.index,
            intensities=self.intensities,
            result=self.result,
            feedback_target_signature=self.feedback_target_signature,
            adaptation=self.adaptation,
            evaluation=evaluation,
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class CGHWorkingRound:
    """Transient desired round that has no committed CGH result yet."""

    index: int
    intensities: np.ndarray
    feedback_target_signature: CGHSignature
    adaptation: IntensityAdaptation | None = None
    feedback_change: FeedbackChangeKind | None = None
    target_state: TargetDefinitionState | None = None
    purpose: CGHPreparedPurpose = CGHPreparedPurpose.WORKING_ROUND
    state: CGHWorkingRoundState = CGHWorkingRoundState.NOT_COMPUTED
    generation: int | None = None
    failure_message: str | None = None

    def __post_init__(self) -> None:
        index = int(self.index)
        if index < 0:
            raise ValueError("Working round index must be >= 0")
        adaptation = self.adaptation
        feedback_change = self.feedback_change
        if feedback_change is not None:
            feedback_change = FeedbackChangeKind(feedback_change)
        if adaptation is not None:
            if index == 0:
                raise ValueError(
                    "Working Round 0 cannot have an intensity adaptation"
                )
            if not isinstance(adaptation,IntensityAdaptation):
                raise TypeError(
                    "adaptation must be IntensityAdaptation or None"
                )
            expected_source = index - 1
            if index > 0 and adaptation.source_round_index != expected_source:
                raise ValueError(
                    "Working adaptation source must be the previous round"
                )
        elif index > 0:
            raise ValueError("Working rounds after round 0 need an adaptation")
        if adaptation is not None and feedback_change is not FeedbackChangeKind.INTENSITY:
            raise ValueError(
                "Intensity-adapted working rounds must be marked as intensity feedback"
            )
        if feedback_change is FeedbackChangeKind.INTENSITY and adaptation is None:
            raise ValueError(
                "Intensity feedback working rounds require an intensity adaptation"
            )
        if feedback_change is FeedbackChangeKind.POSITION:
            if index != 0:
                raise ValueError("Position feedback must restart at Working Round 0")
            if adaptation is not None:
                raise ValueError(
                    "Position feedback working rounds cannot carry intensity adaptation"
                )
        if adaptation is not None and not np.array_equal(
            adaptation.adapted_intensities,self.intensities,
        ):
            raise ValueError(
                "Working intensities must match the adaptation that created them"
            )
        target_state = self.target_state
        if (
            target_state is not None
            and not isinstance(target_state,TargetDefinitionState)
        ):
            raise TypeError(
                "target_state must be TargetDefinitionState or None"
            )
        purpose = CGHPreparedPurpose(self.purpose)
        if purpose is CGHPreparedPurpose.TARGET_REPLACEMENT and target_state is None:
            raise ValueError("Target-replacement working rounds need a target state")
        if purpose is CGHPreparedPurpose.RECOMPUTE_ROUND:
            raise ValueError("Recompute requests are not working rounds")
        generation = self.generation
        if generation is not None and int(generation) <= 0:
            raise ValueError("working generation must be >= 1")
        object.__setattr__(self,"index",index)
        object.__setattr__(
            self,"intensities",
            _freeze_array(self.intensities,"intensities",ndim=1),
        )
        object.__setattr__(
            self,
            "feedback_target_signature",
            CGHSignature(str(self.feedback_target_signature)),
        )
        object.__setattr__(self,"feedback_change",feedback_change)
        object.__setattr__(self,"purpose",purpose)
        object.__setattr__(
            self,"state",CGHWorkingRoundState(self.state),
        )
        object.__setattr__(
            self,"generation",None if generation is None else int(generation),
        )
        failure = self.failure_message
        object.__setattr__(
            self,"failure_message",None if failure is None else str(failure),
        )

    def with_state(
        self,
        state: CGHWorkingRoundState,
        *,
        generation: int | None=None,
        failure_message: str | None=None,
    ) -> "CGHWorkingRound":
        return type(self)(
            index=self.index,
            intensities=self.intensities,
            feedback_target_signature=self.feedback_target_signature,
            adaptation=self.adaptation,
            feedback_change=self.feedback_change,
            target_state=self.target_state,
            purpose=self.purpose,
            state=state,
            generation=generation,
            failure_message=failure_message,
        )


@dataclass(frozen=True)
class AppliedCGH:
    """Runtime-only answer to what hologram is physically applied now."""

    result: CGHResult
    origin_round_index: int | None
    desired_signature_at_apply: CGHSignature

    def __post_init__(self) -> None:
        if not isinstance(self.result,CGHResult):
            raise TypeError("AppliedCGH.result must be CGHResult")
        index = self.origin_round_index
        if index is not None:
            index = int(index)
            if index < 0:
                raise ValueError("origin_round_index must be >= 0 or None")
        object.__setattr__(self,"origin_round_index",index)
        object.__setattr__(
            self,
            "desired_signature_at_apply",
            CGHSignature(str(self.desired_signature_at_apply)),
        )

    def orphaned(self) -> "AppliedCGH":
        return type(self)(
            result=self.result,
            origin_round_index=None,
            desired_signature_at_apply=self.desired_signature_at_apply,
        )


@dataclass(frozen=True)
class CGHPreparedRequest:
    """Session binding for one prepared CGH job."""

    generation: int
    purpose: CGHPreparedPurpose
    round_index: int
    feedback_target_signature: CGHSignature
    spec_signature: CGHSignature
    target_state: TargetDefinitionState | None = None

    def __post_init__(self) -> None:
        generation = int(self.generation)
        if generation <= 0:
            raise ValueError("Prepared generation must be >= 1")
        index = int(self.round_index)
        if index < 0:
            raise ValueError("Prepared round index must be >= 0")
        target_state = self.target_state
        if (
            target_state is not None
            and not isinstance(target_state,TargetDefinitionState)
        ):
            raise TypeError(
                "target_state must be TargetDefinitionState or None"
            )
        purpose = CGHPreparedPurpose(self.purpose)
        if purpose is CGHPreparedPurpose.TARGET_REPLACEMENT and target_state is None:
            raise ValueError("Target-replacement requests need a target state")
        if purpose is not CGHPreparedPurpose.TARGET_REPLACEMENT and target_state is not None:
            raise ValueError("Only target-replacement requests carry target state")
        object.__setattr__(self,"generation",generation)
        object.__setattr__(self,"purpose",purpose)
        object.__setattr__(self,"round_index",index)
        object.__setattr__(
            self,
            "feedback_target_signature",
            CGHSignature(str(self.feedback_target_signature)),
        )
        object.__setattr__(
            self,"spec_signature",CGHSignature(str(self.spec_signature)),
        )


@dataclass(frozen=True)
class CGHTargetDisplay:
    """Presentation-ready resolved target spots for inspection views."""

    positions_kxy: np.ndarray
    intensities: np.ndarray

    def __post_init__(self) -> None:
        positions = _freeze_array(
            self.positions_kxy,"positions_kxy",ndim=2,
        )
        intensities = _freeze_array(
            self.intensities,"intensities",ndim=1,
        )
        if positions.shape[0] != 2:
            raise ValueError(
                "positions_kxy must have shape (2, N), got "
                f"{positions.shape}"
            )
        if intensities.shape != (positions.shape[1],):
            raise ValueError(
                "intensities must align with positions_kxy, got "
                f"{intensities.shape} for {positions.shape[1]} spots"
            )
        object.__setattr__(self,"positions_kxy",positions)
        object.__setattr__(self,"intensities",intensities)


@dataclass(frozen=True)
class CGHRoundInspection:
    """Read-only round view for Qt/session inspection."""

    index: int
    state: str
    intensities: np.ndarray
    result: CGHResult | None
    feedback_target_signature: CGHSignature
    target_preview: np.ndarray | None = None
    target_display: CGHTargetDisplay | None = None
    adaptation: IntensityAdaptation | None = None
    evaluation: RoundEvaluation | None = None
    purpose: CGHPreparedPurpose | None = None
    target_state: TargetDefinitionState | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self,"index",int(self.index))
        result = self.result
        if result is not None and not isinstance(result,CGHResult):
            raise TypeError("result must be CGHResult or None")
        object.__setattr__(
            self,"intensities",
            _freeze_array(self.intensities,"intensities",ndim=1),
        )
        preview = self.target_preview
        if preview is not None:
            preview = _freeze_array(
                preview,"target_preview",ndim=2,
            )
        object.__setattr__(self,"target_preview",preview)
        target_display = self.target_display
        if target_display is not None and not isinstance(
            target_display,CGHTargetDisplay,
        ):
            raise TypeError("target_display must be CGHTargetDisplay or None")
        object.__setattr__(self,"target_display",target_display)
        effective = CGHSignature(str(self.feedback_target_signature))
        if result is not None and result.spec.feedback_target_signature != effective:
            raise ValueError(
                "Inspection effective target signature must match its result"
            )
        purpose = self.purpose
        if purpose is not None:
            purpose = CGHPreparedPurpose(purpose)
        target_state = self.target_state
        if (
            target_state is not None
            and not isinstance(target_state,TargetDefinitionState)
        ):
            raise TypeError("target_state must be TargetDefinitionState or None")
        object.__setattr__(self,"feedback_target_signature",effective)
        object.__setattr__(self,"purpose",purpose)
        reason = self.unavailable_reason
        object.__setattr__(
            self,"unavailable_reason",None if reason is None else str(reason),
        )


@dataclass(frozen=True)
class CGHSessionInspection:
    """Coherent immutable view of session-owned target, rounds and feedback."""

    committed_target: TargetDefinitionState | None
    rounds: tuple[CGHRoundInspection, ...]
    working_round: CGHRoundInspection | None
    applied_round_index: int | None
    position_correction: PositionCorrection | None
    position_active: bool
    position_reference_round: CGHRoundInspection | None
    measurement: FeedbackMeasurement | None

    def __post_init__(self) -> None:
        rounds = tuple(self.rounds or ())
        for item in rounds:
            if not isinstance(item,CGHRoundInspection):
                raise TypeError("rounds must contain CGHRoundInspection")
        working = self.working_round
        if working is not None and not isinstance(working,CGHRoundInspection):
            raise TypeError(
                "working_round must be CGHRoundInspection or None"
            )
        applied = self.applied_round_index
        if applied is not None:
            applied = int(applied)
            if applied < 0:
                raise ValueError("applied_round_index must be >= 0 or None")
        correction = self.position_correction
        if correction is not None and not isinstance(correction,PositionCorrection):
            raise TypeError(
                "position_correction must be PositionCorrection or None"
            )
        reference = self.position_reference_round
        if reference is not None and not isinstance(reference,CGHRoundInspection):
            raise TypeError(
                "position_reference_round must be CGHRoundInspection or None"
            )
        measurement = self.measurement
        if measurement is not None and not isinstance(measurement,FeedbackMeasurement):
            raise TypeError("measurement must be FeedbackMeasurement or None")
        object.__setattr__(self,"rounds",rounds)
        object.__setattr__(self,"applied_round_index",applied)
        object.__setattr__(self,"position_active",bool(self.position_active))

    @property
    def intensity_rounds(self) -> tuple[RoundEvaluation, ...]:
        """Compatibility view of evaluated complete rounds."""
        return tuple(
            item.evaluation for item in self.rounds
            if item.evaluation is not None
            and item.evaluation.intensity_analysis is not None
        )


@dataclass(frozen=True)
class CGHSessionSnapshot:
    """Stable persisted session state, excluding runtime-only working state."""

    committed_target: TargetDefinitionState | None
    position_correction: PositionCorrection | None
    position_active: bool
    position_reference_round: CGHRound | None = None
    rounds: tuple[CGHRound, ...] = ()
    intensity_analysis_params: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        target = self.committed_target
        if target is not None and not isinstance(target,TargetDefinitionState):
            raise TypeError(
                "committed_target must be TargetDefinitionState or None"
            )
        rounds = tuple(self.rounds or ())
        if rounds and target is None:
            raise ValueError("Committed rounds require a committed target")
        expected = tuple(range(len(rounds)))
        actual = tuple(round_record.index for round_record in rounds)
        if actual != expected:
            raise ValueError(
                "CGH session snapshot rounds must be a contiguous sequence "
                "starting at 0"
            )
        correction = self.position_correction
        if correction is not None and not isinstance(correction,PositionCorrection):
            raise TypeError(
                "position_correction must be PositionCorrection or None"
            )
        reference = self.position_reference_round
        if reference is not None and not isinstance(reference,CGHRound):
            raise TypeError(
                "position_reference_round must be CGHRound or None"
            )
        if reference is not None and correction is None:
            raise ValueError(
                "Position reference round requires position correction data"
            )
        if self.position_active and correction is None:
            raise ValueError("Active position correction requires correction data")
        object.__setattr__(self,"rounds",rounds)
        object.__setattr__(self,"position_active",bool(self.position_active))
        object.__setattr__(
            self,"intensity_analysis_params",
            _freeze_mapping(self.intensity_analysis_params),
        )


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str,Any]:
    if value is None:
        value = {}
    if not isinstance(value,Mapping):
        raise TypeError(f"Expected a mapping, got {type(value).__name__}")
    return MappingProxyType(deepcopy(dict(value)))


def _freeze_array(value: Any,name: str,ndim: int) -> np.ndarray:
    array = np.asarray(value,dtype=np.float64)
    if array.ndim != ndim:
        raise ValueError(f"{name} must be {ndim}D, got shape {array.shape}")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    if ndim == 1 and np.any(array < 0):
        raise ValueError(f"{name} cannot contain negative values")
    array = np.array(array,dtype=np.float64,copy=True)
    array.setflags(write=False)
    return array
