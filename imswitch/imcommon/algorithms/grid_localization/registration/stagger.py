"""Backward-compatible stagger inference wrapper.

Finite row/column topology is now implemented by :mod:`.finite` for both known
and unknown stagger.  This private helper remains for internal/tests that still
refer to the former stagger-only entry point.
"""

from __future__ import annotations

from .finite import _infer_finite_grid_initializer


def _infer_unknown_stagger_model(detections,model):
    if bool(model.diagnostics.get("stagger_known",False)):
        return None
    return _infer_finite_grid_initializer(detections,model)


__all__ = ["_infer_unknown_stagger_model"]
