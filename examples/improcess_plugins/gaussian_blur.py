"""Gaussian blur — an ImProcess drop-in plugin with a parameter and an
optional dependency.

Shows three things beyond the minimal example:
  * a real parameter widget whose get_values() feeds apply(),
  * the matching ``default_params()`` declaration — the same keys and the
    same defaults the widget opens with. That declaration is what a
    workflow, a replay and the provenance record use when no widget exists;
    without it the plugin is GUI-only,
  * a ``param_spec()`` describing the parameter the way the widget shows it
    (bounds, step, unit), which is what the workflow editor builds its form
    from; without it the editor still works, with a plain number field,
  * a lazily-imported optional dependency (scipy), reported cleanly if absent.

Copy into ~/.imswitch/improcess_plugins/ and Reload plugins.
"""

from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.processors.base import Processor
from imswitch.improcess.model.array_result import ArrayProcessingResult


class GaussianBlurProcessor(Processor):
    name = "Gaussian blur"
    id = "example.gaussian-blur"
    category = "User"
    kinds = ("image",)

    @classmethod
    def default_params(cls) -> dict:
        # Exactly what a freshly opened widget hands apply(). Keep the two
        # in step: ImProcess compares them when the panel opens, and
        # check_plugin_contract() does the same in a test.
        return {"sigma": 2.0}

    @classmethod
    def param_spec(cls) -> tuple:
        # What the widget knows and default_params() cannot say: the bounds,
        # the step and the unit. The workflow editor builds its form from
        # this; leave it out and the editor shows a plain number field.
        return (
            ParamField("sigma", "float", 2.0, label="Sigma", min=0.0, max=100.0, step=0.5,
                       suffix="px", help="Gaussian width in pixels, per plane"),
        )

    @property
    def applies_to(self):
        return lambda result: getattr(result.data, "ndim", 0) >= 2

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        sigma_spin = QtWidgets.QDoubleSpinBox()
        sigma_spin.setRange(0.0, 100.0)
        sigma_spin.setSingleStep(0.5)
        sigma_spin.setValue(self.default_params()["sigma"])
        sigma_spin.setSuffix(" px")
        layout.addRow("Sigma:", sigma_spin)

        widget.get_values = lambda: {"sigma": float(sigma_spin.value())}
        return widget

    def apply(self, result, params):
        sigma = float(params.get("sigma", 2.0))
        try:
            from scipy.ndimage import gaussian_filter
        except Exception as exc:  # pragma: no cover - only when scipy absent
            raise RuntimeError("Gaussian blur requires scipy") from exc

        data = result.data
        # Blur only the trailing two (Y, X) axes so stacks blur per-plane.
        axis_sigmas = [0.0] * (data.ndim - 2) + [sigma, sigma]
        blurred = gaussian_filter(data, sigma=axis_sigmas)

        return ArrayProcessingResult(
            name=f"{result.name} (blur σ={sigma:g})",
            data=blurred,
            axis_labels=list(result.axis_labels),
            axis_scales=list(getattr(result, "axis_scales", None) or []) or None,
            scale_unit=getattr(result, "scale_unit", "px"),
        )
