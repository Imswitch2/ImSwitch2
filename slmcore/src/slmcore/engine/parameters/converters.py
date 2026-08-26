"""Framework-independent parameter unit converters."""

from typing import Any,Mapping,Protocol

SLM_UNIT = "slm"
METRIC_UNIT = "metric"


class ConverterProtocol(Protocol):
    """Structural interface required by parameter converters."""

    canonical_unit: str
    supported_units: tuple[str, ...]
    types_by_unit: Mapping[str, type[Any]]

    def to_unit(self, value: Any, unit: str, context: Any = None) -> Any:
        """Convert ``value`` into one supported unit."""
        ...

    def type_for_unit(self, unit: str) -> type[Any]:
        """Return the editor input type expected for ``unit``."""
        ...


class _AxisConverter:
    """Small shared implementation for converters operating on one axis."""

    canonical_unit = SLM_UNIT
    supported_units = (SLM_UNIT, METRIC_UNIT)

    def __init__(self, axis: str):
        """Store and validate the converter axis."""
        if axis not in ("x", "y"):
            raise ValueError("Axis should be 'x' or 'y'")
        self.axis = axis

    def to_unit(self, value, unit, context=None):
        """Dispatch conversion to the method associated with ``unit``."""
        self._validate_unit(unit)
        method = getattr(self, f"to_{unit}", None)

        if method is None:
            raise NotImplementedError(
                f"{type(self).__name__} must implement to_{unit}()"
            )

        return method(value, context)

    def type_for_unit(self, unit):
        """Return the editor input type configured for ``unit``."""
        self._validate_unit(unit)

        try:
            return self.types_by_unit[unit]
        except KeyError:
            raise ValueError(
                f"{type(self).__name__} does not define an input type "
                f"for unit '{unit}'"
            )

    def _validate_unit(self, unit):
        """Reject units outside the converter contract."""
        if unit not in self.supported_units:
            raise ValueError(
                f"{type(self).__name__} does not support unit '{unit}'. "
                f"Supported units: {self.supported_units}"
            )

    @staticmethod
    def _require_calibration(context):
        """Return a valid section calibration supplied by the UI adapter."""
        if context is None:
            raise RuntimeError("A valid section calibration is required")
        if hasattr(context, "is_valid") and not context.is_valid():
            raise RuntimeError("A valid section calibration is required")
        return context


class PeriodDisplacementConverter(_AxisConverter):
    """Convert an SLM phase-ramp period to sample-plane displacement."""

    types_by_unit = {
        SLM_UNIT: int,
        METRIC_UNIT: float,
    }

    def to_metric(self, value, context):
        """Convert a phase-ramp period in SLM pixels to displacement in µm."""
        calibration = self._require_calibration(context)
        if value == 0:
            return 0

        kx, ky = ((1 / value, 0) if self.axis == "x" else (0, 1 / value))
        displacement_x, displacement_y = calibration.kxy_to_um(kx, ky)
        return displacement_x if self.axis == "x" else displacement_y

    def to_slm(self, value, context):
        """Convert displacement in µm to an SLM phase-ramp period."""
        calibration = self._require_calibration(context)
        if value == 0:
            return 0

        x_um, y_um = ((value, 0) if self.axis == "x" else (0, value))
        kx, ky = calibration.um_to_kxy(x_um, y_um)
        component = kx if self.axis == "x" else ky
        return 1 / component if component != 0 else 0


class FourierDisplacementConverter(_AxisConverter):
    """Convert fixed-reference Fourier pixels to sample-plane displacement.

    Canonical values are pixel distances on the package-wide fixed Fourier
    reference grid. They are converted to normalized ``kxy`` before applying
    the active section calibration, so they do not depend on section size.
    """

    types_by_unit = {
        SLM_UNIT: float,
        METRIC_UNIT: float,
    }

    def to_metric(self, value, context):
        """Convert reference Fourier pixels to displacement in µm."""
        from ...cgh.coordinates import reference_px_to_k

        calibration = self._require_calibration(context)
        k = reference_px_to_k(value)
        kx, ky = ((k, 0) if self.axis == "x" else (0, k))
        displacement_x, displacement_y = calibration.kxy_to_um(kx, ky)
        return displacement_x if self.axis == "x" else displacement_y

    def to_slm(self, value, context):
        """Convert displacement in µm to fixed-reference Fourier pixels."""
        from ...cgh.coordinates import k_to_reference_px

        calibration = self._require_calibration(context)
        x_um, y_um = ((value, 0) if self.axis == "x" else (0, value))
        kx, ky = calibration.um_to_kxy(x_um, y_um)
        component = kx if self.axis == "x" else ky
        return k_to_reference_px(component)
