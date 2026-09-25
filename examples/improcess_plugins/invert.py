"""Invert intensities — the minimal ImProcess drop-in analysis plugin.

Copy this file into ~/.imswitch/improcess_plugins/ (Plugins -> Open plugins
folder..., or pick it with Plugins -> Add plugin file...) and Reload plugins.
"Invert" then appears in the Load-plugin dropdown of the Plugins toolbar.
"""

import numpy as np

from imswitch.improcess.processors.base import Processor
from imswitch.improcess.model.array_result import ArrayProcessingResult


class InvertProcessor(Processor):
    name = "Invert"
    id = "example.invert"
    category = "User"
    kinds = ("image",)

    @classmethod
    def default_params(cls) -> dict:
        # No parameters -- and said so explicitly. The inherited base
        # implementation also returns {}, but inheriting it means "this
        # plugin never declared its parameters", which makes it GUI-only.
        return {}

    @property
    def applies_to(self):
        # Any 2-D-or-higher image-like result.
        return lambda result: getattr(result.data, "ndim", 0) >= 2

    def make_param_widget(self, parent):
        # No parameters: an empty widget whose get_values() returns {},
        # matching default_params() above.
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        widget.get_values = lambda: {}
        return widget

    def apply(self, result, params):
        # A result's data may be a lazy view over its file (a workflow's
        # view-only reconstruction is one); materialize before computing.
        data = np.asarray(result.data)
        return ArrayProcessingResult(
            name=f"{result.name} (inverted)",
            data=data.max() - data,
            axis_labels=list(result.axis_labels),
            axis_scales=list(getattr(result, "axis_scales", None) or []) or None,
            scale_unit=getattr(result, "scale_unit", "px"),
        )
