from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Mapping,TYPE_CHECKING

from ..signature import CGHSignature,compute_cgh_spec_signature

if TYPE_CHECKING:
    from ...engine.section.context import SectionContext


@dataclass(frozen=True)
class CGHSpec:
    """Immutable description of the exact inputs defining one CGH computation."""

    context: SectionContext
    target_type: str
    algorithm: str
    target_params: Mapping[str,Any]
    compute_params: Mapping[str,Any]
    feedback_target_signature: CGHSignature
    signature: CGHSignature = field(init=False)

    def __post_init__(self) -> None:
        target_params = MappingProxyType(
            deepcopy(dict(self.target_params)))
        compute_params = MappingProxyType(
            deepcopy(dict(self.compute_params)))

        object.__setattr__(self,"target_params",target_params)
        object.__setattr__(self,"compute_params",compute_params)
        object.__setattr__(
            self,"signature",
            compute_cgh_spec_signature(
                self.context,
                self.target_type,
                self.algorithm,
                compute_params,
                self.feedback_target_signature,
            ),
        )

    def has_same_inputs(self,other: CGHSpec) -> bool:
        return self.signature == other.signature
