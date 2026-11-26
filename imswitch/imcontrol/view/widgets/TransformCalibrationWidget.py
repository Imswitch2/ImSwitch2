"""ImControl adapter for the shared transform-calibration widget."""

from imswitch.imcommon.view.widgets.TransformCalibrationWidget import (
    TransformCalibrationWidget as _SharedTransformCalibrationWidget,
)


class TransformCalibrationWidget(_SharedTransformCalibrationWidget):
    """Accept ImControl's standard viewer injection without coupling to Napari.

    ``WidgetFactory`` injects ``napariViewer`` into widgets created after the
    Image widget.  Transform calibration deliberately reaches the viewer
    through ``TransformCalibrationController``/``ImageWidget`` instead, so the
    injected viewer is intentionally ignored here.
    """

    def __init__(self, *args, napariViewer=None, **kwargs):
        del napariViewer
        super().__init__(*args, **kwargs)


__all__ = ["TransformCalibrationWidget"]
