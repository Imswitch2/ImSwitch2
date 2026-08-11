"""Widgets shared by more than one ImSwitch module.

Anything here must stay free of ``imcontrol``/``improcess`` imports -- that is
the point of it living in ``imcommon`` -- so these are plain ``QWidget``
subclasses rather than module-specific base widgets. The hosting module wraps
them in whatever widget/controller pair it uses.
"""

from .TransformCalibrationWidget import TransformCalibrationWidget

__all__ = ["TransformCalibrationWidget"]
