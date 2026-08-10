"""Neural-network denoising processor.

Wraps the existing ``imswitch.improcess.model.Denoiser`` (UNet / UNet+RCAN)
as a registered ImProcess Processor. The wrapped model class lazily imports
torch + torchvision, so importing this module is cheap even on environments
without PyTorch installed.
"""

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import DenoisedResult


class DenoiseProcessor(Processor):
    """Apply a trained UNet / UNet+RCAN denoising model to a 2D image stack."""

    name = "Denoise"
    id = "denoise"
    category = "Restoration"
    # Output is pixel-for-pixel aligned with the input, so an ROI drawn
    # on one measures the same features on the other.
    preserves_grid = True

    def __init__(self):
        self._logger = initLogger(self, tryInheritParent=False)
        # Lazy: the Denoiser triggers a torch import on construction, so build
        # it on demand inside apply() rather than at registry time.
        self._denoiser = None

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Accept any result with at least a 2D image plane (the last two
        axes are treated as spatial)."""
        def _gate(result: ProcessingResult) -> bool:
            try:
                return np.asarray(result.data).ndim >= 2
            except Exception:
                return False
        return _gate

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        model_name_edit = QtWidgets.QLineEdit()
        model_name_edit.setText('Vimentin_UNet_RCAN_lowSNR')
        model_name_edit.setToolTip(
            "Name of the trained model directory under the denoising_models "
            "folder. Must contain config_train.json and model_best_state_dict.pt."
        )
        layout.addRow("Model name:", model_name_edit)

        model_type_combo = QtWidgets.QComboBox()
        model_type_combo.addItems(['Auto', 'UNet', 'UNetRCAN'])
        model_type_combo.setCurrentText('Auto')
        model_type_combo.setToolTip(
            "Auto picks UNetRCAN when the model name contains 'RCAN', "
            "otherwise UNet."
        )
        layout.addRow("Model type:", model_type_combo)

        crop_size_spin = QtWidgets.QSpinBox()
        crop_size_spin.setRange(16, 100000)
        crop_size_spin.setSingleStep(16)
        crop_size_spin.setValue(800)
        crop_size_spin.setToolTip(
            "Center-crop side length in pixels. Rounded down to a multiple of 16."
        )
        layout.addRow("Crop size (px):", crop_size_spin)

        pad_check = QtWidgets.QCheckBox("Zero-pad to input size")
        pad_check.setChecked(True)
        layout.addRow("", pad_check)

        clip_neg_check = QtWidgets.QCheckBox("Clip negative input to zero")
        clip_neg_check.setChecked(True)
        layout.addRow("", clip_neg_check)

        def get_values():
            return {
                "model_name": model_name_edit.text().strip(),
                "model_type": model_type_combo.currentText(),
                "crop_size": int(crop_size_spin.value()),
                "pad": bool(pad_check.isChecked()),
                "clip_neg": bool(clip_neg_check.isChecked()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        from imswitch.improcess.model import Denoiser  # lazy torch import

        if self._denoiser is None:
            self._denoiser = Denoiser()
        if not getattr(self._denoiser, 'denoising_available', False):
            raise RuntimeError(
                "Denoising is not available in this environment: PyTorch "
                "could not be imported. Install torch + torchvision to enable "
                "the denoise processor."
            )

        model_name = str(params.get('model_name', '')).strip()
        if not model_name:
            raise ValueError("denoise processor requires a non-empty 'model_name'")
        model_type_request = str(params.get('model_type', 'Auto'))
        if model_type_request == 'Auto':
            model_type = 'UNetRCAN' if 'RCAN' in model_name else 'UNet'
        else:
            model_type = model_type_request
        crop_size = int(params.get('crop_size', 800))
        pad = bool(params.get('pad', True))
        clip_neg = bool(params.get('clip_neg', True))

        stack, leading_indexer, spatial_shape = self._extract_2d_stack(result)

        self._denoiser.init_model(model_name, model_type)
        self._denoiser.load_model(model_name)
        if self._denoiser.model is None:
            raise RuntimeError(
                f"Failed to load denoising model {model_name!r}; check the "
                "model directory and config_train.json."
            )

        prediction = self._denoiser.predict(
            data=stack, crop_size=crop_size, pad=pad, clip_neg=clip_neg
        ).astype(np.float32)

        denoised = self._embed_prediction(
            np.asarray(result.data), prediction, leading_indexer, spatial_shape, pad
        )
        axis_labels, axis_scales = self._output_axes(
            result,
            denoised,
            leading_indexer,
        )

        source_data = np.asarray(result.data)
        output_data = np.asarray(denoised)
        view_modes = (
            result.view_modes
            if output_data.ndim == source_data.ndim
            else None
        )

        return DenoisedResult(
            name=f"{result.name}_denoise",
            data=denoised,
            axis_labels=axis_labels,
            model_name=model_name,
            model_type=model_type,
            crop_size=crop_size,
            pad=pad,
            view_modes=view_modes,
            display_levels=result.display_levels,
            axis_scales=axis_scales,
            scale_unit=result.scale_unit,
        )

    # ----- helpers --------------------------------------------------------

    @staticmethod
    def _extract_2d_stack(
        result: ProcessingResult,
    ) -> tuple[np.ndarray, tuple, tuple[int, int]]:
        """Return a 3D (N, H, W) float32 stack and an indexer that pinpoints
        the slice within the original array, so the prediction can be written
        back into the same place."""
        data = np.asarray(result.data)
        if data.ndim < 2:
            raise ValueError(
                f"denoise processor needs at least a 2D image, got shape {data.shape}"
            )
        h, w = data.shape[-2:]
        if data.ndim == 2:
            return data[None, ...].astype(np.float32, copy=False), (), (h, w)

        labels = list(result.axis_labels) if result.axis_labels else []
        labels = labels[: data.ndim]
        # Find one frame-like axis (T, then Z), otherwise the first non-spatial axis.
        leading_axes = list(range(data.ndim - 2))
        frame_axis = None
        for candidate in ("T", "Z"):
            if candidate in labels:
                axis = labels.index(candidate)
                if axis in leading_axes:
                    frame_axis = axis
                    break
        if frame_axis is None and leading_axes:
            frame_axis = leading_axes[0]

        indexer = []
        for axis in range(data.ndim):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            elif axis == frame_axis:
                indexer.append(slice(None))
            else:
                indexer.append(0)
        sub = np.asarray(data[tuple(indexer)])
        if sub.ndim != 3:
            sub = sub.reshape((-1, h, w))
        return sub.astype(np.float32, copy=False), tuple(indexer), (h, w)

    @staticmethod
    def _embed_prediction(
        original: np.ndarray,
        prediction: np.ndarray,
        leading_indexer: tuple,
        spatial_shape: tuple[int, int],
        pad: bool,
    ) -> np.ndarray:
        """Write the prediction back into a copy of the original array shape,
        so axis labels and downstream consumers do not have to adapt."""
        out = np.zeros_like(original, dtype=np.float32)
        if not leading_indexer:
            if not pad and prediction.shape[-2:] != spatial_shape:
                # User asked for the cropped output; return a fresh array.
                return prediction[0] if prediction.shape[0] == 1 else prediction
            out[...] = prediction[0]
            return out
        if not pad and prediction.shape[-2:] != spatial_shape:
            # Prediction is smaller than the original spatial extent; the user
            # opted out of zero-padding, so return the cropped prediction.
            return prediction
        out[leading_indexer] = prediction
        return out

    @staticmethod
    def _output_axes(
        result: ProcessingResult,
        output: np.ndarray,
        leading_indexer: tuple,
    ) -> tuple[list[str], list[float] | None]:
        """Return labels/scales that match the denoised output dimensionality."""
        source = np.asarray(result.data)
        out_ndim = np.asarray(output).ndim
        labels = list(getattr(result, "axis_labels", []) or [])
        scales = list(getattr(result, "axis_scales", []) or [])

        if len(labels) != source.ndim:
            labels = DenoiseProcessor._default_axis_labels(source.ndim)
        if len(scales) != source.ndim:
            scales = [1.0] * source.ndim

        if out_ndim == len(labels):
            return labels, scales

        if leading_indexer:
            kept_axes = [
                axis
                for axis, index in enumerate(leading_indexer)
                if isinstance(index, slice)
            ]
            if len(kept_axes) == out_ndim:
                return (
                    [labels[axis] for axis in kept_axes],
                    [scales[axis] for axis in kept_axes],
                )

        if out_ndim <= len(labels):
            return labels[-out_ndim:], scales[-out_ndim:]
        return DenoiseProcessor._default_axis_labels(out_ndim), [1.0] * out_ndim

    @staticmethod
    def _default_axis_labels(ndim: int) -> list[str]:
        labels = ["T", "Z", "C", "Y", "X"]
        if ndim <= len(labels):
            return labels[-ndim:]
        extra_count = ndim - len(labels)
        return [f"D{i}" for i in range(extra_count)] + labels


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
