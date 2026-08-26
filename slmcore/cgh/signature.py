from __future__ import annotations

from dataclasses import fields,is_dataclass
from enum import Enum
from hashlib import sha256
import json
from typing import Any,Mapping,NewType,TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from ..engine.section.context import SectionContext


CGHSignature = NewType("CGHSignature",str)
SignaturePath = tuple[str,...]


# Every SectionContext field affects CGH validity by default. These paths are
# excluded because they only affect how the finished CGH is positioned or
# masked when the complete section pattern is assembled.
CGH_IGNORED_CONTEXT_PATHS = frozenset({
    ("geometry","key"),
    ("geometry","x"),
    ("geometry","y"),
    ("pupil_radius_px",),
    ("center_offset_x_px",),
    ("center_offset_y_px",),
})


def compute_cgh_spec_signature(
    context: "SectionContext",
    target_type: str,
    algorithm: str,
    compute_params: Mapping[str,Any],
    feedback_target_signature: CGHSignature,
) -> CGHSignature:
    """Return the signature of every input defining one CGH result.

    Target configuration parameters are intentionally not hashed here. Their
    semantic identity is already represented by ``feedback_target_signature``.
    ``CGHSpec.target_params`` remains available as provenance.
    """
    return _compute_signature({
        "context":_build_context_signature_payload(context),
        "target_type":target_type,
        "algorithm":algorithm,
        "compute_params":compute_params,
        "feedback_target_signature":feedback_target_signature,
    })


def compute_target_definition_signature(
    context: "SectionContext",
    target_type: str,
    target_params: Mapping[str,Any],
) -> CGHSignature:
    """Return the signature of every input used to resolve a Target object."""
    return _compute_signature({
        "context": _build_context_signature_payload(context),
        "target_type": target_type,
        "target_params": target_params,
    })


def compute_context_signature(context: "SectionContext") -> CGHSignature:
    """Return the signature of context fields that affect CGH computation."""
    return _compute_signature(_build_context_signature_payload(context))


def compute_feedback_target_signature(
    *,
    target_signature: CGHSignature,
    position_signature: CGHSignature | None,
    round_index: int,
    intensities: Any,
) -> CGHSignature:
    """Return the base target identity after feedback adjustments for one round."""
    return _compute_signature({
        "target_signature":target_signature,
        "position_signature":position_signature,
        "round_index":int(round_index),
        "intensities":np.asarray(intensities,dtype=np.float64),
    })


def compute_position_correction_signature(value: Any) -> CGHSignature | None:
    """Return a stable signature for the active session position correction."""
    if value is None:
        return None
    payload = value.to_dict() if hasattr(value,"to_dict") else value
    return _compute_signature(payload)


def _compute_signature(input_values: Any) -> CGHSignature:
    """Normalize nested input values and hash their deterministic JSON form."""
    payload = _normalize_signature_value(input_values)
    encoded = json.dumps(
        payload,sort_keys=True,separators=(",",":"),allow_nan=False
    ).encode("utf-8")
    return CGHSignature(sha256(encoded).hexdigest())


def _build_context_signature_payload(
    context: "SectionContext",
) -> Mapping[str,Any]:
    """Include all SectionContext fields except explicitly ignored paths."""
    return _normalize_signature_value(
        context,ignored_paths=CGH_IGNORED_CONTEXT_PATHS)


def _normalize_signature_value(
    value: Any,
    path: SignaturePath=(),
    ignored_paths=frozenset(),
) -> Any:
    """Convert nested values into a deterministic JSON-compatible structure."""
    if is_dataclass(value):
        normalized = {}
        for field_info in fields(value):
            field_path = path + (field_info.name,)
            if field_path in ignored_paths:
                continue
            normalized[field_info.name] = _normalize_signature_value(
                getattr(value,field_info.name),field_path,ignored_paths)
        return normalized

    if isinstance(value,Mapping):
        return {
            str(key):_normalize_signature_value(
                item,path + (str(key),),ignored_paths)
            for key,item in sorted(
                value.items(),key=lambda item: str(item[0]))
        }

    if isinstance(value,(list,tuple)):
        return [
            _normalize_signature_value(
                item,path + (str(index),),ignored_paths)
            for index,item in enumerate(value)
        ]

    if isinstance(value,np.ndarray):
        array = np.ascontiguousarray(value)
        return {
            "__ndarray__":True,
            "shape":list(array.shape),
            "dtype":str(array.dtype),
            "sha256":sha256(array.tobytes()).hexdigest(),
        }

    if isinstance(value,np.generic):
        return value.item()

    if isinstance(value,Enum):
        return value.value

    if value is None or isinstance(value,(bool,int,float,str)):
        return value

    location = ".".join(path) or "<root>"
    raise TypeError(
        f"Unsupported CGH signature value at {location}: "
        f"{type(value).__name__}"
    )
