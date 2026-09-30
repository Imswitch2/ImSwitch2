"""LISAI restoration — ImProcess drop-in plugin.

Runs a trained LISAI model (https://github.com/GuillaumeMinet/LISAI-resolft,
denoising and sub-sampled-image restoration for RESOLFT / MoNaLISA data) on
the selected result and publishes the prediction as a new result.

Prerequisites, all outside ImProcess:

1. LISAI installed in the same Python environment as ImSwitch2
   (``pip install -e . --no-deps`` from a LISAI checkout, plus ``torch``,
   ``torchvision`` and, for the HDN denoising models, ``scikit-learn``).
   On Windows, torch only loads after Qt with an ImSwitch2 that preloads the
   system MSVC runtime (``imswitch/__init__.py``); otherwise it fails with
   ``WinError 1114``.
2. LISAI's one-time setup done from a terminal, e.g. ``lisai models list``:
   on first import LISAI asks for its data root on stdin and writes
   ``configs/local_config.yml``. This plugin never triggers that prompt; it
   refuses to run until the file exists.
3. At least one promoted model installed, e.g.
   ``lisai models download <name> --install`` (``lisai models catalog``
   lists the published ones).

The plugin always registers, so it shows up in *Load plugin* whether or not
LISAI is installed; running it without LISAI reports what is missing.

Multi-frame models predict each frame from its neighbours. The first and last
``context_length // 2`` frames have no full context and are dropped, unless
*Pad edge frames* is ticked (LISAI's ``dark_frame_context_length``).

Inference goes through LISAI's public ``lisai.api.predict`` entry point, which
reads and writes TIFF files, so each run round-trips the data through a
temporary folder.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import numpy as np

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

_INPUT_NAME = "frame"
_TILING_CHOICES = ("auto", "off", "256", "512", "1024")


def _lisai_spec():
    return importlib.util.find_spec("lisai")


def _lisai_setup_problem() -> str | None:
    """Why LISAI cannot be used right now, found without importing it.

    Importing ``lisai.config`` with no ``local_config.yml`` blocks on
    ``input()``, which would hang the ImProcess GUI, so this looks for the
    file the same way LISAI's ``Settings`` does: the first parent of the
    package that holds a ``configs`` folder.
    """
    spec = _lisai_spec()
    if spec is None:
        return (
            "LISAI is not installed in this environment. Install it into the "
            "Python environment that runs ImSwitch2 to use this processor."
        )
    if spec.origin:
        package_dir = Path(spec.origin).resolve().parent
    else:
        package_dir = Path(list(spec.submodule_search_locations)[0]).resolve()
    for parent in [package_dir / "config", *package_dir.parents]:
        configs = parent / "configs"
        if configs.is_dir():
            if (configs / "local_config.yml").is_file():
                return None
            return (
                f"LISAI has not been set up yet ({configs / 'local_config.yml'} "
                "is missing). Run a LISAI command once from a terminal, for "
                "example `lisai models list`, and answer its data-root prompt."
            )
    return (
        f"Could not find LISAI's 'configs' folder above {package_dir}. LISAI "
        "currently has to be installed from its repository checkout "
        "(pip install -e .)."
    )


def _installed_model_names() -> list[str]:
    from lisai.promoted_models.registry import load_promoted_model_registry

    return sorted(load_promoted_model_registry().models)


def _tiling_policy(value) -> str | int:
    text = str(value).strip().lower()
    if text in ("", "auto"):
        return "auto"
    if text in ("off", "none"):
        return "off"
    size = int(text)
    if size <= 0:
        raise ValueError(f"tiling_size must be positive, 'auto' or 'off', got {value!r}")
    return size


def _frame_stack(result: ProcessingResult) -> tuple[np.ndarray, list[int]]:
    """``(stack, kept_axes)``: the data as ``(Y, X)`` or ``(N, Y, X)``.

    Beyond two axes, one frame-like axis is kept (``T``, then ``Z``, else the
    first leading axis) and every other leading axis is taken at index 0, as
    the built-in denoise processor does. ``kept_axes`` are the source axes the
    stack's axes came from.
    """
    data = np.asarray(result.data)
    if data.ndim < 2:
        raise ValueError(f"LISAI restore needs at least a 2D image, got shape {data.shape}")
    if data.ndim == 2:
        return data.astype(np.float32, copy=False), [0, 1]

    labels = list(result.axis_labels or [])[: data.ndim]
    leading = list(range(data.ndim - 2))
    frame_axis = next(
        (labels.index(c) for c in ("T", "Z") if c in labels and labels.index(c) in leading),
        leading[0],
    )
    indexer = tuple(
        slice(None) if axis == frame_axis or axis >= data.ndim - 2 else 0
        for axis in range(data.ndim)
    )
    kept = [frame_axis, data.ndim - 2, data.ndim - 1]
    return np.asarray(data[indexer]).astype(np.float32, copy=False), kept


def _upsampling_factor(input_shape, output_shape) -> int:
    """The integer factor by which Y and X grew, or 1 if they did not grow evenly."""
    in_y, in_x = input_shape[-2:]
    out_y, out_x = output_shape[-2:]
    if out_y % in_y or out_x % in_x or out_y // in_y != out_x // in_x:
        return 1
    return max(1, out_y // in_y)


def _trim_context_edges(prediction, context_length, pad_edges, model_name) -> tuple[np.ndarray, int]:
    """Drop the frames a multi-frame model could not predict; ``(frames, dropped_per_edge)``.

    Without padding, LISAI skips the first and last ``context_length // 2``
    frames but still returns them, uninitialized.
    """
    if not context_length or pad_edges:
        return prediction, 0
    half = int(context_length) // 2
    frames = prediction if prediction.ndim >= 3 else prediction[None]
    if frames.shape[0] <= 2 * half:
        raise ValueError(
            f"{model_name} predicts each frame from {context_length} consecutive frames, "
            f"but the input has {frames.shape[0]}. Enable 'Pad edge frames' or use a longer stack."
        )
    return frames[half:-half], half


def _output_axes(result, kept_axes, input_shape, prediction) -> tuple[list[str], list[float], int]:
    source_ndim = np.asarray(result.data).ndim
    labels = list(result.axis_labels or [])
    scales = list(result.axis_scales or [])
    if len(labels) != source_ndim:
        labels = [f"D{i}" for i in range(source_ndim - 2)] + ["Y", "X"]
    if len(scales) != source_ndim:
        scales = [1.0] * source_ndim

    out_labels = [labels[axis] for axis in kept_axes]
    out_scales = [float(scales[axis]) for axis in kept_axes]
    factor = _upsampling_factor(input_shape, prediction.shape)
    out_scales[-2] /= factor
    out_scales[-1] /= factor

    if prediction.ndim != len(out_labels):
        extra = prediction.ndim - 2
        out_labels = [f"D{i}" for i in range(extra)] + out_labels[-2:]
        out_scales = [1.0] * extra + out_scales[-2:]
    return out_labels, out_scales, factor


class LisaiRestoredResult(ProcessingResult):
    """A LISAI prediction, carrying the model that made it."""

    supported_formats = ("tiff", "hdf5")

    def __init__(
        self, name, data, axis_labels, *, model_name, upsampling_factor=1, edge_frames_dropped=0,
        **kwargs,
    ):
        super().__init__(name, data, axis_labels, **kwargs)
        self.model_name = str(model_name)
        self.upsampling_factor = int(upsampling_factor)
        self.edge_frames_dropped = int(edge_frames_dropped)

    def _extra(self) -> dict:
        return {
            "lisai_model_name": self.model_name,
            "lisai_upsampling_factor": self.upsampling_factor,
            "lisai_edge_frames_dropped": self.edge_frames_dropped,
        }

    def write_files(self, plan, document) -> None:
        if plan.fmt == "tiff":
            from imswitch.improcess.model.result_io import save_image_result

            save_image_result(self, plan.primary, "tiff", extra=self._extra(), document=document)
        elif plan.fmt == "hdf5":
            import h5py

            from imswitch.improcess.model.save_protocol import embed_hdf5

            with h5py.File(str(plan.primary), "w") as f:
                f.create_dataset(
                    "prediction", data=np.asarray(self.data, dtype=np.float32), compression="gzip"
                )
                f.attrs["axis_labels"] = "".join(self.axis_labels)
                f.attrs["axis_scales"] = np.asarray(self.axis_scales, dtype=float)
                f.attrs["scale_unit"] = self.scale_unit
                for key, value in self._extra().items():
                    f.attrs[key] = value
                embed_hdf5(f, document)
        else:
            raise ValueError(f"LisaiRestoredResult supports TIFF or HDF5, got {plan.fmt!r}")


class LisaiRestoreProcessor(Processor):
    name = "LISAI restore"
    id = "lisai.restore"
    category = "Restoration"
    kinds = ("image",)
    # An up-sampling model changes the pixel grid; which kind of model runs
    # is only known once one is picked.
    preserves_grid = False
    default_params_volatile = ("model_name",)

    @classmethod
    def default_params(cls) -> dict:
        return {"model_name": "", "tiling_size": "auto", "crop_size": 0, "pad_edges": False}

    @property
    def applies_to(self):
        def _gate(result) -> bool:
            ndim = getattr(result.data, "ndim", None)
            return ndim is not None and int(ndim) >= 2

        return _gate

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        defaults = self.default_params()
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        model_combo = QtWidgets.QComboBox()
        problem = _lisai_setup_problem()
        names: list[str] = []
        if problem is None:
            try:
                names = _installed_model_names()
            except Exception as exc:  # noqa: BLE001 - shown in the panel, not raised
                problem = f"Could not read LISAI's model registry: {exc}"
            else:
                if not names:
                    problem = (
                        "No promoted LISAI models installed. Run "
                        "`lisai models download <name> --install` (see `lisai models catalog`)."
                    )
        for model_name in names:
            model_combo.addItem(model_name, model_name)
        if problem is not None:
            model_combo.addItem("(unavailable)", "")
            model_combo.setEnabled(False)
            model_combo.setToolTip(problem)
            note = QtWidgets.QLabel(problem)
            note.setWordWrap(True)
            layout.addRow(note)
        layout.addRow("Model:", model_combo)

        tiling_combo = QtWidgets.QComboBox()
        tiling_combo.setEditable(True)
        tiling_combo.addItems(_TILING_CHOICES)
        tiling_combo.setCurrentText(defaults["tiling_size"])
        tiling_combo.setToolTip(
            "Patch size for tiled inference: 'auto' uses the model's saved default, "
            "'off' runs the whole image at once, or a positive integer."
        )
        layout.addRow("Tiling size:", tiling_combo)

        crop_spin = QtWidgets.QSpinBox()
        crop_spin.setRange(0, 100000)
        crop_spin.setSingleStep(16)
        crop_spin.setValue(defaults["crop_size"])
        crop_spin.setSpecialValueText("No crop")
        crop_spin.setToolTip("Square centre crop before inference, in pixels; 0 runs the full frame.")
        layout.addRow("Crop size (px):", crop_spin)

        pad_check = QtWidgets.QCheckBox("Pad edge frames")
        pad_check.setChecked(defaults["pad_edges"])
        pad_check.setToolTip(
            "Multi-frame models predict each frame from its neighbours, so the first and "
            "last few frames lack context and are dropped. Tick to predict them anyway, "
            "with dark frames standing in for the missing neighbours."
        )
        layout.addRow("", pad_check)

        widget.get_values = lambda: {
            "model_name": str(model_combo.currentData() or ""),
            "tiling_size": tiling_combo.currentText().strip(),
            "crop_size": int(crop_spin.value()),
            "pad_edges": bool(pad_check.isChecked()),
        }
        return widget

    def apply(self, result, params):
        params = {**self.default_params(), **dict(params or {})}
        problem = _lisai_setup_problem()
        if problem is not None:
            raise RuntimeError(problem)
        model_name = str(params["model_name"]).strip()
        if not model_name:
            raise ValueError("LISAI restore needs a 'model_name' (an installed promoted model).")
        tiling_size = _tiling_policy(params["tiling_size"])
        crop_size = int(params["crop_size"] or 0)
        pad_edges = bool(params["pad_edges"])

        import tifffile
        from lisai.api import predict
        from lisai.config.models.inference import ApplyOverrides
        from lisai.evaluation.defaults import resolve_apply_config
        from lisai.promoted_models.package import load_promoted_model

        promoted = load_promoted_model(model_name)
        inference = {"tiling_size": tiling_size, "dark_frame_context_length": pad_edges}
        if crop_size > 0:
            inference["crop_size"] = crop_size
        stack, kept_axes = _frame_stack(result)
        context_length = promoted.saved_run.context_length
        if context_length and stack.ndim == 2:
            raise ValueError(
                f"{model_name} restores each frame from {context_length} consecutive frames; "
                "select a time series rather than a single image."
            )

        with tempfile.TemporaryDirectory(prefix="improcess_lisai_") as tmp:
            input_dir = Path(tmp) / "input"
            output_dir = Path(tmp) / "output"
            input_dir.mkdir()
            tifffile.imwrite(input_dir / f"{_INPUT_NAME}.tif", stack)
            overrides = ApplyOverrides.model_validate(
                {
                    "inference": inference,
                    "postprocess": {"color_code": {"enabled": False}},
                    "saving": {
                        "save_folder": str(output_dir),
                        "save_input_mode": "never",
                        "lvae_save_samples": False,
                    },
                }
            )
            cfg = resolve_apply_config(
                model_config=promoted.inference_config_path, overrides=overrides
            )
            predict(
                cfg,
                model_dataset="",
                model_subfolder="promoted",
                model_name=model_name,
                data_path=input_dir,
                promoted_model_name=model_name,
                progress_bar=False,
                overwrite=True,
            )
            prediction_path = output_dir / f"{_INPUT_NAME}_pred.tif"
            if not prediction_path.is_file():
                raise RuntimeError(f"LISAI finished without writing {prediction_path.name}.")
            prediction = np.asarray(tifffile.imread(prediction_path), dtype=np.float32)

        prediction, dropped = _trim_context_edges(prediction, context_length, pad_edges, model_name)
        labels, scales, factor = _output_axes(result, kept_axes, stack.shape, prediction)
        return LisaiRestoredResult(
            name=f"{result.name}_lisai",
            data=prediction,
            axis_labels=labels,
            model_name=model_name,
            upsampling_factor=factor,
            edge_frames_dropped=dropped,
            axis_scales=scales,
            scale_unit=result.scale_unit,
        )
