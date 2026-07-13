"""Backward-compatibility re-export.

The widget-state persistence service moved to
:mod:`imswitch.imcommon.model.WidgetStatePersistence` so that any module
(imcontrol, improcess, ...) can use it without depending on imcontrol. This
shim keeps existing
``from imswitch.imcontrol.model.WidgetStatePersistence import ...`` and
``from imswitch.imcontrol.model import WidgetStatePersistence`` call sites
working. New code should import from ``imswitch.imcommon.model``.
"""

from imswitch.imcommon.model.WidgetStatePersistence import (
    WidgetStatePersistence,
    getWidgetStatePersistence,
)

__all__ = ["WidgetStatePersistence", "getWidgetStatePersistence"]
