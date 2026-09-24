"""Frame average — an ImProcess drop-in *reconstructor* plugin.

A reconstructor is the other plugin shape: where a processor transforms a
result, a reconstructor turns the raw ``DataObj`` (the file as loaded) into
the first result, and every dataset goes through exactly one of them. This
one averages the frames of a stack into a single image, optionally only the
first N of them.

Copy into ~/.imswitch/improcess_plugins/ (Plugins -> Open plugins folder...,
or Plugins -> Add plugin file...) and Reload plugins. "Frame average" then
appears in the reconstructor picker of the Parameters dock; pick it and press
Reconstruct current.
"""

import numpy as np

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.reconstructors.base import Reconstructor


class FrameAverageReconstructor(Reconstructor):
    name = "Frame average"
    id = "example.frame-average"
    file_extensions = ["hdf5", "tiff", "tif", "zarr"]

    @classmethod
    def default_params(cls) -> dict:
        # Exactly what a fresh widget hands process(): same key, same default.
        # 0 means "all frames".
        return {"frames": 0}

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)
        spin = QtWidgets.QSpinBox(widget)
        spin.setRange(0, 1_000_000)
        spin.setSpecialValueText("all")
        spin.setValue(self.default_params()["frames"])
        layout.addRow("Frames to average (0 = all)", spin)
        widget.get_values = lambda: {"frames": int(spin.value())}
        return widget

    def make_metadata_dialog(self, parent):
        # No acquisition metadata to ask for.
        return None

    def process(self, data_obj, params, context=None):
        data_obj.checkAndLoadData()
        data = np.asarray(data_obj.data)
        if data.ndim < 2:
            raise ValueError("Frame average needs at least a 2-D image")
        # Every leading axis (time, z, channel...) is treated as a frame axis;
        # the last two are the image.
        frames = data.reshape(-1, *data.shape[-2:])
        count = int(params.get("frames", 0) or 0)
        if count > 0:
            frames = frames[:count]
        # Carry the pixel size through when the file had one.
        scales = list(getattr(data_obj, "axis_scales", None) or [])
        return ArrayProcessingResult(
            name=f"{data_obj.name} (frame average)",
            data=frames.mean(axis=0, dtype=np.float32),
            axis_labels=["Y", "X"],
            axis_scales=scales[-2:] if len(scales) >= 2 else None,
            scale_unit=getattr(data_obj, "scale_unit", None) or "px",
        )
