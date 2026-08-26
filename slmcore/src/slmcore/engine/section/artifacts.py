from dataclasses import dataclass
from typing import Mapping

import numpy as np


_ARRAY_DTYPES = (
    ("analytic",np.complex128),
    ("aberrations",np.complex128),
    ("cgh",np.complex128),
    ("combined",np.complex128),
    ("phase",np.float64),
    ("eightbit",np.uint8),
)


@dataclass(frozen=True)
class SectionArtifacts:
    analytic: np.ndarray
    aberrations: np.ndarray
    cgh: np.ndarray
    combined: np.ndarray
    phase: np.ndarray
    eightbit: np.ndarray
    source_revision: int

    def __post_init__(self) -> None:
        """Defensively detach arrays supplied through the public constructor."""
        for name,dtype in _ARRAY_DTYPES:
            value = np.array(getattr(self,name),dtype=dtype,copy=True)
            value.setflags(write=False)
            object.__setattr__(self,name,value)

        object.__setattr__(self,"source_revision",int(self.source_revision))

    @classmethod
    def from_owned(
        cls,
        *,
        analytic: np.ndarray,
        aberrations: np.ndarray,
        cgh: np.ndarray,
        combined: np.ndarray,
        phase: np.ndarray,
        eightbit: np.ndarray,
        source_revision: int,
    ) -> "SectionArtifacts":
        """Build artifacts from internally owned or already immutable arrays.

        This private-contract constructor avoids copying newly computed arrays a
        second time and preserves identity for immutable reused components.
        """
        values = {
            "analytic":analytic,
            "aberrations":aberrations,
            "cgh":cgh,
            "combined":combined,
            "phase":phase,
            "eightbit":eightbit,
        }
        return cls._from_trusted_arrays(values,source_revision)

    @classmethod
    def _from_trusted_arrays(
        cls,
        values: Mapping[str,np.ndarray],
        source_revision: int,
    ) -> "SectionArtifacts":
        instance = object.__new__(cls)

        for name,dtype in _ARRAY_DTYPES:
            value = np.asarray(values[name],dtype=dtype)
            value.setflags(write=False)
            object.__setattr__(instance,name,value)

        object.__setattr__(instance,"source_revision",int(source_revision))
        return instance

    def with_revision(self,source_revision: int) -> "SectionArtifacts":
        """Return a revision wrapper reusing every immutable array."""
        return type(self)._from_trusted_arrays(
            {name:getattr(self,name) for name,_dtype in _ARRAY_DTYPES},
            source_revision,
        )
