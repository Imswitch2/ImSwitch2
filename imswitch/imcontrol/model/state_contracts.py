"""Backward-compatibility re-export.

``ComponentStateApplyMode`` moved to :mod:`imswitch.imcommon.model.state_contracts`
so the shared widget-state persistence service can live in imcommon without any
imcontrol dependency. This shim keeps existing
``from imswitch.imcontrol.model.state_contracts import ...`` call sites working.
"""

from imswitch.imcommon.model.state_contracts import ComponentStateApplyMode

__all__ = ["ComponentStateApplyMode"]
